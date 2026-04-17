import discord
from discord import app_commands
from discord import ui
import os
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
import importlib.util
import re
from utils.permissions import has_roles
from utils.bans import load_bans, save_bans, is_user_banned, remove_ban

OWNER_ID = int(os.getenv('OWNER_ID', 0))

REQUIRED_ROLES = [728858956575014964]

# Duration options in seconds
DURATION_OPTIONS = {
    "5min": 5 * 60,
    "15min": 15 * 60,
    "30min": 30 * 60,
    "45min": 45 * 60,
    "1h": 1 * 60 * 60,
    "2h": 2 * 60 * 60,
    "5h": 5 * 60 * 60,
    "12h": 12 * 60 * 60,
    "1d": 1 * 24 * 60 * 60,
    "2d": 2 * 24 * 60 * 60,
    "3d": 3 * 24 * 60 * 60,
    "5d": 5 * 24 * 60 * 60,
    "1w": 7 * 24 * 60 * 60,
    "2w": 14 * 24 * 60 * 60,
    "3w": 21 * 24 * 60 * 60,
    "1months": 30 * 24 * 60 * 60,
    "2months": 60 * 24 * 60 * 60,
    "3months": 90 * 24 * 60 * 60,
    "6months": 180 * 24 * 60 * 60,
    "forever": None
}

def get_all_command_names(bot):
    """Get all registered command names from the bot"""
    commands = []
    
    # Get slash commands
    for cmd in bot.tree.get_commands():
        commands.append(cmd.name)
    
    # Sort alphabetically (case-insensitive)
    commands.sort(key=str.lower)
    return commands

def parse_custom_duration(duration_str: str) -> tuple:
    """
    Parse a custom duration string into seconds.
    Returns (seconds, formatted_string) or (None, error_message)
    
    Supports formats like:
    - 1min, 5m, 30 minutes, 2 minute
    - 1h, 2 hours, 3 hour
    - 1d, 5 days, 7 day
    - 1w, 2 weeks, 3 week
    - 1mo, 2 months, 3 month
    - 1y, 2 years, 3 year
    - forever, permanent, perm
    """
    duration_str = duration_str.strip().lower()
    
    # Check for forever/permanent
    if duration_str in ['forever', 'permanent', 'perm', 'permanently']:
        return (None, 'forever')
    
    # Regular expression to match number and unit
    # Matches: "5", "5m", "5 m", "5 minutes", "5minutes", etc.
    pattern = r'^(\d+)\s*([a-z]+)$'
    match = re.match(pattern, duration_str)
    
    if not match:
        return (None, "Invalid format. Use formats like: 5m, 1 hour, 30 days, 2 years, or 'forever'")
    
    value = int(match.group(1))
    unit = match.group(2)
    
    # Map units to seconds
    unit_mappings = {
        # Minutes
        'min': 60,
        'mins': 60,
        'minute': 60,
        'minutes': 60,
        'm': 60,
        
        # Hours
        'h': 3600,
        'hr': 3600,
        'hrs': 3600,
        'hour': 3600,
        'hours': 3600,
        
        # Days
        'd': 86400,
        'day': 86400,
        'days': 86400,
        
        # Weeks
        'w': 604800,
        'wk': 604800,
        'week': 604800,
        'weeks': 604800,
        
        # Months (30 days)
        'mo': 2592000,
        'mon': 2592000,
        'month': 2592000,
        'months': 2592000,
        
        # Years (365 days)
        'y': 31536000,
        'yr': 31536000,
        'yrs': 31536000,
        'year': 31536000,
        'years': 31536000,
    }
    
    if unit not in unit_mappings:
        valid_units = "m/min/minute, h/hour, d/day, w/week, mo/month, y/year"
        return (None, f"Unknown unit '{unit}'. Valid units: {valid_units}")
    
    seconds = value * unit_mappings[unit]
    
    # Create a human-readable format
    unit_names = {
        'm': 'minute', 'min': 'minute', 'mins': 'minute', 'minute': 'minute', 'minutes': 'minute',
        'h': 'hour', 'hr': 'hour', 'hrs': 'hour', 'hour': 'hour', 'hours': 'hour',
        'd': 'day', 'day': 'day', 'days': 'day',
        'w': 'week', 'wk': 'week', 'week': 'week', 'weeks': 'week',
        'mo': 'month', 'mon': 'month', 'month': 'month', 'months': 'month',
        'y': 'year', 'yr': 'year', 'yrs': 'year', 'year': 'year', 'years': 'year',
    }
    
    unit_name = unit_names.get(unit, unit)
    if value != 1:
        unit_name += 's'
    
    formatted = f"{value} {unit_name}"
    
    return (seconds, formatted)

class CommandSelector(discord.ui.View):
    """View for selecting commands to ban"""
    
    def __init__(self, user_id: int, reason: str, bot):
        super().__init__(timeout=300)  # 5 minute timeout
        self.user_id = user_id
        self.reason = reason
        self.bot = bot
        self.selected_commands = set()
        self.all_commands = get_all_command_names(bot)
        self.previous_selection = set()
        self.page = 0
        self.commands_per_page = 23  # 24 - 1 for "All" option
        
        self._update_view()
    
    def _update_view(self):
        """Update the view with current page"""
        self.clear_items()
        
        # Calculate page info
        total_pages = (len(self.all_commands) + self.commands_per_page - 1) // self.commands_per_page
        start_idx = self.page * self.commands_per_page
        end_idx = min(start_idx + self.commands_per_page, len(self.all_commands))
        page_commands = self.all_commands[start_idx:end_idx]
        
        # Add the select menu
        self.command_select = discord.ui.Select(
            placeholder=f"Select commands (Page {self.page + 1}/{total_pages})...",
            min_values=1,
            max_values=min(25, len(page_commands)),
            options=self._create_options(page_commands)
        )
        self.command_select.callback = self.select_callback
        self.add_item(self.command_select)
        
        # Add "All" button
        all_button_style = discord.ButtonStyle.green if "All" in self.selected_commands else discord.ButtonStyle.grey
        all_button_label = "✓ All" if "All" in self.selected_commands else "Select All"
        all_button = discord.ui.Button(label=all_button_label, style=all_button_style)
        all_button.callback = self.all_button_callback
        self.add_item(all_button)
        
        # Add navigation buttons if needed
        if total_pages > 1:
            if self.page > 0:
                prev_button = discord.ui.Button(label="◀ Previous", style=discord.ButtonStyle.grey)
                prev_button.callback = self.prev_page_callback
                self.add_item(prev_button)
            
            if self.page < total_pages - 1:
                next_button = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.grey)
                next_button.callback = self.next_page_callback
                self.add_item(next_button)
        
        # Add confirm button
        confirm_button = discord.ui.Button(label="Confirm Selection", style=discord.ButtonStyle.green)
        confirm_button.callback = self.confirm_callback
        self.add_item(confirm_button)
    
    def _create_options(self, page_commands):
        """Create select menu options for current page"""
        options = []
        
        for cmd in page_commands:
            options.append(
                discord.SelectOption(
                    label=cmd,
                    value=cmd,
                    description=f"Ban from {cmd} command",
                    default=cmd in self.selected_commands
                )
            )
        
        return options
    
    async def all_button_callback(self, interaction: discord.Interaction):
        """Handle 'Select All' button click"""
        if "All" in self.selected_commands:
            # Unselect all
            self.selected_commands = self.previous_selection.copy()
            self.previous_selection = set()
        else:
            # Select all
            self.previous_selection = self.selected_commands.copy()
            self.selected_commands = {"All"} | set(self.all_commands)
        
        # Update the view to show current selections
        self._update_view()
        await self._update_message(interaction)
    async def prev_page_callback(self, interaction: discord.Interaction):
        """Go to previous page"""
        self.page -= 1
        self._update_view()
        await self._update_message(interaction)
    
    async def next_page_callback(self, interaction: discord.Interaction):
        """Go to next page"""
        self.page += 1
        self._update_view()
        await self._update_message(interaction)
    
    async def _update_message(self, interaction: discord.Interaction):
        """Update the message with current selection state"""
        # Create response message
        if "All" in self.selected_commands:
            selected_text = "**All commands** selected"
        elif self.selected_commands:
            # Remove "All" from display if it's in there
            display_commands = self.selected_commands - {"All"}
            selected_text = f"Selected: {', '.join(sorted(display_commands))}"
        else:
            selected_text = "No commands selected"
        
        embed = discord.Embed(
            title="Command Selection",
            description=f"{selected_text}\n\nClick 'Confirm Selection' to proceed to duration selection.",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def select_callback(self, interaction: discord.Interaction):
        """Handle command selection"""
        selected_values = set(self.command_select.values)
        
        # Get the current page's commands
        start_idx = self.page * self.commands_per_page
        end_idx = min(start_idx + self.commands_per_page, len(self.all_commands))
        page_commands = set(self.all_commands[start_idx:end_idx])
        
        # Remove commands from current page that weren't selected
        self.selected_commands = self.selected_commands - page_commands
        # Add newly selected commands from current page
        self.selected_commands.update(selected_values)
        
        # If they had "All" before and manually selected specific commands, remove "All"
        if "All" in self.selected_commands and self.selected_commands != {"All"} | set(self.all_commands):
            self.selected_commands.discard("All")
        
        # Update the view to show current selections
        self._update_view()
        await self._update_message(interaction)
    
    async def confirm_callback(self, interaction: discord.Interaction):
        """Apply permanent ban directly"""
        if not self.selected_commands:
            await interaction.response.send_message("Please select at least one command!", ephemeral=True)
            return
        
        # Apply permanent ban directly (no duration selection)
        ban_time = datetime.now(timezone.utc)
        
        # Save ban to database
        bans = load_bans()
        user_id_str = str(self.user_id)
        
        bans[user_id_str] = {
            "user_id": self.user_id,
            "banned_commands": list(self.selected_commands),
            "reason": self.reason or "",
            "ban_time": ban_time.isoformat()
        }
        
        save_bans(bans)
        
        # Create success embed
        if "All" in self.selected_commands:
            commands_text = "**All commands**"
        else:
            commands_text = ', '.join(sorted(self.selected_commands))
        
        embed = discord.Embed(
            title="✅ User Banned",
            description=f"Successfully permanently banned <@{self.user_id}>",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="User ID", value=str(self.user_id), inline=True)
        embed.add_field(name="Commands", value=commands_text, inline=False)
        if self.reason:
            embed.add_field(name="Reason", value=self.reason, inline=False)
        
        await interaction.response.edit_message(embed=embed, view=None)
        print(f"[BAN] User {self.user_id} permanently banned from {commands_text}")

class CustomDurationModal(ui.Modal, title="Custom Ban Duration"):
    """Modal for entering custom duration"""
    
    duration_input = ui.TextInput(
        label="Duration",
        placeholder="e.g., 5m, 1 hour, 30 days, 2 years, forever",
        required=True,
        max_length=50
    )
    
    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view
    
    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission"""
        duration_str = self.duration_input.value
        
        # Parse the custom duration
        result = parse_custom_duration(duration_str)
        
        if result[0] is None and result[1].startswith("Invalid") or result[1].startswith("Unknown"):
            # Error occurred
            error_embed = discord.Embed(
                title="❌ Invalid Duration",
                description=result[1],
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
            error_embed.add_field(
                name="Examples",
                value="5m, 1 hour, 30 days, 2 weeks, 3 months, 1 year, forever",
                inline=False
            )
            await interaction.response.send_message(embed=error_embed, ephemeral=True)
            return
        
        # Valid duration
        duration_seconds = result[0]
        formatted_duration = result[1]
        
        # Apply the ban using parent view's data
        await self.parent_view.apply_ban(
            interaction, 
            duration_seconds, 
            formatted_duration
        )

class DurationSelector(discord.ui.View):
    """View for selecting ban duration"""
    
    def __init__(self, user_id: int, reason: str, commands: set):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.reason = reason
        self.commands = commands
        
        # Create duration select menu (max 25 options, we have 21 + custom)
        self.duration_select = discord.ui.Select(
            placeholder="Select ban duration...",
            min_values=1,
            max_values=1,
            options=self._create_duration_options()
        )
        self.duration_select.callback = self.duration_callback
        self.add_item(self.duration_select)
        
        # Add custom duration button
        custom_button = discord.ui.Button(label="Custom Duration", style=discord.ButtonStyle.blurple)
        custom_button.callback = self.custom_duration_callback
        self.add_item(custom_button)
    
    def _create_duration_options(self):
        """Create duration select options"""
        options = []
        
        # Order as specified
        order = ["5min", "15min", "30min", "45min", "1h", "2h", "5h", "12h", 
                 "1d", "2d", "3d", "5d", "1w", "2w", "3w", "1m", "2m", "3m", "6m", "forever"]
        
        for duration_key in order:
            options.append(
                discord.SelectOption(
                    label=duration_key,
                    value=duration_key,
                    description=f"Ban for {duration_key}"
                )
            )
        
        return options
    
    async def custom_duration_callback(self, interaction: discord.Interaction):
        """Show modal for custom duration input"""
        modal = CustomDurationModal(self)
        await interaction.response.send_modal(modal)
    
    async def apply_ban(self, interaction: discord.Interaction, duration_seconds, duration_key: str):
        """Apply the ban with given duration"""
        # Calculate unban time
        ban_time = datetime.now(timezone.utc)
        if duration_seconds is None:
            unban_time = None
            unban_time_str = None
        else:
            unban_time = ban_time + timedelta(seconds=duration_seconds)
            unban_time_str = unban_time.isoformat()
        
        # Save ban to database
        bans = load_bans()
        user_id_str = str(self.user_id)
        
        bans[user_id_str] = {
            "user_id": self.user_id,
            "banned_commands": list(self.commands),
            "reason": self.reason or "",
            "ban_time": ban_time.isoformat(),
            "unban_time": unban_time_str
        }
        
        save_bans(bans)
        
        # Create success embed
        if "All" in self.commands:
            commands_text = "**All commands**"
        else:
            commands_text = ', '.join(sorted(self.commands))
        
        if unban_time:
            duration_text = f"{duration_key} (until <t:{int(unban_time.timestamp())}:F>)"
        else:
            duration_text = "Forever"
        
        embed = discord.Embed(
            title="✅ User Banned",
            description=f"Successfully banned <@{self.user_id}>",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="User ID", value=str(self.user_id), inline=True)
        embed.add_field(name="Duration", value=duration_text, inline=True)
        embed.add_field(name="Commands", value=commands_text, inline=False)
        if self.reason:
            embed.add_field(name="Reason", value=self.reason, inline=False)
        
        # Check if this is from modal (already responded) or from select menu
        if interaction.response.is_done():
            # From modal - edit the original message
            await interaction.message.edit(embed=embed, view=None)
        else:
            # From select menu - edit as normal
            await interaction.response.edit_message(embed=embed, view=None)
        
        print(f"[BAN] User {self.user_id} banned from {commands_text} for {duration_key}")
    
    async def duration_callback(self, interaction: discord.Interaction):
        """Handle preset duration selection"""
        duration_key = self.duration_select.values[0]
        duration_seconds = DURATION_OPTIONS[duration_key]
        await self.apply_ban(interaction, duration_seconds, duration_key)

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    @bot.tree.command(
        name="ban",
        description="Ban a user from using the bot or specific commands"
    )
    @app_commands.describe(
        user="The user to ban",
        reason="The reason for the ban (optional)"
    )
    async def ban_user(
        interaction: discord.Interaction,
        user: discord.User,
        reason: str = None
    ):
        """Ban a user from using specific commands"""
        
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
        
        if user.id == interaction.user.id:
            error_embed = discord.Embed(
                title="❌ Cannot Ban Self",
                description=f"You cannot ban yourself.",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
            await interaction.response.send_message(embed=error_embed, ephemeral=True)
            return
        
        # Cannot ban the bot owner
        if user.id == OWNER_ID:
            error_embed = discord.Embed(
                title="❌ Cannot Ban Owner",
                description="You cannot ban the bot owner.",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
            await interaction.response.send_message(embed=error_embed, ephemeral=True)
            return
        
        # Admin cannot ban another admin
        if isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator:
            target_member = interaction.guild.get_member(user.id) if interaction.guild else None
            if target_member and target_member.guild_permissions.administrator:
                error_embed = discord.Embed(
                    title="❌ Cannot Ban Admin",
                    description="An admin cannot ban another admin.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc)
                )
                await interaction.response.send_message(embed=error_embed, ephemeral=True)
                return
        
        # Create command selector view
        view = CommandSelector(user.id, reason, bot)
        
        embed = discord.Embed(
            title="Ban User - Select Commands",
            description=f"**User:** {user.mention}\n**Reason:** {reason or 'No reason provided'}\n\nSelect which commands to ban this user from:",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    @bot.tree.command(
        name="ban_remove",
        description="Remove a ban from a user"
    )
    @app_commands.describe(
        user="The user to unban"
    )
    async def ban_remove(
        interaction: discord.Interaction,
        user: discord.User
    ):
        """Remove a ban from a user"""
        
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
        
        # Check if user is banned
        bans = load_bans()
        user_id_str = str(user.id)
        
        if user_id_str not in bans:
            embed = discord.Embed(
                title="❌ Not Banned",
                description=f"{user.mention} is not currently banned.",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        # Remove the ban
        ban_info = bans[user_id_str]
        remove_ban(user.id)
        
        # Format commands text with length limit
        banned_commands = ban_info["banned_commands"]
        if "All" in banned_commands:
            commands_text = "**All commands**"
        elif banned_commands:
            commands_text = ', '.join(banned_commands)
            # Truncate if too long (Discord limit is 1024 chars per field)
            if len(commands_text) > 1000:
                commands_text = commands_text[:997] + "..."
        else:
            commands_text = "None"
        
        embed = discord.Embed(
            title="✅ User Unbanned",
            description=f"Successfully unbanned {user.mention}",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="User ID", value=str(user.id), inline=True)
        embed.add_field(name="Was Banned From", value=commands_text, inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        print(f"[UNBAN] User {user.id} unbanned")
    
    @bot.tree.command(
        name="ban_check",
        description="Check if a user is banned"
    )
    @app_commands.describe(
        user="The user to check"
    )
    async def ban_check(
        interaction: discord.Interaction,
        user: discord.User
    ):
        """Check if a user is banned"""
        
        bans = load_bans()
        user_id_str = str(user.id)
        ban_info = bans.get(user_id_str)
        
        if not ban_info:
            embed = discord.Embed(
                title="✅ Not Banned",
                description=f"{user.mention} is not currently banned.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        # Format commands text with length limit
        banned_commands = ban_info["banned_commands"]
        if "All" in banned_commands:
            commands_text = "**All commands**"
        elif banned_commands:
            commands_text = ', '.join(banned_commands)
            # Truncate if too long (Discord limit is 1024 chars per field)
            if len(commands_text) > 1000:
                commands_text = commands_text[:997] + "..."
        else:
            commands_text = "None"
        
        embed = discord.Embed(
            title="🚫 User is Banned",
            description=f"{user.mention} is currently banned",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="User ID", value=str(user.id), inline=True)
        embed.add_field(name="Banned Commands", value=commands_text, inline=False)
        
        if ban_info.get("reason"):
            embed.add_field(name="Reason", value=ban_info["reason"], inline=False)
        
        ban_time = datetime.fromisoformat(ban_info["ban_time"])
        embed.add_field(name="Banned At", value=f"<t:{int(ban_time.timestamp())}:F>", inline=True)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    print("[OK] Loaded ban_user command")
