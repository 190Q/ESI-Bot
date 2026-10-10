"""
Main Tracker Runner
Runs all trackers concurrently with staggered API calls to prevent rate limiting.
"""

import asyncio
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Import all trackers
from playtime_tracker import run_once as playtime_run_once, init_database as playtime_init, FETCH_INTERVAL_SECONDS as PLAYTIME_INTERVAL
from guild_tracker import run_once as guild_run_once, load_tracked_guild, fetch_guild_data, extract_guild_info, DELAY as GUILD_DELAY
from claim_tracker import run_once as claim_run_once, load_tracked_guild as claim_load_tracked_guild, DELAY as CLAIM_DELAY
from api_tracker import run_once as api_run_once, FETCH_INTERVAL_SECONDS as API_INTERVAL

from utils.status import (
    TRACKER_STATUS_PATH,
    HEARTBEAT_INTERVAL_SECONDS,
    staleness_window,
    write_status,
)

# Global state
import guild_tracker
import claim_tracker

# API call lock to ensure staggered calls (created in main() for Python 3.8 compatibility)
api_lock = None
API_STAGGER_DELAY = 1.0  # 1 second delay between API calls

TRACKER_SPECS = {
    "API Tracker":      {"key": "API",      "interval": API_INTERVAL},
    "Playtime Tracker": {"key": "PLAYTIME", "interval": PLAYTIME_INTERVAL},
    "Guild Tracker":    {"key": "GUILD",    "interval": GUILD_DELAY},
    "Claim Tracker":    {"key": "CLAIM",    "interval": CLAIM_DELAY},
}
_TRACKER_NAME_BY_KEY = {spec["key"]: name for name, spec in TRACKER_SPECS.items()}

_tracker_state = {
    name: {
        "configured": None,
        "last_run_ts": None,
        "last_run_at": None,
        "last_run_ok": None,
        "last_success_at": None,
        "last_error": None,
        "consecutive_failures": 0,
        "total_runs": 0,
        "total_failures": 0,
    }
    for name in TRACKER_SPECS
}


def set_tracker_configured(key, configured):
    """Record whether this tracker has anything to track."""
    name = _TRACKER_NAME_BY_KEY.get(key)
    if name is not None:
        _tracker_state[name]["configured"] = bool(configured)


def record_tracker_run(key, ok, error=None):
    """Record one tracker cycle for the status file."""
    name = _TRACKER_NAME_BY_KEY.get(key)
    if name is None:
        return
    state = _tracker_state[name]
    now_ts = time.time()
    stamp = datetime.now(timezone.utc).isoformat()
    state["last_run_ts"] = now_ts
    state["last_run_at"] = stamp
    state["last_run_ok"] = bool(ok)
    state["total_runs"] += 1
    if ok:
        state["last_success_at"] = stamp
        state["consecutive_failures"] = 0
        state["last_error"] = None
    else:
        state["total_failures"] += 1
        state["consecutive_failures"] += 1
        state["last_error"] = {
            "at": stamp,
            "message": str(error or "cycle reported failure")[:500],
        }


def _result_ok(result):
    """Interpret a run_once() return value: a bool, or a tuple led by one."""
    if isinstance(result, tuple):
        return bool(result[0]) if result else False
    return bool(result)


def build_tracker_payload():
    """The `trackers` list written to data/tracker_status.json."""
    now_ts = time.time()
    payload = []
    for name, spec in TRACKER_SPECS.items():
        state = _tracker_state[name]
        interval = spec["interval"]
        window = staleness_window(interval)
        last_run = state["last_run_ts"]

        remaining = None
        if last_run is not None and (now_ts - last_run) <= window:
            remaining = int(interval - ((now_ts - last_run) % interval))
            if remaining <= 0 or remaining > interval:
                remaining = interval

        payload.append({
            "name": name,
            "interval": interval,
            "configured": state["configured"],
            "stale": bool(state["configured"]) and (
                last_run is None or (now_ts - last_run) > window
            ),
            "lastSeenAt": state["last_run_at"],
            "lastRunOk": state["last_run_ok"],
            "lastSuccessAt": state["last_success_at"],
            "lastError": state["last_error"],
            "consecutiveFailures": state["consecutive_failures"],
            "totalRuns": state["total_runs"],
            "totalFailures": state["total_failures"],
            "remainingSeconds": remaining,
        })
    return payload


async def staggered_api_call(func, tracker_name, record=True):
    """Wrapper to ensure API calls are staggered.

    When *record* is set, the cycle's outcome is recorded for the status file.
    """
    global api_lock
    if api_lock is None:
        api_lock = asyncio.Lock()
    async with api_lock:
        try:
            result = await func()
            await asyncio.sleep(API_STAGGER_DELAY)
        except Exception as e:
            print(f"[{tracker_name}] Error: {e}")
            import traceback
            traceback.print_exc()
            if record:
                record_tracker_run(tracker_name, False, f"{type(e).__name__}: {e}")
            return None
        if record:
            ok = _result_ok(result)
            record_tracker_run(tracker_name, ok, None if ok else "cycle reported failure")
        return result


async def playtime_loop():
    """Playtime tracker loop"""
    playtime_init()
    set_tracker_configured("PLAYTIME", True)
    
    while True:
        await staggered_api_call(playtime_run_once, "PLAYTIME")
        await asyncio.sleep(PLAYTIME_INTERVAL)


async def guild_loop():
    """Guild member tracker loop"""
    # Initialize guild tracker state
    loaded_identifier, loaded_is_prefix, loaded_data, loaded_member_history, loaded_event_history = load_tracked_guild()
    
    if loaded_identifier:
        guild_tracker.tracked_guild = loaded_identifier
        guild_tracker.is_prefix_tracked = loaded_is_prefix
        guild_tracker.member_history = loaded_member_history
        guild_tracker.event_history = loaded_event_history
        
        # Fetch fresh data on startup (staggered)
        async def init_fetch():
            fresh_data = await fetch_guild_data(loaded_identifier, loaded_is_prefix)
            if fresh_data:
                guild_tracker.previous_guild_data = extract_guild_info(fresh_data)
                guild_name = guild_tracker.previous_guild_data.get('name', loaded_identifier)
                print(f"[GUILD] Started tracking {guild_name} with {guild_tracker.previous_guild_data.get('member_count', 0)} members")
            else:
                guild_tracker.previous_guild_data = loaded_data
                guild_name = loaded_data.get('name', loaded_identifier)
                print(f"[GUILD] Started tracking {guild_name} with {loaded_data.get('member_count', 0)} members (cached data)")
        
        await staggered_api_call(init_fetch, "GUILD", record=False)
        set_tracker_configured("GUILD", True)
    else:
        print("[GUILD] No guild configured for tracking. Set up via Discord bot first.")
        set_tracker_configured("GUILD", False)
        # Keep running but do nothing
        while True:
            await asyncio.sleep(60)
        return
    
    while True:
        if guild_tracker.tracked_guild:
            await staggered_api_call(guild_run_once, "GUILD")
        await asyncio.sleep(GUILD_DELAY)


async def claim_loop():
    """Territory/claim tracker loop"""
    # Initialize claim tracker state
    loaded_guild, loaded_territories, loaded_history = claim_load_tracked_guild()
    
    if loaded_guild:
        claim_tracker.tracked_guild = loaded_guild
        claim_tracker.previous_territories = loaded_territories
        claim_tracker.territory_history = loaded_history
        print(f"[CLAIM] Started tracking {loaded_guild['name']} ({loaded_guild['prefix']}) with {len(loaded_territories)} territories")
        set_tracker_configured("CLAIM", True)
    else:
        print("[CLAIM] No guild configured for territory tracking. Set up via Discord bot first.")
        set_tracker_configured("CLAIM", False)
        # Keep running but do nothing
        while True:
            await asyncio.sleep(60)
        return
    
    while True:
        if claim_tracker.tracked_guild:
            await staggered_api_call(claim_run_once, "CLAIM")
        await asyncio.sleep(CLAIM_DELAY)

async def api_loop():
    """API stats tracker loop"""
    set_tracker_configured("API", True)
    while True:
        await staggered_api_call(api_run_once, "API")
        await asyncio.sleep(API_INTERVAL)


async def status_heartbeat():
    """Publish data/tracker_status.json for the control panel."""
    started_at = datetime.now(timezone.utc)
    while True:
        write_status(
            TRACKER_STATUS_PATH,
            service="esi-trackers",
            started_at=started_at,
            trackers=build_tracker_payload(),
        )
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


async def main():
    """Main function that runs all trackers"""
    global api_lock
    # Create the lock here for Python 3.8 compatibility
    api_lock = asyncio.Lock()
    
    print("=" * 60)
    print("ESI-Bot Standalone Trackers")
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    print()
    print("Tracker intervals:")
    print(f"  - Playtime:   {PLAYTIME_INTERVAL}s ({PLAYTIME_INTERVAL // 60} minutes)")
    print(f"  - Guild:      {GUILD_DELAY}s")
    print(f"  - Claims:     {CLAIM_DELAY}s")
    print(f"  - API Stats:  {API_INTERVAL}s ({API_INTERVAL // 60} minutes)")
    print()
    print("API calls are staggered with 1 second delay to prevent rate limiting.")
    print("=" * 60)
    print()
    
    # Create tasks for all trackers with staggered starts
    tasks = []
    
    # Start each tracker with a slight delay to avoid initial burst
    print("[MAIN] Starting playtime tracker...")
    tasks.append(asyncio.create_task(playtime_loop()))
    await asyncio.sleep(1)
    
    print("[MAIN] Starting guild tracker...")
    tasks.append(asyncio.create_task(guild_loop()))
    await asyncio.sleep(1)
    
    print("[MAIN] Starting claim tracker...")
    tasks.append(asyncio.create_task(claim_loop()))
    await asyncio.sleep(1)
    
    print("[MAIN] Starting API tracker...")
    tasks.append(asyncio.create_task(api_loop()))
    await asyncio.sleep(1)
    
    print("[MAIN] Starting status heartbeat...")
    tasks.append(asyncio.create_task(status_heartbeat()))
    
    print()
    print("[MAIN] All trackers started. Press Ctrl+C to stop.")
    print()
    
    # Wait for all tasks (they run forever)
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        print("[MAIN] Trackers cancelled.")


def signal_handler(sig, frame):
    """Handle Ctrl+C gracefully"""
    print()
    print("[MAIN] Shutting down...")
    sys.exit(0)


if __name__ == "__main__":
    # Set up signal handler for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
        print("[MAIN] Shutting down...")
