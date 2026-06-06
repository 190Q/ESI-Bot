"""
API Tracker - Standalone version
Periodically fetches and saves guild member statistics without requiring the Discord bot.
"""

import os
import asyncio
import aiohttp
import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List
from dotenv import load_dotenv
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.esi_points import save_points, init_points_database
from utils.api_fetcher import (
    make_api_request,
    fetch_guild_info,
    extract_guild_members as shared_extract_guild_members,
    extract_member_stats as shared_extract_member_stats,
    build_member_stats_from_guild_payload,
)
from utils.api_tracking_utils import (
    BADGE_ROLES,
    get_current_day_string as shared_get_current_day_string,
    get_day_folder_path as shared_get_day_folder_path,
    cleanup_daily_folder as shared_cleanup_daily_folder,
    cleanup_old_day_folders as shared_cleanup_old_day_folders,
    cleanup_old_databases as shared_cleanup_old_databases,
    get_latest_fault_offsets as shared_get_latest_fault_offsets,
    apply_graid_fault_offset as shared_apply_graid_fault_offset,
    extract_guild_raid_payload as shared_extract_guild_raid_payload,
    update_aspects_from_guild_data as shared_update_aspects_from_guild_data,
    init_points_baseline as shared_init_points_baseline,
    award_points_from_diff as shared_award_points_from_diff,
    get_previous_api_db as shared_get_previous_api_db,
    get_previous_day_latest_db as shared_get_previous_day_latest_db,
    read_graid_totals_from_db as shared_read_graid_totals_from_db,
    read_prev_offsets_from_db as shared_read_prev_offsets_from_db,
    detect_graid_fault_deltas as shared_detect_graid_fault_deltas,
    detect_and_store_graid_fault_offsets as shared_detect_and_store_graid_fault_offsets,
    save_additional_data as shared_save_additional_data,
    save_player_and_raid_stats as shared_save_player_and_raid_stats,
)

# Initialize the points DB on startup
init_points_database()

# Load environment variables
load_dotenv()

# Configuration
GUILDS = ["ESI"]
FETCH_INTERVAL_SECONDS = 300 # 5 minutes

# Load all WYNNCRAFT_KEY_* environment variables
WYNNCRAFT_KEYS = []
key_index = 1
while True:
    key = os.getenv(f'WYNNCRAFT_KEY_{key_index}')
    if key is None or key_index > 6:
        break
    # Filter out placeholder keys
    if not key.startswith('your_key_'):
        WYNNCRAFT_KEYS.append(key)
    key_index += 1

print(f"[API] Loaded {len(WYNNCRAFT_KEYS)} valid API keys")

# Paths (relative to ESI-Bot root)
BASE_DIR = Path(__file__).resolve().parent.parent
DB_FOLDER = BASE_DIR / "databases"
API_TRACKING_FOLDER = DB_FOLDER / "api_tracking"
RECRUITED_DB_PATH = DB_FOLDER / "recruited_data.db"
ASPECTS_FILE = BASE_DIR / "data/aspects.json"
POINTS_BASELINE_DB = DB_FOLDER / "points_baseline.db"
QUEUE_FILE = BASE_DIR / "data/guild_member_queue.json"
PENDING_INVITES_FILE = BASE_DIR / "data/pending_invites.json"


def get_latest_fault_offsets():
    return shared_get_latest_fault_offsets(API_TRACKING_FOLDER)


def apply_graid_fault_offset(total_graids: int, offset: int, username: str, log_warning: bool = False):
    return shared_apply_graid_fault_offset(
        total_graids,
        offset,
        username,
        log_warning=log_warning,
    )


def _extract_guild_raid_payload(member: dict) -> dict:
    return shared_extract_guild_raid_payload(member)


def get_current_day_string():
    return shared_get_current_day_string()


def get_day_folder_path(day_string=None):
    return shared_get_day_folder_path(API_TRACKING_FOLDER, day_string)


def cleanup_daily_folder(day_folder):
    return shared_cleanup_daily_folder(day_folder)


def cleanup_old_day_folders():
    return shared_cleanup_old_day_folders(API_TRACKING_FOLDER, min_days_old=7, max_days_old=None)


def check_and_cleanup_storage():
    return shared_cleanup_old_databases(API_TRACKING_FOLDER)


def get_pending_invites_data() -> list:
    """Return all pending invite entries from pending_invites.json as a list of dicts."""
    if not PENDING_INVITES_FILE.exists():
        return []
    try:
        with open(PENDING_INVITES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    return [
        {
            "discord_id": discord_id,
            "username": entry.get("username"),
            "uuid": entry.get("uuid"),
            "invited_at": entry.get("invited_at"),
        }
        for discord_id, entry in data.items()
        if isinstance(entry, dict)
    ]


def get_queue_counts() -> dict:
    """Return current queue counts from guild_member_queue.json."""
    counts = {"veteran_count": 0, "normal_count": 0, "total_count": 0}

    if not QUEUE_FILE.exists():
        return counts

    try:
        with open(QUEUE_FILE, "r") as f:
            queue_data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return counts

    # Legacy format: flat list = normal queue
    if isinstance(queue_data, list):
        counts["normal_count"] = len(queue_data)
        counts["total_count"] = len(queue_data)
        return counts

    if isinstance(queue_data, dict):
        veteran = queue_data.get("veteran", [])
        normal = queue_data.get("normal", [])
        veteran_count = len(veteran) if isinstance(veteran, list) else 0
        normal_count = len(normal) if isinstance(normal, list) else 0

        counts["veteran_count"] = veteran_count
        counts["normal_count"] = normal_count
        counts["total_count"] = veteran_count + normal_count

    return counts


def update_aspects_from_guild_data(guild_members):
    return shared_update_aspects_from_guild_data(
        guild_members,
        ASPECTS_FILE,
        API_TRACKING_FOLDER,
    )


def init_points_baseline():
    return shared_init_points_baseline(POINTS_BASELINE_DB)


def award_points_from_diff(member_stats: list, guild_members: list):
    return shared_award_points_from_diff(
        member_stats,
        guild_members,
        POINTS_BASELINE_DB,
        API_TRACKING_FOLDER,
        save_points,
    )


class FetchAPI:
    def __init__(self):
        self.base_url = "https://api.wynncraft.com/v3"
        self.db_folder = DB_FOLDER
        
        # Create folders if they don't exist
        self.db_folder.mkdir(exist_ok=True)
        API_TRACKING_FOLDER.mkdir(exist_ok=True)

        # Headers for aiohttp requests
        self.headers = {
            'Authorization': f'Bearer {WYNNCRAFT_KEYS[0]}' if WYNNCRAFT_KEYS else ''
        }
    
    async def make_request(self, session, url, headers=None):
        """Request helper method following aiohttp pattern."""
        return await make_api_request(session, url, headers or self.headers, log_prefix="[API]")
        
    async def get_guild_info(self, guild_name: str) -> Optional[Dict]:
        """Fetch guild information from Wynncraft API."""
        return await fetch_guild_info(self.base_url, guild_name, self.headers, log_prefix="[API]")

    def extract_guild_members(self, guild_data: Dict) -> List[Dict]:
        """Extract member data with ranks, UUIDs, and full globalData from guild data."""
        return shared_extract_guild_members(guild_data)
    
    def get_player_stats(self, member: Dict, guild_data: Optional[Dict] = None) -> Dict:
        """Extract all relevant statistics from a guild member dict."""
        return shared_extract_member_stats(member, guild_data=guild_data, log_prefix="[API]")
    
    async def save_data(self, guild_name: str, member_stats: list, guild_level: int = None, guild_members: list = None):
        """Save member statistics to SQLite database with timestamp."""
        try:
            # Get current day and create day folder
            day_string = get_current_day_string()
            day_folder = get_day_folder_path(day_string)
            day_folder.mkdir(exist_ok=True)
            
            # Create database filename with timestamp
            timestamp = datetime.now(timezone.utc).strftime("%H%M%S")
            db_filename = f"{guild_name}_{day_string}_{timestamp}.db"
            db_path = day_folder / db_filename
            
            # Connect to database
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            # Save guild info (including level)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS guild_info (
                    guild_name TEXT NOT NULL,
                    guild_level INTEGER,
                    timestamp TEXT NOT NULL
                )
            ''')
            cursor.execute(
                "INSERT INTO guild_info (guild_name, guild_level, timestamp) VALUES (?, ?, ?)",
                (guild_name, guild_level, datetime.now(timezone.utc).isoformat())
            )
            if guild_level is not None:
                print(f"[API] Saved guild level: {guild_level}")

            # Save current queue counts
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS queue_stats (
                    veteran_count INTEGER NOT NULL DEFAULT 0,
                    normal_count INTEGER NOT NULL DEFAULT 0,
                    total_count INTEGER NOT NULL DEFAULT 0,
                    timestamp TEXT NOT NULL
                )
            ''')
            queue_counts = get_queue_counts()
            cursor.execute(
                "INSERT INTO queue_stats (veteran_count, normal_count, total_count, timestamp) VALUES (?, ?, ?, ?)",
                (
                    queue_counts["veteran_count"],
                    queue_counts["normal_count"],
                    queue_counts["total_count"],
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

            # Save current pending invites
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS pending_invites (
                    discord_id TEXT NOT NULL,
                    username TEXT,
                    uuid TEXT,
                    invited_at TEXT,
                    snapshot_timestamp TEXT NOT NULL
                )
            ''')
            pending_invites = get_pending_invites_data()
            snapshot_ts = datetime.now(timezone.utc).isoformat()
            cursor.executemany(
                "INSERT INTO pending_invites (discord_id, username, uuid, invited_at, snapshot_timestamp) VALUES (?, ?, ?, ?, ?)",
                [
                    (entry["discord_id"], entry["username"], entry["uuid"], entry["invited_at"], snapshot_ts)
                    for entry in pending_invites
                ],
            )

            save_result = shared_save_player_and_raid_stats(
                cursor,
                member_stats,
                guild_members,
                include_shortened_rank=False,
            )
            if save_result.get("guild_raid_count", 0):
                self._detect_and_store_graid_fault_offsets(conn, db_path)
                update_aspects_from_guild_data(guild_members)
            
            # Save additional data (recruited, quest progress, event progress, badges)
            await self.save_additional_data(conn, guild_name)
            
            # Commit and close
            conn.commit()
            conn.close()
            
            # Award ESI points based on war/raid increases
            award_points_from_diff(member_stats, guild_members)

            print(f"[API] Saved data to {db_path}")
            
            # Cleanup current day's folder (keep 30-min intervals)
            cleanup_daily_folder(day_folder)
            
            # Cleanup old day folders (4-6 days old, keep only latest file)
            cleanup_old_day_folders()
            
            # Check storage limits
            check_and_cleanup_storage()
            
        except Exception as e:
            print(f"[API] Error saving data to database: {e}")
            import traceback
            traceback.print_exc()
    
    def _get_previous_api_db(self, current_db_path):
        return shared_get_previous_api_db(API_TRACKING_FOLDER, current_db_path)

    def _get_previous_day_latest_db(self, current_db_path):
        return shared_get_previous_day_latest_db(API_TRACKING_FOLDER, current_db_path)

    def _read_graid_totals_from_db(self, db_path):
        return shared_read_graid_totals_from_db(db_path)

    def _read_prev_offsets_from_db(self, db_path):
        return shared_read_prev_offsets_from_db(db_path)

    @staticmethod
    def _detect_graid_fault_deltas(prev_totals, curr_totals):
        return shared_detect_graid_fault_deltas(prev_totals, curr_totals)

    def _detect_and_store_graid_fault_offsets(self, conn, current_db_path):
        return shared_detect_and_store_graid_fault_offsets(
            conn,
            current_db_path,
            API_TRACKING_FOLDER,
        )

    async def save_additional_data(self, conn, guild_name: str):
        return shared_save_additional_data(
            conn,
            RECRUITED_DB_PATH,
            badge_roles=BADGE_ROLES,
        )
    
    async def analyze_guild_stats(self, guild_name: str) -> Dict:
        """Analyze stats for all members of a guild using a single guild API call."""
        import time
        start_time = time.time()

        guild_data = await self.get_guild_info(guild_name)
        if not guild_data:
            return {"error": "Failed to fetch guild information"}

        guild_level = guild_data.get('level')

        members, member_stats = build_member_stats_from_guild_payload(guild_data, log_prefix="[API]")
        if not members:
            return {"error": "No members found in guild data"}

        # Save data
        await self.save_data(guild_name, member_stats, guild_level, guild_members=members)

        # Calculate statistics
        valid_stats = [stat for stat in member_stats if isinstance(stat.get("wars"), int) and isinstance(stat.get("completedQuests"), int)]
        total_wars = sum(stat.get("wars", 0) for stat in valid_stats)
        total_quests = sum(stat.get("completedQuests", 0) for stat in valid_stats)

        end_time = time.time()
        fetch_duration = end_time - start_time

        return {
            "guild_name": guild_name,
            "total_members": len(members),
            "members_analyzed": len(valid_stats),
            "total_guild_wars": total_wars,
            "total_guild_quests": total_quests,
            "all_member_stats": member_stats,
            "fetch_duration": fetch_duration,
        }


async def run_once():
    """Run a single API fetch cycle"""
    try:
        fetcher = FetchAPI()
        
        for guild_name in GUILDS:
            print(f"[API] Fetching data for {guild_name}...")
            results = await fetcher.analyze_guild_stats(guild_name)
            
            if "error" in results:
                print(f"[API] Error fetching {guild_name}: {results['error']}")
                # Still save badges and additional data even if API fetch failed
                print(f"[API] Saving badges and additional data despite API failure...")
                await fetcher.save_data(guild_name, [])
            else:
                print(f"[API] Fetched {results['members_analyzed']} members from {guild_name} in {results['fetch_duration']:.1f}s")
        
        return True
    
    except Exception as e:
        print(f"[API] Error in run_once: {e}")
        import traceback
        traceback.print_exc()
        return False


async def run_loop():
    """Run the API tracker in a loop"""
    print("[API] Starting API tracker...")
    
    while True:
        await run_once()
        await asyncio.sleep(FETCH_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run_loop())
