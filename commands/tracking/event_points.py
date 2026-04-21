# events.py - Complete new file
import discord
from discord import app_commands
import sqlite3
from datetime import datetime
import asyncio
import os
import json
from utils.permissions import has_roles
from utils.paths import PROJECT_ROOT, DATA_DIR, DB_DIR
import utils.esi_points as esi

DB_FILE = os.path.join(str(PROJECT_ROOT), "databases", "recruited_data.db")

USERNAME_MATCH_DB_PATH = os.path.join(str(PROJECT_ROOT), "data", "username_matches.json")

REQUIRED_ROLES = (
    os.getenv('OWNER_ID') if os.getenv('OWNER_ID') else 0,
    600185623474601995, # Parliament
    683448131148447929, # Sindrian Pride
)

# Badge thresholds (event points : badge name)
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

def get_badge_for_points(points: int) -> str:
    """Return the badge name corresponding to event points."""
    for threshold, badge in EVENT_BADGE_TIERS:
        if points >= threshold:
            return badge
    return "No badge"

def init_database():
    """Initialize the event progress database"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS event_progress (
            player TEXT PRIMARY KEY,
            points INTEGER NOT NULL,
            badge TEXT NOT NULL,
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

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    # Initialize database on setup
    init_database()
    
    @bot.tree.command(
        name="event_points",
        description="Add or remove event points for a player"
    )
    @app_commands.describe(
        player="Discord user of the player.",
        points="Number of event points to modify (positive to add, negative to remove, cannot be 0).",
        esi_points="ESI points to award alongside events points.",
        reason="Optional reason for the event points change.",
    )
    async def event(interaction: discord.Interaction, player: discord.User, points: int, esi_points: int, reason: str = ""):
        """Manage event points and badges for players"""
        
        await interaction.response.defer()

        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            print(f"User {interaction.user} does not have required roles to use the /event command.")
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.followup.send(embed=missing_roles_embed)
            return
        
        if esi_points <= 0:
            invalid_points = discord.Embed(
                title="Invalid Input",
                description="ESI points has to be 1 or higher.",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.followup.send(embed=invalid_points)
            return

        if points == 0:
            invalid_points = discord.Embed(
                title="Invalid Input",
                description="Event points cannot be 0. Use positive or negative values.",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.followup.send(embed=invalid_points)
            return
        
        # Get username and UUID from Discord user
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

        def db_operation():
            """Run blocking database operations in thread"""
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            try:
                # Retrieve current data
                c.execute("SELECT points, badge FROM event_progress WHERE player=?", (player_uuid,))
                row = c.fetchone()

                current_points = row[0] if row else 0
                current_badge = row[1] if row else "No badge"

                # Calculate new points
                if points > 0:
                    new_points = current_points + points
                else:
                    new_points = max(current_points + points, 0)

                new_badge = get_badge_for_points(new_points)
                badge_changed = new_badge != current_badge

                # Save new progress
                c.execute("""
                    INSERT INTO event_progress (player, points, badge, last_updated)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(player) DO UPDATE SET
                        points=excluded.points,
                        badge=excluded.badge,
                        last_updated=excluded.last_updated
                """, (player_uuid, new_points, new_badge, datetime.utcnow().isoformat()))
                conn.commit()
                
                return {
                    "success": True,
                    "current_points": current_points,
                    "new_points": new_points,
                    "current_badge": current_badge,
                    "new_badge": new_badge,
                    "badge_changed": badge_changed
                }
            except Exception as e:
                return {"success": False, "error": str(e)}
            finally:
                conn.close()

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

        # Build embed response
        embed_color = 0x00FF00 if points > 0 else 0x00FFFF
        description = f"**{player.mention}** (`{player_username}`) now has **{result['new_points']}** event points."

        if result["badge_changed"] and result["new_points"] > result["current_points"]:
            description += f"\n\n**Badge Upgraded!** Congratulations on earning the **{result['new_badge']}**!"
        elif result["badge_changed"] and result["new_points"] < result["current_points"]:
            description += f"\n\n**Badge Lost:** {player_username} has been downgraded to **{result['new_badge']}**."
        
        if reason:
            description += f"\n\n**Reason:** {reason}"
        if esi_points:
            description += f"\n\nAdded **{abs(esi_points)}** ESI Points"

        result_embed = discord.Embed(
            title=f"{abs(points)} Event Points {'Added' if points > 0 else 'Removed'}",
            description=description,
            color=embed_color,
            timestamp=datetime.utcnow()
        )
        await interaction.followup.send(embed=result_embed)
        
        resolved = [{
            "uuid": player_uuid,
            "username": player_username
        }]
        esi.save_points(resolved, esi_points, f"Event: {reason}" or "Event points command")
    
    print("[OK] Loaded event command")