"""
Guild Tracker - Standalone version
Tracks guild member changes (joins, leaves, rank changes) without requiring the Discord bot.
"""

import os
import asyncio
import aiohttp
import json
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

WYNNCRAFT_API_KEY = os.getenv('WYNNCRAFT_KEY_7')

# Data file path (relative to ESI-Bot root)
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_FILE = BASE_DIR / "data/tracked_guild.json"

# State
tracked_guild = None
previous_guild_data = {}
member_history = {}
event_history = []  # Event log for bot notifications
is_prefix_tracked = False

DELAY = 30  # Check every minute

# Rank hierarchy (lower index = higher rank)
RANK_HIERARCHY = ["owner", "chief", "strategist", "captain", "recruiter", "recruit"]


def get_rank_level(rank):
    """Get the hierarchical level of a rank (lower = higher rank)"""
    try:
        return RANK_HIERARCHY.index(rank.lower())
    except ValueError:
        return 999  # Unknown rank


def load_tracked_guild():
    """Load tracked guild data from JSON file"""
    try:
        if DATA_FILE.exists():
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
                return (
                    data.get("guild_identifier"),
                    data.get("is_prefix"),
                    data.get("previous_data", {}),
                    data.get("member_history", {}),
                    data.get("event_history", []),
                )
    except Exception as e:
        print(f"[GUILD] Failed to load tracked guild: {e}")
    return None, False, {}, {}, []


def save_guild_data(guild_identifier, is_prefix, guild_data, member_history_data, events=None):
    """Save guild data to JSON file"""
    # Load existing data to preserve notification settings
    existing_data = {}
    try:
        if DATA_FILE.exists():
            with open(DATA_FILE, "r") as f:
                existing_data = json.load(f)
    except:
        pass
    
    data = {
        "guild_identifier": guild_identifier,
        "is_prefix": is_prefix,
        "last_update": datetime.now(timezone.utc).isoformat(),
        "previous_data": guild_data,
        "member_history": member_history_data,
        "event_history": events if events is not None else existing_data.get("event_history", []),
        "notification_channel_id": existing_data.get("notification_channel_id"),
        "notification_thread_id": existing_data.get("notification_thread_id"),
        "notifications_enabled": existing_data.get("notifications_enabled", False)
    }
    
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)


async def fetch_guild_data(identifier, use_prefix=False):
    """Fetch guild data from Wynncraft API"""
    headers = {
        "apikey": WYNNCRAFT_API_KEY
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            if use_prefix:
                url = f"https://api.wynncraft.com/v3/guild/prefix/{identifier}"
            else:
                url = f"https://api.wynncraft.com/v3/guild/{identifier}"
            
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    print(f"[GUILD] Failed to fetch guild data: {response.status}")
                    return None
    except Exception as e:
        print(f"[GUILD] Request error: {e}")
        import traceback
        traceback.print_exc()
        return None


def extract_guild_info(data):
    """Extract the relevant guild information from API response"""
    if not data:
        return None
    
    guild_info = {
        "name": data.get("name"),
        "prefix": data.get("prefix"),
        "level": data.get("level"),
        "member_count": data.get("members", {}).get("total", 0),
        "members": {}
    }
    
    # Extract members by rank
    members_data = data.get("members", {})
    for rank in ["owner", "chief", "strategist", "captain", "recruiter", "recruit"]:
        if rank in members_data:
            guild_info["members"][rank] = []
            rank_members = members_data[rank]
            
            # Handle case where rank has a single member (owner)
            if isinstance(rank_members, dict):
                for username, member_data in rank_members.items():
                    guild_info["members"][rank].append({
                        "username": username,
                        "uuid": member_data.get("uuid"),
                        "contributed": member_data.get("contributed", 0),
                        "joined": member_data.get("joined")
                    })
            elif isinstance(rank_members, list):
                # If it's a list (shouldn't happen but just in case)
                for member_data in rank_members:
                    if isinstance(member_data, dict):
                        username = list(member_data.keys())[0]
                        data_obj = member_data[username]
                        guild_info["members"][rank].append({
                            "username": username,
                            "uuid": data_obj.get("uuid"),
                            "contributed": data_obj.get("contributed", 0),
                            "joined": data_obj.get("joined")
                        })
    
    return guild_info


def compare_guild_data(old_data, new_data):
    """Compare old and new guild data and return changes"""
    changes = []
    
    if not old_data:
        return changes
    
    # Check level change
    if old_data.get("level") != new_data.get("level"):
        changes.append({
            "type": "level_change",
            "old": old_data.get("level"),
            "new": new_data.get("level")
        })
    
    # Check member count change
    if old_data.get("member_count") != new_data.get("member_count"):
        changes.append({
            "type": "member_count_change",
            "old": old_data.get("member_count"),
            "new": new_data.get("member_count")
        })
    
    # Check for member additions/removals
    old_members = {}
    new_members = {}
    
    # Flatten old members
    for rank, members in old_data.get("members", {}).items():
        for member in members:
            old_members[member["uuid"]] = {
                "username": member["username"],
                "rank": rank,
                "contributed": member.get("contributed", 0),
                "joined": member.get("joined")
            }
    
    # Flatten new members
    for rank, members in new_data.get("members", {}).items():
        for member in members:
            new_members[member["uuid"]] = {
                "username": member["username"],
                "rank": rank,
                "contributed": member.get("contributed", 0),
                "joined": member.get("joined")
            }
    
    # Find removed members
    for uuid, member_data in old_members.items():
        if uuid not in new_members:
            changes.append({
                "type": "member_left",
                "username": member_data["username"],
                "uuid": uuid,
                "rank": member_data["rank"],
                "contributed": member_data["contributed"]
            })
    
    # Find added members
    for uuid, member_data in new_members.items():
        if uuid not in old_members:
            changes.append({
                "type": "member_joined",
                "username": member_data["username"],
                "uuid": uuid,
                "rank": member_data["rank"],
                "joined": member_data["joined"]
            })
    
    # Find rank changes
    for uuid, new_member in new_members.items():
        if uuid in old_members:
            old_member = old_members[uuid]
            if old_member["rank"] != new_member["rank"]:
                changes.append({
                    "type": "rank_change",
                    "username": new_member["username"],
                    "uuid": uuid,
                    "old_rank": old_member["rank"],
                    "new_rank": new_member["rank"]
                })
    
    return changes


def print_change(change_info):
    """Print change to console"""
    change_type = change_info["type"]
    guild_name = change_info.get("guild_name", "Unknown")
    
    if change_type == "level_change":
        print(f"[GUILD LEVEL] {guild_name}: Level {change_info['old']} -> {change_info['new']}")
    elif change_type == "member_joined":
        print(f"[GUILD JOIN] {guild_name}: {change_info['username']} joined as {change_info['rank']}")
    elif change_type == "member_left":
        print(f"[GUILD LEAVE] {guild_name}: {change_info['username']} left ({change_info['rank']})")
    elif change_type == "rank_change":
        old_level = get_rank_level(change_info['old_rank'])
        new_level = get_rank_level(change_info['new_rank'])
        action = "promoted" if new_level < old_level else "demoted"
        print(f"[GUILD RANK] {guild_name}: {change_info['username']} {action} from {change_info['old_rank']} to {change_info['new_rank']}")


async def run_once():
    """Run a single guild tracking cycle"""
    global tracked_guild, previous_guild_data, member_history, is_prefix_tracked, event_history
    
    if not tracked_guild:
        return False
    
    try:
        data = await fetch_guild_data(tracked_guild, is_prefix_tracked)
        
        if data:
            current_guild_data = extract_guild_info(data)
            
            if previous_guild_data:
                changes = compare_guild_data(previous_guild_data, current_guild_data)
                
                # Process changes
                for change in changes:
                    timestamp = datetime.now(timezone.utc).isoformat()
                    
                    # Add to event history for bot notifications
                    event = {
                        **change,
                        "guild_name": current_guild_data.get("name", "Unknown"),
                        "guild_prefix": current_guild_data.get("prefix", "?"),
                        "timestamp": timestamp
                    }
                    event_history.append(event)
                    
                    # Keep event history bounded
                    if len(event_history) > 100:
                        event_history[:] = event_history[-100:]
                    
                    if change["type"] == "rank_change":
                        uuid = change["uuid"]
                        if uuid not in member_history:
                            member_history[uuid] = {
                                "username": change["username"],
                                "uuid": uuid,
                                "rank_changes": [],
                                "joined": None,
                                "left": None,
                                "highest_rank": change["new_rank"]
                            }
                        
                        # Update highest rank if new rank is higher
                        new_rank_level = get_rank_level(change["new_rank"])
                        current_highest_level = get_rank_level(member_history[uuid].get("highest_rank", "recruit"))
                        if new_rank_level < current_highest_level:
                            member_history[uuid]["highest_rank"] = change["new_rank"]
                        
                        member_history[uuid]["rank_changes"].append({
                            "from": change["old_rank"],
                            "to": change["new_rank"],
                            "timestamp": timestamp
                        })
                    
                    elif change["type"] == "member_joined":
                        uuid = change["uuid"]
                        if uuid not in member_history:
                            member_history[uuid] = {
                                "username": change["username"],
                                "uuid": uuid,
                                "rank_changes": [],
                                "joined": change.get("joined"),
                                "left": None,
                                "highest_rank": change["rank"]
                            }
                        else:
                            member_history[uuid]["joined"] = change.get("joined")
                            if "highest_rank" not in member_history[uuid]:
                                member_history[uuid]["highest_rank"] = change["rank"]
                    
                    elif change["type"] == "member_left":
                        uuid = change["uuid"]
                        if uuid not in member_history:
                            member_history[uuid] = {
                                "username": change["username"],
                                "uuid": uuid,
                                "rank_changes": [],
                                "joined": None,
                                "left": timestamp,
                                "highest_rank": change["rank"]
                            }
                        else:
                            member_history[uuid]["left"] = timestamp
                            current_rank_level = get_rank_level(change["rank"])
                            highest_rank_level = get_rank_level(member_history[uuid].get("highest_rank", "recruit"))
                            if current_rank_level < highest_rank_level:
                                member_history[uuid]["highest_rank"] = change["rank"]
                    
                    print_change({**change, "guild_name": current_guild_data["name"]})
            
            previous_guild_data = current_guild_data
            save_guild_data(tracked_guild, is_prefix_tracked, current_guild_data, member_history, event_history)
            return True
    
    except Exception as e:
        print(f"[GUILD] Error in tracking cycle: {e}")
        import traceback
        traceback.print_exc()
    
    return False


async def run_loop():
    """Run the guild tracker in a loop"""
    global tracked_guild, previous_guild_data, member_history, is_prefix_tracked, event_history
    
    # Load saved state
    loaded_identifier, loaded_is_prefix, loaded_data, loaded_member_history, loaded_event_history = load_tracked_guild()
    
    if loaded_identifier:
        tracked_guild = loaded_identifier
        is_prefix_tracked = loaded_is_prefix
        member_history = loaded_member_history
        event_history = loaded_event_history
        
        # Fetch fresh data on startup
        fresh_data = await fetch_guild_data(loaded_identifier, loaded_is_prefix)
        if fresh_data:
            previous_guild_data = extract_guild_info(fresh_data)
            guild_name = previous_guild_data.get('name', loaded_identifier)
            print(f"[GUILD] Started tracking {guild_name} with {previous_guild_data.get('member_count', 0)} members")
        else:
            previous_guild_data = loaded_data
            guild_name = loaded_data.get('name', loaded_identifier)
            print(f"[GUILD] Started tracking {guild_name} with {loaded_data.get('member_count', 0)} members (cached data)")
    else:
        print("[GUILD] No guild configured for tracking. Set up via Discord bot first.")
        return
    
    print("[GUILD] Starting guild tracker...")
    
    while True:
        if tracked_guild:
            await run_once()
        await asyncio.sleep(DELAY)


def teardown(bot_instance):
    """Cleanup function called before reload"""
    global guild_watcher_task
    
    if guild_watcher_task is not None and guild_watcher_task.is_running():
        print("[TEARDOWN] Stopping guild notification watcher...")
        guild_watcher_task.stop()
        print("[TEARDOWN] Guild notification watcher stopped")


if __name__ == "__main__":
    asyncio.run(run_loop())
