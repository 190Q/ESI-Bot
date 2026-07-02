import discord
from discord import app_commands
import sqlite3
from datetime import datetime
import os
import json
from utils.permissions import has_roles
from utils.paths import PROJECT_ROOT, DATA_DIR, DB_DIR
from utils import errors

DB_FILE = os.path.join(str(PROJECT_ROOT), "databases", "recruited_data.db")

USERNAME_MATCH_DB_PATH = os.path.join(str(PROJECT_ROOT), "data", "username_matches.json")

REQUIRED_ROLES = (
    600185623474601995, # Parliament
    954566591520063510, # Jurors
    os.getenv('OWNER_ID') if os.getenv('OWNER_ID') else 0
)

def init_database():
    """Initialize the recruitment database"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS recruited (
            recruiter TEXT NOT NULL,
            recruited TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            UNIQUE(recruiter, recruited)
        )
    """)
    conn.commit()
    conn.close()

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    # Initialize database on setup
    init_database()
    
    @bot.tree.command(
        name="recruitment",
        description="Add or delete a recruitment record"
    )
    @app_commands.describe(
        action="Choose whether to add or delete a recruitment record.",
        recruiter="Discord user of the recruiter.",
        recruited="Discord user of the recruited player."
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="add", value="add"),
            app_commands.Choice(name="delete", value="delete")
        ]
    )
    async def recruit(interaction: discord.Interaction, action: str, recruiter: discord.User, recruited: discord.User):
        """Handle recruitment tracking"""
        

        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            await errors.NO_PERMISSION.send(interaction)
            return

        action = action.lower()
        
        # Look up minecraft usernames from user IDs
        try:
            with open(USERNAME_MATCH_DB_PATH, "r", encoding="utf-8") as f:
                username_db = json.load(f)
        except Exception as e:
            print(f"Error loading username database: {e}")
            username_db = {}

        recruiter_data = username_db.get(str(recruiter.id))
        recruited_data = username_db.get(str(recruited.id))

        if not recruiter_data:
            await errors.USERNAME_NOT_FOUND.send(interaction, user=recruiter.mention)
            return

        if not recruited_data:
            await errors.USERNAME_NOT_FOUND.send(interaction, user=recruited.mention)
            return
        
        # Extract UUID and username
        recruiter_uuid = recruiter_data.get('uuid') if isinstance(recruiter_data, dict) else None
        recruited_uuid = recruited_data.get('uuid') if isinstance(recruited_data, dict) else None
        recruiter_username = recruiter_data.get('username') if isinstance(recruiter_data, dict) else recruiter_data
        recruited_username = recruited_data.get('username') if isinstance(recruited_data, dict) else recruited_data
        
        if not recruiter_uuid or not recruited_uuid:
            await errors.UUID_NOT_FOUND.send(
                interaction, user="one or both of those users"
            )
            return
        
        # Prevent self-recruitment
        if recruiter_uuid == recruited_uuid:
            await errors.INVALID_INPUT.send(
                interaction, reason="A user cannot recruit themselves."
            )
            return
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()

        try:
            if action == "add":
                # Prevent mutual recruitment
                c.execute(
                    "SELECT 1 FROM recruited WHERE recruiter=? AND recruited=?",
                    (recruited_uuid, recruiter_uuid)
                )
                if c.fetchone():
                    await errors.send_custom_error(
                        interaction,
                        "Mutual Recruitment Blocked",
                        f"{recruiter_username} and {recruited_username} cannot recruit each other.",
                        include_support=False,
                    )
                    return

                # Add record
                c.execute(
                    "INSERT OR IGNORE INTO recruited (recruiter, recruited, timestamp) VALUES (?, ?, ?)",
                    (recruiter_uuid, recruited_uuid, datetime.utcnow().isoformat())
                )
                
                if c.rowcount == 0:
                    await errors.send_custom_error(
                        interaction,
                        "Already Recorded",
                        f"{recruiter_username} has already recruited {recruited_username}.",
                        include_support=False,
                    )
                else:
                    conn.commit()
                    success_embed = discord.Embed(
                        title="Recruitment Recorded",
                        description=f"**{recruiter_username}** successfully recruited **{recruited_username}**.",
                        color=0x00FF00,
                        timestamp=datetime.utcnow()
                    )
                    await interaction.response.send_message(embed=success_embed)
            
            elif action == "delete":
                # Delete records by UUID
                c.execute(
                    "DELETE FROM recruited WHERE recruiter=? AND recruited=?",
                    (recruiter_uuid, recruited_uuid)
                )
                
                if c.rowcount == 0:
                    await errors.NOT_FOUND.send(
                        interaction,
                        reason=f"No recruitment record found for {recruiter_username} → {recruited_username}.",
                    )
                else:
                    conn.commit()
                    delete_embed = discord.Embed(
                        title="Recruitment Deleted",
                        description=f"Recruitment record between **{recruiter_username}** and **{recruited_username}** has been removed.",
                        color=0x00FFFF,
                        timestamp=datetime.utcnow()
                    )
                    await interaction.response.send_message(embed=delete_embed)

        except Exception as e:
            print(f"[recruit] Database error: {e}")
            await errors.DATABASE_ERROR.send(interaction)
        
        finally:
            conn.close()
    
    print("[OK] Loaded recruit command")