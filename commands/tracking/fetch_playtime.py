import discord
from discord import app_commands
import os
import asyncio
import aiohttp
import sqlite3
import shutil
from datetime import datetime, timezone
from pathlib import Path
from utils.permissions import has_roles

# Configuration
OWNER_ID_RAW = os.getenv('OWNER_ID')
REQUIRED_ROLES = [int(OWNER_ID_RAW)] if OWNER_ID_RAW else []

# API Key for playtime tracking
WYNNCRAFT_KEY_11 = os.getenv('WYNNCRAFT_KEY_11')

# Database paths
DB_FOLDER = Path(__file__).resolve().parent.parent.parent / "databases"
PLAYTIME_DB_PATH = DB_FOLDER / "playtime_tracking.db"
PLAYTIME_TRACKING_FOLDER = DB_FOLDER / "playtime_tracking"

# Constants
FETCH_INTERVAL_SECONDS = 300  # 5 minutes
SIZE_LIMIT_BYTES = 20 * 1024 * 1024 * 1024  # 20GB
CLEANUP_INTERVAL_MINUTES = 30  # Keep files every 30 minutes


def init_database():
    """Initialize the playtime tracking database"""
    DB_FOLDER.mkdir(exist_ok=True)
    PLAYTIME_TRACKING_FOLDER.mkdir(exist_ok=True)
    
    conn = sqlite3.connect(PLAYTIME_DB_PATH)
    cursor = conn.cursor()
    
    # Create playtime table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS playtime (
            username TEXT PRIMARY KEY,
            playtime_seconds INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT NOT NULL
        )
    ''')
    
    # Create metadata table to track the current day
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')
    
    conn.commit()
    conn.close()


def get_current_day_string():
    """Get the current day as a string for folder naming"""
    return datetime.now(timezone.utc).strftime("%d-%m-%Y")


def get_stored_day():
    """Get the stored day from metadata"""
    conn = sqlite3.connect(PLAYTIME_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM metadata WHERE key = 'current_day'")
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None


def set_stored_day(day_string):
    """Set the stored day in metadata"""
    conn = sqlite3.connect(PLAYTIME_DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES ('current_day', ?)",
        (day_string,)
    )
    conn.commit()
    conn.close()


def get_last_fetch_timestamp():
    """Get the timestamp of the last fetch"""
    conn = sqlite3.connect(PLAYTIME_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM metadata WHERE key = 'last_fetch_timestamp'")
    result = cursor.fetchone()
    conn.close()
    if result:
        try:
            return datetime.fromisoformat(result[0])
        except:
            return None
    return None


def set_last_fetch_timestamp(timestamp: datetime):
    """Set the timestamp of the last fetch"""
    conn = sqlite3.connect(PLAYTIME_DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES ('last_fetch_timestamp', ?)",
        (timestamp.isoformat(),)
    )
    conn.commit()
    conn.close()


def reset_playtime_database():
    """Reset all playtime values to 0 (called when a new day starts)"""
    conn = sqlite3.connect(PLAYTIME_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE playtime SET playtime_seconds = 0")
    conn.commit()
    conn.close()
    print("[PLAYTIME] Database reset for new day")


def get_day_folder_path(day_string=None):
    """Get the folder path for a specific day's backups"""
    if day_string is None:
        day_string = get_current_day_string()
    return PLAYTIME_TRACKING_FOLDER / f"playtime_{day_string}"


def create_daily_backup():
    """Create a timestamped backup of the current database in the daily folder"""
    day_string = get_current_day_string()
    day_folder = get_day_folder_path(day_string)
    day_folder.mkdir(exist_ok=True)
    
    # Create backup filename with timestamp
    timestamp = datetime.now(timezone.utc).strftime("%H%M%S")
    backup_filename = f"playtime_{day_string}_{timestamp}.db"
    backup_path = day_folder / backup_filename
    
    # Copy the database
    if PLAYTIME_DB_PATH.exists():
        shutil.copy2(PLAYTIME_DB_PATH, backup_path)
        print(f"[PLAYTIME] Created backup: {backup_path}")
    
    return backup_path


def cleanup_daily_folder(day_folder):
    """Keep only files that are ~30 minutes apart (with margin)"""
    if not day_folder.exists():
        return
    
    # Get all .db files in the folder
    db_files = sorted(day_folder.glob("*.db"), key=lambda f: f.stat().st_mtime)
    
    if len(db_files) <= 1:
        return
    
    # Parse timestamps from filenames and group by 30-minute windows
    # Files to keep: the latest one in each 30-minute window
    files_to_keep = set()
    last_kept_time = None
    margin_seconds = 3 * 60  # 3 minute margin
    
    for db_file in db_files:
        file_mtime = db_file.stat().st_mtime
        
        if last_kept_time is None:
            files_to_keep.add(db_file)
            last_kept_time = file_mtime
        else:
            # Check if this file is at least ~30 minutes after the last kept file
            time_diff = file_mtime - last_kept_time
            if time_diff >= (CLEANUP_INTERVAL_MINUTES * 60 - margin_seconds):
                files_to_keep.add(db_file)
                last_kept_time = file_mtime
    
    # Always keep the most recent file
    if db_files:
        files_to_keep.add(db_files[-1])
    
    # Delete files not in the keep set
    deleted_count = 0
    for db_file in db_files:
        if db_file not in files_to_keep:
            try:
                db_file.unlink()
                deleted_count += 1
            except Exception as e:
                print(f"[PLAYTIME] Failed to delete {db_file}: {e}")
    
    if deleted_count > 0:
        print(f"[PLAYTIME] Cleaned up {deleted_count} files from {day_folder.name}")


def cleanup_old_day_folders():
    """Clean folders based on age:
    - 4-6 days old: keep only the latest file
    - 14-17 days old: keep only the latest file
    """
    if not PLAYTIME_TRACKING_FOLDER.exists():
        return
    
    today = datetime.now(timezone.utc).date()
    
    for folder in PLAYTIME_TRACKING_FOLDER.iterdir():
        if not folder.is_dir() or not folder.name.startswith("playtime_"):
            continue
        
        try:
            date_str = folder.name.replace("playtime_", "")
            folder_date = datetime.strptime(date_str, "%d-%m-%Y").date()
            days_old = (today - folder_date).days
            
            # Keep only latest file for folders 4-6 days old OR 14-17 days old
            if 4 <= days_old <= 30:
                db_files = sorted(folder.glob("*.db"), key=lambda f: f.stat().st_mtime)
                
                if len(db_files) <= 1:
                    continue
                
                # Keep only the latest file
                files_to_delete = db_files[:-1]  # All except the last (newest)
                deleted_count = 0
                
                for db_file in files_to_delete:
                    try:
                        db_file.unlink()
                        deleted_count += 1
                    except Exception as e:
                        print(f"[PLAYTIME] Failed to delete {db_file}: {e}")
                
                if deleted_count > 0:
                    print(f"[PLAYTIME] Cleaned {deleted_count} files from {folder.name} ({days_old} days old, kept latest only)")
        
        except ValueError:
            continue


def check_and_cleanup_storage():
    """Check if playtime_tracking folder exceeds 20GB and delete oldest day folders"""
    if not PLAYTIME_TRACKING_FOLDER.exists():
        return
    
    # Calculate total size
    total_size = 0
    for path in PLAYTIME_TRACKING_FOLDER.rglob("*"):
        if path.is_file():
            total_size += path.stat().st_size
    
    if total_size <= SIZE_LIMIT_BYTES:
        return
    
    print(f"[PLAYTIME] Storage exceeds 20GB ({total_size / (1024**3):.2f} GB), cleaning up...")
    
    # Get all day folders sorted by name (oldest first since format is DD-MM-YYYY)
    day_folders = sorted(
        [f for f in PLAYTIME_TRACKING_FOLDER.iterdir() if f.is_dir()],
        key=lambda f: datetime.strptime(f.name.replace("playtime_", ""), "%d-%m-%Y")
    )
    
    # Delete oldest folders until under limit
    for folder in day_folders:
        if total_size <= SIZE_LIMIT_BYTES:
            break
        
        folder_size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
        
        try:
            shutil.rmtree(folder)
            total_size -= folder_size
            print(f"[PLAYTIME] Deleted old folder: {folder.name} ({folder_size / (1024**3):.2f} GB)")
        except Exception as e:
            print(f"[PLAYTIME] Failed to delete {folder}: {e}")


async def fetch_online_players():
    """Fetch the list of online players from Wynncraft API"""
    if not WYNNCRAFT_KEY_11:
        print("[PLAYTIME] WYNNCRAFT_KEY_11 not configured")
        return None
    
    url = "https://api.wynncraft.com/v3/player"
    headers = {'Authorization': f'Bearer {WYNNCRAFT_KEY_11}'}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=30) as response:
                if response.status == 200:
                    data = await response.json()
                    return data
                else:
                    print(f"[PLAYTIME] API returned status {response.status}")
                    return None
    except asyncio.TimeoutError:
        print("[PLAYTIME] API request timed out")
        return None
    except Exception as e:
        print(f"[PLAYTIME] API request failed: {e}")
        return None


def update_playtime_database(online_players: dict, elapsed_seconds: int):
    """Update playtime for online players
    
    Args:
        online_players: Dict of online players from API
        elapsed_seconds: Actual seconds elapsed since last fetch
    """
    if not online_players:
        return 0, 0
    
    conn = sqlite3.connect(PLAYTIME_DB_PATH)
    cursor = conn.cursor()
    
    timestamp = datetime.now(timezone.utc).isoformat()
    updated_count = 0
    new_count = 0
    
    for username in online_players.keys():
        # Check if user exists
        cursor.execute("SELECT playtime_seconds FROM playtime WHERE username = ?", (username,))
        result = cursor.fetchone()
        
        if result:
            # User exists, add elapsed seconds
            cursor.execute(
                "UPDATE playtime SET playtime_seconds = playtime_seconds + ?, last_seen = ? WHERE username = ?",
                (elapsed_seconds, timestamp, username)
            )
            updated_count += 1
        else:
            # New user, add with 0 seconds
            cursor.execute(
                "INSERT INTO playtime (username, playtime_seconds, last_seen) VALUES (?, 0, ?)",
                (username, timestamp)
            )
            new_count += 1
    
    conn.commit()
    conn.close()
    
    return updated_count, new_count


async def fetch_and_update_playtime():
    """Main function to fetch and update playtime data - called by bot.py task loop"""
    try:
        # Initialize database if needed
        init_database()
        
        # Get current timestamp
        now = datetime.now(timezone.utc)
        
        # Check if a new day has started
        current_day = get_current_day_string()
        stored_day = get_stored_day()
        
        if stored_day is None:
            # First run, set the day
            set_stored_day(current_day)
        elif stored_day != current_day:
            # New day started
            print(f"[PLAYTIME] New day detected ({stored_day} -> {current_day})")
            
            # Cleanup the previous day's folder before resetting
            prev_day_folder = get_day_folder_path(stored_day)
            cleanup_daily_folder(prev_day_folder)
            
            # Reset the database for the new day
            reset_playtime_database()
            set_stored_day(current_day)
        
        # Calculate elapsed time since last fetch
        last_fetch = get_last_fetch_timestamp()
        if last_fetch is None:
            # First fetch, use default 5 minutes
            elapsed_seconds = FETCH_INTERVAL_SECONDS
        else:
            elapsed_seconds = int((now - last_fetch).total_seconds())
            # Cap at reasonable maximum (15 minutes) to avoid huge jumps if bot was down
            elapsed_seconds = min(elapsed_seconds, 900)
            # Minimum 60 seconds to avoid tiny increments from rapid fetches
            elapsed_seconds = max(elapsed_seconds, 60)
        
        # Fetch online players
        data = await fetch_online_players()
        
        if data is None:
            return False, 0, 0, 0
        
        # Extract players dict
        players = data.get("players", {})
        total_online = data.get("total", len(players))
        
        if not players:
            print("[PLAYTIME] No players online")
            # Still update the last fetch timestamp
            set_last_fetch_timestamp(now)
            return True, total_online, 0, 0
        
        # Update database with actual elapsed time
        updated_count, new_count = update_playtime_database(players, elapsed_seconds)
        
        # Update last fetch timestamp
        set_last_fetch_timestamp(now)
        
        # Create daily backup
        create_daily_backup()
        
        # Cleanup current day's folder (keep 30-min intervals)
        current_day_folder = get_day_folder_path()
        cleanup_daily_folder(current_day_folder)
        
        # Cleanup old day folders (4-6 days old, keep only latest file)
        cleanup_old_day_folders()
        
        # Check storage limits
        check_and_cleanup_storage()
        
        print(f"[PLAYTIME] Updated {updated_count} players (+{elapsed_seconds}s each), {new_count} new players tracked")
        
        return True, total_online, updated_count, new_count, elapsed_seconds
    
    except Exception as e:
        print(f"[PLAYTIME] Error in fetch_and_update_playtime: {e}")
        import traceback
        traceback.print_exc()
        return False, 0, 0, 0, 0


def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    @bot.tree.command(
        name="fetch_playtime",
        description="Force fetch playtime data from Wynncraft API"
    )
    async def fetch_playtime(interaction: discord.Interaction):
        """Force fetch playtime data immediately"""
        
        # Check permissions
        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000,
                timestamp=datetime.now(timezone.utc)
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=False)
        
        try:
            success, total_players, updated_count, new_count, elapsed_seconds = await fetch_and_update_playtime()
            
            if success:
                embed = discord.Embed(
                    title="✅ Playtime Data Fetched",
                    description="Successfully fetched and updated playtime data.",
                    color=0x00FF00,
                    timestamp=datetime.now(timezone.utc)
                )
                embed.add_field(name="Online Players", value=f"**{total_players:,}**", inline=True)
                embed.add_field(name="Updated", value=f"**{updated_count:,}** (+{elapsed_seconds}s)", inline=True)
                embed.add_field(name="New Players", value=f"**{new_count:,}**", inline=True)
                
                # Add storage info
                if PLAYTIME_TRACKING_FOLDER.exists():
                    total_size = sum(
                        f.stat().st_size for f in PLAYTIME_TRACKING_FOLDER.rglob("*") if f.is_file()
                    )
                    embed.add_field(
                        name="Storage Used",
                        value=f"**{total_size / (1024**3):.2f}** GB / 20 GB",
                        inline=True
                    )
                
                await interaction.followup.send(embed=embed)
            else:
                embed = discord.Embed(
                    title="❌ Fetch Failed",
                    description="Failed to fetch playtime data from Wynncraft API.",
                    color=0xFF0000,
                    timestamp=datetime.now(timezone.utc)
                )
                await interaction.followup.send(embed=embed)
        
        except Exception as e:
            error_embed = discord.Embed(
                title="Error",
                description=f"An unexpected error occurred: {str(e)}",
                color=0xFF0000,
                timestamp=datetime.now(timezone.utc)
            )
            await interaction.followup.send(embed=error_embed)
            print(f"[PLAYTIME] Error in force_fetch_playtime: {e}")
    
    print("[OK] Loaded fetch_playtime command (tracking via standalone tracker)")
