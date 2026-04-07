"""
Claim Tracker - Standalone version
Tracks territory/claim changes without requiring the Discord bot.
"""

import os
import asyncio
import aiohttp
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
import tempfile
import shutil

# Load environment variables
load_dotenv()

WYNNCRAFT_API_KEY = os.getenv('WYNNCRAFT_KEY_7')
TERRITORY_API_URL = "https://api.wynncraft.com/v3/guild/list/territory"

# Data file path (relative to ESI-Bot root)
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_FILE = BASE_DIR / "data/guild_territories.json"

# State
tracked_guild = None
previous_territories = {}
last_full_snapshot = {}
territory_history = []
has_baseline = False  # Track if we've done at least one API fetch

DELAY = 3  # Check every 3 seconds


def format_cooldown_time(timestamp_iso):
    """Calculate when territory will be off cooldown (10 minutes from now)"""
    try:
        event_time = datetime.now(timezone.utc)
        cooldown_end = event_time + timedelta(minutes=10)
        return cooldown_end.strftime("%H:%M:%S UTC")
    except:
        return "Unknown"


def load_tracked_guild():
    """Load tracked guild data from JSON file"""
    try:
        if DATA_FILE.exists():
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
                return (
                    data.get("guild"),
                    data.get("territories", {}),
                    data.get("history", []),
                )
    except Exception as e:
        print(f"[CLAIM] Failed to load tracked guild: {e}")
    return None, {}, []


import tempfile
import shutil

def save_territory_data(guild_info, territories, history):
    """Save territory data to JSON file"""
    # Filter out None keys from territories
    clean_territories = {k: v for k, v in territories.items() if k is not None}
    clean_history = [h for h in history if h is not None]
    
    # Load existing data to preserve notification settings
    existing_data = {}
    try:
        if DATA_FILE.exists():
            with open(DATA_FILE, "r") as f:
                existing_data = json.load(f)
    except:
        pass
    
    data = {
        "guild": guild_info,
        "last_update": datetime.now(timezone.utc).isoformat(),
        "territories": clean_territories,
        "history": clean_history,
        "notification_channel_id": existing_data.get("notification_channel_id"),
        "notification_thread_id": existing_data.get("notification_thread_id"),
        "notifications_enabled": existing_data.get("notifications_enabled", False)
    }
    
    # Write atomically to prevent corruption
    temp_fd, temp_path = tempfile.mkstemp(dir=DATA_FILE.parent, suffix='.tmp')
    try:
        with os.fdopen(temp_fd, 'w') as f:
            json.dump(data, f, indent=4)
        shutil.move(temp_path, DATA_FILE)
    except:
        try:
            os.unlink(temp_path)
        except:
            pass
        raise


async def fetch_territories():
    """Fetch territory data from Wynncraft API"""
    headers = {
        "apikey": WYNNCRAFT_API_KEY
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(TERRITORY_API_URL, headers=headers) as response:
                if response.status == 200:
                    try:
                        raw_text = await response.text()
                        raw_data = json.loads(raw_text, strict=False)
                        filtered_data = {}
                        for k, v in raw_data.items():
                            if k is not None and v is not None:
                                filtered_data[k] = v
                        return filtered_data
                    except Exception as e:
                        print(f"[CLAIM] JSON parsing error: {e}")
                        import traceback
                        traceback.print_exc()
                        return None
                else:
                    print(f"[CLAIM] Failed to fetch territories: {response.status}")
                    return None
    except Exception as e:
        print(f"[CLAIM] Request error: {e}")
        import traceback
        traceback.print_exc()
        return None


def get_guild_territories(data, guild_info):
    """Extract all territories owned by a specific guild"""
    territories = {}
    
    for territory_name, territory_data in data.items():
        # Skip None keys
        if territory_name is None or territory_data is None:
            continue
            
        guild_data = territory_data.get("guild", {})
        
        if guild_data.get("uuid") == guild_info["uuid"]:
            # Filter None keys and None values from territory data
            territories[territory_name] = {k: v for k, v in {
                "acquired": territory_data.get("acquired"),
                "location": territory_data.get("location"),
                "guild_name": guild_data.get("name"),
                "guild_prefix": guild_data.get("prefix")
            }.items() if k is not None and v is not None}
    
    return territories


def format_held_duration(acquired_time):
    """Calculate and format how long a territory was held"""
    try:
        acquired_dt = datetime.fromisoformat(acquired_time.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        duration = now - acquired_dt
        
        days = duration.days
        hours = duration.seconds // 3600
        minutes = (duration.seconds % 3600) // 60
        seconds = duration.seconds % 60
        
        parts = []
        if days > 0:
            parts.append(f"{days} Day{'s' if days != 1 else ''}")
        if hours > 0:
            parts.append(f"{hours} Hour{'s' if hours != 1 else ''}")
        if minutes > 0 or (days == 0 and hours == 0):
            parts.append(f"{minutes} Minute{'s' if minutes != 1 else ''}")
        if days == 0 and hours == 0:
            parts.append(f"{seconds} Second{'s' if seconds != 1 else ''}")
        
        return " And ".join(parts)
    except:
        return "Unknown Duration"


def count_guild_territories(data, guild_uuid):
    """Count total territories owned by a guild"""
    count = 0
    for territory_data in data.values():
        if territory_data.get("guild", {}).get("uuid") == guild_uuid:
            count += 1
    return count


async def run_once():
    """Run a single territory tracking cycle"""
    global tracked_guild, previous_territories, territory_history, last_full_snapshot, has_baseline
    
    if not tracked_guild:
        return False
    
    try:
        data = await fetch_territories()
        
        if data:
            current_territories = get_guild_territories(data, tracked_guild)
            
            # Only compare if we have a baseline (previous API fetch)
            if has_baseline:
                lost_now = set(previous_territories.keys()) - set(current_territories.keys())
                gained_now = set(current_territories.keys()) - set(previous_territories.keys())
                
                prev_count = len(previous_territories)
                
                # Process losses
                for idx, territory in enumerate(lost_now):
                    old_data = previous_territories[territory]
                    held_duration = format_held_duration(old_data["acquired"])
                    
                    # Find who now owns this territory
                    new_guild = data[territory].get("guild", {})
                    new_guild_uuid = new_guild.get("uuid")
                    
                    # Skip if territory became neutral (no guild owns it)
                    if not new_guild_uuid or not new_guild.get('name'):
                        print(f"[CLAIM LOST] {territory}: {tracked_guild['name']} -> Neutral (Held: {held_duration})")
                        continue
                    
                    # Count territories BEFORE this change for the new owner
                    new_guild_count_before = count_guild_territories(last_full_snapshot, new_guild_uuid) if last_full_snapshot else 0
                    # Count territories AFTER this change (they gained it)
                    new_guild_count_after = new_guild_count_before + 1
                    
                    loss_info = {
                        "type": "Territory Lost",
                        "territory": territory,
                        "from_guild": f"{tracked_guild['name']} ({prev_count - idx} -> {prev_count - idx - 1})",
                        "to_guild": f"{new_guild.get('name')} ({new_guild_count_before} -> {new_guild_count_after})",
                        "held_for": held_duration,
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }
                    territory_history.append(loss_info)
                    print(f"[CLAIM LOST] {territory}: {tracked_guild['name']} ({prev_count - idx} -> {prev_count - idx - 1}) -> {new_guild.get('name')} ({new_guild_count_before} -> {new_guild_count_after}) (Held: {held_duration})")
                
                # Process gains
                current_count = len(current_territories)
                start_count = prev_count - len(lost_now)

                for idx, territory in enumerate(gained_now):
                    new_data = current_territories[territory]
                    
                    # Find who previously owned this territory from last snapshot
                    prev_guild_name = "Unknown"
                    prev_guild_count_before = 0
                    prev_guild_count_after = 0
                    prev_acquired_time = None
                    
                    if last_full_snapshot and territory in last_full_snapshot:
                        prev_guild = last_full_snapshot[territory].get("guild", {})
                        prev_guild_name = prev_guild.get("name", "Unknown")
                        prev_guild_uuid = prev_guild.get("uuid")
                        prev_acquired_time = last_full_snapshot[territory].get("acquired")
                        
                        # Count before (they had it in the snapshot)
                        prev_guild_count_before = count_guild_territories(last_full_snapshot, prev_guild_uuid)
                        # Count after (they lost it)
                        prev_guild_count_after = prev_guild_count_before - 1
                    
                    # Use previous owner's acquired time if available, otherwise use current
                    held_duration = format_held_duration(prev_acquired_time) if prev_acquired_time else format_held_duration(new_data["acquired"])
                    
                    gain_info = {
                        "type": "Territory Captured",
                        "territory": territory,
                        "from_guild": f"{prev_guild_name} ({prev_guild_count_before} -> {prev_guild_count_after})",
                        "to_guild": f"{tracked_guild['name']} ({start_count + idx} -> {start_count + idx + 1})",
                        "held_for": held_duration,
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }
                    territory_history.append(gain_info)
                    print(f"[CLAIM GAINED] {territory}: {prev_guild_name} ({prev_guild_count_before} -> {prev_guild_count_after}) -> {tracked_guild['name']} ({start_count + idx} -> {start_count + idx + 1}) (Held: {held_duration})")
                    
                if len(territory_history) > 100:
                    territory_history = territory_history[-100:]
            
            # Store full snapshot for next comparison
            last_full_snapshot = {k: v for k, v in data.items() if k is not None}
            previous_territories = current_territories
            has_baseline = True  # We now have a baseline for comparison
            save_territory_data(tracked_guild, current_territories, territory_history)
            return True
    
    except Exception as e:
        print(f"[CLAIM] Error in tracking cycle: {e}")
        import traceback
        traceback.print_exc()
    
    return False


async def run_loop():
    """Run the claim tracker in a loop"""
    global tracked_guild, previous_territories, territory_history, has_baseline
    
    # Load saved state
    loaded_guild, loaded_territories, loaded_history = load_tracked_guild()
    
    if loaded_guild:
        tracked_guild = loaded_guild
        previous_territories = loaded_territories
        territory_history = loaded_history
        has_baseline = True  # We have saved state, so we have a baseline
        print(f"[CLAIM] Started tracking {loaded_guild['name']} ({loaded_guild['prefix']}) with {len(loaded_territories)} territories")
    else:
        print("[CLAIM] No guild configured for territory tracking. Set up via Discord bot first.")
        return
    
    print("[CLAIM] Starting claim tracker...")
    
    while True:
        if tracked_guild:
            await run_once()
        await asyncio.sleep(DELAY)


def teardown(bot_instance):
    """Cleanup function called before reload"""
    global claim_watcher_task
    
    if claim_watcher_task is not None and claim_watcher_task.is_running():
        print("[TEARDOWN] Stopping claim notification watcher...")
        claim_watcher_task.stop()
        print("[TEARDOWN] Claim notification watcher stopped")


if __name__ == "__main__":
    asyncio.run(run_loop())
