"""
Main Tracker Runner
Runs all trackers concurrently with staggered API calls to prevent rate limiting.
"""

import asyncio
import signal
import sys
from datetime import datetime
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

# Import all trackers
from playtime_tracker import run_once as playtime_run_once, init_database as playtime_init, FETCH_INTERVAL_SECONDS as PLAYTIME_INTERVAL
from guild_tracker import run_once as guild_run_once, load_tracked_guild, fetch_guild_data, extract_guild_info, DELAY as GUILD_DELAY
from claim_tracker import run_once as claim_run_once, load_tracked_guild as claim_load_tracked_guild, DELAY as CLAIM_DELAY
from api_tracker import run_once as api_run_once, FETCH_INTERVAL_SECONDS as API_INTERVAL

# Global state
import guild_tracker
import claim_tracker

# API call lock to ensure staggered calls (created in main() for Python 3.8 compatibility)
api_lock = None
API_STAGGER_DELAY = 1.0  # 1 second delay between API calls


async def staggered_api_call(func, tracker_name):
    """Wrapper to ensure API calls are staggered"""
    global api_lock
    if api_lock is None:
        api_lock = asyncio.Lock()
    async with api_lock:
        try:
            result = await func()
            await asyncio.sleep(API_STAGGER_DELAY)
            return result
        except Exception as e:
            print(f"[{tracker_name}] Error: {e}")
            import traceback
            traceback.print_exc()
            return None


async def playtime_loop():
    """Playtime tracker loop"""
    print("[MAIN] Starting playtime tracker...")
    playtime_init()
    
    while True:
        await staggered_api_call(playtime_run_once, "PLAYTIME")
        await asyncio.sleep(PLAYTIME_INTERVAL)


async def guild_loop():
    """Guild member tracker loop"""
    print("[MAIN] Starting guild tracker...")
    
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
        
        await staggered_api_call(init_fetch, "GUILD")
    else:
        print("[GUILD] No guild configured for tracking. Set up via Discord bot first.")
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
    print("[MAIN] Starting claim tracker...")
    
    # Initialize claim tracker state
    loaded_guild, loaded_territories, loaded_history = claim_load_tracked_guild()
    
    if loaded_guild:
        claim_tracker.tracked_guild = loaded_guild
        claim_tracker.previous_territories = loaded_territories
        claim_tracker.territory_history = loaded_history
        print(f"[CLAIM] Started tracking {loaded_guild['name']} ({loaded_guild['prefix']}) with {len(loaded_territories)} territories")
    else:
        print("[CLAIM] No guild configured for territory tracking. Set up via Discord bot first.")
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
    print("[MAIN] Starting API tracker...")
    
    while True:
        await staggered_api_call(api_run_once, "API")
        await asyncio.sleep(API_INTERVAL)


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
