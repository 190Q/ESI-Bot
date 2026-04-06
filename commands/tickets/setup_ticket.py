import discord
from discord import app_commands
from discord.ui import Select, Button, View, Modal, TextInput
import json
import os
from pathlib import Path
from datetime import datetime

_ROOT = Path(__file__).resolve().parent.parent.parent
_PANELS_FILE = _ROOT / 'data' / 'ticket_panels.json'

REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0
]

def load_panels():
    """Load ticket panels from JSON file"""
    if _PANELS_FILE.exists():
        with open(_PANELS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_panels(panels):
    """Save ticket panels to JSON file"""
    with open(_PANELS_FILE, 'w', encoding='utf-8') as f:
        json.dump(panels, f, indent=4, ensure_ascii=False)

def get_panel_choices():
    """Get autocomplete choices for panel selection"""
    panels = load_panels()
    choices = []
    
    for panel_id, panel_data in panels.items():
        app_count = len(panel_data.get('applications', []))
        title = panel_data.get('title', 'Unknown')
        
        # Discord limits choice names to 100 characters
        name = f"{title} (ID: {panel_id[:8]}..., {app_count} apps)"
        if len(name) > 100:
            name = name[:97] + "..."
        
        choices.append(app_commands.Choice(name=name, value=panel_id))
    
    return choices[:25]  # Discord limits to 25 choices

# Add this autocomplete function
from typing import List
from utils.permissions import has_roles
async def panel_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    """Autocomplete function for panel selection"""
    panels = load_panels()
    choices = []
    
    for panel_id, panel_data in panels.items():
        app_count = len(panel_data.get('applications', []))
        title = panel_data.get('title', 'Unknown')
        
        # Create display name
        name = f"{title} (ID: {panel_id[:8]}..., {app_count} apps)"
        if len(name) > 100:
            name = name[:97] + "..."
        
        # Filter based on current input
        if current.lower() in name.lower() or current.lower() in panel_id.lower():
            choices.append(app_commands.Choice(name=name, value=panel_id))
    
    return choices[:25]  # Discord limits to 25 choices

# Permission Manager View
class PermissionManagerView(View):
    def __init__(self, panel_message_id, application_name, panel_data):
        super().__init__(timeout=None)
        self.panel_message_id = panel_message_id
        self.application_name = application_name
        self.panel_data = panel_data
        self.update_buttons()
    
    def update_buttons(self):
        self.clear_items()
        
        # Add role button
        add_role_button = Button(label="➕ Add Role", style=discord.ButtonStyle.green, row=0)
        add_role_button.callback = self.add_role
        self.add_item(add_role_button)
        
        # Add user button
        add_user_button = Button(label="➕ Add User", style=discord.ButtonStyle.green, row=0)
        add_user_button.callback = self.add_user
        self.add_item(add_user_button)
        
        # Settings button
        settings_button = Button(label="⚙️ Settings", style=discord.ButtonStyle.blurple, row=0)
        settings_button.callback = self.open_settings
        self.add_item(settings_button)

        # Questions button
        questions_button = Button(label="❓ Questions", style=discord.ButtonStyle.blurple, row=0)
        questions_button.callback = self.open_questions
        self.add_item(questions_button)
        
        # Remove role dropdown
        if self.panel_data['permissions'][self.application_name]['roles']:
            remove_role_select = Select(
                placeholder="Remove a role",
                options=[
                    discord.SelectOption(
                        label=f"Role ID: {role_id}",
                        value=str(role_id)
                    )
                    for role_id in self.panel_data['permissions'][self.application_name]['roles']
                ],
                row=1
            )
            remove_role_select.callback = self.remove_role
            self.add_item(remove_role_select)
        
        # Remove user dropdown
        if self.panel_data['permissions'][self.application_name]['users']:
            remove_user_select = Select(
                placeholder="Remove a user",
                options=[
                    discord.SelectOption(
                        label=f"User ID: {user_id}",
                        value=str(user_id)
                    )
                    for user_id in self.panel_data['permissions'][self.application_name]['users']
                ],
                row=2
            )
            remove_user_select.callback = self.remove_user
            self.add_item(remove_user_select)
        
        # Save button
        save_button = Button(label="💾 Save & Exit", style=discord.ButtonStyle.primary, row=3)
        save_button.callback = self.save_permissions
        self.add_item(save_button)
    
    async def add_role(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddRoleModal(self))
    
    async def add_user(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddUserModal(self))
    
    async def open_settings(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ApplicationSettingsModal(self))

    async def open_questions(self, interaction: discord.Interaction):
        # Initialize questions if not exists
        if 'questions' not in self.panel_data:
            self.panel_data['questions'] = {}
        
        if self.application_name not in self.panel_data['questions']:
            self.panel_data['questions'][self.application_name] = []
        
        questions = self.panel_data['questions'][self.application_name]
        
        questions_list = ""
        if questions:
            for i, q in enumerate(questions):
                style = "Short" if q.get('style') == 'short' else "Paragraph"
                required = "✅" if q.get('required', True) else "❌"
                questions_list += f"\n**{i+1}.** {q['label']} ({style}) - Required: {required}"
                if q.get('placeholder'):
                    questions_list += f"\n    *Placeholder: {q['placeholder']}*"
        else:
            questions_list = "\n*No questions configured yet*"
        
        embed = discord.Embed(
            title=f"❓ Questions for {self.application_name}",
            description=f"**Current Questions:**{questions_list}",
            color=0x5865F2
        )
        
        view = QuestionsManagerView(self.panel_message_id, self.application_name, self.panel_data)
        await interaction.response.edit_message(embed=embed, view=view)

    async def remove_role(self, interaction: discord.Interaction):
        role_id = int(interaction.data['values'][0])
        self.panel_data['permissions'][self.application_name]['roles'].remove(role_id)
        await self.refresh_view(interaction)
    
    async def remove_user(self, interaction: discord.Interaction):
        user_id = int(interaction.data['values'][0])
        self.panel_data['permissions'][self.application_name]['users'].remove(user_id)
        await self.refresh_view(interaction)
    
    async def refresh_view(self, interaction: discord.Interaction):
        # Get settings info if exists
        settings_info = ""
        if 'settings' in self.panel_data and self.application_name in self.panel_data['settings']:
            settings = self.panel_data['settings'][self.application_name]
            settings_info = f"\n\n**Settings:**\n**Channel Name:** `{settings.get('channel_name', 'application-%user%')}`"
            settings_info += f"\n**Log Creation:** `{'Yes' if settings.get('log_creation', True) else 'No'}`"
            settings_info += f"\n**Confirm Close:** `{'Yes' if settings.get('confirm_close', True) else 'No'}`"
            settings_info += f"\n**DM on Close:** `{'Yes' if settings.get('dm_on_close', True) else 'No'}`"
            settings_info += f"\n**Forward Full:** `{'Yes' if settings.get('forward_full', True) else 'No'}`"
            settings_info += "\n\n*Available variables: %user%, %id%*"
        else:
            settings_info = "\n\n*Available variables: %user%, %id%*"
        
        embed = discord.Embed(
            title=f"🔒 Configuration for {self.application_name}",
            description=f"**Current Permissions:**\n\n"
                    f"**Roles:** {', '.join([f'<@&{role_id}>' for role_id in self.panel_data['permissions'][self.application_name]['roles']]) if self.panel_data['permissions'][self.application_name]['roles'] else '*None*'}\n"
                    f"**Users:** {', '.join([f'<@{user_id}>' for user_id in self.panel_data['permissions'][self.application_name]['users']]) if self.panel_data['permissions'][self.application_name]['users'] else '*None*'}"
                    f"{settings_info}",
            color=0x5865F2
        )
        self.update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def save_permissions(self, interaction: discord.Interaction):
        # Save to JSON
        panels = load_panels()
        panels[self.panel_message_id] = self.panel_data
        save_panels(panels)
        
        # Create application selector to go back
        embed = discord.Embed(
            title="Select Application",
            description="Permissions saved successfully!\n\nSelect which application type you want to configure permissions for.",
            color=0x00FF00
        )
        
        app_select = Select(
            placeholder="Select an application",
            options=[
                discord.SelectOption(
                    label=app['name'],
                    value=app['name']
                )
                for app in self.panel_data['applications']
            ]
        )
        
        async def app_selected(select_interaction: discord.Interaction):
            application_name = select_interaction.data['values'][0]
            
            # Initialize permissions if not exists
            if 'permissions' not in self.panel_data:
                self.panel_data['permissions'] = {}
            
            if application_name not in self.panel_data['permissions']:
                self.panel_data['permissions'][application_name] = {
                    'roles': [],
                    'users': []
                }
            
            # Show permission management view
            perm_embed = discord.Embed(
                title=f"🔒 Permissions for {application_name}",
                description=f"**Current Permissions:**\n\n"
                        f"**Roles:** {', '.join([f'<@&{role_id}>' for role_id in self.panel_data['permissions'][application_name]['roles']]) if self.panel_data['permissions'][application_name]['roles'] else '*None*'}\n"
                        f"**Users:** {', '.join([f'<@{user_id}>' for user_id in self.panel_data['permissions'][application_name]['users']]) if self.panel_data['permissions'][application_name]['users'] else '*None*'}\n\n"
                        f"Use the buttons below to add or remove roles/users.",
                color=0x5865F2
            )
            
            view = PermissionManagerView(self.panel_message_id, application_name, self.panel_data)
            await select_interaction.response.edit_message(embed=perm_embed, view=view)
        
        app_select.callback = app_selected
        
        view = View(timeout=None)
        view.add_item(app_select)
        
        await interaction.response.edit_message(embed=embed, view=view)

# Questions Manager View
class QuestionsManagerView(View):
    def __init__(self, panel_message_id, application_name, panel_data):
        super().__init__(timeout=None)
        self.panel_message_id = panel_message_id
        self.application_name = application_name
        self.panel_data = panel_data
        self.update_buttons()
    
    def update_buttons(self):
        self.clear_items()
        
        # Add question button
        add_question_button = Button(label="➕ Add Question", style=discord.ButtonStyle.green, row=0)
        add_question_button.callback = self.add_question
        self.add_item(add_question_button)
        
        # Show existing questions as a select menu if any exist
        questions = self.panel_data.get('questions', {}).get(self.application_name, [])
        if questions:
            remove_question_select = Select(
                placeholder="Remove a question",
                options=[
                    discord.SelectOption(
                        label=f"Q{i+1}: {q['label'][:50]}...",
                        description=q['placeholder'][:50] if q.get('placeholder') else "No placeholder",
                        value=str(i)
                    )
                    for i, q in enumerate(questions)
                ],
                row=1
            )
            remove_question_select.callback = self.remove_question
            self.add_item(remove_question_select)
        
        # Back button
        back_button = Button(label="⬅️ Back", style=discord.ButtonStyle.secondary, row=2)
        back_button.callback = self.go_back
        self.add_item(back_button)
        
        # Save button
        save_button = Button(label="💾 Save & Exit", style=discord.ButtonStyle.primary, row=2)
        save_button.callback = self.save_questions
        self.add_item(save_button)
    
    async def add_question(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddQuestionModal(self))
    
    async def remove_question(self, interaction: discord.Interaction):
        question_index = int(interaction.data['values'][0])
        self.panel_data['questions'][self.application_name].pop(question_index)
        await self.refresh_view(interaction)
    
    async def refresh_view(self, interaction: discord.Interaction):
        questions = self.panel_data.get('questions', {}).get(self.application_name, [])
        
        questions_list = ""
        if questions:
            for i, q in enumerate(questions):
                style = "Short" if q.get('style') == 'short' else "Paragraph"
                required = "✅" if q.get('required', True) else "❌"
                questions_list += f"\n**{i+1}.** {q['label']} ({style}) - Required: {required}"
                if q.get('placeholder'):
                    questions_list += f"\n    *Placeholder: {q['placeholder']}*"
        else:
            questions_list = "\n*No questions configured yet*"
        
        embed = discord.Embed(
            title=f"❓ Questions for {self.application_name}",
            description=f"**Current Questions:**{questions_list}",
            color=0x5865F2
        )
        self.update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def go_back(self, interaction: discord.Interaction):
        # Go back to permission manager
        perm_embed = discord.Embed(
            title=f"🔒 Permissions for {self.application_name}",
            description=f"**Current Permissions:**\n\n"
                    f"**Roles:** {', '.join([f'<@&{role_id}>' for role_id in self.panel_data['permissions'][self.application_name]['roles']]) if self.panel_data['permissions'][self.application_name]['roles'] else '*None*'}\n"
                    f"**Users:** {', '.join([f'<@{user_id}>' for user_id in self.panel_data['permissions'][self.application_name]['users']]) if self.panel_data['permissions'][self.application_name]['users'] else '*None*'}\n\n"
                    f"Use the buttons below to add or remove roles/users.",
            color=0x5865F2
        )
        
        view = PermissionManagerView(self.panel_message_id, self.application_name, self.panel_data)
        await interaction.response.edit_message(embed=perm_embed, view=view)
    
    async def save_questions(self, interaction: discord.Interaction):
        # Save to JSON
        panels = load_panels()
        panels[self.panel_message_id] = self.panel_data
        save_panels(panels)
        
        # Create application selector to go back
        embed = discord.Embed(
            title="Select Application",
            description="Questions saved successfully!\n\nSelect which application type you want to configure.",
            color=0x00FF00
        )
        
        app_select = Select(
            placeholder="Select an application",
            options=[
                discord.SelectOption(
                    label=app['name'],
                    value=app['name']
                )
                for app in self.panel_data['applications']
            ]
        )
        
        async def app_selected(select_interaction: discord.Interaction):
            application_name = select_interaction.data['values'][0]
            
            # Initialize permissions if not exists
            if 'permissions' not in self.panel_data:
                self.panel_data['permissions'] = {}
            
            if application_name not in self.panel_data['permissions']:
                self.panel_data['permissions'][application_name] = {
                    'roles': [],
                    'users': []
                }
            
            # Show permission management view
            perm_embed = discord.Embed(
                title=f"🔒 Permissions for {application_name}",
                description=f"**Current Permissions:**\n\n"
                        f"**Roles:** {', '.join([f'<@&{role_id}>' for role_id in self.panel_data['permissions'][application_name]['roles']]) if self.panel_data['permissions'][application_name]['roles'] else '*None*'}\n"
                        f"**Users:** {', '.join([f'<@{user_id}>' for user_id in self.panel_data['permissions'][application_name]['users']]) if self.panel_data['permissions'][application_name]['users'] else '*None*'}\n\n"
                        f"Use the buttons below to add or remove roles/users.",
                color=0x5865F2
            )
            
            view = PermissionManagerView(self.panel_message_id, application_name, self.panel_data)
            await select_interaction.response.edit_message(embed=perm_embed, view=view)
        
        app_select.callback = app_selected
        
        view = View(timeout=None)
        view.add_item(app_select)
        
        await interaction.response.edit_message(embed=embed, view=view)

# Modal for adding question
class AddQuestionModal(Modal, title="Add Question"):
    label_input = TextInput(
        label="Question Label",
        placeholder="e.g., What is your in-game name?",
        max_length=45,
        required=True
    )
    
    placeholder_input = TextInput(
        label="Placeholder Text (Optional)",
        placeholder="e.g., Enter your name here...",
        max_length=100,
        required=False
    )
    
    style_input = TextInput(
        label="Style (short/paragraph)",
        placeholder="short or paragraph",
        max_length=10,
        required=True,
        default="short"
    )
    
    required_input = TextInput(
        label="Required? (yes/no)",
        placeholder="yes or no",
        max_length=3,
        required=True,
        default="yes"
    )
    
    max_length_input = TextInput(
        label="Max Length (Optional, default 1000)",
        placeholder="e.g., 100",
        max_length=4,
        required=False
    )
    
    def __init__(self, questions_view):
        super().__init__()
        self.questions_view = questions_view
    
    async def on_submit(self, interaction: discord.Interaction):
        label = self.label_input.value
        placeholder = self.placeholder_input.value if self.placeholder_input.value else None
        style = self.style_input.value.lower().strip()
        required = self.required_input.value.lower().strip() in ['yes', 'y', 'true', '1']
        
        # Validate style
        if style not in ['short', 'paragraph']:
            await interaction.response.send_message(
                "❌ Style must be either 'short' or 'paragraph'!",
                ephemeral=True
            )
            return
        
        # Parse max length
        max_length = 1000
        if self.max_length_input.value:
            try:
                max_length = int(self.max_length_input.value.strip())
                if max_length < 1 or max_length > 4000:
                    await interaction.response.send_message(
                        "❌ Max length must be between 1 and 4000!",
                        ephemeral=True
                    )
                    return
            except ValueError:
                await interaction.response.send_message(
                    "❌ Max length must be a number!",
                    ephemeral=True
                )
                return
        
        # Initialize questions if not exists
        if 'questions' not in self.questions_view.panel_data:
            self.questions_view.panel_data['questions'] = {}
        
        if self.questions_view.application_name not in self.questions_view.panel_data['questions']:
            self.questions_view.panel_data['questions'][self.questions_view.application_name] = []
        
        # Add question
        question_data = {
            'label': label,
            'placeholder': placeholder,
            'style': style,
            'required': required,
            'max_length': max_length
        }
        
        self.questions_view.panel_data['questions'][self.questions_view.application_name].append(question_data)
        
        await self.questions_view.refresh_view(interaction)

# Modal for adding role
class AddRoleModal(Modal, title="Add Role"):
    role_id_input = TextInput(
        label="Role ID",
        placeholder="Right-click role → Copy ID",
        max_length=20,
        required=True
    )
    
    def __init__(self, permission_view):
        super().__init__()
        self.permission_view = permission_view
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            role_id = int(self.role_id_input.value.strip())
            role = interaction.guild.get_role(role_id)
            
            if role is None:
                await interaction.response.send_message("❌ Role not found! Make sure the ID is correct.", ephemeral=True)
                return
            
            if role_id not in self.permission_view.panel_data['permissions'][self.permission_view.application_name]['roles']:
                self.permission_view.panel_data['permissions'][self.permission_view.application_name]['roles'].append(role_id)
            
            await self.permission_view.refresh_view(interaction)
            
        except ValueError:
            await interaction.response.send_message("❌ Invalid role ID! Please enter numbers only.", ephemeral=True)

# Modal for adding user
class AddUserModal(Modal, title="Add User"):
    user_id_input = TextInput(
        label="User ID",
        placeholder="Right-click user → Copy ID",
        max_length=20,
        required=True
    )
    
    def __init__(self, permission_view):
        super().__init__()
        self.permission_view = permission_view
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            user_id = int(self.user_id_input.value.strip())
            user = interaction.guild.get_member(user_id)
            
            if user is None:
                await interaction.response.send_message("❌ User not found! Make sure the ID is correct.", ephemeral=True)
                return
            
            if user_id not in self.permission_view.panel_data['permissions'][self.permission_view.application_name]['users']:
                self.permission_view.panel_data['permissions'][self.permission_view.application_name]['users'].append(user_id)
            
            await self.permission_view.refresh_view(interaction)
            
        except ValueError:
            await interaction.response.send_message("❌ Invalid user ID! Please enter numbers only.", ephemeral=True)

# Modal for setting application channel name
class ApplicationSettingsModal(Modal, title="Application Settings"):
    channel_name = TextInput(
        label="Channel Name Format",
        placeholder="e.g., ticket-%user%-%id% or application-%user%",
        max_length=100,
        required=True,
        default="application-%user%"
    )
    
    log_creation = TextInput(
        label="Log Creation to Channel? (yes/no)",
        placeholder="yes or no",
        max_length=3,
        required=True,
        default="yes"
    )
    
    confirm_close = TextInput(
        label="Confirm Before Closing? (yes/no)",
        placeholder="yes or no",
        max_length=3,
        required=True,
        default="yes"
    )

    dm_on_close = TextInput(
        label="Send DM on Close? (yes/no)",
        placeholder="yes or no",
        max_length=3,
        required=True,
        default="yes"
    )
    
    forward_full = TextInput(
        label="Forward Full Application? (yes/no)",
        placeholder="yes = player stats, no = application info only",
        max_length=3,
        required=True,
        default="yes"
    )
    
    def __init__(self, permission_view):
        super().__init__()
        self.permission_view = permission_view
        
        # Pre-fill if settings already exist
        if 'settings' in self.permission_view.panel_data:
            if self.permission_view.application_name in self.permission_view.panel_data['settings']:
                settings = self.permission_view.panel_data['settings'][self.permission_view.application_name]
                if 'channel_name' in settings:
                    self.channel_name.default = settings['channel_name']
                if 'log_creation' in settings:
                    self.log_creation.default = "yes" if settings['log_creation'] else "no"
                if 'confirm_close' in settings:
                    self.confirm_close.default = "yes" if settings['confirm_close'] else "no"
                if 'dm_on_close' in settings:
                    self.dm_on_close.default = "yes" if settings['dm_on_close'] else "no"
                if 'forward_full' in settings:
                    self.forward_full.default = "yes" if settings['forward_full'] else "no"
    
    async def on_submit(self, interaction: discord.Interaction):
        # Validate and clean channel name
        channel_name = self.channel_name.value
        
        # Check for invalid characters (excluding variables)
        invalid_chars = ['\\', '/', '"', '#', ':', '<', '>', '@']
        
        # Extract variables to temporarily replace them
        import re
        variables = re.findall(r'%\w+%', channel_name)
        temp_name = channel_name
        
        # Replace variables with placeholders
        for i, var in enumerate(variables):
            temp_name = temp_name.replace(var, f'__VAR{i}__')
        
        # Check for invalid characters in non-variable parts
        for char in invalid_chars:
            if char in temp_name:
                await interaction.response.send_message(
                    f"❌ Invalid character '{char}' found in channel name! "
                    f"Allowed: letters, numbers, hyphens, underscores, and variables (%user%, %id%, etc.)",
                    ephemeral=True
                )
                return
        
        # Replace spaces with hyphens
        channel_name = channel_name.replace(' ', '-')
        
        # Convert to lowercase (Discord requirement)
        channel_name = channel_name.lower()
        
        # Parse boolean values
        log_creation = self.log_creation.value.lower().strip() in ['yes', 'y', 'true', '1']
        confirm_close = self.confirm_close.value.lower().strip() in ['yes', 'y', 'true', '1']
        dm_on_close = self.dm_on_close.value.lower().strip() in ['yes', 'y', 'true', '1']
        forward_full = self.forward_full.value.lower().strip() in ['yes', 'y', 'true', '1']
        
        # Store settings data
        if 'settings' not in self.permission_view.panel_data:
            self.permission_view.panel_data['settings'] = {}
        
        self.permission_view.panel_data['settings'][self.permission_view.application_name] = {
            'channel_name': channel_name,
            'log_creation': log_creation,
            'confirm_close': confirm_close,
            'dm_on_close': dm_on_close,
            'forward_full': forward_full
        }
        
        await self.permission_view.refresh_view(interaction)

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    @bot.tree.command(
        name="setup_applications",
        description="Configure application settings for a ticket panel"
    )
    @app_commands.describe(panel_id="Select the ticket panel to configure")
    @app_commands.autocomplete(panel_id=panel_autocomplete)
    async def setup_applications_command(interaction: discord.Interaction, panel_id: str):
        """Setup application settings command"""

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
        
        # Load panels and verify the panel exists
        panels = load_panels()
        
        if panel_id not in panels:
            await interaction.response.send_message(
                "❌ Panel not found! Please select a valid panel from the list.",
                ephemeral=True
            )
            return
        
        panel_data = panels[panel_id]
        
        # Create application selector
        embed = discord.Embed(
            title="Select Application",
            description="Select which application type you want to configure permissions for.",
            color=0x5865F2
        )
        
        app_select = Select(
            placeholder="Select an application",
            options=[
                discord.SelectOption(
                    label=app['name'],
                    value=app['name']
                )
                for app in panel_data['applications']
            ]
        )
        
        async def app_selected(select_interaction: discord.Interaction):
            application_name = select_interaction.data['values'][0]
            
            # Initialize permissions if not exists
            if 'permissions' not in panel_data:
                panel_data['permissions'] = {}
            
            if application_name not in panel_data['permissions']:
                panel_data['permissions'][application_name] = {
                    'roles': [],
                    'users': []
                }
            
            # Show permission management view
            perm_embed = discord.Embed(
                title=f"🔒 Permissions for {application_name}",
                description=f"**Current Permissions:**\n\n"
                        f"**Roles:** {', '.join([f'<@&{role_id}>' for role_id in panel_data['permissions'][application_name]['roles']]) if panel_data['permissions'][application_name]['roles'] else '*None*'}\n"
                        f"**Users:** {', '.join([f'<@{user_id}>' for user_id in panel_data['permissions'][application_name]['users']]) if panel_data['permissions'][application_name]['users'] else '*None*'}\n\n"
                        f"Use the buttons below to add or remove roles/users.",
                color=0x5865F2
            )
            
            view = PermissionManagerView(panel_id, application_name, panel_data)
            await select_interaction.response.edit_message(embed=perm_embed, view=view)
        
        app_select.callback = app_selected
        
        view = View(timeout=None)
        view.add_item(app_select)
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    print("[OK] Loaded template command")