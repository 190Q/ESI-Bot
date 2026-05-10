import discord
from datetime import datetime
import discord
from discord import app_commands
from datetime import datetime
import json
from pathlib import Path
import os
from utils.paths import DATA_DIR

NOTIFICATION_FILE = DATA_DIR / 'app_notifications.json'

REQUIRED_ROLES = (
    600185623474601995, # Parliament
    954566591520063510, # Jurors
    os.getenv('OWNER_ID') if os.getenv('OWNER_ID') else 0
)


def has_roles(user, role_ids):
    """Check if user has any of the required roles or matches user ID"""
    user_role_ids = [role.id for role in user.roles]
    return user.id in role_ids or any(role_id in user_role_ids for role_id in role_ids)

def load_notification_users():
    """Load users who want app notifications"""
    if NOTIFICATION_FILE.exists():
        with open(NOTIFICATION_FILE, 'r') as f:
            return set(json.load(f))
    return set()

def save_notification_users(users):
    """Save users who want app notifications"""
    with open(NOTIFICATION_FILE, 'w') as f:
        json.dump(list(users), f)

def setup(bot):
    """Setup function for bot integration"""
    
    # Always register the command (even on reload)
    @bot.tree.command(name="app_notifications", description="Toggle app notifications on/off")
    async def app_notifications(interaction: discord.Interaction):
        """Toggle app notifications"""

        # Check permissions
        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            await interaction.response.send_message(
                "❌ You don't have permission to use this command!",
                ephemeral=True
            )
            return

        users = load_notification_users()
        user_id = interaction.user.id
        
        if user_id in users:
            users.remove(user_id)
            save_notification_users(users)
            await interaction.response.send_message("✅ You will no longer receive app notifications.", ephemeral=True)
        else:
            users.add(user_id)
            save_notification_users(users)
            await interaction.response.send_message("✅ You will now receive app notifications when apps are created or filled!", ephemeral=True)
    
    print("[OK] Loaded channel creation logger (listeners registered)")