import glob
from datetime import datetime, timedelta, timezone
import discord
from discord import app_commands
import sqlite3
from datetime import datetime
import asyncio
import os
import json
from typing import Optional
from utils.permissions import has_roles
from utils.paths import PROJECT_ROOT, DATA_DIR, DB_DIR

DB_FILE = os.path.join(str(PROJECT_ROOT), "databases", "recruited_data.db")
DB_FOLDER = os.path.join(str(PROJECT_ROOT), "databases")
USERNAME_MATCH_DB_PATH = os.path.join(str(PROJECT_ROOT), "data", "username_matches.json")

owner_id_env = os.getenv('OWNER_ID')
OWNER_ID = int(owner_id_env) if owner_id_env and owner_id_env.isdigit() else 0

REQUIRED_ROLES = (
    OWNER_ID,
    600185623474601995,  # Parliament
)


def init_database():
    """Initialize the event progress database"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS event_progress (
            player TEXT PRIMARY KEY,
            points INTEGER NOT NULL,
            last_updated TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def _load_username_match_db():
    """Load the username match DB from disk."""
    username_match_db_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data/username_matches.json",
    )
    try:
        with open(username_match_db_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        print(f"[WARN] Failed to load username match DB: {e}")
        return {}

def get_databases_in_timeframe(days: int, db_folder: str = "databases"):
    """Get all databases within the timeframe for daily analysis."""
    try:
        # Get all database files sorted by modification time (most recent first)
        # Changed pattern to match your actual database naming: ESI_TIMESTAMP.db
        db_pattern = os.path.join(db_folder, "**", "ESI_*.db")
        db_files = sorted(glob.glob(db_pattern, recursive=True), key=os.path.getmtime, reverse=True)
        
        if not db_files:
            return None, "No database files found"
        
        # Get the most recent database
        latest_db = db_files[0]
        latest_time = datetime.fromtimestamp(os.path.getmtime(latest_db), tz=timezone.utc)
        
        # Calculate target time (X days ago from latest)
        target_time = latest_time - timedelta(days=days)
        
        # Get all databases within the timeframe
        databases_in_range = []
        for db_file in db_files:
            db_time = datetime.fromtimestamp(os.path.getmtime(db_file), tz=timezone.utc)
            if db_time >= target_time:
                databases_in_range.append((db_file, db_time))
        
        if len(databases_in_range) < 2:
            return None, "Not enough historical data for comparison"
        
        # Sort by time (oldest to newest)
        databases_in_range.sort(key=lambda x: x[1])
        
        return databases_in_range, None
    
    except Exception as e:
        return None, f"Error: {str(e)}"
    

def get_player_warcount(db_path: str, username: str) -> Optional[int]:
    """Get player's warcount from a database."""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check if table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='player_stats'")
        if not cursor.fetchone():
            conn.close()
            print(f"Warning: player_stats table not found in {db_path}")
            return None
        
        # Query case-insensitive
        cursor.execute(
            "SELECT wars FROM player_stats WHERE LOWER(username) = LOWER(?)",
            (username,)
        )
        
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return result[0]
        return None
    
    except Exception as e:
        print(f"Error querying database {db_path}: {e}")
        return None


def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    # Initialize database on setup
    init_database()
    
    @bot.tree.command(
        name="weekly_points",
        description="Check weekly points for a player"
    )
    @app_commands.describe(
        player="Discord user of the player.",
        delta="Select amount of days to check"
    )
    async def weekly_points(interaction: discord.Interaction, player: discord.User, delta: Optional[int] = 7):
        """Check weekly points for player"""
        
        await interaction.response.defer()

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
        
        # Get username and UUID from Discord user
        print("Getting player username and UUID...")
        username_db = _load_username_match_db()
        player_data = username_db.get(str(player.id))
        
        if not player_data:
            no_username_embed = discord.Embed(
                title="Username Not Found",
                description=f"No Minecraft username found for {player.mention}. Their discord user ID must be linked to a minecraft username using `/link_user` or `/accept`.",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.followup.send(embed=no_username_embed)
            return
        
        # Extract UUID and username
        print("Getting player UUID and username...")
        player_uuid = player_data.get('uuid') if isinstance(player_data, dict) else None
        player_username = player_data.get('username') if isinstance(player_data, dict) else player_data
        
        if not player_uuid:
            missing_embed = discord.Embed(
                title="UUID Not Found",
                description=f"UUID not found for {player.mention}. Please ensure account is properly linked.",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.followup.send(embed=missing_embed)
            return

        # Define roles whose stats should be excluded/hidden
        EXCLUDED_ROLES = (591765870272053261,  # Duke
                          1396112289832243282, # Grand Duke
                          554514823191199747 ) # Archduke

        def db_operation():
            """Run blocking database operations in thread"""
            try:
                databases, error = get_databases_in_timeframe(delta, DB_FOLDER)
                if error:
                    return {"success": False, "error": error}

                old_db_path = databases[0][0]
                new_db_path = databases[-1][0]
                print(f"Old DB: {old_db_path}, New DB: {new_db_path}")

                old_wars = get_player_warcount(old_db_path, player_username) or 0
                new_wars = get_player_warcount(new_db_path, player_username) or 0
                print(f"Old wars: {old_wars}, New wars: {new_wars}")

                
                calculated_points = max(0, (new_wars - old_wars))
                
                print(f"Calculated points: {calculated_points}")

                
                is_excluded = any(role.id in EXCLUDED_ROLES for role in player.roles) if isinstance(player, discord.Member) else False
                
                print(f"Is excluded: {is_excluded}")
                
                if is_excluded:
                    war_points_to_save = 0  
                else:
                    war_points_to_save = calculated_points

                points_to_save = war_points_to_save 


                # Get event statistics
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                c.execute("""
                    SELECT player, points, last_updated 
                    FROM event_progress 
                    ORDER BY points DESC
                """)
                event_data = c.fetchall()

                return {
                    "success": True,
                    "points": points_to_save,
                    "actual_diff": calculated_points,
                    "is_excluded": is_excluded
                }
            except Exception as e:
                return {"success": False, "error": str(e)}

        # Run database operation in executor to avoid blocking
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, db_operation)

        if not result["success"]:
            error_embed = discord.Embed(
                title="Database Error",
                description=f"An error occurred: `{result['error']}`",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.followup.send(embed=error_embed)
            return
        
        points = result['points']

        result_embed = discord.Embed(
            title=f"Weekly Points {points}",
            description=f"**{player}**\n\n**Points:** {points}\n**Actual Difference:** {result['actual_diff']}\n\n**Excluded:** {result['is_excluded']}",
            color=0x00FF00,
            timestamp=datetime.utcnow()
        )
        await interaction.followup.send(embed=result_embed)
    
    print("[OK] Loaded weekly points command")