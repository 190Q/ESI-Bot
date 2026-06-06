import discord
from discord import app_commands
import os
import asyncio
import aiohttp
from typing import Optional, Dict, List
from datetime import datetime, timezone
import sqlite3
from utils.permissions import has_roles
from utils.paths import DATA_DIR, DB_DIR
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

# Initialize the points DB on load
init_points_database()

OWNER_ID_RAW = os.getenv('OWNER_ID')
REQUIRED_ROLES = [int(OWNER_ID_RAW)] if OWNER_ID_RAW else []
GUILDS = ["ESI"]

ASPECTS_FILE = DATA_DIR / "aspects.json"

POINTS_BASELINE_DB = DB_DIR / "points_baseline.db"

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

print(f"[INFO] Loaded {len(WYNNCRAFT_KEYS)} valid API keys")

def update_aspects_from_guild_data(guild_members):
    """Delegate aspects sync to shared utility for command/tracker parity."""
    return shared_update_aspects_from_guild_data(guild_members, ASPECTS_FILE, DB_DIR / "api_tracking")


def init_points_baseline():
    """Delegate baseline initialization to shared utility."""
    return shared_init_points_baseline(POINTS_BASELINE_DB)


def get_latest_fault_offsets():
    """Delegate fault-offset loading to shared utility."""
    return shared_get_latest_fault_offsets(DB_DIR / "api_tracking")


def apply_graid_fault_offset(total_graids: int, offset: int, username: str, log_warning: bool = False):
    """Delegate fault-offset application to shared utility."""
    return shared_apply_graid_fault_offset(total_graids, offset, username, log_warning=log_warning)


def _extract_guild_raid_payload(member: dict) -> dict:
    """Delegate guild-raid payload normalization to shared utility."""
    return shared_extract_guild_raid_payload(member)


def award_points_from_diff(member_stats: list, guild_members: list):
    """
    Compare current wars/graids against the stored baseline.
    Award 1 point per new war, 10 points per new guild raid.
    """
    return shared_award_points_from_diff(
        member_stats,
        guild_members,
        POINTS_BASELINE_DB,
        DB_DIR / "api_tracking",
        save_points,
    )

class FetchAPI:
    def __init__(self, guild_name: str = None):
        self.base_url = "https://api.wynncraft.com/v3"
        self.db_folder = "databases"
        self.api_tracking_folder = os.path.join(self.db_folder, "api_tracking")
        
        # Create folders if they don't exist
        os.makedirs(self.db_folder, exist_ok=True)
        os.makedirs(self.api_tracking_folder, exist_ok=True)

        # Headers for aiohttp requests
        self.headers = {
            'Authorization': f'Bearer {WYNNCRAFT_KEYS[0]}'
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
    
    async def get_player_info(self, player_identifier: str, max_retries: int = 3, guild_name: str = None, api_key: str = None) -> Optional[Dict]:
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
                            print(f"Player '{player_identifier}' not found (404)")
                            return None
                        elif response.status == 429:
                            if attempt < max_retries:
                                wait_time = (2 ** attempt) * 2
                                print(f"Rate limited for {player_identifier}, waiting {wait_time}s")
                                await asyncio.sleep(wait_time)
                                continue
                            else:
                                print(f"Rate limit exceeded for {player_identifier}")
                                return None
                        elif response.status == 500:
                            if attempt < max_retries:
                                wait_time = 2
                                print(f"Server error for {player_identifier}, retrying...")
                                await asyncio.sleep(wait_time)
                                continue
                            else:
                                print(f"Server error for {player_identifier}")
                                return None
                        else:
                            print(f"HTTP error {response.status} for {player_identifier}")
                            return None
                            
                except aiohttp.ClientError as e:
                    if attempt < max_retries:
                        wait_time = 1
                        print(f"Request error for {player_identifier}, retrying: {e}")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        print(f"Request error for {player_identifier}: {e}")
                        return None
                except Exception as e:
                    print(f"Request failed for {player_identifier}: {e}")
                    if attempt < max_retries:
                        await asyncio.sleep(1)
                        continue
                    else:
                        return None
        
        return None
    
    def get_player_stats(self, member: Dict, guild_data: Optional[Dict] = None) -> Dict:
        """Extract all relevant statistics from a guild member dict."""
        return shared_extract_member_stats(member, guild_data=guild_data, log_prefix="[API]")
    
    def cleanup_daily_folder(self, day_folder):
        """Delegate per-day snapshot cleanup to shared utility."""
        return shared_cleanup_daily_folder(day_folder)
    
    def cleanup_old_day_folders(self):
        """Delegate old day-folder compaction to shared utility."""
        return shared_cleanup_old_day_folders(self.api_tracking_folder)
    
    def cleanup_old_databases(self):
        """Delegate storage-limit cleanup to shared utility."""
        return shared_cleanup_old_databases(self.api_tracking_folder)
    
    async def save_additional_data(self, conn, guild_name: str):
        """Delegate additional snapshot persistence to shared utility."""
        return shared_save_additional_data(
            conn,
            DB_DIR / "recruited_data.db",
            badge_roles=BADGE_ROLES,
        )
    
    def _detect_and_update_username_changes(self, member_stats: list):
        """Detect username changes by UUID and update all occurrences in databases and JSON files."""
        import glob
        from datetime import datetime, timezone
        
        username_changes = {}  # {uuid: (old_username, new_username)}
        
        # Get all database files from api_tracking folder structure
        db_files = []
        if os.path.exists(self.api_tracking_folder):
            for day_folder in os.listdir(self.api_tracking_folder):
                day_path = os.path.join(self.api_tracking_folder, day_folder)
                if os.path.isdir(day_path) and day_folder.startswith("api_"):
                    pattern = os.path.join(day_path, "ESI_*.db")
                    db_files.extend(glob.glob(pattern))
        
        # Also check old flat structure for backwards compatibility
        old_pattern = os.path.join(self.db_folder, "ESI_*.db")
        db_files.extend(glob.glob(old_pattern))
        
        # Sort by modification time (newest first)
        db_files = sorted(db_files, key=os.path.getmtime, reverse=True)
        
        if len(db_files) < 2:
            return  # Need at least 2 databases to detect changes
        
        # Use PREVIOUS database (index 1) to compare against current member_stats from API
        try:
            conn_prev = sqlite3.connect(db_files[1])
            cur_prev = conn_prev.cursor()
            
            # Get previous data by UUID
            cur_prev.execute("SELECT uuid, username FROM player_stats WHERE uuid IS NOT NULL")
            prev_data = {row[0]: row[1] for row in cur_prev.fetchall()}
            conn_prev.close()
            
            # Compare current API response (member_stats) with previous database
            for stat in member_stats:
                uuid = stat.get('uuid')
                current_username = stat.get('username')
                
                if uuid and uuid in prev_data:
                    prev_username = prev_data[uuid]
                    # Only flag if it's a REAL change (case-insensitive comparison)
                    if current_username and current_username.lower() != prev_username.lower():
                        username_changes[uuid] = (prev_username, current_username)
                        print(f"[USERNAME_CHANGE] Detected change: '{prev_username}' → '{current_username}' (UUID: {uuid})")
            
            # Only proceed if there are actual changes detected
            if not username_changes:
                return
            
            # Update all databases in the folder
            for db_file in db_files:
                try:
                    conn = sqlite3.connect(db_file)
                    cur = conn.cursor()
                    
                    for uuid, (old_name, new_name) in username_changes.items():
                        # Update player_stats table
                        cur.execute(
                            "UPDATE player_stats SET username = ? WHERE uuid = ?",
                            (new_name, uuid)
                        )
                    
                    conn.commit()
                    conn.close()
                except Exception as e:
                    print(f"[USERNAME_CHANGE] Error updating {db_file}: {e}")
            
            # Update recruited_data.db if it exists
            try:
                recruited_db_path = "databases/recruited_data.db"
                if os.path.exists(recruited_db_path):
                    conn = sqlite3.connect(recruited_db_path)
                    cur = conn.cursor()
                    
                    for uuid, (old_name, new_name) in username_changes.items():
                        # Update recruiter names
                        cur.execute(
                            "UPDATE recruited SET recruiter = ? WHERE recruiter = ?",
                            (new_name, old_name)
                        )
                        # Update recruited names
                        cur.execute(
                            "UPDATE recruited SET recruited = ? WHERE recruited = ?",
                            (new_name, old_name)
                        )
                        # Update quest_progress
                        cur.execute(
                            "UPDATE quest_progress SET player = ? WHERE player = ?",
                            (new_name, old_name)
                        )
                        # Update event_progress
                        cur.execute(
                            "UPDATE event_progress SET player = ? WHERE player = ?",
                            (new_name, old_name)
                        )
                        
                        if cur.rowcount > 0:
                            print(f"[USERNAME_CHANGE] Updated recruited_data.db: '{old_name}' → '{new_name}'")
                    
                    conn.commit()
                    conn.close()
            except Exception as e:
                print(f"[USERNAME_CHANGE] Error updating recruited_data.db: {e}")
        
        except Exception as e:
            print(f"[USERNAME_CHANGE] Error detecting username changes: {e}")
        
        except Exception as e:
            print(f"[USERNAME_CHANGE] Error detecting username changes: {e}")
    
    def _get_current_day_string(self):
        """Get the current day as a string for folder naming"""
        return shared_get_current_day_string()
    
    def _get_day_folder_path(self, day_string=None):
        """Get the folder path for a specific day's API snapshots"""
        return str(shared_get_day_folder_path(self.api_tracking_folder, day_string))

    def _get_previous_api_db(self, current_db_path):
        """Find the most recent API DB before the current one."""
        return shared_get_previous_api_db(self.api_tracking_folder, current_db_path)

    def _get_previous_day_latest_db(self, current_db_path):
        """Find the latest DB from the calendar day before the current DB's day."""
        return shared_get_previous_day_latest_db(self.api_tracking_folder, current_db_path)

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
            self.api_tracking_folder,
        )

    async def save_data(self, guild_name: str, member_stats: list, guild_level: int = None, guild_members: list = None):
        """Save member statistics to SQLite database with timestamp."""
        import sqlite3
        import json
        from datetime import datetime, timezone
        
        try:
            # Get current day and create day folder
            day_string = self._get_current_day_string()
            day_folder = self._get_day_folder_path(day_string)
            os.makedirs(day_folder, exist_ok=True)
            
            # Create database filename with timestamp
            timestamp = datetime.now(timezone.utc).strftime("%H%M%S")
            db_filename = f"{guild_name}_{day_string}_{timestamp}.db"
            db_path = os.path.join(day_folder, db_filename)
            
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
            
            save_result = shared_save_player_and_raid_stats(
                cursor,
                member_stats,
                guild_members,
                include_shortened_rank=True,
            )
            if save_result.get("uuid_username_count", 0):
                print(f"[SAVE_DATA] Saved {save_result['uuid_username_count']} UUID-username mappings")
            if save_result.get("guild_raid_count", 0):
                print(f"[API] Saved {save_result['guild_raid_count']} guild raid stats")
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
            self.cleanup_daily_folder(day_folder)
            
            # Cleanup old day folders
            self.cleanup_old_day_folders()
            
            # Check storage limits
            self.cleanup_old_databases()
            
        except Exception as e:
            print(f"Error saving data to database: {e}")
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
        
        members, member_stats = build_member_stats_from_guild_payload(guild_data, log_prefix="[API]")
        if not members:
            return {"error": "No members found in guild data"}
        
        # Save data
        await self.save_data(guild_name, member_stats, guild_level, guild_members=members)
        
        # Detect and update username changes
        self._detect_and_update_username_changes(member_stats)
        
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

    async def analyze_multiple_guilds(self, guild_names: List[str]) -> Dict[str, Dict]:
        """
        Analyze multiple guilds concurrently and return a dictionary of results.
        Each key is the guild name, and each value is the result of analyze_guild_stats().
        """
        
        # Run all analyze_guild_stats calls concurrently
        results = await asyncio.gather(
            *(self.analyze_guild_stats(guild_name) for guild_name in guild_names),
            return_exceptions=True
        )

        # Handle results and exceptions
        guild_results = {}
        for guild_name, result in zip(guild_names, results):
            if isinstance(result, Exception):
                print(f"Error analyzing guild '{guild_name}': {result}")
                guild_results[guild_name] = {"error": str(result)}
            else:
                guild_results[guild_name] = result

        return guild_results

def setup(bot, has_required_role, config):
    """Wynncraft API Fetcher"""
    
    @bot.tree.command(
        name="fetch_api",
        description="Fetch the guild information from Wynncraft API",
    )
    async def fetch_api(interaction: discord.Interaction):
        """Command to fetch the guild information from Wynncraft API"""

        # Check permissions if required
        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return
        
        # Defer the response since this will take time
        await interaction.response.defer(ephemeral=False)
        
        try:
            # Initialize the FetchAPI class
            fetcher = FetchAPI()
            
            # Fetch data for guilds specified in GUILDS list
            if len(GUILDS) == 1:
                # Single guild
                guild_name = GUILDS[0]
                results = await fetcher.analyze_guild_stats(guild_name)
                
                if "error" in results:
                    error_embed = discord.Embed(
                        title="Error Fetching Guild Data",
                        description=f"Failed to fetch data for guild '{guild_name}': {results['error']}",
                        color=0xFF0000,
                        timestamp=datetime.utcnow()
                    )
                    await interaction.followup.send(embed=error_embed)
                    return
                
                # Calculate additional statistics
                member_stats = results.get('all_member_stats', [])
                total_raids = sum(stat.get('raids', {}).get('total', 0) for stat in member_stats if isinstance(stat.get('raids'), dict))
                total_dungeons = sum(stat.get('dungeons', {}).get('total', 0) for stat in member_stats if isinstance(stat.get('dungeons'), dict))
                total_playtime_hours = sum(stat.get('playtime', 0) for stat in member_stats)  # Already in hours from API
                total_world_events = sum(stat.get('worldEvents', 0) for stat in member_stats)
                total_pvp_kills = sum(stat.get('pvp', {}).get('kills', 0) for stat in member_stats if isinstance(stat.get('pvp'), dict))
                
                # Count members with API disabled (wars=0 and quests=0)
                api_disabled = sum(1 for stat in member_stats if stat.get('completedQuests', 0) == 0)
                
                # Replace the section that creates the success_embed (around line 600)

                # Create success embed
                success_embed = discord.Embed(
                    title=f"✅ Guild Data Fetched: {guild_name}",
                    description=f"Successfully fetched and saved statistics for **{results['members_analyzed']}** members.",
                    color=0x00FF00,
                    timestamp=datetime.utcnow()
                )
                
                # Member info
                success_embed.add_field(
                    name="Member Info",
                    value=f"Total: **{results['total_members']}**\nAnalyzed: **{results['members_analyzed']}**\nAPI Disabled: **{api_disabled}**",
                    inline=True
                )
                
                # Combat stats
                success_embed.add_field(
                    name="Combat Stats",
                    value=f"Wars: **{results['total_guild_wars']:,}**\nPvP Kills: **{total_pvp_kills:,}**\nWorld Events: **{total_world_events:,}**",
                    inline=True
                )
                
                # PvE stats
                success_embed.add_field(
                    name="PvE Stats",
                    value=f"Quests: **{results['total_guild_quests']:,}**\nRaids: **{total_raids:,}**\nDungeons: **{total_dungeons:,}**",
                    inline=True
                )
                
                # Badge statistics - read from recruited_data.db and guild API data
                badge_stats = {}
                try:
                    # Get recruitment count
                    if os.path.exists("databases/recruited_data.db"):
                        rec_conn = sqlite3.connect("databases/recruited_data.db")
                        rec_cursor = rec_conn.cursor()
                        rec_cursor.execute("SELECT COUNT(*) FROM recruited")
                        badge_stats['recruitments'] = rec_cursor.fetchone()[0]
                        rec_conn.close()
                    else:
                        badge_stats['recruitments'] = 0
                    
                    # Get quest points
                    if os.path.exists("databases/recruited_data.db"):
                        rec_conn = sqlite3.connect("databases/recruited_data.db")
                        rec_cursor = rec_conn.cursor()
                        rec_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='quest_progress'")
                        if rec_cursor.fetchone():
                            rec_cursor.execute("SELECT SUM(points) FROM quest_progress")
                            result = rec_cursor.fetchone()
                            badge_stats['quest_points'] = result[0] if result[0] else 0
                        else:
                            badge_stats['quest_points'] = 0
                        rec_conn.close()
                    else:
                        badge_stats['quest_points'] = 0
                    
                    # Get event points
                    if os.path.exists("databases/recruited_data.db"):
                        rec_conn = sqlite3.connect("databases/recruited_data.db")
                        rec_cursor = rec_conn.cursor()
                        rec_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='event_progress'")
                        if rec_cursor.fetchone():
                            rec_cursor.execute("SELECT SUM(points) FROM event_progress")
                            result = rec_cursor.fetchone()
                            badge_stats['event_points'] = result[0] if result[0] else 0
                        else:
                            badge_stats['event_points'] = 0
                        rec_conn.close()
                    else:
                        badge_stats['event_points'] = 0
                    
                    # Get total guild raids from guild API data
                    badge_stats['total_graids'] = sum(
                        stat.get('raids', {}).get('total', 0)
                        for stat in member_stats
                        if isinstance(stat.get('raids'), dict)
                    )
                    
                except Exception as e:
                    print(f"Error fetching badge stats: {e}")
                    badge_stats = {
                        'recruitments': 0,
                        'quest_points': 0,
                        'event_points': 0,
                        'total_graids': 0
                    }
                
                # Badge Data
                success_embed.add_field(
                    name="Badge Data",
                    value=f"Recruitments: **{badge_stats['recruitments']:,}**\nQuest Points: **{badge_stats['quest_points']:,}**\nEvent Points: **{badge_stats['event_points']:,}**",
                    inline=True
                )
                
                # Guild Raid Data
                success_embed.add_field(
                    name="Guild Raid Data",
                    value=f"Total Graids: **{badge_stats['total_graids']:,}**",
                    inline=True
                )
                
                # Playtime
                success_embed.add_field(
                    name="Total Playtime",
                    value=f"**{total_playtime_hours:,.0f}** hours\n({total_playtime_hours/24:,.1f} days)",
                    inline=True
                )
                
                # API keys used
                success_embed.add_field(
                    name="API Keys",
                    value=f"Used **{len(WYNNCRAFT_KEYS)}** key(s)",
                    inline=True
                )
                
                # Database info
                success_embed.add_field(
                    name="Database",
                    value=f"Saved to databases folder",
                    inline=True
                )
                
                # Empty field for spacing
                success_embed.add_field(
                    name="\u200b",
                    value="\u200b",
                    inline=True
                )
                
                # Fetch duration
                fetch_duration = results.get('fetch_duration', 0)
                minutes = int(fetch_duration // 60)
                seconds = int(fetch_duration % 60)
                if minutes > 0:
                    duration_text = f"{minutes}m {seconds}s"
                else:
                    duration_text = f"{seconds}s"
                
                success_embed.set_footer(text=f"Fetched in {duration_text} | {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S.%f UTC')[:-3]}")
                
                await interaction.followup.send(embed=success_embed)
                
            else:
                # Multiple guilds
                results = await fetcher.analyze_multiple_guilds(GUILDS)
                
                # Create summary embed
                summary_embed = discord.Embed(
                    title="Guild Data Fetched",
                    description=f"Fetched data for {len(GUILDS)} guild(s).",
                    color=0x00FF00,
                    timestamp=datetime.utcnow()
                )
                
                for guild_name, result in results.items():
                    if "error" in result:
                        summary_embed.add_field(
                            name=f"❌ {guild_name}",
                            value=f"Error: {result['error']}",
                            inline=False
                        )
                    else:
                        # Calculate stats for this guild
                        member_stats = result.get('all_member_stats', [])
                        total_raids = sum(stat.get('raids', {}).get('total', 0) for stat in member_stats if isinstance(stat.get('raids'), dict))
                        total_dungeons = sum(stat.get('dungeons', {}).get('total', 0) for stat in member_stats if isinstance(stat.get('dungeons'), dict))
                        
                        summary_embed.add_field(
                            name=f"✅ {guild_name}",
                            value=(
                                f"Members: **{result['total_members']}** | Wars: **{result['total_guild_wars']:,}**\n"
                                f"Quests: **{result['total_guild_quests']:,}** | Raids: **{total_raids:,}** | Dungeons: **{total_dungeons:,}**"
                            ),
                            inline=False
                        )
                
                await interaction.followup.send(embed=summary_embed)
                
        except Exception as e:
            error_embed = discord.Embed(
                title="Error",
                description=f"An unexpected error occurred: {str(e)}",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.followup.send(embed=error_embed)
            print(f"Error in fetch_api command: {e}")
    
    print("[OK] Loaded fetch_api command")
