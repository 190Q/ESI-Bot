import discord
from discord import app_commands
import os
import json
from datetime import datetime
import aiohttp
from utils.permissions import has_roles

# Path to the username ↔ user_id match database
USERNAME_MATCH_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data/username_matches.json",
)

REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0,
    954566591520063510, # Juror
    600185623474601995, # Parliament
]

def _load_username_match_db():
    """Load the username match DB from disk."""
    try:
        with open(USERNAME_MATCH_DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}
    except Exception as e:
        print(f"[WARN] Failed to load username match DB: {e}")
        return {}

def save_username_match(user_id: int, username: str, uuid: str = None) -> None:
    """Persist a mapping of Discord user ID → in‑game username and UUID to the JSON DB."""
    db = _load_username_match_db()
    # Save as dict with username and uuid if uuid is provided, otherwise just username for backwards compatibility
    if uuid:
        db[str(user_id)] = {'username': username, 'uuid': uuid}
    else:
        db[str(user_id)] = username
    try:
        with open(USERNAME_MATCH_DB_PATH, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] Failed to save username match for {user_id}: {e}")

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    @bot.tree.command(
        name="link_user",
        description="Link a Discord user to their Minecraft IGN"
    )
    @app_commands.describe(
        user="The Discord user to link",
        username="The Minecraft username (IGN)"
    )
    async def link_user(
        interaction: discord.Interaction,
        user: discord.Member,
        username: str
    ):
        """Link a Discord user to their Minecraft IGN"""

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
        
        # Check if user already has a linked username
        db = _load_username_match_db()
        existing_entry = db.get(str(user.id))
        existing_username = None
        if isinstance(existing_entry, dict):
            existing_username = existing_entry.get('username')
        elif isinstance(existing_entry, str):
            existing_username = existing_entry
        
        # Fetch UUID from Wynncraft API
        uuid = None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://api.wynncraft.com/v3/player/{username}") as response:
                    if response.status == 200:
                        data = await response.json()
                        uuid = data.get('uuid')
        except Exception as e:
            print(f"[WARN] Failed to fetch UUID for {username}: {e}")
        
        # Save the username match with UUID
        save_username_match(user.id, username, uuid)
        
        # Create success embed
        if existing_username:
            embed = discord.Embed(
                title="✅ Username Updated",
                description=f"Successfully updated link for {user.mention}",
                color=0x00FF00,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Previous IGN", value=f"`{existing_username}`", inline=True)
            embed.add_field(name="New IGN", value=f"`{username}`", inline=True)
        else:
            embed = discord.Embed(
                title="✅ Username Linked",
                description=f"Successfully linked {user.mention} to IGN `{username}`",
                color=0x00FF00,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Discord User", value=user.mention, inline=True)
            embed.add_field(name="Minecraft IGN", value=f"`{username}`", inline=True)
        
        if uuid:
            embed.add_field(name="UUID", value=f"`{uuid}`", inline=False)
        
        embed.set_footer(text=f"Linked by {interaction.user.name}")
        
        await interaction.response.send_message(embed=embed, ephemeral=False)
        print(f"[INFO] Linked {user.name} ({user.id}) to username '{username}' with UUID '{uuid}' by {interaction.user.name}")
    
    print("[OK] Loaded link_user command")