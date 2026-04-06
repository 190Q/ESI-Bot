import discord
from discord import app_commands
import os
from datetime import datetime, timedelta
import json
from discord.ext import tasks
import random
from utils.permissions import has_roles

# Store task reference for teardown
_birthday_task = None

def teardown(bot):
    """Called when the module is unloaded/reloaded"""
    global _birthday_task
    if _birthday_task is not None and _birthday_task.is_running():
        _birthday_task.stop()
        print("[Birthday] Stopped birthday check task")
    _birthday_task = None

BIRTH_REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0,
    600185623474601995, # Parliament
]
REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0,
]

# File to store birthdays
BIRTHDAY_FILE = 'birthdays.json'

def load_birthdays():
    """Load birthdays from JSON file"""
    if os.path.exists(BIRTHDAY_FILE):
        with open(BIRTHDAY_FILE, 'r') as f:
            data = json.load(f)
            # Ensure config exists
            if 'config' not in data:
                data['config'] = {}
            if 'birthdays' not in data:
                data['birthdays'] = {}
            return data
    return {'config': {}, 'birthdays': {}}

def save_birthdays(data):
    """Save birthdays and config to JSON file"""
    with open(BIRTHDAY_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    # Configuration for birthday announcements
    BIRTHDAY_ROLE_ID = 1127122018639302699
    
    @tasks.loop(minutes=1)
    async def check_birthdays():
        """Check for birthdays every minute"""
        
        data_file = load_birthdays()

        birthdays = data_file['birthdays']
        now_utc = datetime.utcnow()

        for user_id, data in birthdays.items():
            try:
                timezone_offset = int(data.get('timezone', '0'))
                user_time = now_utc + timedelta(hours=timezone_offset)
                
                birthday_date = datetime.strptime(data['date'], "%d/%m")
                
                if (user_time.month == birthday_date.month and 
                    user_time.day == birthday_date.day and 
                    user_time.hour == 0 and 
                    user_time.minute == 0):
                    
                    for guild in bot.guilds:
                        member = guild.get_member(int(user_id))
                        if member:
                            birthday_role = guild.get_role(BIRTHDAY_ROLE_ID)
                            if birthday_role and birthday_role not in member.roles:
                                await member.add_roles(birthday_role)
                                print(f"[Birthday] Added birthday role to {member}")
                            
                            birthday_channel_id = data_file['config'].get('birthday_channel_id', None)
                            if birthday_channel_id:
                                channel = guild.get_channel(birthday_channel_id)
                                if channel:
                                    catgun = discord.utils.get(guild.emojis, id=1449121592306303026)
                                    if not catgun:
                                        catgun = discord.utils.get(guild.emojis, id=923374169771614249)
                                    catgun_str = str(catgun) if catgun else ""
                                    
                                    birthday_messages = [
                                        f"Congrats {member.mention} for getting one year closer to death!",
                                        f"{member.mention}\n# Birth 🫵",
                                        f"happy birthday {member.mention}!\nThey grow so fast 😢",
                                        f"Another year older, {member.mention}. Truly groundbreaking.",
                                        f"Happy birthday, {member.mention}. I guess we are celebrating slow decay now.",
                                        f"{member.mention}, congratulations on surviving another lap around the sun.",
                                        f"Happy birthday, {member.mention}. Don't worry, maturity is still not your thing.",
                                        f"Happy birthday {member.mention}, you're old!",
                                        f"{member.mention}, how's the bingo night at the retirement home?"
                                    ]
                                    birthday_message = f"{random.choice(birthday_messages)}\n-# tell them happy birthday {catgun_str} and if you want your birthday to be announced too contact a parliament member."
                                    await channel.send(birthday_message)
                                    print(f"[Birthday] Announced birthday for {member}")
                
                if (user_time.month == birthday_date.month and 
                    user_time.day == birthday_date.day + 1 and 
                    user_time.hour == 0 and 
                    user_time.minute == 0):
                    for guild in bot.guilds:
                        member = guild.get_member(int(user_id))
                        if member:
                            birthday_role = guild.get_role(BIRTHDAY_ROLE_ID)
                            if birthday_role and birthday_role in member.roles:
                                await member.remove_roles(birthday_role)
                                print(f"[Birthday] Removed birthday role from {member}")
            
            except Exception as e:
                print(f"[Birthday] Error checking birthday for user {user_id}: {e}")
    
    @bot.tree.command(
        name="birthday_channel",
        description="Set the channel for birthday announcements"
    )
    @app_commands.describe(
        channel="The channel where birthdays will be announced"
    )
    async def birthday_channel(interaction: discord.Interaction, channel: discord.TextChannel):
        """Set the birthday announcement channel"""
        
        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return
        
        data_file = load_birthdays()
        data_file['config']['birthday_channel_id'] = channel.id
        save_birthdays(data_file)
        
        success_embed = discord.Embed(
            title="✅ Birthday Channel Set",
            description=f"Birthday announcements will be sent to {channel.mention}",
            color=0x00FF00
        )
        await interaction.response.send_message(embed=success_embed, ephemeral=True)
        print(f"[Birthday] Set announcement channel to {channel}")
    
    @check_birthdays.before_loop
    async def before_check_birthdays():
        await bot.wait_until_ready()
    
    # Store reference for teardown and start the task
    global _birthday_task
    _birthday_task = check_birthdays
    check_birthdays.start()
    
    @bot.tree.command(
        name="birthday_add",
        description="Add a birthday for a user"
    )
    @app_commands.describe(
        user="The user whose birthday to add",
        date="Birthday in DD/MM format (e.g., 15/03)"
    )
    async def add_birthday(interaction: discord.Interaction, user: discord.Member, date: str):
        """Add a birthday for a user"""
        
        # Check permissions
        if not has_roles(interaction.user, BIRTH_REQUIRED_ROLES) and BIRTH_REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return
        
        # Validate date format
        try:
            parsed_date = datetime.strptime(date, "%d/%m")
            formatted_date = parsed_date.strftime("%d/%m")
        except ValueError:
            error_embed = discord.Embed(
                title="Invalid Date Format",
                description="Please use DD/MM format (e.g., 15/03)",
                color=0xFF0000
            )
            await interaction.response.send_message(embed=error_embed, ephemeral=True)
            return
        
        # Create the timezone selector view
        class TimezoneSelectView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=180)
                self.timezone = None
            
            @discord.ui.select(
                placeholder="Select Timezone",
                options=[
                    discord.SelectOption(label="UTC-12", value="-12"),
                    discord.SelectOption(label="UTC-11", value="-11"),
                    discord.SelectOption(label="UTC-10", value="-10"),
                    discord.SelectOption(label="UTC-9", value="-9"),
                    discord.SelectOption(label="UTC-8 (PST)", value="-8"),
                    discord.SelectOption(label="UTC-7 (MST)", value="-7"),
                    discord.SelectOption(label="UTC-6 (CST)", value="-6"),
                    discord.SelectOption(label="UTC-5 (EST)", value="-5"),
                    discord.SelectOption(label="UTC-4", value="-4"),
                    discord.SelectOption(label="UTC-3", value="-3"),
                    discord.SelectOption(label="UTC-2", value="-2"),
                    discord.SelectOption(label="UTC-1", value="-1"),
                    discord.SelectOption(label="UTC+0 (GMT)", value="0"),
                    discord.SelectOption(label="UTC+1 (CET)", value="1"),
                    discord.SelectOption(label="UTC+2", value="2"),
                    discord.SelectOption(label="UTC+3", value="3"),
                    discord.SelectOption(label="UTC+4", value="4"),
                    discord.SelectOption(label="UTC+5", value="5"),
                    discord.SelectOption(label="UTC+6", value="6"),
                    discord.SelectOption(label="UTC+7", value="7"),
                    discord.SelectOption(label="UTC+8", value="8"),
                    discord.SelectOption(label="UTC+9", value="9"),
                    discord.SelectOption(label="UTC+10", value="10"),
                    discord.SelectOption(label="UTC+11", value="11"),
                    discord.SelectOption(label="UTC+12", value="12"),
                ]
            )
            async def timezone_select(self, interaction: discord.Interaction, select: discord.ui.Select):
                self.timezone = select.values[0]
                
                data_file = load_birthdays()

                # Add birthday with timezone
                data_file['birthdays'][str(user.id)] = {
                    "date": formatted_date,
                    "username": str(user),
                    "timezone": self.timezone
                }

                # Save data
                save_birthdays(data_file)

                # Calculate time until birthday
                now_utc = datetime.utcnow()
                timezone_offset = int(self.timezone)
                user_time = now_utc + timedelta(hours=timezone_offset)

                # Parse the birthday date and set it to current year
                birthday_this_year = datetime.strptime(formatted_date, "%d/%m").replace(year=user_time.year)


                # If birthday already passed this year, calculate for next year
                if birthday_this_year < user_time:
                    birthday_this_year = birthday_this_year.replace(year=user_time.year + 1)

                # Calculate time difference
                time_until = birthday_this_year - user_time
                days_until = time_until.days
                hours_until = time_until.seconds // 3600
                minutes_until = (time_until.seconds % 3600) // 60

                # Create time string
                if days_until > 0:
                    time_str = f"{days_until} day{'s' if days_until != 1 else ''}, {hours_until} hour{'s' if hours_until != 1 else ''}"
                else:
                    time_str = f"{hours_until} hour{'s' if hours_until != 1 else ''}, {minutes_until} minute{'s' if minutes_until != 1 else ''}"

                success_embed = discord.Embed(
                    title="🎂 Birthday Added",
                    description=f"Added birthday for {user.mention}: **{formatted_date}** (UTC{self.timezone})\n\nTime until birthday: **{time_str}**",
                    color=0x00FF00
                )
                await interaction.response.edit_message(embed=success_embed, view=None)
                print(f"[Birthday] Added {formatted_date} (UTC{self.timezone}) for {user}")
        
        view = TimezoneSelectView()
        prompt_embed = discord.Embed(
            title="🎂 Add Birthday",
            description=f"Birthday date: **{formatted_date}**\nNow select the timezone for {user.mention}",
            color=0x3498DB
        )
        await interaction.response.send_message(embed=prompt_embed, view=view, ephemeral=True)
    
    @bot.tree.command(
        name="birthday_remove",
        description="Remove a birthday for a user"
    )
    @app_commands.describe(
        user="The user whose birthday to remove (mention or user ID)"
    )
    async def remove_birthday(interaction: discord.Interaction, user: str):
        """Remove a birthday for a user"""
        
        # Check permissions
        if not has_roles(interaction.user, BIRTH_REQUIRED_ROLES) and BIRTH_REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return
        
        # Parse user ID from mention format <@123456> or raw ID
        user_id = user.strip().strip('<@!>')
        if not user_id.isdigit():
            error_embed = discord.Embed(
                title="Invalid User",
                description="Please provide a valid user mention or user ID.",
                color=0xFF0000
            )
            await interaction.response.send_message(embed=error_embed, ephemeral=True)
            return
        
        # Load data
        data_file = load_birthdays()
        birthdays = data_file['birthdays']

        # Check if birthday exists
        if user_id not in birthdays:
            error_embed = discord.Embed(
                title="Birthday Not Found",
                description=f"No birthday found for <@{user_id}>",
                color=0xFF0000
            )
            await interaction.response.send_message(embed=error_embed, ephemeral=True)
            return

        # Remove birthday
        removed_date = birthdays[user_id]["date"]
        del data_file['birthdays'][user_id]

        # Save data
        save_birthdays(data_file)
        
        # Send success message
        success_embed = discord.Embed(
            title="🗑️ Birthday Removed",
            description=f"Removed birthday for <@{user_id}> ({removed_date})",
            color=0xFFA500
        )
        await interaction.response.send_message(embed=success_embed, ephemeral=True)
        print(f"[Birthday] Removed birthday for {user_id}")
    
    @bot.tree.command(
        name="birthday_list",
        description="List all birthdays"
    )
    async def list_birthdays(interaction: discord.Interaction):
        """List all birthdays with pagination"""
        
        # Load birthdays
        data_file = load_birthdays()
        birthdays = data_file['birthdays']
        
        if not birthdays:
            empty_embed = discord.Embed(
                title="🎂 Birthday List",
                description="No birthdays have been added yet!",
                color=0x3498DB
            )
            await interaction.response.send_message(embed=empty_embed)
            return
        
        # Sort birthdays by date (handle both DD/MM and MM/DD formats)
        def parse_birthday_date(item):
            date_str = item[1]["date"]
            # Try DD/MM format first
            try:
                return datetime.strptime(date_str, "%d/%m").replace(year=2000)
            except ValueError:
                # Fallback to MM/DD format for old entries
                try:
                    return datetime.strptime(date_str, "%m/%d").replace(year=2000)
                except ValueError:
                    # If both fail, return a far future date so it appears last
                    return datetime(2000, 12, 31)
        
        sorted_birthdays = sorted(
            birthdays.items(),
            key=parse_birthday_date
        )
        
        # Pagination settings
        per_page = 10
        total_pages = (len(sorted_birthdays) + per_page - 1) // per_page
        current_page = 0
        
        async def create_embed(page):
            start = page * per_page
            end = start + per_page
            page_birthdays = sorted_birthdays[start:end]
            
            embed = discord.Embed(
                title="🎂 Birthday List",
                description=f"Total: {len(birthdays)} birthdays | Page {page + 1}/{total_pages}",
                color=0x3498DB
            )
            
            for user_id, data in page_birthdays:
                # Calculate time until birthday
                now_utc = datetime.utcnow()
                timezone_offset = int(data.get('timezone', '0'))
                user_time = now_utc + timedelta(hours=timezone_offset)
                
                birthday_this_year = datetime.strptime(data['date'], "%d/%m").replace(year=user_time.year)
                
                if birthday_this_year < user_time:
                    birthday_this_year = birthday_this_year.replace(year=user_time.year + 1)
                
                time_until = birthday_this_year - user_time
                days_until = time_until.days
                hours_until = time_until.seconds // 3600
                
                if days_until > 0:
                    countdown = f"in {days_until}d {hours_until}h"
                elif hours_until > 0:
                    minutes_until = (time_until.seconds % 3600) // 60
                    countdown = f"in {hours_until}h {minutes_until}m"
                else:
                    countdown = "Today!"
                
                try:
                    user = await bot.fetch_user(int(user_id))
                    timezone_info = f" (UTC{data.get('timezone', '0')})" if 'timezone' in data else ""
                    embed.add_field(
                        name=f"{data['date']}{timezone_info}",
                        value=f"{user.mention} • {countdown}",
                        inline=False
                    )
                except:
                    timezone_info = f" (UTC{data.get('timezone', '0')})" if 'timezone' in data else ""
                    embed.add_field(
                        name=f"{data['date']}{timezone_info}",
                        value=f"{data['username']} (User not found) • {countdown}",
                        inline=False
                    )
            
            return embed
        
        # Create view with buttons
        class PaginationView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=180)
                self.page = 0
            
            @discord.ui.button(label="⏮️", style=discord.ButtonStyle.gray)
            async def first_page(self, interaction: discord.Interaction, button: discord.ui.Button):
                self.page = 0
                await interaction.response.edit_message(embed=await create_embed(self.page), view=self)
            
            @discord.ui.button(label="◀️", style=discord.ButtonStyle.primary)
            async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
                self.page = max(0, self.page - 1)
                await interaction.response.edit_message(embed=await create_embed(self.page), view=self)
            
            @discord.ui.button(label="▶️", style=discord.ButtonStyle.primary)
            async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
                self.page = min(total_pages - 1, self.page + 1)
                await interaction.response.edit_message(embed=await create_embed(self.page), view=self)
            
            @discord.ui.button(label="⏭️", style=discord.ButtonStyle.gray)
            async def last_page(self, interaction: discord.Interaction, button: discord.ui.Button):
                self.page = total_pages - 1
                await interaction.response.edit_message(embed=await create_embed(self.page), view=self)

        if total_pages > 1:
            view = PaginationView()
            await interaction.response.send_message(embed=await create_embed(0), view=view)
        else:
            await interaction.response.send_message(embed=await create_embed(0))
        print("[Birthday] Listed all birthdays")
    
    @bot.tree.command(
        name="birthday_test",
        description="Test the birthday announcement (triggers it immediately)"
    )
    @app_commands.describe(
        user="The user to test the birthday announcement for"
    )
    async def test_birthday(interaction: discord.Interaction, user: discord.Member):
        """Test birthday announcement for a user"""
        
        # Check permissions
        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return
        
        # Load data
        data_file = load_birthdays()
        
        # Get birthday channel
        birthday_channel_id = data_file['config'].get('birthday_channel_id', None)
        if not birthday_channel_id:
            error_embed = discord.Embed(
                title="No Birthday Channel Set",
                description="Please set a birthday channel first with `/birthday_channel`.",
                color=0xFF0000
            )
            await interaction.response.send_message(embed=error_embed, ephemeral=True)
            return
        
        channel = interaction.guild.get_channel(birthday_channel_id)
        if not channel:
            error_embed = discord.Embed(
                title="Channel Not Found",
                description="The configured birthday channel no longer exists.",
                color=0xFF0000
            )
            await interaction.response.send_message(embed=error_embed, ephemeral=True)
            return
        
        # Add birthday role
        birthday_role = interaction.guild.get_role(BIRTHDAY_ROLE_ID)
        if birthday_role and birthday_role not in user.roles:
            await user.add_roles(birthday_role)
        
        # Send birthday announcement
        catgun = discord.utils.get(interaction.guild.emojis, id=1449121592306303026)
        if not catgun:
            catgun = discord.utils.get(interaction.guild.emojis, id=923374169771614249)
        catgun_str = str(catgun) if catgun else ""
        
        birthday_messages = [
            f"Congrats {user.mention} for getting one year closer to death!",
            f"{user.mention}\n# Birth 🫵",
            f"happy birthday {user.mention}!\nThey grow so fast 😢",
            f"Another year older, {user.mention}. Truly groundbreaking.",
            f"Happy birthday, {user.mention}. I guess we are celebrating slow decay now.",
            f"{user.mention}, congratulations on surviving another lap around the sun.",
            f"Happy birthday, {user.mention}. Don't worry, maturity is still not your thing.",
            f"Happy birthday {user.mention}, you're old!",
            f"{user.mention}, how's the bingo night at the retirement home?"
        ]
        birthday_message = f"{random.choice(birthday_messages)}\n\n-# Tell them happy birthday {catgun_str}\n-# If you want your birthday added as well contact any member of [parliament](https://discord.com/channels/554418045397762048/1381292106928095312/1381292106928095312)"
        await channel.send(birthday_message)
        
        # Confirm to command user
        success_embed = discord.Embed(
            title="✅ Test Birthday Sent",
            description=f"Birthday announcement sent for {user.mention} in {channel.mention}!",
            color=0x00FF00
        )
        await interaction.response.send_message(embed=success_embed, ephemeral=True)
    
    print("[OK] Loaded birthday commands")