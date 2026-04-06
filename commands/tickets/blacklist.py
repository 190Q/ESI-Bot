import discord
from discord import app_commands
import sqlite3
from datetime import datetime
import os
import aiohttp
from utils.permissions import has_roles
from utils.paths import PROJECT_ROOT, DATA_DIR, DB_DIR

_BLACKLIST_DB = os.path.join(str(PROJECT_ROOT), 'databases', 'blacklist.db')

REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0,
    600185623474601995, # Parliament
]

def init_blacklist_db():
    """Initialize the blacklist database"""
    conn = sqlite3.connect(_BLACKLIST_DB)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS blacklist
                 (username TEXT PRIMARY KEY, reason TEXT, date TEXT)''')
    conn.commit()
    conn.close()

def add_to_blacklist(username, reason=None):
    """Add a user to the blacklist"""
    init_blacklist_db()  # Ensure table exists
    conn = sqlite3.connect(_BLACKLIST_DB)
    c = conn.cursor()
    
    date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # Check if user already exists
    c.execute('SELECT * FROM blacklist WHERE username=?', (username,))
    exists = c.fetchone()
    
    if exists:
        # Update existing entry
        c.execute('UPDATE blacklist SET reason=?, date=? WHERE username=?',
                  (reason, date, username))
        conn.commit()
        conn.close()
        return False  # Already existed
    else:
        # Insert new entry
        c.execute('INSERT INTO blacklist (username, reason, date) VALUES (?, ?, ?)',
                  (username, reason, date))
        conn.commit()
        conn.close()
        return True  # Newly added

def remove_from_blacklist(username):
    """Remove a user from the blacklist"""
    init_blacklist_db()  # Ensure table exists
    conn = sqlite3.connect(_BLACKLIST_DB)
    c = conn.cursor()
    
    c.execute('DELETE FROM blacklist WHERE username=?', (username,))
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    
    return deleted

def is_blacklisted(username):
    """Check if a user is blacklisted"""
    init_blacklist_db()  # Ensure table exists
    conn = sqlite3.connect(_BLACKLIST_DB)
    c = conn.cursor()
    
    c.execute('SELECT reason FROM blacklist WHERE username=?', (username,))
    result = c.fetchone()
    conn.close()
    
    if result:
        return True, result[0]  # Return True and reason
    return False, None

async def get_current_username(original_username):
    """Check for current username and detect if it changed"""
    try:
        async with aiohttp.ClientSession() as session:
            # Try Ashcon API
            url = f"https://api.ashcon.app/mojang/v2/user/{original_username}"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    current_name = data.get('username')
                    
                    # Check if username actually changed (case-insensitive comparison)
                    if current_name and current_name.lower() != original_username.lower():
                        return current_name
                    
                    # Username exists but hasn't changed
                    return original_username
                elif resp.status == 404:
                    return None
                    
            return None
    except Exception as e:
        print(f"Error checking username {original_username}: {e}")
        return None

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    # Initialize database
    init_blacklist_db()
    
    @bot.tree.command(
        name="blacklist_add",
        description="Add a Minecraft username to the blacklist"
    )
    @app_commands.describe(
        username="The Minecraft username to blacklist",
        reason="Reason for blacklisting (optional)"
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def blacklist(
        interaction: discord.Interaction,
        username: str,
        reason: str = None
    ):
        """Add a Minecraft username to the blacklist"""

        # Check permissions
        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return
        
        # Add to blacklist
        newly_added = add_to_blacklist(username, reason)
        
        if newly_added:
            embed = discord.Embed(
                title="✅ User Blacklisted",
                description=f"**{username}** has been added to the blacklist.",
                color=0x00FF00,
                timestamp=datetime.utcnow()
            )
        else:
            embed = discord.Embed(
                title="⚠️ User Already Blacklisted",
                description=f"**{username}** was already blacklisted. Their entry has been updated.",
                color=0xFFA500,
                timestamp=datetime.utcnow()
            )
        
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
        print(f"Blacklist command used: {username} - Reason: {reason or 'None'}")
    
    @bot.tree.command(
        name="blacklist_remove",
        description="Remove a Minecraft username from the blacklist"
    )
    @app_commands.describe(
        username="The Minecraft username to remove from blacklist"
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def unblacklist(
        interaction: discord.Interaction,
        username: str
    ):
        """Remove a Minecraft username from the blacklist"""

        # Check permissions
        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return
        
        # Remove from blacklist
        deleted = remove_from_blacklist(username)
        
        if deleted:
            embed = discord.Embed(
                title="✅ User Removed from Blacklist",
                description=f"**{username}** has been removed from the blacklist.",
                color=0x00FF00,
                timestamp=datetime.utcnow()
            )
        else:
            embed = discord.Embed(
                title="❌ User Not Found",
                description=f"**{username}** was not in the blacklist.",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
        print(f"Unblacklist command used: {username} - Deleted: {deleted}")
    
    @bot.tree.command(
        name="blacklist_check",
        description="Check if a Minecraft username is blacklisted"
    )
    @app_commands.describe(
        username="The Minecraft username to check"
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def checkblacklist(
        interaction: discord.Interaction,
        username: str
    ):
        """Check if a Minecraft username is blacklisted"""
        
        # Check blacklist
        blacklisted, reason = is_blacklisted(username)
        
        if blacklisted:
            embed = discord.Embed(
                title="🚫 User is Blacklisted",
                description=f"**{username}** is currently blacklisted.",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            if reason:
                embed.add_field(name="Reason", value=reason, inline=False)
        else:
            embed = discord.Embed(
                title="✅ User is Not Blacklisted",
                description=f"**{username}** is not in the blacklist.",
                color=0x00FF00,
                timestamp=datetime.utcnow()
            )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @bot.tree.command(
        name="blacklist",
        description="List all blacklisted Minecraft usernames"
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def listblacklist(
        interaction: discord.Interaction
    ):
        """List all blacklisted Minecraft usernames"""

        # Defer response since checking usernames might take time
        await interaction.response.defer(ephemeral=True)

        init_blacklist_db()
        
        # Check blacklist
        conn = sqlite3.connect(_BLACKLIST_DB)
        c = conn.cursor()
        c.execute('SELECT username, reason, date FROM blacklist ORDER BY date DESC')
        blacklisted_users = c.fetchall()
        conn.close()
        
        if not blacklisted_users:
            embed = discord.Embed(
                title="Blacklist",
                description="No users are currently blacklisted.",
                color=0x00FF00,
                timestamp=datetime.utcnow()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        embed = discord.Embed(
            title=f"Blacklist ({len(blacklisted_users)} users)",
            color=0xFF0000,
            timestamp=datetime.utcnow()
        )
        
        for username, reason, date in blacklisted_users:
            reason_text = reason if reason else "No reason provided"
            
            # Check for current username
            current_username = await get_current_username(username)

            if current_username is None:
                username_display = f"~~{username}~~ (account deleted/invalid)"
            elif current_username != username:
                username_display = f"~~{username}~~ → **{current_username}**"
            else:
                username_display = username
            
            embed.add_field(
                name=username_display,
                value=f"**Reason:** {reason_text}\n**Date:** {date}\n__NameMC profile__: https://namemc.com/search?q={username}",
                inline=False
            )
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
        print(f"List blacklist command used by {interaction.user.name}")
    
    print("[OK] Loaded blacklist commands")