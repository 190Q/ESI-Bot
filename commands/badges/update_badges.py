import discord
from datetime import datetime
from discord import ButtonStyle, Embed, app_commands
from discord.ui import View, Button
import sqlite3
import asyncio
import os
from io import StringIO
import unicodedata
import re
from pathlib import Path
import json
from utils.permissions import has_roles

GUILD_DB = "databases/guild_stats_data.db"
RECRUITED_DB = "databases/recruited_data.db"
BADGES_CACHE_FILE = "databases/badges_cache.db"
DB_FOLDER = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))) / "databases"

# Path to the username ↔ user_id match database used by /accept and username_match
USERNAME_MATCH_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data/username_matches.json",
)


def load_username_match_db() -> dict:
    """Load the username match DB from disk.

    Returns an empty dict if the file does not exist or is invalid.
    """
    try:
        with open(USERNAME_MATCH_DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        print(f"[WARN] Failed to load username match DB: {e}")
    return {}

REQUIRED_ROLES = (
    600185623474601995, # Parliment
    os.getenv('OWNER_ID') if os.getenv('OWNER_ID') else 0
)

CYRILLIC_TO_LATIN = {
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H",
    "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X", "а": "a",
    "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o",
    "р": "p", "с": "c", "т": "t", "х": "x"
}

FORCED_MATCHES = {
    "nceusnomore": "Magi Nceus von Dalanar",
    "MysticStrider": "Magi Mystic von Dalanar",
    "CelestialFlare": "Viscount Steve4212",
    "_Sxm": "Count _Sxm",
    "__Lan": "Duchess __Lan"
}

IGNORED_PLAYERS = {
    "Alle_Sandstorm"
}

BADGE_ROLES = {
    "War Badges": {
        "10k": 1426633275635404981,
        "6k": 1426633206857465888,
        "3k": 1426633036736368861,
        "1.5k": 1426632920528846880,
        "750": 1426633144093638778,
        "300": 1426632862207049778,
        "100": 1426632780615385098
    },
    "Quest Badges": {
        "350": 1426636141242617906,
        "225": 1426636108321525891,
        "150": 1426636066856898593,
        "90": 1426636018664341675,
        "50": 1426635982614040676,
        "25": 1426635948992761988,
        "10": 1426635880462024937
    },
    "Recruitment Badges": {
        "250": 1426637291706912788,
        "150": 1426637244109946920,
        "80": 1426637209301160039,
        "50": 1426637168071282808,
        "25": 1426637134378303619,
        "10": 1426637094339608586,
        "5": 1426636993630175447
    },
    "Raid Badges": {
        "6k": 1426634664025526405,
        "3.5k": 1426634622791323938,
        "2k": 1426634579644514347,
        "1k": 1426634531284324353,
        "500": 1426634469401432194,
        "100": 1426634408370114773,
        "50": 1426634317970542613
    },
    "Event Badges": {
        "100": 1440682465717915779,
        "75": 1440682471086751815,
        "55": 1440682473641083011,
        "35": 1440682477055115304,
        "20": 1440682480846897232,
        "10": 1440682485548711997,
        "3": 1440682762133569730
    }
}

TIER_NAMES = ["Bronze", "Silver", "Gold", "Platinum", "Diamond", "Onyx", "[nssssame]}"]

BADGE_TYPE_SHORT = {
    "War Badges": "War",
    "Quest Badges": "Quest",
    "Recruitment Badges": "Recruitment",
    "Raid Badges": "Graid",
    "Event Badges": "Event",
}

# Reverse lookup: role_id -> (badge_type, tier_key)
BADGE_ROLE_REVERSE = {}
for _btype, _tiers in BADGE_ROLES.items():
    for _tier, _rid in _tiers.items():
        BADGE_ROLE_REVERSE[_rid] = (_btype, _tier)

# Ordered tier keys lowest -> highest for each badge type
BADGE_TIERS_ORDERED = {}
for _btype, _tiers in BADGE_ROLES.items():
    BADGE_TIERS_ORDERED[_btype] = list(reversed(list(_tiers.keys())))


def get_tier_display(badge_type, tier_key):
    """Get display name like 'Bronze (100)' for a tier."""
    ordered = BADGE_TIERS_ORDERED.get(badge_type, [])
    if tier_key in ordered:
        idx = ordered.index(tier_key)
        if idx < len(TIER_NAMES):
            return f"{TIER_NAMES[idx]} ({tier_key})"
    return tier_key


class BadgeUpdateView(View):
    def __init__(self, interaction: discord.Interaction, updates: list, badge_cache_data: dict = None):
        super().__init__(timeout=None)
        self.interaction = interaction
        self.updates = updates
        self.badge_cache_data = badge_cache_data or {}
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.interaction.user.id:
            await interaction.response.send_message(
                "You cannot use this button — only the command author can.",
                ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=ButtonStyle.green)
    async def confirm(self, button_interaction: discord.Interaction, button: Button):
        if not await self.interaction_check(button_interaction):
            return
        
        await button_interaction.response.defer(thinking=True)

        self.clear_items()
        try:
            await button_interaction.message.edit(view=self)
        except Exception as e:
            print(f"Failed to remove buttons from the view: {e}")

        total_members = len(self.updates)
        processed = 0

        progress_message = await button_interaction.followup.send(
            embed=discord.Embed(
                title="Badge Role Update - In Progress",
                description=f"Processing 0/{total_members} members...",
                color=0x00AAFF
            ),
            ephemeral=False
        )

        for member, roles_to_add, roles_to_remove in self.updates:
            for i in range(0, len(roles_to_remove), 5):
                batch = [r for r in roles_to_remove[i:i+5] if r is not None]
                if batch:
                    try:
                        await member.remove_roles(*batch, reason="Batch badge removal")
                        await asyncio.sleep(1)
                    except Exception as e:
                        print(f"Failed to remove roles from {member}: {e}")
            
            separator_role = self.interaction.guild.get_role(1426272204521341101)
            if separator_role and separator_role not in member.roles:
                roles_to_add.append(separator_role)

            for i in range(0, len(roles_to_add), 5):
                batch = [r for r in roles_to_add[i:i+5] if r is not None]
                if batch:
                    try:
                        await member.add_roles(*batch, reason="Batch badge update")
                        await asyncio.sleep(1)
                    except Exception as e:
                        print(f"Failed to add roles to {member}: {e}")

            processed += 1
            try:
                await progress_message.edit(
                    embed=discord.Embed(
                        title="Badge Role Update - In Progress",
                        description=f"Processing **{processed}/{total_members}** members...\nCurrently: {member.display_name}",
                        color=0x00AAFF
                    )
                )
            except Exception as e:
                print(f"Failed to update progress message: {e}")

        # Build badge change summary
        change_lines = []
        for upd_member, upd_add, upd_remove in self.updates:
            removed_by_type = {}
            added_by_type = {}
            for role in upd_remove:
                if role and role.id in BADGE_ROLE_REVERSE:
                    btype, tier = BADGE_ROLE_REVERSE[role.id]
                    removed_by_type[btype] = tier
            for role in upd_add:
                if role and role.id in BADGE_ROLE_REVERSE:
                    btype, tier = BADGE_ROLE_REVERSE[role.id]
                    added_by_type[btype] = tier

            display = upd_member.nick or upd_member.name
            for btype in sorted(set(removed_by_type) | set(added_by_type)):
                short = BADGE_TYPE_SHORT.get(btype, btype)
                old = removed_by_type.get(btype)
                if old is not None:
                    old = old.replace("[name]", f"{display} {short}") if isinstance(old, str) else old
                new = added_by_type.get(btype)
                if new is not None:
                    new = new.replace("[name]", f"{display} {short}") if isinstance(new, str) else new
                if old and new:
                    change_lines.append(
                        f"**{display}** {short} badge: {get_tier_display(btype, old)} \u2192 {get_tier_display(btype, new)}")
                elif new:
                    change_lines.append(
                        f"**{display}** {short} badge: None \u2192 {get_tier_display(btype, new)}")
                elif old:
                    change_lines.append(
                        f"**{display}** {short} badge: {get_tier_display(btype, old)} \u2192 None")

        try:
            summary = "\n".join(change_lines) if change_lines else "No badge changes."
            summary = f"```\n{summary}\n```"
            if len(summary) > 4000:
                summary = summary[:4000] + "\n```\n...(truncated)"
            await button_interaction.message.edit(
                embed=discord.Embed(
                    title="\U0001F3C5 | Badge Updates",
                    description=summary,
                    color=0x00FF00
                ),
                view=None
            )
        except Exception as e:
            print(f"Failed to edit original command message: {e}")

        try:
            await progress_message.delete()
        except Exception as e:
            print(f"Failed to delete temporary progress message: {e}")
        
        # Update badge cache after successful update
        if self.badge_cache_data:
            try:
                save_badges_cache(self.badge_cache_data)
            except Exception as e:
                print(f"Failed to update badge cache: {e}")

        self.stop()

    @discord.ui.button(label="Cancel", style=ButtonStyle.red)
    async def cancel(self, button_interaction: discord.Interaction, button: Button):
        await button_interaction.response.edit_message(
            embed=discord.Embed(
                title="Badge Role Check Results - CANCELED",
                description="Badge update has been canceled. No roles were changed.",
                color=0xFF0000
            ),
            view=None
        )
        self.stop()

def log_matched_users(matches, filename="matched_users.log"):
    """Log all matched users to a file for debugging"""
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(f"Matched Users Report - {datetime.utcnow()}\n")
        f.write("="*80 + "\n\n")
        
        for db_name, member, data in matches:
            f.write(f"Database Name: {db_name}\n")
            f.write(f"Normalized: {normalize_name(db_name)}\n")
            f.write(f"Discord Member: {member.name} (Nick: {member.nick})\n")
            f.write(f"Member ID: {member.id}\n")
            f.write(f"Data: Wars={data.get('wars', 0)}, Quests={data.get('quests', 0)}, "
                   f"Recruits={data.get('recruits', 0)}, Graids={data.get('graids', 0)}\n")
            f.write("-"*80 + "\n")

def normalize_name(name: str) -> str:
    """Normalize names for comparison by removing special characters and standardizing format"""
    name = unicodedata.normalize("NFKC", name.strip().lower())
    
    for cyr, lat in CYRILLIC_TO_LATIN.items():
        name = name.replace(cyr.lower(), lat.lower())
    
    # Remove all common separators
    name = name.replace(" ", "").replace("_", "")
    name = name.replace("-", "").replace(".", "")
    
    return name

def create_normalized_lookup(data_dict):
    """
    Create a lookup dictionary that maps normalized names to their original keys and values.
    This handles cases where the same player has different name formats across databases.
    
    Returns: dict mapping normalized_name -> (original_key, value)
    """
    lookup = {}
    duplicates_found = {}
    
    for key, value in data_dict.items():
        normalized = normalize_name(key)
        
        # Track if we're overwriting an existing entry
        if normalized in lookup:
            if normalized not in duplicates_found:
                duplicates_found[normalized] = [lookup[normalized]]
            duplicates_found[normalized].append((key, value))
        
        # If multiple keys normalize to the same name, keep the one with highest value
        if normalized not in lookup or value > lookup[normalized][1]:
            lookup[normalized] = (key, value)
    
    # Log any duplicates found for debugging
    if duplicates_found:
        print(f"\n[WARNING] Found {len(duplicates_found)} normalized names with multiple database entries:")
        for norm_name, entries in duplicates_found.items():
            print(f"  '{norm_name}' has {len(entries)} entries:")
            for orig_key, val in entries:
                print(f"    - '{orig_key}' = {val}")
            print(f"    -> Using '{lookup[norm_name][0]}' with value {lookup[norm_name][1]}")
    
    return lookup

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

def get_latest_databases(db_folder=None, guild_prefix="ESI"):
    """Get the most recent database file for a guild."""
    import glob
    
    if db_folder is None:
        db_folder = str(DB_FOLDER)
    
    # Look in the new api_tracking folder structure
    api_tracking_folder = os.path.join(db_folder, "api_tracking")
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
    
    return db_files[:1] if len(db_files) >= 1 else []

def load_guild_data():
    """Load war data from the latest timestamped database (current guild members only)."""
    db_files = get_latest_databases()
    
    if not db_files:
        print("[UPDATE_BADGES] No databases found in databases/ folder")
        return {}
    
    db_path = db_files[0]
    print(f"[UPDATE_BADGES] Loading guild data from: {db_path}")
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT username, wars
            FROM player_stats
        """)
        
        data = {}
        for row in cursor.fetchall():
            username, wars = row
            data[username] = wars or 0
        
        conn.close()
        print(f"[UPDATE_BADGES] Loaded data for {len(data)} current guild members")
        return data
    except Exception as e:
        print(f"[UPDATE_BADGES] Error loading guild data from {db_path}: {e}")
        return {}

def load_quest_data():
    if not os.path.exists(RECRUITED_DB):
        return {}
    conn = sqlite3.connect(RECRUITED_DB)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT player, points FROM quest_progress")
        data = {r[0]: r[1] for r in cursor.fetchall()}
    except sqlite3.OperationalError:
        data = {}
    conn.close()
    return data

def load_recruit_data():
    if not os.path.exists(RECRUITED_DB):
        return {}
    conn = sqlite3.connect(RECRUITED_DB)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT recruiter, COUNT(recruited) FROM recruited GROUP BY recruiter")
        data = {r[0]: r[1] for r in cursor.fetchall()}
    except sqlite3.OperationalError:
        data = {}
    conn.close()
    return data

def load_graid_data():
    """Load guild raid data from the latest api_tracking database's guild_raid_stats table."""
    graid_counts = {}
    
    api_tracking_folder = DB_FOLDER / "api_tracking"
    if not api_tracking_folder.exists():
        print(f"[UPDATE_BADGES] api_tracking folder not found at {api_tracking_folder}")
        return graid_counts
    
    try:
        import glob
        db_files = []
        for day_folder in api_tracking_folder.iterdir():
            if day_folder.is_dir() and day_folder.name.startswith("api_"):
                for db_file in day_folder.glob("ESI_*.db"):
                    db_files.append(db_file)
        
        if not db_files:
            print("[UPDATE_BADGES] No api_tracking database files found")
            return graid_counts
        
        db_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        latest_db = db_files[0]
        
        conn = sqlite3.connect(str(latest_db))
        cursor = conn.cursor()
        
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='guild_raid_stats'")
        if cursor.fetchone():
            cursor.execute("SELECT username, total_graids FROM guild_raid_stats WHERE username IS NOT NULL")
            for row in cursor.fetchall():
                graid_counts[row[0]] = row[1]
            print(f"[UPDATE_BADGES] Loaded graid counts for {len(graid_counts)} players from {latest_db}")
        else:
            print(f"[UPDATE_BADGES] guild_raid_stats table not found in {latest_db}")
        
        conn.close()
    except Exception as e:
        print(f"[UPDATE_BADGES] Error loading graid counts from api_tracking: {e}")
    
    return graid_counts

def load_event_data():
    """Load event points data from recruited database"""
    if not os.path.exists(RECRUITED_DB):
        return {}
    conn = sqlite3.connect(RECRUITED_DB)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT player, points FROM event_progress")
        data = {r[0]: r[1] for r in cursor.fetchall()}
    except sqlite3.OperationalError:
        data = {}
    conn.close()
    return data

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

def get_war_badge(wars):
    if wars >= 10000: return "10k"
    if wars >= 6000: return "6k"
    if wars >= 3000: return "3k"
    if wars >= 1500: return "1.5k"
    if wars >= 750: return "750"
    if wars >= 300: return "300"
    if wars >= 100: return "100"
    return None

def get_quest_badge(points):
    if points >= 350: return "350"
    if points >= 225: return "225"
    if points >= 150: return "150"
    if points >= 90: return "90"
    if points >= 50: return "50"
    if points >= 25: return "25"
    if points >= 10: return "10"
    return None

def get_recruit_badge(count):
    if count >= 250: return "250"
    if count >= 150: return "150"
    if count >= 80: return "80"
    if count >= 50: return "50"
    if count >= 25: return "25"
    if count >= 10: return "10"
    if count >= 5: return "5"
    return None

def get_graid_badge(count):
    """Determine which graid badge a player should have"""
    if count >= 6000: return "6k"
    if count >= 3500: return "3.5k"
    if count >= 2000: return "2k"
    if count >= 1000: return "1k"
    if count >= 500: return "500"
    if count >= 100: return "100"
    if count >= 50: return "50"
    return None

def get_event_badge(points):
    """Determine which event badge a player should have"""
    if points >= 100: return "100"
    if points >= 75: return "75"
    if points >= 55: return "55"
    if points >= 35: return "35"
    if points >= 20: return "20"
    if points >= 10: return "10"
    if points >= 3: return "3"
    return None

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
        print(f"[UPDATE_BADGES] Badge cache updated for {len(cache_data)} players")
    except (sqlite3.Error, Exception) as e:
        print(f"Error saving badges cache: {e}")

def setup(bot, has_required_role, config):

    @bot.tree.command(
        name="update_badges",
        description="Check all guild members for missing or wrong badge roles."
    )
    @app_commands.describe(username="Optional username to check.")
    async def check_badges(interaction: discord.Interaction, username: str = None):
        
        try:
            await interaction.response.defer()
        except discord.errors.NotFound:
            print(f"[WARNING] Interaction expired before defer for user {interaction.user}")
            return
        
        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.followup.send(embed=missing_roles_embed, ephemeral=True)
            return

        guild_members = interaction.guild.members
        
        # Load username_matches.json to get UUID -> Discord Member mapping
        username_match_db = load_username_match_db()
        uuid_to_discord_member = {}  # UUID -> Discord Member mapping
        
        for member in guild_members:
            if member.bot:
                continue
            data = username_match_db.get(str(member.id))
            if not data:
                continue
            
            # Handle both string format and dict format with username and uuid
            if isinstance(data, dict):
                uuid = data.get('uuid')
                if uuid:
                    uuid_to_discord_member[uuid] = member
        
        print(f"[UPDATE_BADGES] Built UUID->Discord mapping for {len(uuid_to_discord_member)} members")
        
        # === EXACT SAME LOGIC AS /badges COMMAND ===
        # Load UUID to username mapping from latest database
        db_files = get_latest_databases()
        if not db_files:
            await interaction.followup.send("No database files found. Run /fetch_api first.")
            return
        
        uuid_map = load_uuid_to_username_map(db_files[0])
        print(f"[UPDATE_BADGES] Loaded {len(uuid_map)} UUID->username mappings")
        
        # Load quest data from recruited_data.db (players are UUIDs)
        conn = sqlite3.connect(RECRUITED_DB)
        c = conn.cursor()
        try:
            c.execute("SELECT player, points, badge FROM quest_progress ORDER BY points DESC")
            quest_data_raw = c.fetchall()
        except:
            quest_data_raw = []
        
        # Load recruitment data from recruited_data.db (recruiters are UUIDs)
        try:
            c.execute("SELECT recruiter, COUNT(*) as recruitment_count FROM recruited GROUP BY recruiter ORDER BY recruitment_count DESC")
            recruitment_data_raw = c.fetchall()
        except:
            recruitment_data_raw = []
        
        # Load event data from recruited_data.db (players are UUIDs)
        try:
            c.execute("SELECT player, points, badge FROM event_progress ORDER BY points DESC")
            event_data_raw = c.fetchall()
        except:
            event_data_raw = []
        conn.close()
        
        # Load war data from latest database (usernames, not UUIDs)
        war_data = load_war_data_from_database(db_files[0])
        
        # Load graid data from latest api_tracking database
        graid_counts_raw = {}
        try:
            api_tracking_folder = DB_FOLDER / "api_tracking"
            if api_tracking_folder.exists():
                graid_db_files = []
                for day_folder in api_tracking_folder.iterdir():
                    if day_folder.is_dir() and day_folder.name.startswith("api_"):
                        for db_file in day_folder.glob("ESI_*.db"):
                            graid_db_files.append(db_file)
                
                if graid_db_files:
                    graid_db_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
                    latest_graid_db = graid_db_files[0]
                    
                    conn_graid = sqlite3.connect(str(latest_graid_db))
                    cursor_graid = conn_graid.cursor()
                    
                    cursor_graid.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='guild_raid_stats'")
                    if cursor_graid.fetchone():
                        cursor_graid.execute("SELECT username, total_graids FROM guild_raid_stats WHERE username IS NOT NULL")
                        for row in cursor_graid.fetchall():
                            graid_counts_raw[row[0]] = row[1]
                        print(f"[UPDATE_BADGES] Loaded {len(graid_counts_raw)} graid entries from api_tracking")
                    else:
                        print(f"[UPDATE_BADGES] guild_raid_stats table not found in {latest_graid_db}")
                    
                    conn_graid.close()
                else:
                    print("[UPDATE_BADGES] No api_tracking database files found for graid")
            else:
                print(f"[UPDATE_BADGES] api_tracking folder not found at {api_tracking_folder}")
        except Exception as e:
            print(f"[UPDATE_BADGES] Error loading graids: {e}")
        
        # Build player_stats dictionary with UUIDs as keys (SAME AS /badges)
        player_stats = {}  # UUID -> stats
        
        # Helper function from /badges
        def _normalize_name(name: str) -> str:
            if not isinstance(name, str):
                return ""
            name = unicodedata.normalize("NFKC", name.strip().lower())
            for ch in [" ", "_", "-", "."]:
                name = name.replace(ch, "")
            return name
        
        # Build member_name_map from war_data (current guild members)
        member_name_map = {}
        if war_data:
            for username in war_data.keys():
                member_name_map[_normalize_name(username)] = username
        
        # Add quest data (player is UUID)
        for player_uuid, points, badge in quest_data_raw:
            if player_uuid not in player_stats:
                player_stats[player_uuid] = {
                    'quest_count': 0, 'recruited_count': 0, 'war_count': 0,
                    'graid_count': 0, 'event_count': 0
                }
            player_stats[player_uuid]['quest_count'] = points
        
        # Add recruitment data (recruiter is UUID) - with legacy name merging
        for recruiter_uuid, count in recruitment_data_raw:
            username = resolve_to_username(recruiter_uuid, uuid_map)
            key_uuid = recruiter_uuid
            norm = _normalize_name(username)
            if norm in member_name_map:
                # Find UUID for the canonical member name
                canonical_name = member_name_map[norm]
                for uuid, uname in uuid_map.items():
                    if uname == canonical_name:
                        key_uuid = uuid
                        break
            
            if key_uuid not in player_stats:
                player_stats[key_uuid] = {
                    'quest_count': 0, 'recruited_count': 0, 'war_count': 0,
                    'graid_count': 0, 'event_count': 0
                }
            player_stats[key_uuid]['recruited_count'] = player_stats[key_uuid].get('recruited_count', 0) + count
        
        # Add event data (player is UUID)
        for player_uuid, points, badge in event_data_raw:
            if player_uuid not in player_stats:
                player_stats[player_uuid] = {
                    'quest_count': 0, 'recruited_count': 0, 'war_count': 0,
                    'graid_count': 0, 'event_count': 0
                }
            player_stats[player_uuid]['event_count'] = points
        
        # Add war data (username from API, need to find UUID)
        for username, data in war_data.items():
            wars = data.get('wars', 0)
            # Find UUID for this username
            player_uuid = None
            for uuid, uname in uuid_map.items():
                if uname == username:
                    player_uuid = uuid
                    break
            
            if player_uuid:
                if player_uuid not in player_stats:
                    player_stats[player_uuid] = {
                        'quest_count': 0, 'recruited_count': 0, 'war_count': 0,
                        'graid_count': 0, 'event_count': 0
                    }
                player_stats[player_uuid]['war_count'] = wars
        
        # Add graid data (username from guild_raid_stats, need to find UUID)
        for username, count in graid_counts_raw.items():
            player_uuid = None
            for uuid, uname in uuid_map.items():
                if uname == username:
                    player_uuid = uuid
                    break
            
            if player_uuid:
                if player_uuid not in player_stats:
                    player_stats[player_uuid] = {
                        'quest_count': 0, 'recruited_count': 0, 'war_count': 0,
                        'graid_count': 0, 'event_count': 0
                    }
                player_stats[player_uuid]['graid_count'] = count
        
        print(f"[UPDATE_BADGES] Built player_stats for {len(player_stats)} UUIDs")
        
        # Filter to only current guild members (those in war_data)
        # Build set of current member UUIDs
        current_member_uuids = set()
        for username in war_data.keys():
            for uuid, uname in uuid_map.items():
                if uname == username:
                    current_member_uuids.add(uuid)
                    break
        
        # Filter player_stats to only current members
        player_stats = {uuid: stats for uuid, stats in player_stats.items() if uuid in current_member_uuids}
        print(f"[UPDATE_BADGES] Filtered to {len(player_stats)} current guild members")
        
        # Now iterate through player_stats (UUID-based) and match to Discord members
        results, updates, rows = [], [], []
        badge_changes = 0
        matched_count = 0
        unmatched_count = 0
        
        for player_uuid, stats in player_stats.items():
            # Get Discord member for this UUID
            member = uuid_to_discord_member.get(player_uuid)
            if not member:
                unmatched_count += 1
                continue
            
            matched_count += 1
            username = uuid_map.get(player_uuid, player_uuid)
            
            # Check if player is ignored
            if username in IGNORED_PLAYERS or normalize_name(username) in {normalize_name(p) for p in IGNORED_PLAYERS}:
                print(f"[IGNORED] Skipping {username}")
                continue
            
            # Calculate expected badges
            correct_war = get_war_badge(stats['war_count'])
            correct_quest = get_quest_badge(stats['quest_count'])
            correct_recruit = get_recruit_badge(stats['recruited_count'])
            correct_graid = get_graid_badge(stats['graid_count'])
            correct_event = get_event_badge(stats['event_count'])
            
            # Get member's current roles
            role_ids = [r.id for r in member.roles]
            
            # Get expected role IDs
            expected_war = BADGE_ROLES["War Badges"].get(correct_war)
            expected_quest = BADGE_ROLES["Quest Badges"].get(correct_quest)
            expected_recruit = BADGE_ROLES["Recruitment Badges"].get(correct_recruit)
            expected_graid = BADGE_ROLES["Raid Badges"].get(correct_graid)
            expected_event = BADGE_ROLES["Event Badges"].get(correct_event)
            
            # Get current badges
            current_war_badges = [tier for tier, rid in BADGE_ROLES["War Badges"].items() if rid in role_ids]
            current_quest_badges = [tier for tier, rid in BADGE_ROLES["Quest Badges"].items() if rid in role_ids]
            current_recruit_badges = [tier for tier, rid in BADGE_ROLES["Recruitment Badges"].items() if rid in role_ids]
            current_graid_badges = [tier for tier, rid in BADGE_ROLES["Raid Badges"].items() if rid in role_ids]
            current_event_badges = [tier for tier, rid in BADGE_ROLES["Event Badges"].items() if rid in role_ids]
            
            # Build roles to add/remove
            roles_to_add = []
            roles_to_remove = []
            
            # War badges
            if correct_war:
                if expected_war not in role_ids:
                    if expected_war:
                        roles_to_add.append(interaction.guild.get_role(expected_war))
                roles_to_remove += [interaction.guild.get_role(BADGE_ROLES["War Badges"][tier]) 
                                    for tier in current_war_badges if tier != correct_war]
            elif len(current_war_badges) > 0:
                roles_to_remove += [interaction.guild.get_role(BADGE_ROLES["War Badges"][tier]) for tier in current_war_badges]
            
            # Quest badges
            if correct_quest:
                if expected_quest not in role_ids:
                    if expected_quest:
                        roles_to_add.append(interaction.guild.get_role(expected_quest))
                roles_to_remove += [interaction.guild.get_role(BADGE_ROLES["Quest Badges"][tier]) 
                                    for tier in current_quest_badges if tier != correct_quest]
            elif len(current_quest_badges) > 0:
                roles_to_remove += [interaction.guild.get_role(BADGE_ROLES["Quest Badges"][tier]) for tier in current_quest_badges]
            
            # Recruitment badges
            if correct_recruit:
                if expected_recruit not in role_ids:
                    if expected_recruit:
                        roles_to_add.append(interaction.guild.get_role(expected_recruit))
                roles_to_remove += [interaction.guild.get_role(BADGE_ROLES["Recruitment Badges"][tier]) 
                                    for tier in current_recruit_badges if tier != correct_recruit]
            elif len(current_recruit_badges) > 0:
                roles_to_remove += [interaction.guild.get_role(BADGE_ROLES["Recruitment Badges"][tier]) for tier in current_recruit_badges]
            
            # Graid badges
            if correct_graid:
                if expected_graid and expected_graid not in role_ids:
                    roles_to_add.append(interaction.guild.get_role(expected_graid))
                roles_to_remove += [interaction.guild.get_role(BADGE_ROLES["Raid Badges"][tier]) 
                                    for tier in current_graid_badges if tier != correct_graid]
            elif len(current_graid_badges) > 0:
                roles_to_remove += [interaction.guild.get_role(BADGE_ROLES["Raid Badges"][tier]) 
                                   for tier in current_graid_badges]
            
            # Event badges
            if correct_event:
                if expected_event and expected_event not in role_ids:
                    roles_to_add.append(interaction.guild.get_role(expected_event))
                roles_to_remove += [interaction.guild.get_role(BADGE_ROLES["Event Badges"][tier]) 
                                    for tier in current_event_badges if tier != correct_event]
            elif len(current_event_badges) > 0:
                roles_to_remove += [interaction.guild.get_role(BADGE_ROLES["Event Badges"][tier]) 
                                   for tier in current_event_badges]
            
            roles_to_add = [r for r in roles_to_add if r is not None]
            roles_to_remove = [r for r in roles_to_remove if r is not None]
            
            if roles_to_add or roles_to_remove:
                updates.append((member, roles_to_add, roles_to_remove))
            
            # Build status strings
            war_status = "-"
            quest_status = "-"
            recruit_status = "-"
            graid_status = "-"
            event_status = "-"
            
            # Helper function from earlier
            def is_upgrade(current_badge, new_badge, badge_tiers):
                if not current_badge or not new_badge:
                    return False
                try:
                    tier_order = list(badge_tiers.keys())
                    current_idx = tier_order.index(current_badge)
                    new_idx = tier_order.index(new_badge)
                    return new_idx < current_idx
                except (ValueError, KeyError):
                    return False
            
            if correct_war:
                if expected_war not in role_ids:
                    if current_war_badges:
                        if len(current_war_badges) == 1 and is_upgrade(current_war_badges[0], correct_war, BADGE_ROLES["War Badges"]):
                            war_status = f"Upgrade ({current_war_badges[0]} → {correct_war})"
                        else:
                            war_status = f"Wrong ({', '.join(current_war_badges)} → {correct_war})"
                    else:
                        war_status = f"Missing ({correct_war})"
                elif len(current_war_badges) > 1:
                    extra = [t for t in current_war_badges if t != correct_war]
                    if extra:
                        war_status = f"Extra ({', '.join(extra)})"
            elif len(current_war_badges) > 0:
                war_status = f"Remove ({', '.join(current_war_badges)})"
            
            # Similar for quest
            if correct_quest:
                if expected_quest not in role_ids:
                    if current_quest_badges:
                        if len(current_quest_badges) == 1 and is_upgrade(current_quest_badges[0], correct_quest, BADGE_ROLES["Quest Badges"]):
                            quest_status = f"Upgrade ({current_quest_badges[0]} → {correct_quest})"
                        else:
                            quest_status = f"Wrong ({', '.join(current_quest_badges)} → {correct_quest})"
                    else:
                        quest_status = f"Missing ({correct_quest})"
                elif len(current_quest_badges) > 1:
                    extra = [t for t in current_quest_badges if t != correct_quest]
                    if extra:
                        quest_status = f"Extra ({', '.join(extra)})"
            elif len(current_quest_badges) > 0:
                quest_status = f"Remove ({', '.join(current_quest_badges)})"
            
            # Similar for recruit
            if correct_recruit:
                if expected_recruit not in role_ids:
                    if current_recruit_badges:
                        if len(current_recruit_badges) == 1 and is_upgrade(current_recruit_badges[0], correct_recruit, BADGE_ROLES["Recruitment Badges"]):
                            recruit_status = f"Upgrade ({current_recruit_badges[0]} → {correct_recruit})"
                        else:
                            recruit_status = f"Wrong ({', '.join(current_recruit_badges)} → {correct_recruit})"
                    else:
                        recruit_status = f"Missing ({correct_recruit})"
                elif len(current_recruit_badges) > 1:
                    extra = [t for t in current_recruit_badges if t != correct_recruit]
                    if extra:
                        recruit_status = f"Extra ({', '.join(extra)})"
            elif len(current_recruit_badges) > 0:
                recruit_status = f"Remove ({', '.join(current_recruit_badges)})"
            
            # Similar for graid
            if correct_graid:
                if expected_graid not in role_ids:
                    if current_graid_badges:
                        if len(current_graid_badges) == 1 and is_upgrade(current_graid_badges[0], correct_graid, BADGE_ROLES["Raid Badges"]):
                            graid_status = f"Upgrade ({current_graid_badges[0]} → {correct_graid})"
                        else:
                            graid_status = f"Wrong ({', '.join(current_graid_badges)} → {correct_graid})"
                    else:
                        graid_status = f"Missing ({correct_graid})"
                elif len(current_graid_badges) > 1:
                    extra = [t for t in current_graid_badges if t != correct_graid]
                    if extra:
                        graid_status = f"Extra ({', '.join(extra)})"
            elif len(current_graid_badges) > 0:
                graid_status = f"Remove ({', '.join(current_graid_badges)})"
            
            # Similar for event
            if correct_event:
                if expected_event not in role_ids:
                    if current_event_badges:
                        if len(current_event_badges) == 1 and is_upgrade(current_event_badges[0], correct_event, BADGE_ROLES["Event Badges"]):
                            event_status = f"Upgrade ({current_event_badges[0]} → {correct_event})"
                        else:
                            event_status = f"Wrong ({', '.join(current_event_badges)} → {correct_event})"
                    else:
                        event_status = f"Missing ({correct_event})"
                elif len(current_event_badges) > 1:
                    extra = [t for t in current_event_badges if t != correct_event]
                    if extra:
                        event_status = f"Extra ({', '.join(extra)})"
            elif len(current_event_badges) > 0:
                event_status = f"Remove ({', '.join(current_event_badges)})"
            
            display_name = member.nick or member.name
            
            if war_status != "-" or quest_status != "-" or recruit_status != "-" or graid_status != "-" or event_status != "-":
                badge_changes += 1
                rows.append((display_name, war_status, quest_status, recruit_status, graid_status, event_status))
        
        print(f"[UPDATE_BADGES] Matched {matched_count} players to Discord members, {unmatched_count} unmatched")
        print(f"[UPDATE_BADGES] Found {badge_changes} badge changes")
        
        # Format rows for output
        name_width = max((len(r[0]) for r in rows), default=10)
        war_width = max((len(r[1]) for r in rows), default=5)
        quest_width = max((len(r[2]) for r in rows), default=5)
        recruit_width = max((len(r[3]) for r in rows), default=5)
        graid_width = max((len(r[4]) for r in rows), default=5)
        event_width = max((len(r[5]) for r in rows), default=5)
        
        badge_cache_data = {}  # We don't need to rebuild cache here since /badges handles it
        
        results = []
        for display_name_clean, war_status, quest_status, recruit_status, graid_status, event_status in rows:
            results.append(
                f"{display_name_clean:<{name_width + 1}}: "
                f"War: {war_status:<{war_width}} | "
                f"Quest: {quest_status:<{quest_width}} | "
                f"Recruit: {recruit_status:<{recruit_width}} | "
                f"Raid: {graid_status:<{graid_width}} | "
                f"Event: {event_status:<{event_width}}"
            )
        
        if not results:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Badge Role Check Results",
                    description=f"Checked **{matched_count}** member{'' if matched_count == 1 else 's'}. All badge roles are correct!",
                    color=0x00FF00
                )
            )
            return
        
        report = "\n".join(results)
        f = StringIO()
        f.write(report)
        f.seek(0)
        discord_file = discord.File(f, filename="missing_or_wrong_badges.txt")
        
        view = BadgeUpdateView(interaction, updates, badge_cache_data) if updates else None

        await interaction.followup.send(
            embed=discord.Embed(
                title="Badge Role Check Results",
                description=f"Checked **{matched_count}** member{'' if matched_count == 1 else 's'}, and found **{badge_changes}** change{'' if badge_changes == 1 else 's'}.\n**Do you want to update badges?**",
                color=0x00AAFF
            ),
            file=discord_file,
            view=view
        )
    
    print("[OK] Loaded check_badges command")
