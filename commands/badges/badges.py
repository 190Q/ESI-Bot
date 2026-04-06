import discord
from discord import app_commands
import sqlite3
from datetime import datetime
import os
import aiohttp
import asyncio
import urllib3
from typing import Dict, List, Optional
import tempfile
from pathlib import Path
from typing import Dict, List
from utils.permissions import has_roles
from utils.paths import PROJECT_ROOT, DATA_DIR, DB_DIR

DB_FILE = "databases/recruited_data.db"
WAR_DATA_FILE = "databases/guild_stats_data.db"
BADGES_CACHE_FILE = "databases/badges_cache.db"
API_TRACKING_FOLDER = DB_DIR / "api_tracking"

# API Configuration
WYNNCRAFT_API_KEY = os.getenv('WYNNCRAFT_KEY_1')
GUILDS = ["ESI"]

REQUIRED_ROLES = (
    554889169705500672, # Sindrian Citizen
    os.getenv('OWNER_ID') if os.getenv('OWNER_ID') else 0
)

# Badge tier definitions
QUEST_BADGE_TIERS = [
    (350, "[Name] Badge"),
    (225, "Onyx"), 
    (150, "Diamond"),
    (90, "Platinum"),
    (50, "Gold"),
    (25, "Silver"),
    (10, "Bronze"),
    (1, "No badge"),
    (0, "No data")
]

RECRUITED_BADGE_TIERS = [
    (250, "[Name] Badge"),
    (150, "Onyx"),
    (80, "Diamond"),
    (50, "Platinum"),
    (25, "Gold"),
    (10, "Silver"),
    (5, "Bronze"),
    (1, "No badge"),
    (0, "No data")
]

WAR_BADGE_TIERS = [
    (10000, "Alle_Sandstorm War Badge"),
    (6000, "Onyx"),
    (3000, "Diamond"),
    (1500, "Platinum"),
    (750, "Gold"),
    (300, "Silver"),
    (100, "Bronze"),
    (1, "No badge"),
    (0, "No data")
]

GRAID_BADGE_TIERS = [
    (6000, "[Name] Badge"),
    (3500, "Onyx"),
    (2000, "Diamond"),
    (1000, "Platinum"),
    (500, "Gold"),
    (100, "Silver"),
    (50, "Bronze"),
    (1, "No badge"),
    (0, "No data")
]

EVENT_BADGE_TIERS = [
    (100, "[Name] Badge"),
    (75, "Onyx"),
    (55, "Diamond"),
    (35, "Platinum"),
    (20, "Gold"),
    (10, "Silver"),
    (3, "Bronze"),
    (0, "No badge")
]

# Disable SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def determine_quest_badge(count: int) -> str:
    """Return the quest badge tier for a given quest count."""
    for threshold, name in QUEST_BADGE_TIERS:
        if count >= threshold:
            return name
    return "No badge"

def determine_recruited_badge(count: int) -> str:
    """Return the recruitment badge tier for a given recruitment count."""
    for threshold, name in RECRUITED_BADGE_TIERS:
        if count >= threshold:
            return name
    return "No badge"

def determine_war_badge(count: int, quests: int = None) -> str:
    """Return the war badge tier for a given war count."""
    # If quest count is 0, API is likely disabled
    if quests is not None and quests == 0 and count == 0:
        return "API off"
    
    for threshold, name in WAR_BADGE_TIERS:
        if count >= threshold:
            return name
    return "No badge"

def determine_graid_badge(count: int) -> str:
    """Return the guild raid badge tier for a given raid count."""
    for threshold, name in GRAID_BADGE_TIERS:
        if count >= threshold:
            return name
    return "No badge"

def determine_event_badge(count: int) -> str:
    """Return the event badge tier for a given event count."""
    for threshold, name in EVENT_BADGE_TIERS:
        if count >= threshold:
            return name
    return "No badge"

def determine_badge(count: int, bagde_type) -> str:
    """Return the event badge tier for a given event count."""
    for threshold, name in bagde_type:
        if count >= threshold:
            return name
    return "No badge"

def normalize_tier_value(tier_str):
    """Convert tier strings like '1.5k' to actual numbers like 1500."""
    if not tier_str or not isinstance(tier_str, str):
        return tier_str
    
    tier_str = tier_str.strip()
    
    # If it ends with 'k', convert to thousands
    if tier_str.lower().endswith('k'):
        try:
            num = float(tier_str[:-1])
            return int(num * 1000)
        except ValueError:
            return tier_str
    
    # Try to convert to int directly
    try:
        return int(tier_str)
    except ValueError:
        return tier_str

def _get_badge_rank(badge_name: str, badge_type: str) -> int:
    """Return a numeric rank for a badge name within its type.

    Higher number = higher tier. Unknown names get rank 0.
    """
    badge_name = badge_name or "No badge"

    # Handle "None" string specially - treat it as the lowest rank
    if badge_name == "None":
        return 0

    if badge_type == "quest":
        tiers = QUEST_BADGE_TIERS
    elif badge_type == "recruitment":
        tiers = RECRUITED_BADGE_TIERS
    elif badge_type == "war":
        tiers = WAR_BADGE_TIERS
    elif badge_type == "graid":
        tiers = GRAID_BADGE_TIERS
    elif badge_type == "event":
        tiers = EVENT_BADGE_TIERS
    else:
        tiers = []

    # Try to normalize numeric tier values (e.g., '1.5k' -> 1500)
    normalized = normalize_tier_value(badge_name)
    if isinstance(normalized, int):
        # Find the rank based on threshold value
        for idx, (threshold, name) in enumerate(reversed(tiers)):
            if normalized >= threshold:
                return len(tiers) - idx - 1
        return 0

    # Names from lowest to highest by reversing the tier list (which is
    # defined highest-threshold-first).
    names_low_to_high = [name for _, name in reversed(tiers)]
    rank_map = {name: idx for idx, name in enumerate(names_low_to_high)}

    # Special-case war "API off" as equivalent to "No data" if present.
    if badge_name == "API off" and badge_type == "war":
        if "No data" in rank_map:
            return rank_map["No data"]
        return 0

    return rank_map.get(badge_name, 0)

def get_latest_databases(db_folder="databases", guild_prefix="ESI", hours_ago=None):
    """Get the most recent database file and optionally a comparison database from X hours ago.
    
    Args:
        db_folder: Folder containing database files
        guild_prefix: Prefix for guild database files
        hours_ago: If specified, find the database closest to this many hours ago for comparison
    
    Returns:
        List of database paths [latest_db] or [latest_db, comparison_db]
    """
    import glob
    from datetime import datetime, timedelta
    
    # Look in the new api_tracking folder structure
    api_tracking_folder = os.path.join(db_folder, "api_tracking")
    
    # Collect all .db files from all day folders
    db_files = []
    if os.path.exists(api_tracking_folder):
        for day_folder in os.listdir(api_tracking_folder):
            day_path = os.path.join(api_tracking_folder, day_folder)
            if os.path.isdir(day_path) and day_folder.startswith("api_"):
                pattern = os.path.join(day_path, f"{guild_prefix}_*.db")
                db_files.extend(glob.glob(pattern))
    
    # Fallback: also check old flat structure for backwards compatibility
    old_pattern = os.path.join(db_folder, f"{guild_prefix}_*.db")
    db_files.extend(glob.glob(old_pattern))
    
    # Sort by modification time (newest first)
    db_files = sorted(db_files, key=os.path.getmtime, reverse=True)
    
    if not db_files:
        return []
    
    latest_db = db_files[0]
    
    # If no time comparison requested, return latest and previous
    if hours_ago is None:
        return db_files[:2] if len(db_files) >= 2 else db_files
    
    # Find database closest to the target time
    target_time = datetime.now() - timedelta(hours=hours_ago)
    
    # Find the database file closest to the target time
    closest_db = None
    min_diff = float('inf')
    
    for db_file in db_files[1:]:  # Skip the latest one
        db_time = datetime.fromtimestamp(os.path.getmtime(db_file))
        time_diff = abs((db_time - target_time).total_seconds())
        
        if time_diff < min_diff:
            min_diff = time_diff
            closest_db = db_file
    
    if closest_db:
        return [latest_db, closest_db]
    else:
        return [latest_db]

def load_uuid_to_username_map(db_path):
    """Load UUID to username mapping from database."""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check if uuid_username_map table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='uuid_username_map'")
        if not cursor.fetchone():
            # Fallback: build map from player_stats
            cursor.execute("SELECT uuid, username FROM player_stats WHERE uuid IS NOT NULL")
            uuid_map = {row[0]: row[1] for row in cursor.fetchall()}
        else:
            cursor.execute("SELECT uuid, username FROM uuid_username_map")
            uuid_map = {row[0]: row[1] for row in cursor.fetchall()}
        
        conn.close()
        return uuid_map
    except Exception as e:
        print(f"Error loading UUID map from {db_path}: {e}")
        return {}

def resolve_to_username(player_id, uuid_map):
    """Resolve a player ID (UUID or username) to username for display."""
    # Check if it looks like a UUID (contains hyphens and is long)
    if player_id and '-' in player_id and len(player_id) >= 32:
        return uuid_map.get(player_id, player_id)
    return player_id

def load_war_data_from_database(db_path):
    """Load war and quest data from a fetch_api database."""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT username, wars, completed_quests
            FROM player_stats
        """)
        
        data = {}
        for row in cursor.fetchall():
            username, wars, quests = row
            data[username] = {
                'wars': wars or 0,
                'quests': quests or 0
            }
        
        conn.close()
        return data
    except Exception as e:
        print(f"Error loading war data from {db_path}: {e}")
        return {}

def save_badges_cache(cache_data):
    """Save badges cache to database."""
    try:
        conn = sqlite3.connect(BADGES_CACHE_FILE)
        cursor = conn.cursor()
        
        # Create table if it doesn't exist with all columns including graid
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS badge_cache (
                username TEXT PRIMARY KEY,
                quest_count INTEGER,
                quest_badge TEXT,
                recruited_count INTEGER,
                recruited_badge TEXT,
                war_count INTEGER,
                war_badge TEXT,
                api_quest_count INTEGER,
                is_api_available INTEGER,
                graid_count INTEGER DEFAULT 0,
                graid_badge TEXT DEFAULT 'No badge',
                event_count INTEGER DEFAULT 0,
                event_badge TEXT DEFAULT 'No badge'
            )
        """)
        
        # Check if graid columns exist, if not add them (migration)
        cursor.execute("PRAGMA table_info(badge_cache)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'graid_count' not in columns:
            print("[MIGRATION] Adding graid_count column to badge_cache")
            cursor.execute("ALTER TABLE badge_cache ADD COLUMN graid_count INTEGER DEFAULT 0")
        
        if 'graid_badge' not in columns:
            print("[MIGRATION] Adding graid_badge column to badge_cache")
            cursor.execute("ALTER TABLE badge_cache ADD COLUMN graid_badge TEXT DEFAULT 'No badge'")
        
        if 'event_count' not in columns:
            print("[MIGRATION] Adding event_count column to badge_cache")
            cursor.execute("ALTER TABLE badge_cache ADD COLUMN event_count INTEGER DEFAULT 0")
        
        if 'event_badge' not in columns:
            print("[MIGRATION] Adding event_badge column to badge_cache")
            cursor.execute("ALTER TABLE badge_cache ADD COLUMN event_badge TEXT DEFAULT 'No badge'")
        
        # Clear old cache
        cursor.execute("DELETE FROM badge_cache")
        
        # Insert new cache data
        for username, data in cache_data.items():
            cursor.execute("""
                INSERT INTO badge_cache (
                    username, quest_count, quest_badge, recruited_count, recruited_badge,
                    war_count, war_badge, api_quest_count, is_api_available,
                    graid_count, graid_badge, event_count, event_badge
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                username,
                data.get("quest_count", 0),
                data.get("quest_badge", "No badge"),
                data.get("recruited_count", 0),
                data.get("recruited_badge", "No badge"),
                data.get("war_count", 0),
                data.get("war_badge", "No badge"),
                data.get("api_quest_count"),
                1 if data.get("is_api_available", False) else 0,
                data.get("graid_count", 0),
                data.get("graid_badge", "No badge"),
                data.get("event_count", 0),
                data.get("event_badge", "No badge")
            ))
        
        conn.commit()
        conn.close()
    except (sqlite3.Error, Exception) as e:
        print(f"Error saving badges cache: {e}")

def load_badges_cache():
    """Load badges cache from database."""
    if not os.path.exists(BADGES_CACHE_FILE):
        return {}
    try:
        conn = sqlite3.connect(BADGES_CACHE_FILE)
        cursor = conn.cursor()
        
        # Check which columns exist
        cursor.execute("PRAGMA table_info(badge_cache)")
        columns = [column[1] for column in cursor.fetchall()]
        has_graid = 'graid_count' in columns and 'graid_badge' in columns
        has_event = 'event_count' in columns and 'event_badge' in columns
        
        if has_graid and has_event:
            cursor.execute("""
                SELECT username, quest_count, quest_badge, recruited_count, recruited_badge,
                    war_count, war_badge, api_quest_count, is_api_available,
                    graid_count, graid_badge, event_count, event_badge
                FROM badge_cache
            """)
        elif has_graid:
            cursor.execute("""
                SELECT username, quest_count, quest_badge, recruited_count, recruited_badge,
                    war_count, war_badge, api_quest_count, is_api_available,
                    graid_count, graid_badge
                FROM badge_cache
            """)
        else:
            cursor.execute("""
                SELECT username, quest_count, quest_badge, recruited_count, recruited_badge,
                    war_count, war_badge, api_quest_count, is_api_available
                FROM badge_cache
            """)
        
        rows = cursor.fetchall()
        conn.close()
        
        cache = {}
        for row in rows:
            cache[row[0]] = {
                "quest_count": row[1],
                "quest_badge": row[2],
                "recruited_count": row[3],
                "recruited_badge": row[4],
                "war_count": row[5],
                "war_badge": row[6],
                "api_quest_count": row[7],
                "is_api_available": bool(row[8]),
                "graid_count": row[9] if has_graid and len(row) > 9 else 0,
                "graid_badge": row[10] if has_graid and len(row) > 10 else "No badge",
                "event_count": row[11] if has_event and len(row) > 11 else 0,
                "event_badge": row[12] if has_event and len(row) > 12 else "No badge"
            }
        return cache
    except (sqlite3.Error, Exception) as e:
        print(f"Error loading badges cache: {e}")
        return {}

def parse_time_string(time_str: str) -> Optional[float]:
    """Parse a time string like '4 hours', '30 minutes', '1 day' into hours.
    
    Args:
        time_str: Time string (e.g., "4 hours", "1 day", "30 minutes")
    
    Returns:
        Number of hours as float, or None if invalid
    """
    if not time_str:
        return None
    
    import re
    
    # Match patterns like "4 hours", "1 day", "30 minutes"
    match = re.match(r'(\d+(?:\.\d+)?)\s*(second|seconds|sec|s|minute|minutes|min|m|hour|hours|hr|h|day|days|d|week|weeks|w)', time_str.lower().strip())
    
    if not match:
        return None
    
    value = float(match.group(1))
    unit = match.group(2)
    
    # Convert to hours
    if unit in ['second', 'seconds', 'sec', 's']:
        return value / 3600
    elif unit in ['minute', 'minutes', 'min', 'm']:
        return value / 60
    elif unit in ['hour', 'hours', 'hr', 'h']:
        return value
    elif unit in ['day', 'days', 'd']:
        return value * 24
    elif unit in ['week', 'weeks', 'w']:
        return value * 24 * 7
    
    return None

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    @bot.tree.command(
        name="badges",
        description="Display quest, recruitment, and war statistics for all players"
    )
    @app_commands.describe(
        badge_type="Choose a specific badge type to view detailed statistics (optional)",
        time_ago="Compare with data from X time ago (e.g., '4 hours', '1 day', '30 minutes', '1 week')"
    )
    @app_commands.choices(
        badge_type=[
            app_commands.Choice(name="All Badges", value="all"),
            app_commands.Choice(name="Quest Badges", value="quest"),
            app_commands.Choice(name="Recruitment Badges", value="recruitment"),
            app_commands.Choice(name="War Badges", value="war"),
            app_commands.Choice(name="Guild Raid Badges", value="graid"),
            app_commands.Choice(name="Event Badges", value="event")
        ]
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def badges(interaction: discord.Interaction, badge_type: str = "all", time_ago: str = None):
        """Show statistics about quests, recruitments, and wars"""
        
        # Defer immediately to prevent timeout
        try:
            await interaction.response.defer()
        except discord.errors.NotFound:
            # Interaction token expired or already responded
            return
        except Exception as e:
            print(f"Error deferring interaction: {e}")
            return
        
        # Check permissions only if in a guild
        if interaction.guild:

            if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
                missing_roles_embed = discord.Embed(
                    title="Permission Denied",
                    description="You don't have permission to use this command!",
                    color=0xFF0000,
                    timestamp=datetime.utcnow()
                )
                await interaction.followup.send(embed=missing_roles_embed, ephemeral=True)
                return

        # Validate time_ago parameter if provided
        if time_ago is not None:
            hours_ago = parse_time_string(time_ago)
            if hours_ago is None:
                print("Invalid time format")
                error_embed = discord.Embed(
                    title="Invalid Time Format",
                    description="Please use a valid time format like '4 hours', '1 day', '30 minutes', or '1 week'",
                    color=0xFF0000,
                    timestamp=datetime.utcnow()
                )
                await interaction.followup.send(embed=error_embed, ephemeral=True)
                return
        else:
            hours_ago = 168 # 1 week in hours

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()

        try:
            # Load previous badge cache
            previous_cache = load_badges_cache()
            
            # Get quest statistics
            c.execute("""
                SELECT player, points, badge 
                FROM quest_progress 
                ORDER BY points DESC
            """)
            quest_data = c.fetchall()

            # Get recruitment statistics
            c.execute("""
                SELECT recruiter, COUNT(*) as recruitment_count 
                FROM recruited 
                GROUP BY recruiter 
                ORDER BY recruitment_count DESC
            """)
            recruitment_data = c.fetchall()
            
            # Get event statistics
            c.execute("""
                SELECT player, points, badge 
                FROM event_progress 
                ORDER BY points DESC
            """)
            event_data = c.fetchall()
            
            # Get detailed recruitment data (who recruited whom)
            c.execute("""
                SELECT recruiter, recruited, timestamp 
                FROM recruited 
                ORDER BY recruiter, timestamp
            """)
            detailed_recruitment_data = c.fetchall()

            # Load war data from databases
            db_files = get_latest_databases(hours_ago=hours_ago)  
            war_data = {}
            comparison_time_info = ""
            uuid_map = {}  # UUID to username mapping

            if len(db_files) >= 1:
                # Load latest database
                latest_db = db_files[0]
                print(f"Loading latest database: {latest_db}")
                
                # Load UUID to username mapping
                uuid_map = load_uuid_to_username_map(latest_db)
                
                latest_data = load_war_data_from_database(latest_db)
                war_data = {
                    "guild_name": ", ".join(GUILDS),
                    "members": {username: data for username, data in latest_data.items()}
                }
                
                # Get time info for display
                if len(db_files) >= 2:
                    comparison_db = db_files[1]
                    latest_time = datetime.fromtimestamp(os.path.getmtime(latest_db))
                    comparison_time = datetime.fromtimestamp(os.path.getmtime(comparison_db))
                    time_diff = latest_time - comparison_time
                    hours_diff = time_diff.total_seconds() / 3600

            else:
                print("No databases found. Run /fetch_api first to collect war data.")
                war_data = {"guild_name": ", ".join(GUILDS), "members": {}}
            
            graid_counts = {}

            # Load graid counts from latest api_tracking database
            try:
                if API_TRACKING_FOLDER.exists():
                    db_files_graid = []
                    for day_folder in API_TRACKING_FOLDER.iterdir():
                        if day_folder.is_dir() and day_folder.name.startswith("api_"):
                            for db_file in day_folder.glob("ESI_*.db"):
                                db_files_graid.append(db_file)
                    
                    if db_files_graid:
                        # Get the most recent database
                        db_files_graid.sort(key=lambda f: f.stat().st_mtime, reverse=True)
                        latest_graid_db = db_files_graid[0]
                        
                        conn_graid = sqlite3.connect(str(latest_graid_db))
                        cursor_graid = conn_graid.cursor()
                        
                        cursor_graid.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='guild_raid_stats'")
                        if cursor_graid.fetchone():
                            cursor_graid.execute("SELECT username, total_graids FROM guild_raid_stats WHERE username IS NOT NULL")
                            for row in cursor_graid.fetchall():
                                graid_counts[row[0]] = row[1]
                        else:
                            print(f"[BADGES] guild_raid_stats table not found in {latest_graid_db}")
                        
                        conn_graid.close()
                    else:
                        print("[BADGES] No api_tracking database files found")
                else:
                    print(f"[BADGES] api_tracking folder not found at {API_TRACKING_FOLDER}")
            except Exception as e:
                print(f"[BADGES] Error loading graid counts from api_tracking: {e}")

            # Create a combined dictionary for all players with calculated badges
            # Key is UUID (for calculations), but we'll resolve to username for display
            player_stats = {}
            badge_changes = []

            # Build a normalization map from current guild member names so that
            # legacy recruiter names (different casing, spacing, etc.) are
            # merged into the current in-game name.
            def _normalize_name(name: str) -> str:
                import unicodedata
                if not isinstance(name, str):
                    return ""
                name = unicodedata.normalize("NFKC", name.strip().lower())
                for ch in [" ", "_", "-", "."]:
                    name = name.replace(ch, "")
                return name

            member_name_map = {}
            if war_data and "members" in war_data:
                for username in war_data["members"].keys():
                    member_name_map[_normalize_name(username)] = username

            # Add quest data (player is UUID, resolve to username)
            for player, points, badge in quest_data:
                username = resolve_to_username(player, uuid_map)
                if username not in player_stats:
                    player_stats[username] = {
                        "quest_count": 0,
                        "quest_badge": "No badge",
                        "recruited_count": 0,
                        "recruited_badge": "No badge",
                        "war_count": 0,
                        "war_badge": "No badge",
                        "api_available": False,
                        "api_quest_count": None,
                        "graid_count": 0,
                        "graid_badge": "No badge",
                        "event_count": 0,
                        "event_badge": "No badge",
                    }
                player_stats[username]["quest_count"] = points
                player_stats[username]["quest_badge"] = determine_quest_badge(points)

            # Add recruitment data (recruiter is UUID, resolve to username)
            # Merge legacy names into current guild member names using normalization
            for recruiter, count in recruitment_data:
                # Resolve UUID to username first
                username = resolve_to_username(recruiter, uuid_map)
                key = username
                norm = _normalize_name(username)
                if norm in member_name_map:
                    key = member_name_map[norm]

                if key not in player_stats:
                    player_stats[key] = {
                        "quest_count": 0,
                        "quest_badge": "No badge",
                        "recruited_count": 0,
                        "recruited_badge": "No badge",
                        "war_count": 0,
                        "war_badge": "No badge",
                        "api_available": False,
                        "api_quest_count": None,
                        "graid_count": 0,
                        "graid_badge": "No badge",
                        "event_count": 0,
                        "event_badge": "No badge",
                    }

                # Sum counts in case multiple recruiter aliases map to the same
                # canonical guild member name.
                prev = player_stats[key].get("recruited_count", 0)
                new_total = prev + count
                player_stats[key]["recruited_count"] = new_total
                player_stats[key]["recruited_badge"] = determine_recruited_badge(new_total)
            
            # Add event data to player_stats (player is UUID, resolve to username)
            for player, points, badge in event_data:
                username = resolve_to_username(player, uuid_map)
                if username not in player_stats:
                    player_stats[username] = {
                        "quest_count": 0,
                        "quest_badge": "No badge",
                        "recruited_count": 0,
                        "recruited_badge": "No badge",
                        "war_count": 0,
                        "war_badge": "No badge",
                        "api_available": False,
                        "api_quest_count": None,
                        "graid_count": 0,
                        "graid_badge": "No badge",
                        "event_count": 0,
                        "event_badge": "No badge",
                    }
                player_stats[username]["event_count"] = points
                player_stats[username]["event_badge"] = determine_event_badge(points)

            # Add war data from API
            if war_data and 'members' in war_data:
                for username, member_data in war_data['members'].items():
                    if username not in player_stats:
                        player_stats[username] = {
                        "quest_count": 0,
                        "quest_badge": "No badge",
                        "recruited_count": 0,
                        "recruited_badge": "No badge",
                        "war_count": 0,
                        "war_badge": "No badge",
                        "api_available": False,
                        "api_quest_count": None,
                        "graid_count": 0,
                        "graid_badge": "No badge",
                        "event_count": 0,
                        "event_badge": "No badge",
                    }
                    
                    wars = member_data.get('wars', 0)
                    api_quests = member_data.get('quests', 0)
                    
                    player_stats[username]["war_count"] = wars
                    player_stats[username]["api_quest_count"] = api_quests
                    player_stats[username]["api_available"] = not (wars == 0 and api_quests == 0)
                    player_stats[username]["war_badge"] = determine_war_badge(wars, api_quests)
            
            # Add guild raid data from api_tracking database
            for username, count in graid_counts.items():
                if username not in player_stats:
                    player_stats[username] = {
                        "quest_count": 0,
                        "quest_badge": "No badge",
                        "recruited_count": 0,
                        "recruited_badge": "No badge",
                        "war_count": 0,
                        "war_badge": "No badge",
                        "api_available": False,
                        "api_quest_count": None,
                        "graid_count": 0,
                        "graid_badge": "No badge"
                    }
                
                player_stats[username]["graid_count"] = count
                player_stats[username]["graid_badge"] = determine_graid_badge(count)
            
            # Filter to only include current guild members (those in war_data)
            if war_data and 'members' in war_data:
                current_guild_members = set(war_data['members'].keys())
                player_stats = {
                    username: stats 
                    for username, stats in player_stats.items() 
                    if username in current_guild_members
                }
                print(f"[BADGES] Filtered to {len(player_stats)} current guild members")

            def _load_badges_from_db(db_path: Path):
                """Return dict[(player, category)] -> {tier, role_id, value} from a snapshot DB.
                Resolves UUIDs to usernames for consistent comparison.

                If the badges table does not exist, returns an empty dict.
                """
                data = {}
                try:
                    conn = sqlite3.connect(db_path)
                    cur = conn.cursor()

                    cur.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='badges'"
                    )
                    if not cur.fetchone():
                        return data
                    
                    # Load UUID map from this database
                    db_uuid_map = load_uuid_to_username_map(db_path)

                    cur.execute(
                        "SELECT player, category, tier, role_id, value FROM badges"
                    )
                    for player, category, tier, role_id, value in cur.fetchall():
                        # Resolve UUID to username for consistent comparison
                        username = resolve_to_username(player, db_uuid_map)
                        key = (username, category)
                        data[key] = {
                            "tier": tier,
                            "role_id": role_id,
                            "value": value,
                        }
                except Exception as e:
                    print(f"Failed to load badges from {db_path}: {e}")
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass

                return data

            # Check for badge changes
            current_guild_members = set(war_data['members'].keys()) if war_data and 'members' in war_data else set()

            # Build badge changes from latest vs comparison snapshot databases
            badge_changes = []

            # Get old badges from comparison database (second in list)
            old_badges = {}
            previous_guild_members = set()
            if len(db_files) >= 2:
                old_badges = _load_badges_from_db(db_files[1])
                # Get actual guild members from the comparison database's player_stats table
                try:
                    conn_prev = sqlite3.connect(db_files[1])
                    cur_prev = conn_prev.cursor()
                    cur_prev.execute("SELECT username FROM player_stats")
                    previous_guild_members = set(row[0] for row in cur_prev.fetchall())
                    conn_prev.close()
                except Exception as e:
                    print(f"Failed to load previous guild members from {db_files[1]}: {e}")
                    previous_guild_members = set()

            # Get new badges from current database  
            new_badges = {}
            if len(db_files) >= 1:
                new_badges = _load_badges_from_db(db_files[0])

            # Track joins and leaves
            newly_joined = current_guild_members - previous_guild_members
            newly_left = previous_guild_members - current_guild_members

            # Compare badges
            all_keys = set(old_badges.keys()) | set(new_badges.keys())
            for key in all_keys:
                player, category = key

                # Only show changes for players currently in the guild
                if player not in current_guild_members:
                    continue

                old = old_badges.get(key)
                new = new_badges.get(key)
                
                # Map category names to badge types
                category_type_map = {
                    "Quest Badges": "quest",
                    "Recruitment Badges": "recruitment",
                    "War Badges": "war",
                    "Raid Badges": "graid",
                    "Event Badges": "event"
                }
                badge_type_name = category_type_map.get(category, category.lower())
                
                if old is None and new is not None:
                    badge_changes.append({
                        "user": player,
                        "type": badge_type_name,
                        "old_badge": "None",
                        "new_badge": new["tier"],
                        "old_count": 0,
                        "new_count": new["value"]
                    })
                elif old is not None and new is None:
                    badge_changes.append({
                        "user": player,
                        "type": badge_type_name,
                        "old_badge": old["tier"],
                        "new_badge": "None",
                        "old_count": old["value"],
                        "new_count": 0
                    })
                elif (
                    old is not None
                    and new is not None
                    and old["tier"] != new["tier"]
                ):
                    badge_changes.append({
                        "user": player,
                        "type": badge_type_name,
                        "old_badge": old["tier"],
                        "new_badge": new["tier"],
                        "old_count": old["value"],
                        "new_count": new["value"]
                    })

            # Add join/leave changes
            for user in newly_joined:
                badge_changes.append({
                    "user": user,
                    "type": "joined",
                    "old_badge": "",
                    "new_badge": "",
                    "old_count": 0,
                    "new_count": 0
                })

            for user in newly_left:
                badge_changes.append({
                    "user": user,
                    "type": "left",
                    "old_badge": "",
                    "new_badge": "",
                    "old_count": 0,
                    "new_count": 0
                })

            # Check if there's any data
            if not player_stats:
                no_data_embed = discord.Embed(
                    title="Badge Statistics",
                    description="No statistics available yet. Start completing quests and recruiting players!",
                    color=0xFFA500,
                    timestamp=datetime.utcnow()
                )
                await interaction.followup.send(embed=no_data_embed)
                return

            # Filter badge changes by type
            filtered_badge_changes = badge_changes if badge_type == "all" else [c for c in badge_changes if c["type"] == badge_type]

            # player_stats already uses usernames (resolved from UUIDs earlier)
            filtered_player_stats = player_stats

            # Sort players by total score or specific badge type
            if badge_type == "quest":
                sorted_players = sorted(
                    filtered_player_stats.items(),
                    key=lambda x: x[1]["quest_count"],
                    reverse=True
                )
            elif badge_type == "recruitment":
                sorted_players = sorted(
                    filtered_player_stats.items(),
                    key=lambda x: x[1]["recruited_count"],
                    reverse=True
                )
            elif badge_type == "war":
                sorted_players = sorted(
                    filtered_player_stats.items(),
                    key=lambda x: x[1]["war_count"],
                    reverse=True
                )
            elif badge_type == "graid":
                sorted_players = sorted(
                    filtered_player_stats.items(),
                    key=lambda x: x[1]["graid_count"],
                    reverse=True
                )
            elif badge_type == "event":
                sorted_players = sorted(
                    filtered_player_stats.items(),
                    key=lambda x: x[1]["event_count"],
                    reverse=True
                )
            else:
                sorted_players = sorted(
                    filtered_player_stats.items(),
                    key=lambda x: (x[1]["quest_count"] + x[1]["recruited_count"] * 5 + 
                                x[1]["war_count"] / 10 + x[1]["graid_count"] * 3),
                    reverse=True
                )

            # Calculate totals
            total_players = len(filtered_player_stats)
            total_quest_points = sum(stats["quest_count"] for stats in filtered_player_stats.values())
            total_recruitments = sum(stats["recruited_count"] for stats in filtered_player_stats.values())
            total_wars = sum(stats["war_count"] for stats in filtered_player_stats.values())
            total_graids = sum(stats["graid_count"] for stats in filtered_player_stats.values())
            total_events = sum(stats["event_count"] for stats in filtered_player_stats.values())

            # Create detailed text file based on badge type
            report_lines = []
            
            if badge_type == "all":
                report_lines.append(f"Badge Statistics Report for {', '.join(GUILDS)}")
                report_lines.append(f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
                report_lines.append(f"Total Players: {total_players}")
                report_lines.append("")
                
                # Add badge changes section
                if filtered_badge_changes:
                    report_lines.append("RECENT BADGE CHANGES")
                    report_lines.append("=" * 120)
                    for change in filtered_badge_changes:
                        if change["type"] == "joined":
                            report_lines.append(f"✓ {change['user']}: Joined the guild")
                        elif change["type"] == "left":
                            report_lines.append(f"✗ {change['user']}: Left the guild")
                        else:
                            old_rank = _get_badge_rank(change["old_badge"], change["type"])
                            new_rank = _get_badge_rank(change["new_badge"], change["type"])
                            if new_rank > old_rank:
                                change_type = "↑"
                            elif new_rank < old_rank:
                                change_type = "↓"
                            else:
                                change_type = "→"
                            report_lines.append(
                                f"{change_type} {change['user']}: {change['type'].title()} badge: "
                                f"{change['old_badge']} → {change['new_badge']} "
                                f"({change['old_count']} → {change['new_count']})"
                            )
                    report_lines.append("")
                
                report_lines.append("ALL PLAYERS")
                report_lines.append("=" * 180)
                report_lines.append("")
                
                # Calculate dynamic column widths
                max_username_len = max(len(player) for player in player_stats.keys()) if player_stats else 8
                max_quest_badge_len = max(len(stats["quest_badge"]) for stats in player_stats.values()) if player_stats else 14
                max_recruited_badge_len = max(len(stats["recruited_badge"]) for stats in player_stats.values()) if player_stats else 14
                max_war_badge_len = max(len(stats["war_badge"]) for stats in player_stats.values()) if player_stats else 28
                max_graid_badge_len = max(len(stats["graid_badge"]) for stats in player_stats.values()) if player_stats else 14
                max_event_badge_len = max(len(stats["event_badge"]) for stats in player_stats.values()) if player_stats else 14
                
                username_width = max(max_username_len, 8)
                quest_badge_width = max(max_quest_badge_len, 14)
                recruited_badge_width = max(max_recruited_badge_len, 14)
                war_badge_width = max(max_war_badge_len, 28)
                graid_badge_width = max(max_graid_badge_len, 14)
                event_badge_width = max(max_event_badge_len, 14)
                
                # Create header
                header = f"{'Username':<{username_width}} | {'Q.Pts':<6} | {'Q.Badge':<{quest_badge_width}} | {'Rec':<4} | {'R.Badge':<{recruited_badge_width}} | {'Wars':<8} | {'W.Badge':<{war_badge_width}} | {'GRaids':<8} | {'G.Badge':<{graid_badge_width}} | {'E.Pts':<6} | {'E.Badge':<{event_badge_width}} | {'API':<12}"
                report_lines.append(header)
                report_lines.append("-" * len(header))
                
                # Add player data
                for player, stats in sorted_players:
                    api_status = "Available" if stats["api_available"] else "Off/Private"
                    if stats["war_count"] == 0 and not stats["api_available"]:
                        api_status = "No data"
                    
                    line = (
                        f"{player:<{username_width}} | "
                        f"{stats['quest_count']:<6} | "
                        f"{stats['quest_badge']:<{quest_badge_width}} | "
                        f"{stats['recruited_count']:<4} | "
                        f"{stats['recruited_badge']:<{recruited_badge_width}} | "
                        f"{stats['war_count']:<8} | "
                        f"{stats['war_badge']:<{war_badge_width}} | "
                        f"{stats['graid_count']:<8} | "
                        f"{stats['graid_badge']:<{graid_badge_width}} | "
                        f"{stats['event_count']:<6} | "
                        f"{stats['event_badge']:<{event_badge_width}} | "
                        f"{api_status:<12}"
                    )
                    report_lines.append(line)
                
                report_lines.append("")
                report_lines.append("=" * 180)
                report_lines.append("")
                report_lines.append("SUMMARY STATISTICS")
                report_lines.append("-" * 40)
                report_lines.append(f"Total Players: {total_players}")
                report_lines.append(f"Total Quests Points: {total_quest_points:,}")
                report_lines.append(f"Total Recruitment Points: {total_recruitments:,}")
                report_lines.append(f"Total Event Points: {total_events:,}")
                report_lines.append(f"Total Wars: {total_wars:,}")
                report_lines.append(f"Total Guild Raids: {total_graids:,}")
            
            elif badge_type == "recruitment":
                report_lines.append(f"Detailed Recruitment Statistics for {', '.join(GUILDS)}")
                report_lines.append(f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
                report_lines.append(f"Total Recruiters: {sum(1 for stats in player_stats.values() if stats['recruited_count'] > 0)}")
                report_lines.append(f"Total Recruitments: {total_recruitments}")
                report_lines.append("")
                
                # Add recruitment badge changes
                if filtered_badge_changes:
                    report_lines.append("RECENT RECRUITMENT BADGE CHANGES")
                    report_lines.append("=" * 120)
                    for change in filtered_badge_changes:
                        if change["type"] == "joined":
                            report_lines.append(f"✓ {change['user']}: Joined the guild")
                        elif change["type"] == "left":
                            report_lines.append(f"✗ {change['user']}: Left the guild")
                        else:
                            old_rank = _get_badge_rank(change["old_badge"], "recruitment")
                            new_rank = _get_badge_rank(change["new_badge"], "recruitment")
                            if new_rank > old_rank:
                                change_type = "↑"
                            elif new_rank < old_rank:
                                change_type = "↓"
                            else:
                                change_type = "→"
                            report_lines.append(
                                f"{change_type} {change['user']}: "
                                f"{change['old_badge']} → {change['new_badge']} "
                                f"({change['old_count']} → {change['new_count']} recruitments)"
                            )
                    report_lines.append("")
                
                # Leaderboard section
                report_lines.append("RECRUITMENT LEADERBOARD")
                report_lines.append("=" * 100)
                report_lines.append("")
                
                # Calculate widths for leaderboard
                max_username_len = max(len(player) for player, stats in sorted_players if stats["recruited_count"] > 0) if any(stats["recruited_count"] > 0 for _, stats in sorted_players) else 8
                max_badge_len = max(len(stats["recruited_badge"]) for _, stats in sorted_players if stats["recruited_count"] > 0) if any(stats["recruited_count"] > 0 for _, stats in sorted_players) else 14
                
                username_width = max(max_username_len, 8)
                badge_width = max(max_badge_len, 14)
                
                header = f"{'Rank':<6} | {'Recruiter':<{username_width}} | {'Count':<6} | {'Badge':<{badge_width}}"
                report_lines.append(header)
                report_lines.append("-" * len(header))
                
                rank = 1
                for player, stats in sorted_players:
                    if stats["recruited_count"] > 0:
                        line = f"{rank:<6} | {player:<{username_width}} | {stats['recruited_count']:<6} | {stats['recruited_badge']:<{badge_width}}"
                        report_lines.append(line)
                        rank += 1
                
                report_lines.append("")
                report_lines.append("=" * 100)
                report_lines.append("")
                
                # Detailed recruitment records
                report_lines.append("DETAILED RECRUITMENT RECORDS")
                report_lines.append("=" * 120)
                report_lines.append("")
                
                # Group recruitments by recruiter
                recruitment_map = {}
                for recruiter, recruited, timestamp in detailed_recruitment_data:
                    # Resolve UUIDs to usernames
                    recruiter_username = resolve_to_username(recruiter, uuid_map)
                    recruited_username = resolve_to_username(recruited, uuid_map)
                    
                    if recruiter_username not in recruitment_map:
                        recruitment_map[recruiter_username] = []
                    recruitment_map[recruiter_username].append((recruited_username, timestamp))
                
                # Sort recruiters by number of recruitments
                sorted_recruiters = sorted(recruitment_map.items(), key=lambda x: len(x[1]), reverse=True)
                
                for recruiter, recruits in sorted_recruiters:
                    recruiter_badge = player_stats.get(recruiter, {}).get("recruited_badge", "No badge")
                    report_lines.append(f"{recruiter} ({len(recruits)} recruitments - {recruiter_badge})")
                    report_lines.append("-" * 80)
                    
                    for recruited, timestamp in recruits:
                        try:
                            dt = datetime.fromisoformat(timestamp)
                            formatted_time = dt.strftime('%Y-%m-%d %H:%M:%S')
                        except:
                            formatted_time = timestamp
                        report_lines.append(f"  • {recruited} (recruited on {formatted_time})")
                    
                    report_lines.append("")
                
                # Badge distribution
                report_lines.append("RECRUITMENT BADGE DISTRIBUTION")
                report_lines.append("=" * 60)
                badge_counts = {}
                for stats in player_stats.values():
                    if stats["recruited_count"] > 0:
                        badge = stats["recruited_badge"]
                        badge_counts[badge] = badge_counts.get(badge, 0) + 1
                
                # Sort by badge tier
                for threshold, badge_name in RECRUITED_BADGE_TIERS:
                    if badge_name in badge_counts:
                        report_lines.append(f"{badge_name}: {badge_counts[badge_name]} players")
                
                report_lines.append("")
                report_lines.append("SUMMARY")
                report_lines.append("-" * 40)
                report_lines.append(f"Total Recruiters: {sum(1 for stats in player_stats.values() if stats['recruited_count'] > 0)}")
                report_lines.append(f"Total Recruitments: {total_recruitments}")
                report_lines.append(f"Average Recruitments per Recruiter: {total_recruitments / max(sum(1 for stats in player_stats.values() if stats['recruited_count'] > 0), 1):.2f}")
            
            elif badge_type == "quest":
                report_lines.append(f"Detailed Quest Statistics for {', '.join(GUILDS)}")
                report_lines.append(f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
                report_lines.append(f"Total Players with Quests: {sum(1 for stats in player_stats.values() if stats['quest_count'] > 0)}")
                report_lines.append(f"Total Quest Points: {total_quest_points:,}")
                report_lines.append("")
                
                # Add quest badge changes
                if filtered_badge_changes:
                    report_lines.append("RECENT BADGE CHANGES")
                    report_lines.append("=" * 120)
                    for change in filtered_badge_changes:
                        if change["type"] == "joined":
                            report_lines.append(f"✓ {change['user']}: Joined the guild")
                        elif change["type"] == "left":
                            report_lines.append(f"✗ {change['user']}: Left the guild")
                        else:
                            old_rank = _get_badge_rank(change["old_badge"], "quest")
                            new_rank = _get_badge_rank(change["new_badge"], "quest")
                            if new_rank > old_rank:
                                change_type = "↑"
                            elif new_rank < old_rank:
                                change_type = "↓"
                            else:
                                change_type = "→"
                            report_lines.append(
                                f"{change_type} {change['user']}: {change['type'].title()} badge "
                                f"{change_type} {change['user']}: {change['type'].title()} badge "
                                f"{change['old_badge']} → {change['new_badge']} "
                                f"({change['old_count']} → {change['new_count']})"
                            )
                    report_lines.append("")
                
                # Leaderboard
                report_lines.append("QUEST LEADERBOARD")
                report_lines.append("=" * 100)
                report_lines.append("")
                
                max_username_len = max(len(player) for player, stats in sorted_players if stats["quest_count"] > 0) if any(stats["quest_count"] > 0 for _, stats in sorted_players) else 8
                max_badge_len = max(len(stats["quest_badge"]) for _, stats in sorted_players if stats["quest_count"] > 0) if any(stats["quest_count"] > 0 for _, stats in sorted_players) else 14
                
                username_width = max(max_username_len, 8)
                badge_width = max(max_badge_len, 14)
                
                header = f"{'Rank':<6} | {'Player':<{username_width}} | {'Points':<8} | {'Badge':<{badge_width}}"
                report_lines.append(header)
                report_lines.append("-" * len(header))
                
                rank = 1
                for player, stats in sorted_players:
                    if stats["quest_count"] > 0:
                        line = f"{rank:<6} | {player:<{username_width}} | {stats['quest_count']:<8} | {stats['quest_badge']:<{badge_width}}"
                        report_lines.append(line)
                        rank += 1
                
                report_lines.append("")
                report_lines.append("=" * 100)
                report_lines.append("")
                
                # Badge distribution
                report_lines.append("QUEST BADGE DISTRIBUTION")
                report_lines.append("=" * 60)
                badge_counts = {}
                for stats in player_stats.values():
                    if stats["quest_count"] > 0:
                        badge = stats["quest_badge"]
                        badge_counts[badge] = badge_counts.get(badge, 0) + 1
                
                for threshold, badge_name in QUEST_BADGE_TIERS:
                    if badge_name in badge_counts:
                        report_lines.append(f"{badge_name}: {badge_counts[badge_name]} players")
                
                report_lines.append("")
                report_lines.append("SUMMARY")
                report_lines.append("-" * 40)
                report_lines.append(f"Total Players: {sum(1 for stats in player_stats.values() if stats['quest_count'] > 0)}")
                report_lines.append(f"Total Quest Points: {total_quest_points:,}")
                report_lines.append(f"Average Quest Points per Player: {total_quest_points / max(sum(1 for stats in player_stats.values() if stats['quest_count'] > 0), 1):.2f}")
            
            elif badge_type == "war":
                report_lines.append(f"Detailed War Statistics for {', '.join(GUILDS)}")
                report_lines.append(f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
                report_lines.append(f"Total Players with Wars: {sum(1 for stats in player_stats.values() if stats['war_count'] > 0)}")
                report_lines.append(f"Total Wars: {total_wars:,}")
                report_lines.append("")
                
                # Add war badge changes
                if filtered_badge_changes:
                    report_lines.append("RECENT BADGE CHANGES")
                    report_lines.append("=" * 120)
                    for change in filtered_badge_changes:
                        if change["type"] == "joined":
                            report_lines.append(f"✓ {change['user']}: Joined the guild")
                        elif change["type"] == "left":
                            report_lines.append(f"✗ {change['user']}: Left the guild")
                        else:
                            old_rank = _get_badge_rank(change["old_badge"], "war")
                            new_rank = _get_badge_rank(change["new_badge"], "war")
                            if new_rank > old_rank:
                                change_type = "↑"
                            elif new_rank < old_rank:
                                change_type = "↓"
                            else:
                                change_type = "→"
                            report_lines.append(
                                f"{change_type} {change['user']}: {change['type'].title()} badge "
                                f"{change['old_badge']} → {change['new_badge']} "
                                f"({change['old_count']} → {change['new_count']})"
                            )
                    report_lines.append("")
                
                # Leaderboard
                report_lines.append("WAR LEADERBOARD")
                report_lines.append("=" * 120)
                report_lines.append("")
                
                max_username_len = max(len(player) for player, stats in sorted_players if stats["war_count"] > 0) if any(stats["war_count"] > 0 for _, stats in sorted_players) else 8
                max_badge_len = max(len(stats["war_badge"]) for _, stats in sorted_players if stats["war_count"] > 0) if any(stats["war_count"] > 0 for _, stats in sorted_players) else 28
                
                username_width = max(max_username_len, 8)
                badge_width = max(max_badge_len, 28)
                
                header = f"{'Rank':<6} | {'Player':<{username_width}} | {'Wars':<10} | {'Badge':<{badge_width}} | {'API Status':<12}"
                report_lines.append(header)
                report_lines.append("-" * len(header))
                
                rank = 1
                for player, stats in sorted_players:
                    if stats["war_count"] > 0:
                        api_status = "Available" if stats["api_available"] else "Off/Private"
                        line = f"{rank:<6} | {player:<{username_width}} | {stats['war_count']:<10} | {stats['war_badge']:<{badge_width}} | {api_status:<12}"
                        report_lines.append(line)
                        rank += 1
                
                report_lines.append("")
                report_lines.append("=" * 120)
                report_lines.append("")
                
                # Badge distribution
                report_lines.append("WAR BADGE DISTRIBUTION")
                report_lines.append("=" * 60)
                badge_counts = {}
                for stats in player_stats.values():
                    if stats["war_count"] > 0:
                        badge = stats["war_badge"]
                        badge_counts[badge] = badge_counts.get(badge, 0) + 1
                
                for threshold, badge_name in WAR_BADGE_TIERS:
                    if badge_name in badge_counts:
                        report_lines.append(f"{badge_name}: {badge_counts[badge_name]} players")
                
                report_lines.append("")
                report_lines.append("SUMMARY")
                report_lines.append("-" * 40)
                report_lines.append(f"Total Players: {sum(1 for stats in player_stats.values() if stats['war_count'] > 0)}")
                report_lines.append(f"Total Wars: {total_wars:,}")
                report_lines.append(f"Average Wars per Player: {total_wars / max(sum(1 for stats in player_stats.values() if stats['war_count'] > 0), 1):.2f}")
            
            elif badge_type == "graid":
                report_lines.append(f"Detailed Guild Raid Statistics for {', '.join(GUILDS)}")
                report_lines.append(f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
                report_lines.append(f"Total Raiders: {sum(1 for stats in player_stats.values() if stats['graid_count'] > 0)}")
                report_lines.append(f"Total Guild Raids: {total_graids:,}")
                report_lines.append("")
                
                # Add graid badge changes
                if filtered_badge_changes:
                    report_lines.append("RECENT BADGE CHANGES")
                    report_lines.append("=" * 120)
                    for change in filtered_badge_changes:
                        if change["type"] == "joined":
                            report_lines.append(f"✓ {change['user']}: Joined the guild")
                        elif change["type"] == "left":
                            report_lines.append(f"✗ {change['user']}: Left the guild")
                        else:
                            old_rank = _get_badge_rank(change["old_badge"], "graid")
                            new_rank = _get_badge_rank(change["new_badge"], "graid")
                            if new_rank > old_rank:
                                change_type = "↑"
                            elif new_rank < old_rank:
                                change_type = "↓"
                            else:
                                change_type = "→"
                            report_lines.append(
                                f"{change_type} {change['user']}: {change['type'].title()} badge "
                                f"{change['old_badge']} → {change['new_badge']} "
                                f"({change['old_count']} → {change['new_count']})"
                            )
                    report_lines.append("")
                
                # Leaderboard
                report_lines.append("GUILD RAID LEADERBOARD")
                report_lines.append("=" * 100)
                report_lines.append("")
                
                max_username_len = max(len(player) for player, stats in sorted_players if stats["graid_count"] > 0) if any(stats["graid_count"] > 0 for _, stats in sorted_players) else 8
                max_badge_len = max(len(stats["graid_badge"]) for _, stats in sorted_players if stats["graid_count"] > 0) if any(stats["graid_count"] > 0 for _, stats in sorted_players) else 14
                
                username_width = max(max_username_len, 8)
                badge_width = max(max_badge_len, 14)
                
                header = f"{'Rank':<6} | {'Player':<{username_width}} | {'GRaids':<8} | {'Badge':<{badge_width}}"
                report_lines.append(header)
                report_lines.append("-" * len(header))
                
                rank = 1
                for player, stats in sorted_players:
                    if stats["graid_count"] > 0:
                        line = f"{rank:<6} | {player:<{username_width}} | {stats['graid_count']:<8} | {stats['graid_badge']:<{badge_width}}"
                        report_lines.append(line)
                        rank += 1
                
                report_lines.append("")
                report_lines.append("=" * 100)
                report_lines.append("")
                
                # Badge distribution
                report_lines.append("GUILD RAID BADGE DISTRIBUTION")
                report_lines.append("=" * 60)
                badge_counts = {}
                for stats in player_stats.values():
                    if stats["graid_count"] > 0:
                        badge = stats["graid_badge"]
                        badge_counts[badge] = badge_counts.get(badge, 0) + 1
                
                for threshold, badge_name in GRAID_BADGE_TIERS:
                    if badge_name in badge_counts:
                        report_lines.append(f"{badge_name}: {badge_counts[badge_name]} players")
                
                report_lines.append("")
                report_lines.append("SUMMARY")
                report_lines.append("-" * 40)
                report_lines.append(f"Total Raiders: {sum(1 for stats in player_stats.values() if stats['graid_count'] > 0)}")
                report_lines.append(f"Total Guild Raids: {total_graids:,}")
                report_lines.append(f"Average Raids per Player: {total_graids / max(sum(1 for stats in player_stats.values() if stats['graid_count'] > 0), 1):.2f}")

            elif badge_type == "event":
                report_lines.append(f"Detailed Event Statistics for {', '.join(GUILDS)}")
                report_lines.append(f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
                report_lines.append(f"Total Players with Events: {sum(1 for stats in player_stats.values() if stats['event_count'] > 0)}")
                report_lines.append(f"Total Event Points: {total_events:,}")
                report_lines.append("")
                
                # Add event badge changes
                if filtered_badge_changes:
                    report_lines.append("RECENT BADGE CHANGES")
                    report_lines.append("=" * 120)
                    for change in filtered_badge_changes:
                        if change["type"] == "joined":
                            report_lines.append(f"✓ {change['user']}: Joined the guild")
                        elif change["type"] == "left":
                            report_lines.append(f"✗ {change['user']}: Left the guild")
                        else:
                            if change["new_count"] > change["old_count"]:
                                change_type = "↑"
                            elif change["new_count"] < change["old_count"]:
                                change_type = "↓"
                            else:
                                change_type = "→"
                            report_lines.append(
                                f"{change_type} {change['user']}: {change['type'].title()} badge "
                                f"{change['old_badge']} → {change['new_badge']} "
                                f"({change['old_count']} → {change['new_count']})"
                            )
                    report_lines.append("")
                
                # Leaderboard
                report_lines.append("EVENT LEADERBOARD")
                report_lines.append("=" * 100)
                report_lines.append("")
                
                max_username_len = max(len(player) for player, stats in sorted_players if stats["event_count"] > 0) if any(stats["event_count"] > 0 for _, stats in sorted_players) else 8
                max_badge_len = max(len(stats["event_badge"]) for _, stats in sorted_players if stats["event_count"] > 0) if any(stats["event_count"] > 0 for _, stats in sorted_players) else 14
                
                username_width = max(max_username_len, 8)
                badge_width = max(max_badge_len, 14)
                
                header = f"{'Rank':<6} | {'Player':<{username_width}} | {'Points':<8} | {'Badge':<{badge_width}}"
                report_lines.append(header)
                report_lines.append("-" * len(header))
                
                rank = 1
                for player, stats in sorted_players:
                    if stats["event_count"] > 0:
                        line = f"{rank:<6} | {player:<{username_width}} | {stats['event_count']:<8} | {stats['event_badge']:<{badge_width}}"
                        report_lines.append(line)
                        rank += 1
                
                report_lines.append("")
                report_lines.append("=" * 100)
                report_lines.append("")
                
                # Badge distribution
                report_lines.append("EVENT BADGE DISTRIBUTION")
                report_lines.append("=" * 60)
                badge_counts = {}
                for stats in player_stats.values():
                    if stats["event_count"] > 0:
                        badge = stats["event_badge"]
                        badge_counts[badge] = badge_counts.get(badge, 0) + 1
                
                for threshold, badge_name in EVENT_BADGE_TIERS:
                    if badge_name in badge_counts:
                        report_lines.append(f"{badge_name}: {badge_counts[badge_name]} players")
                
                report_lines.append("")
                report_lines.append("SUMMARY")
                report_lines.append("-" * 40)
                report_lines.append(f"Total Players: {sum(1 for stats in player_stats.values() if stats['event_count'] > 0)}")
                report_lines.append(f"Total Event Points: {total_events:,}")
                report_lines.append(f"Average Event Points per Player: {total_events / max(sum(1 for stats in player_stats.values() if stats['event_count'] > 0), 1):.2f}")

            # Create temporary file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
                f.write("\n".join(report_lines))
                temp_file_path = f.name
            
            file_attachment = discord.File(temp_file_path, filename=f"badge_report_{badge_type}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt")

            # Build the embed based on badge type
            if badge_type == "all":
                embed_title = "Player Statistics"
                embed_description = "Badge statistics for all players - Full report attached"
                embed_color = 0x00AAFF
            elif badge_type == "recruitment":
                embed_title = "Recruitment Statistics"
                embed_description = f"Detailed recruitment statistics - {sum(1 for stats in player_stats.values() if stats['recruited_count'] > 0)} recruiters found"
                embed_color = 0x00FF00
            elif badge_type == "quest":
                embed_title = "Quest Statistics"
                embed_description = f"Detailed quest statistics - {sum(1 for stats in player_stats.values() if stats['quest_count'] > 0)} players with quests"
                embed_color = 0xFFAA00
            elif badge_type == "war":
                embed_title = "War Statistics"
                embed_description = f"Detailed war statistics - {sum(1 for stats in player_stats.values() if stats['war_count'] > 0)} players with wars"
                embed_color = 0xFF0000
            elif badge_type == "graid":
                embed_title = "Guild Raid Statistics"
                embed_description = f"Detailed guild raid statistics - {sum(1 for stats in player_stats.values() if stats['graid_count'] > 0)} raiders found"
                embed_color = 0xFFFF00
            else: # event
                embed_title = "Event Statistics"
                embed_description = f"Detailed event statistics - {sum(1 for stats in player_stats.values() if stats['event_count'] > 0)} players with events"
                embed_color = 0xFF00FF
            
            stats_embed = discord.Embed(
                title=embed_title,
                description=embed_description,
                color=embed_color,
                timestamp=datetime.utcnow()
            )

            # Show badge changes if any
            if filtered_badge_changes:
                changes_text = []
                display_limit = 10 if badge_type == "all" else 15
                for change in filtered_badge_changes[:display_limit]:
                    if change["type"] == "joined":
                        changes_text.append(f"✓ **{change['user']}** joined the guild")
                    elif change["type"] == "left":
                        changes_text.append(f"✗ **{change['user']}** left the guild")
                    else:
                        changes_text.append(
                            f"**{change['user']}** {change['type'].title()} badge: "
                            f"`{change['old_badge']}` → `{change['new_badge']}`"
                        )
                
                if len(filtered_badge_changes) > display_limit:
                    changes_text.append(f"\n... and {len(filtered_badge_changes) - display_limit} more changes (see attached file)")
                
                stats_embed.add_field(
                    name=f"Recent {badge_type.title() if badge_type != 'all' else ''} Badge Changes".strip(),
                    value="\n".join(changes_text),
                    inline=False
                )

            # Add statistics summary based on badge type
            if badge_type == "all":
                stats_summary = (
                    f"**Total Players:** {total_players}\n"
                    f"**Total Quest Points:** {total_quest_points:,}\n"
                    f"**Total Recruitment Points:** {total_recruitments:,}\n"
                    f"**Total Event Points:** {total_events:,}\n"
                    f"**Total Wars:** {total_wars:,}\n"
                    f"**Total Guild Raids:** {total_graids:,}"
                )
                stats_embed.add_field(
                    name="Overall Statistics",
                    value=stats_summary,
                    inline=False
                )
            elif badge_type == "recruitment":
                # Top recruiters
                top_recruiters = [(player, stats) for player, stats in sorted_players if stats["recruited_count"] > 0][:10]
                if top_recruiters:
                    top_text = []
                    for idx, (player, stats) in enumerate(top_recruiters, 1):
                        medal = "1." if idx == 1 else "2." if idx == 2 else "3." if idx == 3 else f"{idx}."
                        top_text.append(f"{medal} **{player}**: {stats['recruited_count']} recruitments ({stats['recruited_badge']})")
                    
                    stats_embed.add_field(
                        name="Top Recruiters",
                        value="\n".join(top_text),
                        inline=False
                    )
                
                stats_summary = (
                    f"**Total Recruiters:** {sum(1 for stats in player_stats.values() if stats['recruited_count'] > 0)}\n"
                    f"**Total Recruitments:** {total_recruitments:,}\n"
                    f"**Average per Recruiter:** {total_recruitments / max(sum(1 for stats in player_stats.values() if stats['recruited_count'] > 0), 1):.2f}"
                )
                stats_embed.add_field(
                    name="Recruitment Summary",
                    value=stats_summary,
                    inline=False
                )
            elif badge_type == "quest":
                # Top questers
                top_questers = [(player, stats) for player, stats in sorted_players if stats["quest_count"] > 0][:10]
                if top_questers:
                    top_text = []
                    for idx, (player, stats) in enumerate(top_questers, 1):
                        medal = "1." if idx == 1 else "2." if idx == 2 else "3." if idx == 3 else f"{idx}."
                        top_text.append(f"{medal} **{player}**: {stats['quest_count']} points ({stats['quest_badge']})")
                    
                    stats_embed.add_field(
                        name="Top Questers",
                        value="\n".join(top_text),
                        inline=False
                    )
                
                stats_summary = (
                    f"**Total Players:** {sum(1 for stats in player_stats.values() if stats['quest_count'] > 0)}\n"
                    f"**Total Quest Points:** {total_quest_points:,}\n"
                    f"**Average per Player:** {total_quest_points / max(sum(1 for stats in player_stats.values() if stats['quest_count'] > 0), 1):.2f}"
                )
                stats_embed.add_field(
                    name="Quest Summary",
                    value=stats_summary,
                    inline=False
                )
            elif badge_type == "war":
                # Top warriors
                top_warriors = [(player, stats) for player, stats in sorted_players if stats["war_count"] > 0][:10]
                if top_warriors:
                    top_text = []
                    for idx, (player, stats) in enumerate(top_warriors, 1):
                        medal = "1." if idx == 1 else "2." if idx == 2 else "3." if idx == 3 else f"{idx}."
                        top_text.append(f"{medal} **{player}**: {stats['war_count']:,} wars ({stats['war_badge']})")
                    
                    stats_embed.add_field(
                        name="Top Warriors",
                        value="\n".join(top_text),
                        inline=False
                    )
                
                stats_summary = (
                    f"**Total Players:** {sum(1 for stats in player_stats.values() if stats['war_count'] > 0)}\n"
                    f"**Total Wars:** {total_wars:,}\n"
                    f"**Average per Player:** {total_wars / max(sum(1 for stats in player_stats.values() if stats['war_count'] > 0), 1):.2f}"
                )
                stats_embed.add_field(
                    name="War Summary",
                    value=stats_summary,
                    inline=False
                )
            elif badge_type == "graid":
                # Top raiders
                top_raiders = [(player, stats) for player, stats in sorted_players if stats["graid_count"] > 0][:10]
                if top_raiders:
                    top_text = []
                    for idx, (player, stats) in enumerate(top_raiders, 1):
                        medal = "1." if idx == 1 else "2." if idx == 2 else "3." if idx == 3 else f"{idx}."
                        top_text.append(f"{medal} **{player}**: {stats['graid_count']:,} graids ({stats['graid_badge']})")
                    
                    stats_embed.add_field(
                        name="Top Guild Raiders",
                        value="\n".join(top_text),
                        inline=False
                    )
                
                stats_summary = (
                    f"**Total Raiders:** {sum(1 for stats in player_stats.values() if stats['graid_count'] > 0)}\n"
                    f"**Total Guild Raids:** {total_graids:,}\n"
                    f"**Average per Raider:** {total_graids / max(sum(1 for stats in player_stats.values() if stats['graid_count'] > 0), 1):.2f}"
                )
                stats_embed.add_field(
                    name="Guild Raid Summary",
                    value=stats_summary,
                    inline=False
                )
            else:  # event
                # Top event participants
                top_event_players = [(player, stats) for player, stats in sorted_players if stats["event_count"] > 0][:10]
                if top_event_players:
                    top_text = []
                    for idx, (player, stats) in enumerate(top_event_players, 1):
                        medal = "1." if idx == 1 else "2." if idx == 2 else "3." if idx == 3 else f"{idx}."
                        top_text.append(f"{medal} **{player}**: {stats['event_count']:,} points ({stats['event_badge']})")
                    
                    stats_embed.add_field(
                        name="Top Event Participants",
                        value="\n".join(top_text),
                        inline=False
                    )
                
                stats_summary = (
                    f"**Total Players:** {sum(1 for stats in player_stats.values() if stats['event_count'] > 0)}\n"
                    f"**Total Event Points:** {total_events:,}\n"
                    f"**Average per Player:** {total_events / max(sum(1 for stats in player_stats.values() if stats['event_count'] > 0), 1):.2f}"
                )
                stats_embed.add_field(
                    name="Event Summary",
                    value=stats_summary,
                    inline=False
                )

            # Create footer text with timestamp information
            footer_text = ""

            if len(db_files) >= 2:
                latest_time = datetime.fromtimestamp(os.path.getmtime(db_files[0]))
                comparison_time = datetime.fromtimestamp(os.path.getmtime(db_files[1]))
                footer_text += f" Changes from {comparison_time.strftime('%Y-%m-%d %H:%M')} to {latest_time.strftime('%Y-%m-%d %H:%M')}"
            elif len(db_files) == 1:
                latest_time = datetime.fromtimestamp(os.path.getmtime(db_files[0]))
                footer_text += f"Current data as of {latest_time.strftime('%Y-%m-%d %H:%M')}"
            else:
                footer_text += f"No timestamp data available"

            stats_embed.set_footer(text=footer_text)

            # Send the response as followup
            await interaction.followup.send(embed=stats_embed, file=file_attachment)
            
            # Clean up temp file
            try:
                os.unlink(temp_file_path)
            except:
                pass

        except Exception as e:
            error_embed = discord.Embed(
                title="Database Error",
                description=f"An error occurred while fetching statistics: `{str(e)}`",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.followup.send(embed=error_embed, ephemeral=True)
        
        finally:
            conn.close()
    
    print("[OK] Loaded badges command")