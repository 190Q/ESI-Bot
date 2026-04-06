import discord
from discord import app_commands
import os
import random
import json
from utils.permissions import has_roles

REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0,
    600185623474601995 # Parliament
]

# Welcome messages
WELCOME_MESSAGES = [
    "Welcome {user}, you can head over to <#555019955913883648> to apply!",
    "Hey {user}! Welcome to the server! Feel free to apply in <#555019955913883648>.",
    "Welcome to ESI, {user}! Check out <#555019955913883648> to submit your application.",
    "Hey there {user}, welcome! You can apply to join us in <#555019955913883648>.",
    "Welcome {user}! To apply for membership, head to <#555019955913883648>.",
    "Hi {user}, welcome! If you're interested in joining, check out <#555019955913883648> to apply.",
    "Welcome aboard {user}! Applications can be submitted in <#555019955913883648>.",
    "Hey {user}, welcome to the community! Apply in <#555019955913883648> if you'd like to join us!"
]

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'config', 'welcome.json')
    
    # Load welcome channel from file
    def load_config():
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
    
    # Save welcome channel to file
    def save_config(data):
        with open(CONFIG_FILE, 'w') as f:
            json.dump(data, f)
    
    # Load the saved channel ID
    saved_config = load_config()
    welcome_channel_id = saved_config.get('welcome_channel_id', None)

    # Send welcome messages
    async def send_welcome_message(channel, member, is_test=False):
        """Send a welcome message to the specified channel"""
        raw_message = random.choice(WELCOME_MESSAGES).format(user=member.mention)
        
        if is_test:
            message = f"**[TEST]**⟶    {raw_message}"
        else:
            message = f"⟶    {raw_message}"
        
        try:
            await channel.send(message)
            return True
        except Exception as e:
            print(f"[Welcome] Error sending message: {e}")
            return False
    
    @bot.event
    async def on_member_join(member):
        """Event triggered when a new member joins the server"""
        nonlocal welcome_channel_id
        
        # If no channel is set, skip
        if not welcome_channel_id:
            print(f"[Welcome] {member} joined but no welcome channel is set")
            return
        
        # Get the welcome channel
        channel = bot.get_channel(welcome_channel_id)
        server = member.guild.id
        if not channel:
            print(f"[Welcome] Channel {welcome_channel_id} not found")
            return
        
        # Send the welcome message
        success = False
        if server == 554418045397762048:
            success = await send_welcome_message(channel, member)
        if success:
            print(f"[Welcome] Sent welcome message for {member}")
    
    @bot.tree.command(
        name="welcome_channel",
        description="Set the channel for welcome messages"
    )
    @app_commands.describe(channel="The channel to send welcome messages in")
    async def set_welcome(interaction: discord.Interaction, channel: discord.TextChannel):
        """Set the welcome channel"""
        nonlocal welcome_channel_id
        
        # Check permissions
        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        # Update the welcome channel
        welcome_channel_id = channel.id
        save_config({'welcome_channel_id': welcome_channel_id})

        await interaction.response.send_message(f"Welcome messages will now be sent to {channel.mention}", ephemeral=True)
        print(f"[Welcome] Channel set to {channel.name} ({channel.id})")
    
    print("[OK] Loaded welcome message system")