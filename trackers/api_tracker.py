"""
API Tracker - Standalone version
Periodically fetches and saves guild member statistics without requiring the Discord bot.
"""

import os
import asyncio
import aiohttp
import sqlite3
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
GUILDS = ["ESI"]
FETCH_INTERVAL_SECONDS = 300 # 5 minutes

# Badge role definitions (same as command file)
BADGE_ROLES = {
    "War Badges": {
        "10k": 1426633275635404981,
        "6k": 1426633206857465888,
        "3k": 1426633036736368861,
        "1.5k": 1426632920528846880,
        "750": 1426633144093638778,
        "300": 1426632862207049778,
        "100": 1426632780615385098,
    },
    "Quest Badges": {
        "350": 1426636141242617906,
        "225": 1426636108321525891,
        "150": 1426636066856898593,
        "90": 1426636018664341675,
        "50": 1426635982614040676,
        "25": 1426635948992761988,
        "10": 1426635880462024937,
    },
    "Recruitment Badges": {
        "250": 1426637291706912788,
        "150": 1426637244109946920,
        "80": 1426637209301160039,
        "50": 1426637168071282808,
        "25": 1426637134378303619,
        "10": 1426637094339608586,
        "5": 1426636993630175447,
    },
    "Raid Badges": {
        "6k": 1426634664025526405,
        "3.5k": 1426634622791323938,
        "2k": 1426634579644514347,
        "1k": 1426634531284324353,
        "500": 1426634469401432194,
        "100": 1426634408370114773,
        "50": 1426634317970542613,
    },
    "Event Badges": {
        "100": 1440682465717915779,
        "75": 1440682471086751815,
        "55": 1440682473641083011,
        "35": 1440682477055115304,
        "20": 1440682480846897232,
        "10": 1440682485548711997,
        "3": 1440682762133569730,
    },
}

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

# Storage constants
SIZE_LIMIT_BYTES = 20 * 1024 * 1024 * 1024  # 20GB
CLEANUP_INTERVAL_MINUTES = 30  # Keep files every 30 minutes


def get_current_day_string():
    """Get the current day as a string for folder naming"""
    return datetime.now(timezone.utc).strftime("%d-%m-%Y")


def get_day_folder_path(day_string=None):
    """Get the folder path for a specific day's API snapshots"""
    if day_string is None:
        day_string = get_current_day_string()
    return API_TRACKING_FOLDER / f"api_{day_string}"


def cleanup_daily_folder(day_folder):
    """Keep only files that are ~30 minutes apart (with margin)"""
    if not day_folder.exists():
        return
    
    db_files = sorted(day_folder.glob("*.db"), key=lambda f: f.stat().st_mtime)
    
    if len(db_files) <= 1:
        return
    
    files_to_keep = set()
    last_kept_time = None
    margin_seconds = 3 * 60  # 3 minute margin
    
    for db_file in db_files:
        file_mtime = db_file.stat().st_mtime
        
        if last_kept_time is None:
            files_to_keep.add(db_file)
            last_kept_time = file_mtime
        else:
            time_diff = file_mtime - last_kept_time
            if time_diff >= (CLEANUP_INTERVAL_MINUTES * 60 - margin_seconds):
                files_to_keep.add(db_file)
                last_kept_time = file_mtime
    
    # Always keep the most recent file
    if db_files:
        files_to_keep.add(db_files[-1])
    
    deleted_count = 0
    for db_file in db_files:
        if db_file not in files_to_keep:
            try:
                db_file.unlink()
                deleted_count += 1
            except Exception as e:
                print(f"[API] Failed to delete {db_file}: {e}")
    
    if deleted_count > 0:
        print(f"[API] Cleaned up {deleted_count} files from {day_folder.name}")


def cleanup_old_day_folders():
    """Clean folders that are 4-6 days old by keeping only the latest file in each"""
    if not API_TRACKING_FOLDER.exists():
        return
    
    today = datetime.now(timezone.utc).date()
    
    for folder in API_TRACKING_FOLDER.iterdir():
        if not folder.is_dir() or not folder.name.startswith("api_"):
            continue
        
        try:
            date_str = folder.name.replace("api_", "")
            folder_date = datetime.strptime(date_str, "%d-%m-%Y").date()
            days_old = (today - folder_date).days
            
            if 7 <= days_old <= 10:
                db_files = sorted(folder.glob("*.db"), key=lambda f: f.stat().st_mtime)
                
                if len(db_files) <= 1:
                    continue
                
                files_to_delete = db_files[:-1]
                deleted_count = 0
                
                for db_file in files_to_delete:
                    try:
                        db_file.unlink()
                        deleted_count += 1
                    except Exception as e:
                        print(f"[API] Failed to delete {db_file}: {e}")
                
                if deleted_count > 0:
                    print(f"[API] Cleaned {deleted_count} files from {folder.name} ({days_old} days old, kept latest only)")
        
        except ValueError:
            continue


def check_and_cleanup_storage():
    """Check if api_tracking folder exceeds 20GB and delete oldest day folders"""
    if not API_TRACKING_FOLDER.exists():
        return
    
    total_size = 0
    for path in API_TRACKING_FOLDER.rglob("*"):
        if path.is_file():
            total_size += path.stat().st_size
    
    if total_size <= SIZE_LIMIT_BYTES:
        return
    
    print(f"[API] Storage exceeds 20GB ({total_size / (1024**3):.2f} GB), cleaning up...")
    
    day_folders = sorted(
        [f for f in API_TRACKING_FOLDER.iterdir() if f.is_dir()],
        key=lambda f: datetime.strptime(f.name.replace("api_", ""), "%d-%m-%Y")
    )
    
    for folder in day_folders:
        if total_size <= SIZE_LIMIT_BYTES:
            break
        
        folder_size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
        
        try:
            shutil.rmtree(folder)
            total_size -= folder_size
            print(f"[API] Deleted old folder: {folder.name} ({folder_size / (1024**3):.2f} GB)")
        except Exception as e:
            print(f"[API] Failed to delete {folder}: {e}")


def update_aspects_from_guild_data(guild_members):
    """Update aspects_data.json based on current guild raid data.
    Called on every API fetch to keep aspects up to date.
    2 guild raids = 1 aspect.
    """
    try:
        # Load current aspects data
        if ASPECTS_FILE.exists():
            with open(ASPECTS_FILE, 'r') as f:
                aspects_data = json.load(f)
        else:
            aspects_data = {"total_aspects": 22, "members": {}}
        
        changed = False
        
        for member in guild_members:
            uuid = member.get('uuid')
            if not uuid:
                continue
            
            username = member.get('username', '')
            graids_data = member.get('guildRaids', {})
            total_graids = graids_data.get('total', 0) if isinstance(graids_data, dict) else 0
            
            if uuid not in aspects_data['members']:
                # New member - set baseline to current graids
                aspects_data['members'][uuid] = {
                    'name': username,
                    'baseline_graids': total_graids,
                    'owed': 0
                }
                changed = True
            else:
                stored = aspects_data['members'][uuid]
                
                # Update name if changed
                if stored['name'] != username:
                    stored['name'] = username
                    changed = True
                
                # Calculate new aspects earned since baseline
                baseline = stored.get('baseline_graids', total_graids)
                new_graids = total_graids - baseline
                
                if new_graids >= 2:
                    new_aspects = new_graids // 2
                    stored['owed'] = stored.get('owed', 0) + new_aspects
                    aspects_data['total_aspects'] += new_aspects
                    # Advance baseline by the graids that were converted
                    stored['baseline_graids'] = baseline + (new_aspects * 2)
                    changed = True
                    print(f"[ASPECTS] {username}: +{new_aspects} aspects ({new_graids} new graids)")
        
        if changed:
            with open(ASPECTS_FILE, 'w') as f:
                json.dump(aspects_data, f, indent=2)
            print(f"[ASPECTS] Updated aspects data (total: {aspects_data['total_aspects']})")
    
    except Exception as e:
        print(f"[ASPECTS] Error updating aspects data: {e}")


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
        try:
            req_headers = headers or self.headers
            async with session.get(url, headers=req_headers) as response:
                if response.status == 200:
                    data = await response.json()
                    return True, data
                else:
                    print(f"[API] Request failed with status {response.status}")
                    return False, None
        except Exception as e:
            print(f"[API] Request failed for {url}: {e}")
            return False, None
        
    async def get_guild_info(self, guild_name: str) -> Optional[Dict]:
        """Fetch guild information from Wynncraft API."""
        import urllib.parse
        encoded_guild_name = urllib.parse.quote(guild_name)
        url = f"{self.base_url}/guild/prefix/{encoded_guild_name}"
        
        async with aiohttp.ClientSession() as session:
            success, data = await self.make_request(session, url)
            if success:
                return data
            else:
                print("[API] API request failed.")
                return None

    def extract_guild_members(self, guild_data: Dict) -> List[Dict]:
        """Extract member data with ranks, UUIDs, and guildRaids from guild data."""
        members = []
        
        if 'members' in guild_data:
            members_data = guild_data['members']
            
            for rank, rank_members in members_data.items():
                if rank == 'total':
                    continue
                    
                if isinstance(rank_members, dict):
                    for username, member_info in rank_members.items():
                        member_dict = {"username": username, "rank": rank}
                        
                        if isinstance(member_info, dict):
                            # Extract UUID if available
                            if 'uuid' in member_info:
                                member_dict["uuid"] = member_info['uuid']
                            # Extract guildRaids if available
                            if 'guildRaids' in member_info:
                                member_dict["guildRaids"] = member_info['guildRaids']
                        
                        members.append(member_dict)
        
        return members
    
    async def get_player_info(self, player_identifier: str, max_retries: int = 3, api_key: str = None) -> Optional[Dict]:
        """Fetch player information from Wynncraft API with retry logic."""
        url = f"{self.base_url}/player/{player_identifier}"
        
        # Use provided API key or default
        headers = {'Authorization': f'Bearer {api_key}'} if api_key else self.headers
        
        async with aiohttp.ClientSession() as session:
            for attempt in range(max_retries + 1):
                try:
                    async with session.get(url, headers=headers) as response:
                        if response.status == 200:
                            data = await response.json()
                            return data
                        elif response.status == 404:
                            print(f"[API] Player '{player_identifier}' not found (404)")
                            return None
                        elif response.status == 429:
                            if attempt < max_retries:
                                wait_time = (2 ** attempt) * 2
                                print(f"[API] Rate limited for {player_identifier}, waiting {wait_time}s")
                                await asyncio.sleep(wait_time)
                                continue
                            else:
                                print(f"[API] Rate limit exceeded for {player_identifier}")
                                return None
                        elif response.status == 500:
                            if attempt < max_retries:
                                wait_time = 2
                                print(f"[API] Server error for {player_identifier}, retrying...")
                                await asyncio.sleep(wait_time)
                                continue
                            else:
                                print(f"[API] Server error for {player_identifier}")
                                return None
                        else:
                            print(f"[API] HTTP error {response.status} for {player_identifier}")
                            return None
                            
                except aiohttp.ClientError as e:
                    if attempt < max_retries:
                        wait_time = 1
                        print(f"[API] Request error for {player_identifier}, retrying: {e}")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        print(f"[API] Request error for {player_identifier}: {e}")
                        return None
                except Exception as e:
                    print(f"[API] Request failed for {player_identifier}: {e}")
                    if attempt < max_retries:
                        await asyncio.sleep(1)
                        continue
                    else:
                        return None
        
        return None
    
    def get_player_stats(self, player_data: Dict) -> Dict:
        """Extract all relevant statistics from player data."""
        stats = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'uuid': None,
            'shortenedRank': None,
            'guild': {
                'uuid': None,
                'name': None,
                'prefix': None,
                'rank': None
            },
            'playtime': 0,
            'wars': 0,
            'totalLevel': 0,
            'mobsKilled': 0,
            'chestsFound': 0,
            'dungeons': {
                'total': 0,
                'list': {}
            },
            'raids': {
                'total': 0,
                'list': {}
            },
            'worldEvents': 0,
            'lootRuns': 0,
            'caves': 0,
            'completedQuests': 0,
            'pvp': {
                'kills': 0,
                'deaths': 0
            }
        }
        
        try:
            # Shortened rank (supportRank) - prioritize supportRank over others
            rank_value = None
            if 'supportRank' in player_data and player_data['supportRank']:
                rank_value = player_data['supportRank']
            elif 'shortenedRank' in player_data and player_data['shortenedRank']:
                rank_value = player_data['shortenedRank']
            elif 'rank' in player_data and player_data['rank']:
                rank_value = player_data['rank']
            
            # Replace 'plus' with '+' (e.g., vipplus -> vip+)
            if rank_value:
                stats['shortenedRank'] = rank_value.replace('plus', '+')
            
            # Player UUID
            if 'uuid' in player_data:
                stats['uuid'] = player_data['uuid']
            
            # Guild information
            if 'guild' in player_data and isinstance(player_data['guild'], dict):
                guild_data = player_data['guild']
                stats['guild']['uuid'] = guild_data.get('uuid')
                stats['guild']['name'] = guild_data.get('name')
                stats['guild']['prefix'] = guild_data.get('prefix')
                stats['guild']['rank'] = guild_data.get('rank')
            
            # Global data statistics
            if 'globalData' in player_data and isinstance(player_data['globalData'], dict):
                global_data = player_data['globalData']
                
                if 'playtime' in global_data:
                    stats['playtime'] = global_data['playtime']
                
                if 'wars' in global_data and isinstance(global_data['wars'], (int, float)):
                    stats['wars'] = int(global_data['wars'])
                
                if 'totalLevel' in global_data:
                    stats['totalLevel'] = global_data['totalLevel']
                
                if 'mobsKilled' in global_data:
                    stats['mobsKilled'] = global_data['mobsKilled']
                elif 'killedMobs' in global_data:
                    stats['mobsKilled'] = global_data['killedMobs']
                
                if 'chestsFound' in global_data:
                    stats['chestsFound'] = global_data['chestsFound']
                elif 'foundChests' in global_data:
                    stats['chestsFound'] = global_data['foundChests']
                
                if 'dungeons' in global_data and isinstance(global_data['dungeons'], dict):
                    dungeons = global_data['dungeons']
                    if 'total' in dungeons:
                        stats['dungeons']['total'] = dungeons['total']
                    if 'list' in dungeons and isinstance(dungeons['list'], dict):
                        stats['dungeons']['list'] = dungeons['list']
                
                if 'raids' in global_data and isinstance(global_data['raids'], dict):
                    raids = global_data['raids']
                    if 'total' in raids:
                        stats['raids']['total'] = raids['total']
                    if 'list' in raids and isinstance(raids['list'], dict):
                        stats['raids']['list'] = raids['list']
                
                if 'worldEvents' in global_data:
                    stats['worldEvents'] = global_data['worldEvents']
                elif 'completedWorldEvents' in global_data:
                    stats['worldEvents'] = global_data['completedWorldEvents']
                
                if 'lootRuns' in global_data:
                    stats['lootRuns'] = global_data['lootRuns']
                elif 'completedLootRuns' in global_data:
                    stats['lootRuns'] = global_data['completedLootRuns']
                
                if 'caves' in global_data:
                    stats['caves'] = global_data['caves']
                elif 'completedCaves' in global_data:
                    stats['caves'] = global_data['completedCaves']
                
                if 'completedQuests' in global_data and isinstance(global_data['completedQuests'], (int, float)):
                    stats['completedQuests'] = int(global_data['completedQuests'])
                
                if 'pvp' in global_data and isinstance(global_data['pvp'], dict):
                    pvp = global_data['pvp']
                    if 'kills' in pvp:
                        stats['pvp']['kills'] = pvp['kills']
                    if 'deaths' in pvp:
                        stats['pvp']['deaths'] = pvp['deaths']
            
            # Fallback for playtime at top level
            if stats['playtime'] == 0 and 'playtime' in player_data:
                stats['playtime'] = player_data['playtime']
            
            return stats
            
        except (KeyError, TypeError, ValueError) as e:
            print(f"[API] Error extracting player stats: {e}")
            return stats
    
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
            
            # Create tables
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS player_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    uuid TEXT,
                    timestamp TEXT,
                    shortened_rank TEXT,
                    guild_uuid TEXT,
                    guild_name TEXT,
                    guild_prefix TEXT,
                    guild_rank TEXT,
                    playtime INTEGER,
                    wars INTEGER,
                    total_level INTEGER,
                    mobs_killed INTEGER,
                    chests_found INTEGER,
                    dungeons_total INTEGER,
                    dungeons_list TEXT,
                    raids_total INTEGER,
                    raids_list TEXT,
                    world_events INTEGER,
                    loot_runs INTEGER,
                    caves INTEGER,
                    completed_quests INTEGER,
                    pvp_kills INTEGER,
                    pvp_deaths INTEGER
                )
            ''')
            
            # Create UUID to username mapping table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS uuid_username_map (
                    uuid TEXT PRIMARY KEY,
                    username TEXT NOT NULL
                )
            ''')
            
            # Insert data and build UUID mapping
            uuid_username_pairs = []
            for stats in member_stats:
                # Collect UUID-username pairs for mapping table
                if stats.get('uuid') and stats.get('username'):
                    uuid_username_pairs.append((stats['uuid'], stats['username']))
                
                guild_data = stats.get('guild', {})
                dungeons_data = stats.get('dungeons', {})
                raids_data = stats.get('raids', {})
                pvp_data = stats.get('pvp', {})
                
                cursor.execute('''
                    INSERT INTO player_stats (
                        username, uuid, timestamp, shortened_rank,
                        guild_uuid, guild_name, guild_prefix, guild_rank,
                        playtime, wars, total_level, mobs_killed, chests_found,
                        dungeons_total, dungeons_list, raids_total, raids_list,
                        world_events, loot_runs, caves, completed_quests,
                        pvp_kills, pvp_deaths
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    stats.get('username'),
                    stats.get('uuid'),
                    stats.get('timestamp'),
                    stats.get('shortenedRank'),
                    guild_data.get('uuid') if isinstance(guild_data, dict) else None,
                    guild_data.get('name') if isinstance(guild_data, dict) else None,
                    guild_data.get('prefix') if isinstance(guild_data, dict) else None,
                    guild_data.get('rank') if isinstance(guild_data, dict) else None,
                    stats.get('playtime', 0),
                    stats.get('wars', 0),
                    stats.get('totalLevel', 0),
                    stats.get('mobsKilled', 0),
                    stats.get('chestsFound', 0),
                    dungeons_data.get('total', 0) if isinstance(dungeons_data, dict) else 0,
                    json.dumps(dungeons_data.get('list', {})) if isinstance(dungeons_data, dict) else '{}',
                    raids_data.get('total', 0) if isinstance(raids_data, dict) else 0,
                    json.dumps(raids_data.get('list', {})) if isinstance(raids_data, dict) else '{}',
                    stats.get('worldEvents', 0),
                    stats.get('lootRuns', 0),
                    stats.get('caves', 0),
                    stats.get('completedQuests', 0),
                    pvp_data.get('kills', 0) if isinstance(pvp_data, dict) else 0,
                    pvp_data.get('deaths', 0) if isinstance(pvp_data, dict) else 0
                ))
            
            # Insert UUID to username mappings
            if uuid_username_pairs:
                cursor.executemany(
                    "INSERT OR REPLACE INTO uuid_username_map (uuid, username) VALUES (?, ?)",
                    uuid_username_pairs
                )
            
            # Save guild raid stats from guild API
            if guild_members:
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS guild_raid_stats (
                        username TEXT NOT NULL,
                        uuid TEXT,
                        total_graids INTEGER DEFAULT 0,
                        canyon_colossus INTEGER DEFAULT 0,
                        orphions_nexus INTEGER DEFAULT 0,
                        grootslangs INTEGER DEFAULT 0,
                        nameless_anomaly INTEGER DEFAULT 0
                    )
                ''')
                
                for member in guild_members:
                    graids = member.get('guildRaids', {})
                    graid_list = graids.get('list', {}) if isinstance(graids, dict) else {}
                    cursor.execute('''
                        INSERT INTO guild_raid_stats (username, uuid, total_graids, canyon_colossus, orphions_nexus, grootslangs, nameless_anomaly)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        member.get('username'),
                        member.get('uuid'),
                        graids.get('total', 0) if isinstance(graids, dict) else 0,
                        graid_list.get('The Canyon Colossus', 0),
                        graid_list.get("Orphion's Nexus of Light", 0),
                        graid_list.get('Nest of the Grootslangs', 0),
                        graid_list.get('The Nameless Anomaly', 0)
                    ))
                
                # Update aspects_data.json with new graid data
                update_aspects_from_guild_data(guild_members)
            
            # Save additional data (recruited, quest progress, event progress, badges)
            await self.save_additional_data(conn, guild_name)
            
            # Commit and close
            conn.commit()
            conn.close()
            
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
    
    async def save_additional_data(self, conn, guild_name: str):
        """Save recruited_data.db data into the same database.
        Also derive and store badge tiers per player based on the collected data.
        """
        def _parse_threshold(label: str) -> int:
            """Convert badge threshold labels like '10k', '1.5k', '750' into integer values."""
            try:
                s = label.strip().lower()
                multiplier = 1
                if s.endswith("k"):
                    multiplier = 1000
                    s = s[:-1]
                value = float(s)
                return int(value * multiplier)
            except Exception:
                return 0

        def _get_badge_for_value(category: str, value: int):
            """Return (tier_label, role_id) for the best badge the value qualifies for."""
            thresholds = BADGE_ROLES.get(category, {})
            best_label = None
            best_role = None
            best_threshold = -1
            for label, role_id in thresholds.items():
                threshold_value = _parse_threshold(label)
                if value >= threshold_value and threshold_value > best_threshold:
                    best_threshold = threshold_value
                    best_label = label
                    best_role = role_id
            return best_label, best_role

        try:
            cursor = conn.cursor()
            current_timestamp = datetime.now(timezone.utc).isoformat()
            
            # Save recruited_data.db if it exists
            if RECRUITED_DB_PATH.exists():
                recruited_conn = sqlite3.connect(RECRUITED_DB_PATH)
                recruited_cursor = recruited_conn.cursor()
                
                # Create recruited table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS recruited (
                        recruiter TEXT NOT NULL,
                        recruited TEXT NOT NULL,
                        timestamp TEXT NOT NULL
                    )
                ''')
                
                # Copy data from recruited table
                recruited_cursor.execute("SELECT recruiter, recruited, timestamp FROM recruited")
                recruited_data = recruited_cursor.fetchall()
                
                cursor.executemany(
                    "INSERT INTO recruited (recruiter, recruited, timestamp) VALUES (?, ?, ?)",
                    recruited_data
                )
                
                # Copy quest_progress table if it exists
                recruited_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='quest_progress'")
                if recruited_cursor.fetchone():
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS quest_progress (
                            player TEXT NOT NULL,
                            points INTEGER NOT NULL,
                            last_updated TEXT,
                            snapshot_timestamp TEXT NOT NULL
                        )
                    ''')
                    
                    recruited_cursor.execute("SELECT player, points, last_updated FROM quest_progress")
                    quest_data = recruited_cursor.fetchall()
                    
                    quest_data_with_timestamp = [(player, points, last_updated, current_timestamp) for player, points, last_updated in quest_data]
                    
                    cursor.executemany(
                        "INSERT INTO quest_progress (player, points, last_updated, snapshot_timestamp) VALUES (?, ?, ?, ?)",
                        quest_data_with_timestamp
                    )
                
                # Copy event_progress table if it exists
                recruited_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='event_progress'")
                if recruited_cursor.fetchone():
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS event_progress (
                            player TEXT NOT NULL,
                            points INTEGER NOT NULL,
                            last_updated TEXT,
                            snapshot_timestamp TEXT NOT NULL
                        )
                    ''')
                    
                    recruited_cursor.execute("SELECT player, points, last_updated FROM event_progress")
                    event_data = recruited_cursor.fetchall()
                    
                    event_data_with_timestamp = [(player, points, last_updated, current_timestamp) for player, points, last_updated in event_data]
                    
                    cursor.executemany(
                        "INSERT INTO event_progress (player, points, last_updated, snapshot_timestamp) VALUES (?, ?, ?, ?)",
                        event_data_with_timestamp
                    )
                
                recruited_conn.close()
            else:
                print("[API] recruited_data.db not found, skipping...")
            
            # Derive and store badge tiers
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS badges (
                    player TEXT NOT NULL,
                    category TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    role_id INTEGER,
                    value INTEGER NOT NULL,
                    snapshot_timestamp TEXT NOT NULL
                )
            ''')

            badge_rows = []

            # War Badges: based on wars from player_stats
            try:
                cursor.execute("SELECT username, wars FROM player_stats")
                for username, wars in cursor.fetchall():
                    wars = wars or 0
                    tier, role_id = _get_badge_for_value("War Badges", wars)
                    if tier is not None:
                        badge_rows.append((username, "War Badges", tier, role_id, wars, current_timestamp))
            except Exception as e:
                print(f"[API] Failed to compute war badges: {e}")

            # Quest Badges: based on points from quest_progress
            try:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='quest_progress'")
                if cursor.fetchone():
                    cursor.execute("SELECT player, points FROM quest_progress")
                    for player, points in cursor.fetchall():
                        points = points or 0
                        tier, role_id = _get_badge_for_value("Quest Badges", points)
                        if tier is not None:
                            badge_rows.append((player, "Quest Badges", tier, role_id, points, current_timestamp))
            except Exception as e:
                print(f"[API] Failed to compute quest badges: {e}")

            # Recruitment Badges: based on number of recruits per recruiter
            try:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='recruited'")
                if cursor.fetchone():
                    cursor.execute("SELECT recruiter, COUNT(*) FROM recruited GROUP BY recruiter")
                    for recruiter, count in cursor.fetchall():
                        count = count or 0
                        tier, role_id = _get_badge_for_value("Recruitment Badges", count)
                        if tier is not None:
                            badge_rows.append((recruiter, "Recruitment Badges", tier, role_id, count, current_timestamp))
            except Exception as e:
                print(f"[API] Failed to compute recruitment badges: {e}")

            # Raid Badges: based on guild_raid_stats table (from guild API)
            try:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='guild_raid_stats'")
                if cursor.fetchone():
                    cursor.execute("SELECT username, total_graids FROM guild_raid_stats")
                    for username, total_graids in cursor.fetchall():
                        total_graids = total_graids or 0
                        tier, role_id = _get_badge_for_value("Raid Badges", total_graids)
                        if tier is not None:
                            badge_rows.append((username, "Raid Badges", tier, role_id, total_graids, current_timestamp))
            except Exception as e:
                print(f"[API] Failed to compute raid badges: {e}")

            # Event Badges: based on points from event_progress
            try:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='event_progress'")
                if cursor.fetchone():
                    cursor.execute("SELECT player, points FROM event_progress")
                    for player, points in cursor.fetchall():
                        points = points or 0
                        tier, role_id = _get_badge_for_value("Event Badges", points)
                        if tier is not None:
                            badge_rows.append((player, "Event Badges", tier, role_id, points, current_timestamp))
            except Exception as e:
                print(f"[API] Failed to compute event badges: {e}")

            if badge_rows:
                cursor.executemany(
                    "INSERT INTO badges (player, category, tier, role_id, value, snapshot_timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                    badge_rows
                )
            else:
                print("[API] No badge records computed for this snapshot")
            
            conn.commit()
        
        except Exception as e:
            print(f"[API] Error saving additional data: {e}")
            import traceback
            traceback.print_exc()
    
    async def analyze_guild_stats(self, guild_name: str) -> Dict:
        """Analyze war and quest statistics for all members of a guild."""
        import time
        start_time = time.time()
        
        guild_data = await self.get_guild_info(guild_name)
        if not guild_data:
            return {"error": "Failed to fetch guild information"}
        
        # Extract guild level
        guild_level = guild_data.get('level')
        
        members = self.extract_guild_members(guild_data)
        if not members:
            return {"error": "No members found in guild data"}
        
        # Distribute members across available API keys
        num_keys = len(WYNNCRAFT_KEYS)
        
        # Create tasks for parallel fetching with different API keys
        async def fetch_member(member, key_index):
            username = member['username']
            uuid = member.get('uuid')
            
            # Select API key based on index (round-robin distribution)
            api_key = WYNNCRAFT_KEYS[key_index % num_keys] if num_keys > 0 else (WYNNCRAFT_KEYS[0] if WYNNCRAFT_KEYS else None)
            
            # Use UUID if available, otherwise use username
            identifier = uuid if uuid else username
            player_data = await self.get_player_info(identifier, api_key=api_key)
            
            if player_data:
                player_stats = self.get_player_stats(player_data)
                return {
                    "username": username,
                    "uuid": player_stats['uuid'],
                    "timestamp": player_stats['timestamp'],
                    "shortenedRank": player_stats['shortenedRank'],
                    "guild": player_stats['guild'],
                    "playtime": player_stats['playtime'],
                    "wars": player_stats['wars'],
                    "totalLevel": player_stats['totalLevel'],
                    "mobsKilled": player_stats['mobsKilled'],
                    "chestsFound": player_stats['chestsFound'],
                    "dungeons": player_stats['dungeons'],
                    "raids": player_stats['raids'],
                    "worldEvents": player_stats['worldEvents'],
                    "lootRuns": player_stats['lootRuns'],
                    "caves": player_stats['caves'],
                    "completedQuests": player_stats['completedQuests'],
                    "pvp": player_stats['pvp']
                }
            else:
                return {
                    "username": username,
                    "wars": 0,
                    "completedQuests": 0
                }
        
        # Fetch all members in parallel
        tasks = [fetch_member(member, i) for i, member in enumerate(members)]
        member_stats = await asyncio.gather(*tasks)
        
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
            "fetch_duration": fetch_duration
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
