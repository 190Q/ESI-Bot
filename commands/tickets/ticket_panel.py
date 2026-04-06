import discord
from discord import app_commands
from discord.ui import Select, Button, View, Modal, TextInput
import json
import os
from pathlib import Path
from datetime import datetime
from typing import List
from utils.permissions import has_roles

REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0
]
PANEL_REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0,
    600185623474601995 # Parliament
]

# Add this autocomplete function
async def panel_autocomplete_toggle(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    """Autocomplete function for panel selection in toggle command"""
    panels = load_panels()
    choices = []
    
    for panel_id, panel_data in panels.items():
        app_count = len(panel_data.get('applications', []))
        title = panel_data.get('title', 'Unknown')
        
        name = f"{title} (ID: {panel_id[:8]}..., {app_count} apps)"
        if len(name) > 100:
            name = name[:97] + "..."
        
        if current.lower() in name.lower() or current.lower() in panel_id.lower():
            choices.append(app_commands.Choice(name=name, value=panel_id))
    
    return choices[:25]

# Modal for title and description input
class PanelInfoModal(Modal, title="Set Panel Info"):
    title_input = TextInput(
        label="Title",
        placeholder="Enter the panel title...",
        max_length=256,
        required=True
    )
    
    description_input = TextInput(
        label="Description",
        placeholder="Enter the panel description...",
        style=discord.TextStyle.paragraph,
        max_length=4000,
        required=True
    )
    
    color_input = TextInput(
        label="Color (Hex Code)",
        placeholder="e.g., #5865F2 or 5865F2",
        max_length=7,
        required=True,
        default="#5865F2"
    )
    
    def __init__(self, setup_view):
        super().__init__()
        self.setup_view = setup_view
    
    async def on_submit(self, interaction: discord.Interaction):
        self.setup_view.panel_title = self.title_input.value
        self.setup_view.panel_description = self.description_input.value
        
        # Parse color
        color_hex = self.color_input.value.strip().replace('#', '')
        try:
            self.setup_view.panel_color = int(color_hex, 16)
        except ValueError:
            self.setup_view.panel_color = 0x5865F2
        
        # Go directly to application manager
        await self.setup_view.show_application_manager(interaction, edit=False)

# Modal for color input
class ColorModal(Modal, title="Set Panel Color"):
    color_input = TextInput(
        label="Color (Hex Code)",
        placeholder="e.g., #5865F2 or 5865F2",
        max_length=7,
        required=True,
        default="#5865F2"
    )
    
    def __init__(self, setup_view):
        super().__init__()
        self.setup_view = setup_view
    
    async def on_submit(self, interaction: discord.Interaction):
        color_hex = self.color_input.value.strip().replace('#', '')
        try:
            self.setup_view.panel_color = int(color_hex, 16)
        except ValueError:
            await interaction.response.send_message("❌ Invalid hex color! Using default color.", ephemeral=True)
            self.setup_view.panel_color = 0x5865F2
            return
        
        embed = discord.Embed(
            title="✅ Color Set!",
            description=f"**Color:** #{color_hex}\n\nClick the button below to manage applications.",
            color=self.setup_view.panel_color
        )
        
        view = View(timeout=None)
        continue_button = Button(label="📋 Manage Applications", style=discord.ButtonStyle.primary)
        
        async def continue_callback(btn_interaction: discord.Interaction):
            await self.setup_view.show_application_manager(btn_interaction, edit=False)
        
        continue_button.callback = continue_callback
        view.add_item(continue_button)
        
        await interaction.response.edit_message(embed=embed, view=view)

class LoggingChannelView(View):
    def __init__(self, setup_view):
        super().__init__(timeout=None)
        self.setup_view = setup_view
        
        # Create logging channel selector
        log_select = Select(
            placeholder="Select a logging channel",
            options=[
                discord.SelectOption(
                    label=f"#{channel.name}",
                    value=str(channel.id),
                    description=f"ID: {channel.id}"
                )
                for channel in setup_view.original_interaction.guild.text_channels[:25]
            ]
        )
        log_select.callback = self.log_selected
        self.add_item(log_select)
        
        # Add manual input button
        manual_button = Button(label="📝 Enter Channel ID Manually", style=discord.ButtonStyle.secondary)
        manual_button.callback = self.manual_logging_input
        self.add_item(manual_button)
    
    async def log_selected(self, interaction: discord.Interaction):
        self.setup_view.logging_channel_id = int(interaction.data['values'][0])
        await self.setup_view.finalize_setup(interaction)
    
    async def manual_logging_input(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ManualLoggingModal(self.setup_view))

# Modal for adding application types with customization
class AddApplicationModal(Modal, title="Add Application Type"):
    app_name = TextInput(
        label="Application Name",
        placeholder="e.g., Support, Partnership, Report",
        max_length=80,
        required=True
    )
    
    app_emoji = TextInput(
        label="Button Emoji (Optional)",
        placeholder="e.g., 🎫 or leave empty",
        max_length=10,
        required=False
    )
    
    app_color = TextInput(
        label="Button Color",
        placeholder="blurple, grey, green, or red",
        max_length=10,
        required=True,
        default="blurple"
    )
    
    def __init__(self, setup_view):
        super().__init__()
        self.setup_view = setup_view
    
    async def on_submit(self, interaction: discord.Interaction):
        if len(self.setup_view.applications) >= 5:
            await interaction.response.send_message("Maximum of 5 applications reached!", ephemeral=True)
            return
        
        # Map color names to ButtonStyle
        color_map = {
            "blurple": discord.ButtonStyle.primary,
            "grey": discord.ButtonStyle.secondary,
            "gray": discord.ButtonStyle.secondary,
            "green": discord.ButtonStyle.success,
            "red": discord.ButtonStyle.danger
        }
        
        color_input = self.app_color.value.lower().strip()
        button_style = color_map.get(color_input, discord.ButtonStyle.primary)
        
        app_data = {
            "name": self.app_name.value,
            "emoji": self.app_emoji.value.strip() if self.app_emoji.value else None,
            "style": button_style
        }
        
        self.setup_view.applications.append(app_data)
        await self.setup_view.show_application_manager(interaction, edit=False)

# Modal for entering channel ID
# Modal for entering channel ID
class ManualChannelModal(Modal, title="Enter Channel ID"):
    channel_id_input = TextInput(
        label="Channel ID",
        placeholder="Right-click channel → Copy ID",
        max_length=20,
        required=True
    )
    
    def __init__(self, setup_view):
        super().__init__()
        self.setup_view = setup_view
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            channel_id = int(self.channel_id_input.value.strip())
            channel = interaction.guild.get_channel(channel_id)
            
            if channel is None:
                await interaction.response.send_message("❌ Channel not found! Make sure the ID is correct.", ephemeral=True)
                return
            
            if not isinstance(channel, discord.TextChannel):
                await interaction.response.send_message("❌ That's not a text channel!", ephemeral=True)
                return
            
            self.setup_view.channel = channel
            
            # Instead of sending a modal, create a button to open the modal
            embed = discord.Embed(
                title="✅ Channel Set!",
                description=f"**Channel:** {channel.mention}\n\nClick the button below to set panel information.",
                color=0x00FF00
            )
            
            view = View(timeout=None)
            panel_info_button = Button(label="📝 Set Panel Info", style=discord.ButtonStyle.primary)
            
            async def panel_info_callback(btn_interaction: discord.Interaction):
                await btn_interaction.response.send_modal(PanelInfoModal(self.setup_view))
            
            panel_info_button.callback = panel_info_callback
            view.add_item(panel_info_button)
            
            await interaction.response.edit_message(embed=embed, view=view)
            
        except ValueError:
            await interaction.response.send_message("❌ Invalid channel ID! Please enter numbers only.", ephemeral=True)

# Modal for entering category ID
class ManualCategoryModal(Modal, title="Enter Category ID"):
    category_id_input = TextInput(
        label="Category ID",
        placeholder="Right-click category → Copy ID",
        max_length=20,
        required=True
    )
    
    def __init__(self, setup_view):
        super().__init__()
        self.setup_view = setup_view
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            category_id = int(self.category_id_input.value.strip())
            category = interaction.guild.get_channel(category_id)
            
            if category is None:
                await interaction.response.send_message("❌ Category not found! Make sure the ID is correct.", ephemeral=True)
                return
            
            if not isinstance(category, discord.CategoryChannel):
                await interaction.response.send_message("❌ That's not a category!", ephemeral=True)
                return
            
            self.setup_view.ticket_category_id = category_id
            
            # Show logging channel selector
            embed = discord.Embed(
                title="✅ Category Set!",
                description=f"Tickets will be created in **{category.name}**\n\nNow select the logging channel.",
                color=0x00FF00
            )
            
            view = LoggingChannelView(self.setup_view)
            await interaction.response.edit_message(embed=embed, view=view)
            
        except ValueError:
            await interaction.response.send_message("❌ Invalid category ID! Please enter numbers only.", ephemeral=True)

# Modal for entering logging channel ID
class ManualLoggingModal(Modal, title="Enter Logging Channel ID"):
    channel_id_input = TextInput(
        label="Channel ID",
        placeholder="Right-click channel → Copy ID",
        max_length=20,
        required=True
    )
    
    def __init__(self, setup_view):
        super().__init__()
        self.setup_view = setup_view
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            channel_id = int(self.channel_id_input.value.strip())
            channel = interaction.guild.get_channel(channel_id)
            
            if channel is None:
                await interaction.response.send_message("❌ Channel not found! Make sure the ID is correct.", ephemeral=True)
                return
            
            if not isinstance(channel, discord.TextChannel):
                await interaction.response.send_message("❌ That's not a text channel!", ephemeral=True)
                return
            
            self.setup_view.logging_channel_id = channel_id
            await self.setup_view.finalize_setup(interaction)
            
        except ValueError:
            await interaction.response.send_message("❌ Invalid channel ID! Please enter numbers only.", ephemeral=True)

# View for managing applications
class ApplicationManagerView(View):
    def __init__(self, setup_view):
        super().__init__(timeout=None)
        self.setup_view = setup_view
        self.update_buttons()
    
    def update_buttons(self):
        self.clear_items()
        
        # Add button
        if len(self.setup_view.applications) < 5:
            add_button = Button(label="➕ Add Application", style=discord.ButtonStyle.green)
            add_button.callback = self.add_application
            self.add_item(add_button)
        
        # Remove button (with select)
        if self.setup_view.applications:
            remove_select = Select(
                placeholder="Select application to remove",
                options=[
                    discord.SelectOption(label=app['name'], value=str(i))  # Changed from app to app['name']
                    for i, app in enumerate(self.setup_view.applications)
                ]
            )
            remove_select.callback = self.remove_application
            self.add_item(remove_select)
        
        # Finish button
        finish_button = Button(label="✅ Confirm Applications", style=discord.ButtonStyle.primary)
        finish_button.callback = self.finish_setup
        self.add_item(finish_button)
    
    async def add_application(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddApplicationModal(self.setup_view))
    
    async def remove_application(self, interaction: discord.Interaction):
        index = int(interaction.data['values'][0])
        removed = self.setup_view.applications.pop(index)
        
        # Show updated applications
        embed = discord.Embed(
            title="📋 Manage Application Types",
            description=f"**Current applications ({len(self.setup_view.applications)}/5):**\n" + 
                    ("\n".join([f"• {app['name']}" for app in self.setup_view.applications]) if self.setup_view.applications else "*No applications added yet*"),
            color=0x00FF00
        )
        
        self.update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def finish_setup(self, interaction: discord.Interaction):
        # Instead of creating panel directly, go to channel config
        embed = discord.Embed(
            title="✅ Applications Configured!",
            description=f"**{len(self.setup_view.applications)} application(s) added.**\n\nNow select the category where ticket channels will be created.",
            color=0x00FF00
        )
        
        view = ChannelConfigView(self.setup_view)
        await interaction.response.edit_message(embed=embed, view=view)

# View for selecting category and logging channel
class ChannelConfigView(View):
    def __init__(self, setup_view):
        super().__init__(timeout=None)
        self.setup_view = setup_view
        
        # Category selector
        self.category_select = Select(
            placeholder="Select a category for ticket channels",
            options=[
                discord.SelectOption(
                    label=category.name,
                    value=str(category.id),
                    description=f"ID: {category.id}"
                )
                for category in setup_view.original_interaction.guild.categories[:25]
            ]
        )
        self.category_select.callback = self.category_selected
        self.add_item(self.category_select)

        # Add manual input button
        manual_button = Button(label="📝 Enter Category ID Manually", style=discord.ButtonStyle.secondary)
        manual_button.callback = self.manual_category_input
        self.add_item(manual_button)
    
    async def category_selected(self, interaction: discord.Interaction):
        self.setup_view.ticket_category_id = int(interaction.data['values'][0])
        
        # Now show logging channel selector
        embed = discord.Embed(
            title="✅ Category Set!",
            description=f"Tickets will be created in <#{self.setup_view.ticket_category_id}>\n\nNow select the logging channel.",
            color=0x00FF00
        )
        
        # Create logging channel selector
        log_select = Select(
            placeholder="Select a logging channel",
            options=[
                discord.SelectOption(
                    label=f"#{channel.name}",
                    value=str(channel.id),
                    description=f"ID: {channel.id}"
                )
                for channel in interaction.guild.text_channels[:25]
            ]
        )
        
        async def log_selected(log_interaction: discord.Interaction):
            self.setup_view.logging_channel_id = int(log_interaction.data['values'][0])
            await self.setup_view.finalize_setup(log_interaction)
        
        log_select.callback = log_selected

        view = View(timeout=None)
        view.add_item(log_select)

        # Add manual input button
        manual_log_button = Button(label="📝 Enter Channel ID Manually", style=discord.ButtonStyle.secondary)

        async def manual_log_callback(btn_interaction: discord.Interaction):
            await btn_interaction.response.send_modal(ManualLoggingModal(self.setup_view))

        manual_log_button.callback = manual_log_callback
        view.add_item(manual_log_button)

        await interaction.response.edit_message(embed=embed, view=view)
    
    async def manual_category_input(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ManualCategoryModal(self.setup_view))

class ForwardingChannelView(View):
    def __init__(self, setup_view):
        super().__init__(timeout=None)
        self.setup_view = setup_view
        
        # Create forwarding channel selector
        forward_select = Select(
            placeholder="Select a forwarding channel",
            options=[
                discord.SelectOption(
                    label=f"#{channel.name}",
                    value=str(channel.id),
                    description=f"ID: {channel.id}"
                )
                for channel in setup_view.original_interaction.guild.text_channels[:25]
            ]
        )
        forward_select.callback = self.forward_selected
        self.add_item(forward_select)
        
        # Add manual input button
        manual_button = Button(label="📝 Enter Channel ID Manually", style=discord.ButtonStyle.secondary)
        manual_button.callback = self.manual_forwarding_input
        self.add_item(manual_button)
    
    async def forward_selected(self, interaction: discord.Interaction):
        self.setup_view.forwarding_channel_id = int(interaction.data['values'][0])
        await self.setup_view.complete_setup(interaction)
    
    async def manual_forwarding_input(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ManualForwardingModal(self.setup_view))

class ManualForwardingModal(Modal, title="Enter Forwarding Channel ID"):
    channel_id_input = TextInput(
        label="Channel ID",
        placeholder="Right-click channel → Copy ID",
        max_length=20,
        required=True
    )
    
    def __init__(self, setup_view):
        super().__init__()
        self.setup_view = setup_view
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            channel_id = int(self.channel_id_input.value.strip())
            channel = interaction.guild.get_channel(channel_id)
            
            if channel is None:
                await interaction.response.send_message("❌ Channel not found! Make sure the ID is correct.", ephemeral=True)
                return
            
            if not isinstance(channel, discord.TextChannel):
                await interaction.response.send_message("❌ That's not a text channel!", ephemeral=True)
                return
            
            self.setup_view.forwarding_channel_id = channel_id
            await self.setup_view.complete_setup(interaction)
            
        except ValueError:
            await interaction.response.send_message("❌ Invalid channel ID! Please enter numbers only.", ephemeral=True)

# Main setup view
class TicketSetupView(View):
    def __init__(self, interaction: discord.Interaction):
        super().__init__(timeout=None)
        self.original_interaction = interaction
        self.channel = None
        self.panel_title = None
        self.panel_description = None
        self.panel_color = 0x5865F2
        self.applications = []
        self.ticket_category_id = None
        self.logging_channel_id = None
        self.forwarding_channel_id = None
        
        # Add channel selector
        self.channel_select = Select(
            placeholder="Select a channel for the ticket panel",
            options=[
                discord.SelectOption(
                    label=f"#{channel.name}",
                    value=str(channel.id),
                    description=f"ID: {channel.id}"
                )
                for channel in interaction.guild.text_channels[:25]  # Discord limit
            ]
        )
        self.channel_select.callback = self.channel_selected
        self.add_item(self.channel_select)

        # Add manual input button
        manual_button = Button(label="📝 Enter Channel ID Manually", style=discord.ButtonStyle.secondary)
        manual_button.callback = self.manual_channel_input
        self.add_item(manual_button)
    
    # Update the channel_selected method in TicketSetupView
    async def channel_selected(self, interaction: discord.Interaction):
        self.channel = interaction.guild.get_channel(int(interaction.data['values'][0]))
        await interaction.response.send_modal(PanelInfoModal(self))  # Changed from TitleModal

    async def manual_channel_input(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ManualChannelModal(self))
    
    async def manual_category_input(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ManualCategoryModal(self))
    
    async def show_application_manager(self, interaction: discord.Interaction, edit=False):
        embed = discord.Embed(
            title="📋 Manage Application Types",
            description=f"**Current applications ({len(self.applications)}/5):**\n" + 
                    ("\n".join([f"• {app['name']}" for app in self.applications]) if self.applications else "*No applications added yet*"),
            color=0x00FF00
        )
        
        view = ApplicationManagerView(self)
        
        if edit:
            await interaction.message.edit(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)
    
    # Add this new method to TicketSetupView class (before or after create_panel)
    async def finalize_setup(self, interaction: discord.Interaction):
        """Called after logging channel is configured"""
        embed = discord.Embed(
            title="✅ Logging Channel Set!",
            description=f"**Logging Channel:** <#{self.logging_channel_id}>\n\nNow select the channel where applications will be forwarded.",
            color=0x00FF00
        )
        
        view = ForwardingChannelView(self)
        await interaction.response.edit_message(embed=embed, view=view)
    
    async def complete_setup(self, interaction: discord.Interaction):
        """Called after all configuration including forwarding channel is complete"""
        embed = discord.Embed(
            title="✅ Configuration Complete!",
            description=f"**Category:** <#{self.ticket_category_id}>\n**Logging Channel:** <#{self.logging_channel_id}>\n**Forwarding Channel:** <#{self.forwarding_channel_id}>\n\nCreating panel...",
            color=0x00FF00
        )
        await interaction.response.edit_message(embed=embed, view=None)
        
        # Now create the panel
        await self.create_panel(interaction)

    async def create_panel(self, interaction: discord.Interaction):
        # Import the create_application_channel function
        import importlib.util
        import sys

        # Load the module dynamically
        spec = importlib.util.spec_from_file_location("ticket_handler", Path(__file__).parent / "ticket_handler.py")
        ticket_handler = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ticket_handler)
        create_application_channel = ticket_handler.create_application_channel
        
        # Create the ticket panel embed
        panel_embed = discord.Embed(
            title=self.panel_title,
            description=self.panel_description,
            color=self.panel_color
        )
        
        # Create buttons for each application type with ACTUAL functionality
        class TicketPanelView(View):
            def __init__(self, applications, panel_data):
                super().__init__(timeout=None)
                self.panel_data = panel_data
                
                for app in applications:
                    button = Button(
                        label=app['name'],
                        style=app['style'],
                        emoji=app['emoji'] if app['emoji'] else None,
                        custom_id=f"ticket_{app['name'].lower().replace(' ', '_')}"
                    )
                    
                    # Create callback with proper scope (same as application_handler.py)
                    def create_callback(app_name):
                        async def callback(btn_interaction: discord.Interaction):
                            await create_application_channel(btn_interaction, app_name, self.panel_data)
                        return callback
                    
                    button.callback = create_callback(app['name'])
                    self.add_item(button)
        
        # Prepare panel data BEFORE sending the message (so it can be passed to the view)
        panel_data = {
            "channel_id": self.channel.id,
            "guild_id": interaction.guild.id,
            "created_at": datetime.utcnow().isoformat(),
            "created_by": interaction.user.id,
            "title": self.panel_title,
            "description": self.panel_description,
            "color": self.panel_color,
            "ticket_category_id": self.ticket_category_id,
            "logging_channel_id": self.logging_channel_id,
            "forwarding_channel_id": self.forwarding_channel_id,
            "applications": [
                {
                    "name": app['name'],
                    "emoji": app['emoji'],
                    "style": app['style'].value
                }
                for app in self.applications
            ],
            "permissions": {},  # Initialize empty permissions
            "settings": {}  # Initialize empty settings
        }
        
        # Send panel to selected channel
        try:
            panel_message = await self.channel.send(
                embed=panel_embed, 
                view=None
            )
            
            # Load existing panels
            panels = load_panels()
            
            # Add message_id to panel_data
            panel_data["message_id"] = panel_message.id
            
            # Use message ID as key
            panels[str(panel_message.id)] = panel_data
            
            # Save to JSON
            save_panels(panels)
            
            success_embed = discord.Embed(
                title="✅ Ticket Panel Created!",
                description=f"Panel created in {self.channel.mention}\n\n"
                        f"**Title:** {self.panel_title}\n"
                        f"**Applications:** {', '.join([app['name'] for app in self.applications])}\n"
                        f"**Ticket Category:** <#{self.ticket_category_id}>\n"
                        f"**Logging Channel:** <#{self.logging_channel_id}>\n"
                        f"**Forwarding Channel:** <#{self.forwarding_channel_id}>\n"
                        f"**Message ID:** `{panel_message.id}`\n\n"
                        f"⚠️ **Panel is disabled by default.** Use `/toggle_panel panel_id:{panel_message.id} enabled:True` to enable it.\n"
                        f"💡 Use `/setup_applications panel_id:{panel_message.id}` to configure permissions for each application type.",
                color=0x00FF00
            )
            
            # Edit the message that was already responded to in finalize_setup
            await interaction.edit_original_response(embed=success_embed, view=None)
        except Exception as e:
            await interaction.edit_original_response(
                content=f"❌ Error creating panel: {str(e)}",
                embed=None,
                view=None
            )

def load_panels():
    """Load ticket panels from JSON file"""
    panels_file = Path(__file__).resolve().parent.parent.parent / 'data' / 'ticket_panels.json'
    if panels_file.exists():
        with open(panels_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_panels(panels):
    """Save ticket panels to JSON file"""
    panels_file = Path(__file__).resolve().parent.parent.parent / 'data' / 'ticket_panels.json'
    with open(panels_file, 'w', encoding='utf-8') as f:
        json.dump(panels, f, indent=4, ensure_ascii=False)

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    @bot.tree.command(
        name="panel_setup",
        description="Setup the ticket panel"
    )
    async def setup_command(interaction: discord.Interaction):
        """Setup ticket panel command"""

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
        
        # Start the setup process
        setup_embed = discord.Embed(
            title="🎫 Ticket Panel Setup",
            description="Let's set up your ticket panel! First, select the channel where the panel should be posted.",
            color=0x5865F2
        )
        
        view = TicketSetupView(interaction)
        await interaction.response.send_message(embed=setup_embed, view=view, ephemeral=True)

    # Replace the entire toggle_panel_command with this:
    @bot.tree.command(
        name="panel_toggle",
        description="Enable or disable a ticket panel"
    )
    @app_commands.describe(
        panel_id="Select the ticket panel to toggle",
        enabled="True to enable, False to disable"
    )
    @app_commands.autocomplete(panel_id=panel_autocomplete_toggle)
    async def toggle_panel_command(interaction: discord.Interaction, panel_id: str, enabled: bool):
        """Toggle a ticket panel on/off"""
        
        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return
        
        panels = load_panels()
        
        if panel_id not in panels:
            await interaction.response.send_message(
                f"❌ Panel with ID `{panel_id}` not found!",
                ephemeral=True
            )
            return
        
        panel_data = panels[panel_id]
        
        try:
            channel = interaction.guild.get_channel(panel_data['channel_id'])
            if not channel:
                await interaction.response.send_message(
                    "❌ Panel channel not found!",
                    ephemeral=True
                )
                return
            
            message = await channel.fetch_message(int(panel_id))
            
            if enabled:
                import importlib.util
                import sys
                
                spec = importlib.util.spec_from_file_location("ticket_handler", Path(__file__).parent / "ticket_handler.py")
                ticket_handler = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(ticket_handler)
                create_application_channel = ticket_handler.create_application_channel
                
                class TicketPanelView(View):
                    def __init__(self, applications, panel_data):
                        super().__init__(timeout=None)
                        self.panel_data = panel_data
                        
                        for app in applications:
                            button = Button(
                                label=app['name'],
                                style=discord.ButtonStyle(app['style']),
                                emoji=app['emoji'] if app['emoji'] else None,
                                custom_id=f"ticket_{app['name'].lower().replace(' ', '_')}"
                            )
                            
                            def create_callback(app_name):
                                async def callback(btn_interaction: discord.Interaction):
                                    await create_application_channel(btn_interaction, app_name, self.panel_data)
                                return callback
                            
                            button.callback = create_callback(app['name'])
                            self.add_item(button)
                
                view = TicketPanelView(panel_data['applications'], panel_data)
                await message.edit(view=view)
                
                status_embed = discord.Embed(
                    title="✅ Panel Enabled",
                    description=f"Panel `{panel_id}` has been enabled in {channel.mention}",
                    color=0x00FF00,
                    timestamp=datetime.utcnow()
                )
            else:
                await message.edit(view=None)
                
                status_embed = discord.Embed(
                    title="🔒 Panel Disabled",
                    description=f"Panel `{panel_id}` has been disabled in {channel.mention}",
                    color=0xFF0000,
                    timestamp=datetime.utcnow()
                )
            
            await interaction.response.send_message(embed=status_embed, ephemeral=True)
            
        except discord.NotFound:
            await interaction.response.send_message(
                "❌ Panel message not found! It may have been deleted.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Error toggling panel: {str(e)}",
                ephemeral=True
            )
    
    @bot.tree.command(
        name="panel_move",
        description="Move a ticket panel to a different channel"
    )
    async def move_panel_command(interaction: discord.Interaction):
        """Move a ticket panel to a different channel"""
        
        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return
        
        panels = load_panels()
        
        if not panels:
            await interaction.response.send_message(
                "❌ No panels found! Create a panel first using `/setup_panel`.",
                ephemeral=True
            )
            return
        
        embed = discord.Embed(
            title="📦 Move Panel",
            description="Select the panel you want to move to a different channel.",
            color=0x5865F2
        )
        
        panel_options = []
        for panel_id, panel_data in panels.items():
            app_count = len(panel_data.get('applications', []))
            title = panel_data.get('title', 'Unknown')
            
            name = f"{title} (ID: {panel_id[:8]}..., {app_count} apps)"
            if len(name) > 100:
                name = name[:97] + "..."
            
            panel_options.append(discord.SelectOption(
                label=name,
                value=panel_id,
                description=f"Panel ID: {panel_id[:20]}..."
            ))
        
        panel_select = Select(
            placeholder="Select a panel to move",
            options=panel_options[:25]
        )
        
        async def panel_selected(select_interaction: discord.Interaction):
            selected_panel_id = select_interaction.data['values'][0]
            panel_data = panels[selected_panel_id]
            
            channel_embed = discord.Embed(
                title="📍 Select Target Channel",
                description=f"Select the channel where you want to move the panel.\n\n**Panel:** {panel_data.get('title', 'Unknown')}",
                color=0x5865F2
            )
            
            channel_options = []
            for channel in interaction.guild.text_channels[:25]:
                channel_options.append(discord.SelectOption(
                    label=f"#{channel.name}",
                    value=str(channel.id),
                    description=f"ID: {channel.id}"
                ))
            
            channel_select = Select(
                placeholder="Select target channel",
                options=channel_options
            )
            
            async def channel_selected(channel_interaction: discord.Interaction):
                target_channel_id = int(channel_interaction.data['values'][0])
                target_channel = interaction.guild.get_channel(target_channel_id)
                
                try:
                    old_channel = interaction.guild.get_channel(panel_data['channel_id'])
                    if not old_channel:
                        await channel_interaction.response.send_message(
                            "❌ Original panel channel not found!",
                            ephemeral=True
                        )
                        return
                    
                    old_message = await old_channel.fetch_message(int(selected_panel_id))
                    
                    embed = discord.Embed(
                        title=panel_data['title'],
                        description=panel_data['description'],
                        color=panel_data['color'],
                    )
                    
                    import importlib.util
                    import sys
                    
                    spec = importlib.util.spec_from_file_location("ticket_handler", Path(__file__).parent / "ticket_handler.py")
                    ticket_handler = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(ticket_handler)
                    create_application_channel = ticket_handler.create_application_channel
                    
                    class TicketPanelView(View):
                        def __init__(self, applications, panel_data):
                            super().__init__(timeout=None)
                            self.panel_data = panel_data
                            
                            for app in applications:
                                button = Button(
                                    label=app['name'],
                                    style=discord.ButtonStyle(app['style']),
                                    emoji=app['emoji'] if app['emoji'] else None,
                                    custom_id=f"ticket_{app['name'].lower().replace(' ', '_')}"
                                )
                                
                                def create_callback(app_name):
                                    async def callback(btn_interaction: discord.Interaction):
                                        await create_application_channel(btn_interaction, app_name, self.panel_data)
                                    return callback
                                
                                button.callback = create_callback(app['name'])
                                self.add_item(button)
                    
                    view = TicketPanelView(panel_data['applications'], panel_data) if old_message.components else None
                    
                    new_message = await target_channel.send(embed=embed, view=view)
                    
                    await old_message.delete()
                    
                    del panels[selected_panel_id]
                    panel_data['channel_id'] = target_channel.id
                    panel_data['message_id'] = new_message.id
                    panels[str(new_message.id)] = panel_data
                    
                    save_panels(panels)
                    
                    success_embed = discord.Embed(
                        title="✅ Panel Moved",
                        description=f"Panel has been moved from {old_channel.mention} to {target_channel.mention}\n\n"
                                f"**New Message ID:** `{new_message.id}`",
                        color=0x00FF00,
                        timestamp=datetime.utcnow()
                    )
                    
                    await channel_interaction.response.send_message(embed=success_embed, ephemeral=True)
                    
                except discord.NotFound:
                    await channel_interaction.response.send_message(
                        "❌ Panel message not found! It may have been deleted.",
                        ephemeral=True
                    )
                except discord.Forbidden:
                    await channel_interaction.response.send_message(
                        "❌ I don't have permission to send messages in that channel or delete the original message!",
                        ephemeral=True
                    )
                except Exception as e:
                    await channel_interaction.response.send_message(
                        f"❌ Error moving panel: {str(e)}",
                        ephemeral=True
                    )
            
            channel_select.callback = channel_selected
            
            channel_view = View(timeout=None)
            channel_view.add_item(channel_select)
            
            manual_button = Button(label="📝 Enter Channel ID Manually", style=discord.ButtonStyle.secondary)
            
            async def manual_channel_callback(btn_interaction: discord.Interaction):
                class ManualChannelModal(Modal, title="Enter Channel ID"):
                    channel_id_input = TextInput(
                        label="Channel ID",
                        placeholder="Right-click channel → Copy ID",
                        max_length=20,
                        required=True
                    )
                    
                    def __init__(self, selected_panel_id, panel_data):
                        super().__init__()
                        self.selected_panel_id = selected_panel_id
                        self.panel_data = panel_data
                    
                    async def on_submit(self, modal_interaction: discord.Interaction):
                        try:
                            channel_id = int(self.channel_id_input.value.strip())
                            target_channel = modal_interaction.guild.get_channel(channel_id)
                            
                            if not target_channel:
                                await modal_interaction.response.send_message(
                                    "❌ Channel not found!",
                                    ephemeral=True
                                )
                                return
                            
                            if not isinstance(target_channel, discord.TextChannel):
                                await modal_interaction.response.send_message(
                                    "❌ That's not a text channel!",
                                    ephemeral=True
                                )
                                return
                            
                            old_channel = modal_interaction.guild.get_channel(self.panel_data['channel_id'])
                            if not old_channel:
                                await modal_interaction.response.send_message(
                                    "❌ Original panel channel not found!",
                                    ephemeral=True
                                )
                                return
                            
                            old_message = await old_channel.fetch_message(int(self.selected_panel_id))
                            
                            embed = discord.Embed(
                                title=self.panel_data['title'],
                                description=self.panel_data['description'],
                                color=self.panel_data['color']
                            )
                            
                            import importlib.util
                            import sys
                            
                            spec = importlib.util.spec_from_file_location("ticket_handler", Path(__file__).parent / "ticket_handler.py")
                            ticket_handler = importlib.util.module_from_spec(spec)
                            spec.loader.exec_module(ticket_handler)
                            create_application_channel = ticket_handler.create_application_channel
                            
                            class TicketPanelView(View):
                                def __init__(self, applications, panel_data):
                                    super().__init__(timeout=None)
                                    self.panel_data = panel_data
                                    
                                    for app in applications:
                                        button = Button(
                                            label=app['name'],
                                            style=discord.ButtonStyle(app['style']),
                                            emoji=app['emoji'] if app['emoji'] else None,
                                            custom_id=f"ticket_{app['name'].lower().replace(' ', '_')}"
                                        )
                                        
                                        def create_callback(app_name):
                                            async def callback(btn_interaction: discord.Interaction):
                                                await create_application_channel(btn_interaction, app_name, self.panel_data)
                                            return callback
                                        
                                        button.callback = create_callback(app['name'])
                                        self.add_item(button)
                            
                            view = TicketPanelView(self.panel_data['applications'], self.panel_data) if old_message.components else None
                            
                            new_message = await target_channel.send(embed=embed, view=view)
                            
                            await old_message.delete()
                            
                            del panels[self.selected_panel_id]
                            self.panel_data['channel_id'] = target_channel.id
                            self.panel_data['message_id'] = new_message.id
                            panels[str(new_message.id)] = self.panel_data
                            
                            save_panels(panels)
                            
                            success_embed = discord.Embed(
                                title="✅ Panel Moved",
                                description=f"Panel has been moved from {old_channel.mention} to {target_channel.mention}\n\n"
                                        f"**New Message ID:** `{new_message.id}`",
                                color=0x00FF00,
                                timestamp=datetime.utcnow()
                            )
                            
                            await modal_interaction.response.send_message(embed=success_embed, ephemeral=True)
                            
                        except ValueError:
                            await modal_interaction.response.send_message(
                                "❌ Invalid channel ID!",
                                ephemeral=True
                            )
                        except Exception as e:
                            await modal_interaction.response.send_message(
                                f"❌ Error: {str(e)}",
                                ephemeral=True
                            )
                
                await btn_interaction.response.send_modal(ManualChannelModal(selected_panel_id, panel_data))
            
            manual_button.callback = manual_channel_callback
            channel_view.add_item(manual_button)
            
            await select_interaction.response.edit_message(embed=channel_embed, view=channel_view)
        
        panel_select.callback = panel_selected
        
        view = View(timeout=None)
        view.add_item(panel_select)
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    @bot.tree.command(
        name="panel_debug",
        description="Debug panel registration status"
    )
    async def debug_panels(interaction: discord.Interaction):
        """Check which panels are registered and if their messages exist"""
        
        if not has_roles(interaction.user, PANEL_REQUIRED_ROLES) and PANEL_REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return
        
        panels = load_panels()
        
        debug_info = "**Panel Debug Information:**\n\n"
        
        for panel_id, panel_data in panels.items():
            debug_info += f"**Panel ID:** `{panel_id}`\n"
            debug_info += f"**Title:** {panel_data.get('title', 'Unknown')}\n"
            debug_info += f"**Channel ID:** {panel_data.get('channel_id')}\n"
            
            # Try to fetch the message
            try:
                channel = interaction.guild.get_channel(panel_data['channel_id'])
                if channel:
                    message = await channel.fetch_message(int(panel_id))
                    debug_info += f"✅ Message exists in {channel.mention}\n"
                    debug_info += f"Has components: {len(message.components) > 0}\n"
                else:
                    debug_info += f"❌ Channel not found\n"
            except discord.NotFound:
                debug_info += f"❌ Message not found (deleted)\n"
            except Exception as e:
                debug_info += f"❌ Error: {str(e)}\n"
            
            debug_info += "\n"
        
        await interaction.response.send_message(debug_info, ephemeral=True)
    
    @bot.tree.command(
        name="panel_refresh",
        description="Refresh all panel buttons (fixes broken buttons)"
    )
    async def refresh_panels(interaction: discord.Interaction):
        """Refresh all panel buttons"""
        
        if not has_roles(interaction.user, PANEL_REQUIRED_ROLES) and PANEL_REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        panels = load_panels()
        success_count = 0
        error_count = 0
        
        import importlib.util
        spec = importlib.util.spec_from_file_location("ticket_handler", Path(__file__).parent / "ticket_handler.py")
        ticket_handler = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ticket_handler)
        create_application_channel = ticket_handler.create_application_channel
        
        for panel_id, panel_data in panels.items():
            try:
                channel = interaction.guild.get_channel(panel_data['channel_id'])
                if not channel:
                    error_count += 1
                    continue
                
                message = await channel.fetch_message(int(panel_id))
                
                # Recreate view
                class TicketPanelView(View):
                    def __init__(self, applications, panel_data):
                        super().__init__(timeout=None)
                        self.panel_data = panel_data
                        
                        for app in applications:
                            button = Button(
                                label=app['name'],
                                style=discord.ButtonStyle(app['style']),
                                emoji=app['emoji'] if app['emoji'] else None,
                                custom_id=f"ticket_{app['name'].lower().replace(' ', '_')}"
                            )
                            
                            def create_callback(app_name):
                                async def callback(btn_interaction: discord.Interaction):
                                    await create_application_channel(btn_interaction, app_name, self.panel_data)
                                return callback
                            
                            button.callback = create_callback(app['name'])
                            self.add_item(button)
                
                view = TicketPanelView(panel_data['applications'], panel_data)
                await message.edit(view=view)
                success_count += 1
                
            except Exception as e:
                print(f"Error refreshing panel {panel_id}: {e}")
                error_count += 1
        
        await interaction.followup.send(
            f"✅ Refreshed {success_count} panel(s)\n❌ Failed: {error_count}",
            ephemeral=True
        )
    
    print("[OK] Loaded setup command")