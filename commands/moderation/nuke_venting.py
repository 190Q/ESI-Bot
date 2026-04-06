import discord
from discord import app_commands
from datetime import datetime
import asyncio
import os
from utils.permissions import has_roles

VENTING_CHANNEL = 786149800373649408
PROTECTED_MSG = [1391812798165680280, 1268744322736586752]
REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0,
    1356674258390225076 # Admin role
]

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    @bot.tree.command(
        name="nuke_venting",
        description="Remove all messages from a channel except protected ones"
    )
    async def nuke_venting(interaction: discord.Interaction):
        """Purge all messages except protected ones"""
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
        
        await interaction.response.defer(ephemeral=True)
        
        # Get the channel object
        venting_channel_id = VENTING_CHANNEL
        protected_messages = PROTECTED_MSG
        if interaction.guild.id == 1442126799369670770:
            venting_channel_id = 1447724149266186463
            protected_messages = [1447724687139541195, 1447724690490659006]
            
        # Check if command is used in venting channel
        if interaction.channel.id != venting_channel_id:
            await interaction.response.send_message(
                "This command can only be used in the venting channel!",
                ephemeral=True
            )
            return
        
        # Get the channel object
        protected = set(protected_messages)
        channel = bot.get_channel(venting_channel_id)
        
        deleted_count = 0
        protected_count = 0
        
        try:
            # Send initial status message
            status_embed = discord.Embed(
                title="Purge in Progress",
                description="Starting to delete messages...",
                color=0xFFA500,
                timestamp=datetime.utcnow()
            )
            await interaction.followup.send(embed=status_embed, ephemeral=True)
            
            async for message in channel.history(limit=None, oldest_first=False):
                if message.id in protected:
                    protected_count += 1
                    continue
                
                try:
                    await message.delete()
                    deleted_count += 1
                    
                    await asyncio.sleep(0.7)  # Rate limit protection
                except discord.Forbidden:
                    await interaction.edit_original_response(content="I don't have permission to delete messages!", embed=None)
                    return
                except discord.HTTPException:
                    pass
            
            await interaction.edit_original_response(content=f"Successfully deleted {deleted_count} messages and skipped {protected_count} protected messages!", embed=None)
            
        except Exception as e:
            await interaction.edit_original_response(content=f"Error: {str(e)}", embed=None)
    
    print("[OK] Loaded nuke_venting command")