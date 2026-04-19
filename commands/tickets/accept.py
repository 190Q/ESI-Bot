import discord
from discord import app_commands
from discord.ui import Select, Button, View, Modal, TextInput
from datetime import datetime
from pathlib import Path
import os
import sys
import json
from datetime import datetime, timezone
import aiohttp
from typing import Tuple

# Add parent directory to path to import blacklist
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from blacklist import is_blacklisted
from rank_logger import log_rank_change
from guild_queue import get_guild_capacity, get_queue_position, add_to_queue, remove_from_queue, extract_username_from_embeds
from utils.permissions import has_roles

# Path to the username â†” user_id match database
USERNAME_MATCH_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data/username_matches.json",
)

async def validate_wynncraft_username(username: str) -> Tuple[bool, str, str]:
    """
    Validate if a username exists on Wynncraft.
    Returns (is_valid, error_message, uuid)
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.wynncraft.com/v3/player/{username}") as response:
                if response.status == 200:
                    data = await response.json()
                    uuid = data.get('uuid', '')
                    return True, None, uuid
                elif response.status == 300:
                    # Multiple usernames found - need to pick the most recent one
                    data = await response.json()
                    # The API returns: {"error": "...", "objects": {"uuid1": {...}, "uuid2": {...}}}
                    players_dict = data.get('objects', {})
                    
                    if not players_dict:
                        return False, f"âŒ No valid players found for username `{username}`!", None
                    
                    print(f"[INFO] Multiple usernames found for {username}: {data}")
                    
                    # Fetch full data for each UUID to get lastJoin
                    most_recent_player = None
                    most_recent_last_join = None
                    most_recent_uuid = None
                    
                    for player_uuid in players_dict.keys():
                        try:
                            async with session.get(f"https://api.wynncraft.com/v3/player/{player_uuid}") as player_response:
                                if player_response.status == 200:
                                    player_data = await player_response.json()
                                    last_join = player_data.get('lastJoin')
                                    
                                    if last_join and (most_recent_last_join is None or last_join > most_recent_last_join):
                                        most_recent_last_join = last_join
                                        most_recent_player = player_data
                                        most_recent_uuid = player_uuid
                        except Exception as e:
                            print(f"[WARN] Error fetching player data for UUID {player_uuid}: {e}")
                            continue
                    
                    if most_recent_player and most_recent_uuid:
                        print(f"[INFO] Selected player with UUID {most_recent_uuid} (most recent lastJoin: {most_recent_last_join})")
                        return True, None, most_recent_uuid
                    else:
                        return False, f"âš ï¸ Could not determine the correct player for username `{username}`.", None
                elif response.status == 404:
                    return False, f"âŒ Username `{username}` not found on Wynncraft!", None
                else:
                    return False, f"âš ï¸ Could not verify username (API status: {response.status}). Please try again.", None
    except Exception as e:
        return False, f"âš ï¸ Error validating username: {str(e)}", None

def _load_username_match_db():
    """Load the username match DB from disk.

    Returns an empty dict if the file does not exist or is invalid.
    """
    try:
        with open(USERNAME_MATCH_DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        # Corrupt or empty file â€“ start fresh rather than breaking the command
        return {}
    except Exception as e:
        print(f"[WARN] Failed to load username match DB: {e}")
        return {}

def save_username_match(user_id: int, username: str, uuid: str = None) -> None:
    """Persist a mapping of Discord user ID â†’ inâ€‘game username and UUID to the JSON DB."""
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
        # Don't break the command flow if saving fails; just log it.
        print(f"[WARN] Failed to save username match for {user_id}: {e}")

# Role IDs
SINDRIAN_CITIZEN_ID = 554889169705500672
NOBILITY_ID = 554530279516274709
MISC_SEPARATOR_ID = 968121600488656906
EX_CITIZEN_ID = 706338091312349195
VETERAN_ID = 914422269802070057
ALT_ACCOUNT_ID = 1448898522224001174

# Rank configurations with required roles and roles to remove
RANK_CONFIGS = {
    "envoy": {
        "main_role": 554896955638153216,  # Envoy
        "required_roles": [
            554896955638153216,  # Envoy
            MISC_SEPARATOR_ID      # Miscellaneous separator
        ],
        "roles_to_remove": [
            688438690137243892,    # Knight
            NOBILITY_ID,           # Nobility
            681030746651230351,    # Squire
            EX_CITIZEN_ID,         # Ex-Citizen
            VETERAN_ID,            # Veteran
            SINDRIAN_CITIZEN_ID,   # Sindrian Citizen
        ],
        "display_name": "Envoy"
    },
    "squire": {
        "main_role": 681030746651230351,  # Squire
        "required_roles": [
            681030746651230351,    # Squire
            SINDRIAN_CITIZEN_ID,   # Sindrian Citizen
            MISC_SEPARATOR_ID      # Miscellaneous separator
        ],
        "roles_to_remove": [
            554896955638153216,    # Envoy
            EX_CITIZEN_ID,         # Ex-Citizen
            VETERAN_ID             # Veteran
        ],
        "display_name": "Squire"
    },
    "knight": {
        "main_role": 688438690137243892,  # Knight
        "required_roles": [
            688438690137243892,    # Knight
            NOBILITY_ID,           # Nobility
            SINDRIAN_CITIZEN_ID,   # Sindrian Citizen
            MISC_SEPARATOR_ID      # Miscellaneous separator
        ],
        "roles_to_remove": [
            681030746651230351,    # Squire
            554896955638153216,    # Envoy
            EX_CITIZEN_ID,         # Ex-Citizen
            VETERAN_ID             # Veteran
        ],
        "display_name": "Knight"
    },
    "viscount": {
        "main_role": 591769392828776449,  # Viscount
        "required_roles": [
            591769392828776449,    # Viscount
            NOBILITY_ID,           # Nobility
            SINDRIAN_CITIZEN_ID,   # Sindrian Citizen
            MISC_SEPARATOR_ID      # Miscellaneous separator
        ],
        "roles_to_remove": [
            688438690137243892,    # Knight
            681030746651230351,    # Squire
            554896955638153216,    # Envoy
            EX_CITIZEN_ID,         # Ex-Citizen
            VETERAN_ID             # Veteran
        ],
        "display_name": "Viscount"
    }
}

RANK_HIERARCHY = [
    (554506531949772812, "Emperor"),
    (554514823191199747, "Archduke"),
    (1396112289832243282, "Grand Duke"),
    (591765870272053261, "Duke"),
    (1391424890938195998, "Count"),
    (591769392828776449, "Viscount"),
    (688438690137243892, "Knight"),
    (681030746651230351, "Squire")
]

REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0,
    954566591520063510, # Juror
    600185623474601995, # Parliament
]

class AltAccountSelectView(discord.ui.View):
    """View for selecting if the user is an alt account"""
    def __init__(self, user: discord.Member, username: str, source_message_id: int = None, is_ex_citizen: bool = False, from_application: bool = False, rank_key: str = None, uuid: str = None):
        super().__init__()
        self.user = user
        self.username = username
        self.source_message_id = source_message_id
        self.is_ex_citizen = is_ex_citizen
        self.from_application = from_application
        self.rank_key = rank_key
        self.uuid = uuid
        
        alt_select = discord.ui.Select(
            placeholder="Is this an alt account?",
            options=[
                discord.SelectOption(label="No - Main Account", value="false"),
                discord.SelectOption(label="Yes - Alt Account", value="true")
            ]
        )
        alt_select.callback = self.alt_select_callback
        self.add_item(alt_select)
    
    async def alt_select_callback(self, interaction: discord.Interaction):
        is_alt = interaction.data['values'][0] == "true"
        
        if is_alt:
            # Show modal to enter main account username
            modal = MainUsernameModal(self.user, self.username, self.source_message_id, self.is_ex_citizen, self.from_application, self.rank_key, uuid=self.uuid)
            await interaction.response.send_modal(modal)
        else:
            # Continue to confirmation without alt info
            await show_confirmation_embed(interaction, self.user, self.username, self.rank_key, None, self.source_message_id, is_alt=False, main_username=None, uuid=self.uuid)

class MainUsernameModal(discord.ui.Modal, title="Enter Main Account Username"):
    def __init__(self, user: discord.Member, alt_username: str, source_message_id: int = None, is_ex_citizen: bool = False, from_application: bool = False, rank_key: str = None, uuid: str = None):
        super().__init__()
        self.user = user
        self.alt_username = alt_username
        self.source_message_id = source_message_id
        self.is_ex_citizen = is_ex_citizen
        self.from_application = from_application
        self.rank_key = rank_key
        self.uuid = uuid
        
        self.main_username_input = discord.ui.TextInput(
            label="Main Account Username",
            placeholder="Enter the main account username",
            required=True,
            max_length=32
        )
        self.add_item(self.main_username_input)
    
    async def on_submit(self, interaction: discord.Interaction):
        main_username = self.main_username_input.value
        
        # Validate main username on Wynncraft
        is_valid, error_msg, uuid = await validate_wynncraft_username(main_username)
        if not is_valid:
            await interaction.response.send_message(error_msg, ephemeral=True)
            return
        
        # Go directly to confirmation with alt info
        await show_confirmation_embed(interaction, self.user, self.alt_username, self.rank_key, None, self.source_message_id, is_alt=True, main_username=main_username, uuid=self.uuid)

class UsernameEditModal(discord.ui.Modal, title="Confirm Username"):
    def __init__(self, user: discord.Member, username: str, rank_key: str, needs_pronoun: bool, executor_id: int, detected_pronoun: str = None, source_message_id: int = None, uuid: str = None):
        super().__init__()
        self.source_message_id = source_message_id
        self.uuid = uuid
        self.user = user
        self.username = username or "Unknown"
        self.rank_key = rank_key
        self.needs_pronoun = needs_pronoun
        self.executor_id = executor_id
        self.detected_pronoun = detected_pronoun
        
        self.username_input = discord.ui.TextInput(
            label="Username",
            placeholder="Confirm or edit the username",
            required=True,
            max_length=32,
            default=self.username[:32] if self.username else None
        )
        self.add_item(self.username_input)
    
    async def on_submit(self, interaction: discord.Interaction):
        username = self.username_input.value
    
        # Only validate username for non-envoy ranks (full apps)
        uuid = self.uuid
        if not uuid or username != self.username:
            # Re-validate if username changed or UUID not already set
            # Skip validation for envoy rank
            if self.rank_key != "envoy":
                is_valid, error_msg, uuid = await validate_wynncraft_username(username)
                if not is_valid:
                    await interaction.response.send_message(error_msg, ephemeral=True)
                    return
    
        # Build message based on whether pronouns were detected
        if self.needs_pronoun and self.detected_pronoun:
            content = (f"**Accept {self.user.mention} as {self.rank_key.title()}**\n\n"
                    f"Username: `{username}`\n"
                    f"Detected pronoun: {self.detected_pronoun}\n\n"
                    f"Click 'Confirm & Accept' to proceed or select a different title below:")
        elif self.needs_pronoun:
            content = (f"**Accept {self.user.mention} as {self.rank_key.title()}**\n\n"
                    f"Username: `{username}`\n"
                    f"Select pronouns below:")
        else:
            content = (f"**Accept {self.user.mention} as {self.rank_key.title()}**\n\n"
                    f"Username: `{username}`\n"
                    f"Click 'Confirm & Accept' to proceed")
        
        # Show the AutoAcceptView with the confirmed username and UUID
        view = AutoAcceptView(self.user, username, self.rank_key, self.needs_pronoun, self.executor_id, self.detected_pronoun, self.source_message_id, uuid=uuid)
        await interaction.response.send_message(content, view=view, ephemeral=True)

class AutoAcceptView(discord.ui.View):
    """View for auto-accepting from applications with optional pronoun selection"""
    def __init__(self, user: discord.Member, username: str, rank_key: str, needs_pronoun: bool, executor_id: int, detected_pronoun: str = None, source_message_id: int = None, uuid: str = None):
        super().__init__(timeout=None)
        self.user = user
        self.username = username or "Unknown"
        self.rank_key = rank_key
        self.needs_pronoun = needs_pronoun
        self.executor_id = executor_id
        self.selected_pronoun = detected_pronoun
        self.source_message_id = source_message_id
        self.uuid = uuid
        
        # Add username confirmation input
        self.username_input = discord.ui.TextInput(
            label="Username",
            placeholder="Confirm or edit the username",
            required=True,
            max_length=32,
            default=self.username
        )
        
        # If pronouns needed, add pronoun select
        if needs_pronoun:
            # Determine options based on rank
            if rank_key == "envoy":
                options = [
                    discord.SelectOption(label="Lady", value="she/her", default=(detected_pronoun == "she/her")),
                    discord.SelectOption(label="Sir", value="he/him", default=(detected_pronoun == "he/him")),
                    discord.SelectOption(label="Envoy", value="they/them", default=(detected_pronoun == "they/them"))
                ]
                # Map pronoun to title for display
                title_map = {"she/her": "Lady", "he/him": "Sir", "they/them": "Envoy"}
                current_title = title_map.get(detected_pronoun, "")
                placeholder = "Choose a title..." if not detected_pronoun else f"Current: {current_title}"
            else:  # viscount
                options = [
                    discord.SelectOption(label="Viscountess", value="she/her", default=(detected_pronoun == "she/her")),
                    discord.SelectOption(label="Viscount", value="he/him", default=(detected_pronoun == "he/him"))
                ]
                title_map = {"she/her": "Viscountess", "he/him": "Viscount"}
                current_title = title_map.get(detected_pronoun, "")
                placeholder = "Choose a title..." if not detected_pronoun else f"Current: {current_title}"
            
            pronoun_select = discord.ui.Select(
                placeholder=placeholder,
                options=options
            )
            pronoun_select.callback = self.pronoun_selected
            self.add_item(pronoun_select)
        
        # Add confirm button
        confirm_button = discord.ui.Button(
            label="Confirm & Accept",
            style=discord.ButtonStyle.success,
            custom_id="auto_accept_confirm",
            disabled=(needs_pronoun and not detected_pronoun)  # Only disabled if pronouns needed but not detected
        )
        confirm_button.callback = self.confirm_accept
        self.add_item(confirm_button)
        
        # Add edit username button
        edit_button = discord.ui.Button(
            label="Edit Username",
            style=discord.ButtonStyle.secondary,
            custom_id="edit_username"
        )
        edit_button.callback = self.edit_username
        self.add_item(edit_button)
        
        # Add cancel button
        cancel_button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.danger,
            custom_id="auto_accept_cancel"
        )
        cancel_button.callback = self.cancel_accept
        self.add_item(cancel_button)
    
    async def pronoun_selected(self, interaction: discord.Interaction):
        """Handle pronoun selection"""
        self.selected_pronoun = interaction.data['values'][0]
        # Enable confirm button - find it by checking custom_id
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id == "auto_accept_confirm":
                child.disabled = False
                break
        
        # Build updated content that preserves the original message
        content = (f"**Accept {self.user.mention} as {self.rank_key.title()}**\n\n"
                f"username: `{self.username}`\n"
                f"Pronoun selected: {self.selected_pronoun}\n\n"
                f"Click 'Confirm & Accept' to proceed.")
        
        await interaction.response.edit_message(content=content, view=self)
    
    async def edit_username(self, interaction: discord.Interaction):
        """Show modal to edit username"""
        modal = discord.ui.Modal(title="Edit Username")
        username_input = discord.ui.TextInput(
            label="Username",
            placeholder="Enter the correct username",
            required=True,
            max_length=32,
            default=self.username
        )
        modal.add_item(username_input)
        
        async def modal_callback(modal_interaction: discord.Interaction):
            self.username = username_input.value
            
            # Re-validate username and fetch UUID for non-envoy ranks
            if self.rank_key in ("squire", "knight", "viscount"):
                is_valid, error_msg, uuid = await validate_wynncraft_username(self.username)
                if not is_valid:
                    await modal_interaction.response.send_message(error_msg, ephemeral=True)
                    return
                self.uuid = uuid
            
            # Build the message preserving original format
            if self.needs_pronoun:
                if self.selected_pronoun:
                    content = (f"**Accept {self.user.mention} as {self.rank_key.title()}**\n\n"
                            f"Detected username: `{self.username}`\n"
                            f"Pronoun selected: {self.selected_pronoun}\n\n"
                            f"Click 'Confirm & Accept' to proceed.")
                else:
                    content = (f"**Accept {self.user.mention} as {self.rank_key.title()}**\n\n"
                            f"Detected username: `{self.username}`\n"
                            f"Select pronouns below:")
            else:
                content = (f"**Accept {self.user.mention} as {self.rank_key.title()}**\n\n"
                        f"Detected username: `{self.username}`\n"
                        f"Click 'Confirm & Accept' to proceed.")
            
            await modal_interaction.response.edit_message(content=content, view=self)
        
        modal.on_submit = modal_callback
        await interaction.response.send_modal(modal)
    
    async def confirm_accept(self, interaction: discord.Interaction):
        """Proceed to show confirmation embed using existing logic"""
        
        if self.needs_pronoun and not self.selected_pronoun:
            await interaction.response.send_message("Please select pronouns first!", ephemeral=True)
            return
        
        await show_confirmation_embed(
            interaction, 
            self.user, 
            self.username, 
            self.rank_key, 
            self.selected_pronoun,
            self.source_message_id,
            uuid=self.uuid
        )
    
    async def cancel_accept(self, interaction: discord.Interaction):
        """Cancel the accept operation"""
        await interaction.response.edit_message(
            content="âŒ Accept operation cancelled.",
            view=None
        )

class UsernameModal(discord.ui.Modal, title="Enter Username"):
    def __init__(self, user: discord.Member, default_username: str = None, source_message_id: int = None, is_ex_citizen: bool = False, from_application: bool = False):
        super().__init__()
        self.user = user
        self.source_message_id = source_message_id
        self.is_ex_citizen = is_ex_citizen
        self.from_application = from_application
        
        # Update the username_input to use the default value if provided
        self.username_input = discord.ui.TextInput(
            label="Username",
            placeholder="Enter the username (without rank prefix)",
            required=True,
            max_length=32,
            default=default_username if default_username else None
        )
        self.add_item(self.username_input)
    
    async def on_submit(self, interaction: discord.Interaction):
        username = self.username_input.value
        
        # Go directly to rank selection
        view = RankSelectView(self.user, username, self.source_message_id, self.is_ex_citizen, self.from_application)
        await interaction.response.send_message("Select a rank:", view=view, ephemeral=True)

class RankSelectView(discord.ui.View):
    def __init__(self, user: discord.Member, username: str, source_message_id: int = None, is_ex_citizen: bool = False, from_application: bool = False, uuid: str = None):
        super().__init__()
        self.user = user
        self.username = username
        self.source_message_id = source_message_id
        self.is_ex_citizen = is_ex_citizen
        self.from_application = from_application
        self.uuid = uuid
        
        # Build options list - exclude Envoy for ex-citizens OR applications
        options = [
            discord.SelectOption(label="Squire", value="squire"),
            discord.SelectOption(label="Knight", value="knight"),
            discord.SelectOption(label="Viscount", value="viscount")
        ]
        
        if not from_application:
            options.insert(0, discord.SelectOption(label="Envoy", value="envoy"))
        
        # Create the select with dynamic options
        rank_select = discord.ui.Select(
            placeholder="Choose a rank...",
            options=options
        )
        rank_select.callback = self.rank_select_callback
        self.add_item(rank_select)

    async def rank_select_callback(self, interaction: discord.Interaction):
        rank_key = interaction.data['values'][0]
        
        # Only validate username for full apps (squire, knight, viscount)
        # Skip validation for envoy rank
        if rank_key != "envoy":
            is_valid, error_msg, uuid = await validate_wynncraft_username(self.username)
            if not is_valid:
                await interaction.response.send_message(error_msg, ephemeral=True)
                return
            # Store UUID for later use
            self.uuid = uuid
        
        # Check if pronoun is required
        if rank_key in ("envoy", "viscount"):
            view = PronounSelectView(self.user, self.username, rank_key, self.source_message_id, uuid=self.uuid)
            await interaction.response.edit_message(content="Select a title:", view=view)
        else:
            # Go directly to confirmation (no alt account selection)
            await show_confirmation_embed(interaction, self.user, self.username, rank_key, None, self.source_message_id, uuid=self.uuid)

class PronounSelectView(discord.ui.View):
    def __init__(self, user: discord.Member, username: str, rank_key: str, source_message_id: int = None, uuid: str = None):
        super().__init__()
        self.user = user
        self.username = username
        self.rank_key = rank_key
        self.source_message_id = source_message_id
        self.uuid = uuid
        
        # Create options based on rank
        if rank_key == "envoy":
            options = [
                discord.SelectOption(label="Lady", value="she/her"),
                discord.SelectOption(label="Sir", value="he/him"),
                discord.SelectOption(label="Envoy", value="they/them")
            ]
            placeholder = "Choose a title..."
        else:  # viscount
            options = [
                discord.SelectOption(label="Viscountess", value="she/her"),
                discord.SelectOption(label="Viscount", value="he/him")
            ]
            placeholder = "Choose a title..."
        
        # Create the select dropdown
        title_select = discord.ui.Select(
            placeholder=placeholder,
            options=options
        )
        title_select.callback = self.pronoun_select
        self.add_item(title_select)
    
    async def pronoun_select(self, interaction: discord.Interaction):
        pronoun_value = interaction.data['values'][0]
        
        # Validate username if UUID not already set (only for viscount, not envoy)
        if not self.uuid and self.rank_key != "envoy":
            is_valid, error_msg, uuid = await validate_wynncraft_username(self.username)
            if not is_valid:
                await interaction.response.send_message(error_msg, ephemeral=True)
                return
            self.uuid = uuid
        
        # Go directly to confirmation (no alt account selection)
        await show_confirmation_embed(interaction, self.user, self.username, self.rank_key, pronoun_value, self.source_message_id, uuid=self.uuid)

class AcceptConfirmView(discord.ui.View):
    """View for handling accept confirmation with blacklist double-confirm"""
    def __init__(self, user, username, rank_key, pronoun_value, roles_to_add, roles_to_remove, nickname, is_blacklisted, executor_id, source_message_id=None, is_alt=False, main_username=None, uuid=None):
        super().__init__(timeout=None)
        self.user = user
        self.username = username
        self.rank_key = rank_key
        self.pronoun_value = pronoun_value
        self.roles_to_add = roles_to_add
        self.roles_to_remove = roles_to_remove
        self.nickname = nickname
        self.is_blacklisted = is_blacklisted
        self.executor_id = executor_id
        self.confirm_count = 0
        self.source_message_id = source_message_id
        self.is_alt = is_alt
        self.main_username = main_username
        self.uuid = uuid
        self.queued = False
        self.queue_position = None
        self.queue_type = None
        
        # Add confirm and cancel buttons
        confirm_button = discord.ui.Button(
            label="Confirm" if not is_blacklisted else "Confirm (1/2)",
            style=discord.ButtonStyle.success,
            custom_id="accept_confirm"
        )
        confirm_button.callback = self.confirm_callback
        
        cancel_button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.danger,
            custom_id="accept_cancel"
        )
        cancel_button.callback = self.cancel_callback
        
        self.add_item(confirm_button)
        self.add_item(cancel_button)
    
    def check_ex_member_role(self, user):
        """Check if user has ex-citizen or veteran role"""
        user_role_ids = [role.id for role in user.roles]
        
        has_veteran = VETERAN_ID in user_role_ids
        has_ex_citizen = EX_CITIZEN_ID in user_role_ids
        
        if has_veteran:
            return "veteran", True
        elif has_ex_citizen:
            return "ex_citizen", True
        else:
            return None, False
    
    async def confirm_callback(self, interaction: discord.Interaction):
        """Handle confirmation of the accept action"""
        
        # Check if user is the one who initiated the accept command
        if interaction.user.id != self.executor_id:
            await interaction.response.defer(ephemeral=True)
            error_embed = discord.Embed(
                title="Permission Denied",
                description="Only the user who used the `/accept` command can confirm this action.",
                color=0xFF0000
            )
            await interaction.edit_original_response(embed=error_embed)
            return
        
        # If blacklisted, require double confirmation
        if self.is_blacklisted:
            self.confirm_count += 1
            if self.confirm_count < 2:
                # Update button to show second confirmation needed
                self.children[0].label = "Confirm (2/2) - Click Again!"
                self.children[0].style = discord.ButtonStyle.danger
                await interaction.response.edit_message(view=self)
                return
        
        await interaction.response.defer()
        
        failed_action = None
        
        try:
            role_name, has_valid_role = self.check_ex_member_role(self.user)
            
            # Remove conflicting roles first
            if self.roles_to_remove:
                failed_action = "remove roles"
                await self.user.remove_roles(
                    *self.roles_to_remove,
                    reason=f"Rank change via /accept {self.rank_key} by {interaction.user.name}"
                )
            
            # Add new roles
            failed_action = "add roles"
            roles_to_add_final = list(self.roles_to_add)
            if self.is_alt:
                alt_role = interaction.guild.get_role(ALT_ACCOUNT_ID)
                if alt_role and alt_role not in roles_to_add_final:
                    roles_to_add_final.append(alt_role)
            
            if not roles_to_add_final:
                print(f"[WARN] No roles to add for user {self.user.id} - self.roles_to_add was empty!")
            
            await self.user.add_roles(
                *roles_to_add_final,
                reason=f"Role assignment via /accept {self.rank_key} by {interaction.user.name}"
            )
            
            # Determine previous rank BEFORE making any changes
            previous_rank = "None"
            for rank_id, rank_name in RANK_HIERARCHY:
                if rank_id in [role.id for role in self.user.roles]:
                    previous_rank = rank_name
                    break
            
            # Set nickname
            failed_action = "change nickname"
            nickname_status = await self.set_nickname(interaction)
            
            # Log the rank change
            try:
                # Determine previous rank
                previous_rank = "None"
                for rank_id, rank_name in RANK_HIERARCHY:
                    if rank_id in [role.id for role in self.user.roles]:
                        previous_rank = rank_name
                        break
                
                # Additional info for accept logs
                additional_info = {
                    'username_assigned': self.username,
                    'pronoun': self.pronoun_value if self.pronoun_value else None,
                    'nickname_status': nickname_status,
                    'was_blacklisted': self.is_blacklisted,
                    'returning_member': role_name in ("veteran", "ex_citizen") if role_name else False,
                    'is_alt': self.is_alt,
                    'main_username': self.main_username if self.is_alt else None
                }
                
                log_rank_change(
                    target_user_id=self.user.id,
                    target_username=str(self.user),
                    executor_user_id=interaction.user.id,
                    executor_username=str(interaction.user),
                    previous_rank=previous_rank,
                    new_rank=RANK_CONFIGS[self.rank_key]['display_name'],
                    action_type='accept',
                    guild_id=interaction.guild.id,
                    guild_name=interaction.guild.name,
                    additional_info=additional_info
                )
            except Exception as e:
                print(f"[WARN] Failed to log rank change: {e}")
            
            # Save alt account info to JSON
            in_guild = None
            if self.is_alt and self.main_username:
                try:
                    # Fetch UUID for main username if not already available
                    main_uuid = None
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(f"https://api.wynncraft.com/v3/player/{self.main_username}") as response:
                                if response.status == 200:
                                    data = await response.json()
                                    main_uuid = data.get('uuid')
                                    in_guild = data.get('guild').get('name') if data.get('guild').get('name') else None
                    except Exception as uuid_error:
                        print(f"[WARN] Could not fetch UUID for main username {self.main_username}: {uuid_error}")
                    
                    alt_accounts_file = Path('alt_accounts.json')
                    alt_accounts = {}
                    if alt_accounts_file.exists():
                        with open(alt_accounts_file, 'r') as f:
                            alt_accounts = json.load(f)
                    
                    alt_accounts[str(self.user.id)] = {
                        'alt_username': self.username,
                        'alt_uuid': self.uuid,
                        'main_username': self.main_username,
                        'main_uuid': main_uuid
                    }
                    
                    with open(alt_accounts_file, 'w') as f:
                        json.dump(alt_accounts, f, indent=4)
                        
                except Exception as e:
                    print(f"[WARN] Failed to save alt account info: {e}")
            
            # Check if player was already queued at approve-threshold time
            if self.rank_key != "envoy":
                try:
                    result = get_queue_position(self.user.id)
                    if result is not None:
                        queue_pos, queue_type = result
                        # Only show the "full capacity" message if the guild is still full
                        capacity = get_guild_capacity()
                        if capacity.get('is_full'):
                            self.queued = True
                            self.queue_position = queue_pos
                            self.queue_type = queue_type
                            print(f"[QUEUE] Player {self.username} is in {queue_type} queue at position {queue_pos} (guild still full)")
                        else:
                            print(f"[QUEUE] Player {self.username} was in queue but guild has open slots â€” showing normal accept message")
                        remove_from_queue(self.user.id)
                        print(f"[QUEUE] Removed {self.username} from queue (accepted)")
                except Exception as e:
                    print(f"[WARN] Failed to check queue position: {e}")

            # Create success embeds (public + private)
            full_embed = self.create_success_embed(nickname_status, role_name, interaction.user.name, in_guild)
            public_embed = self.create_success_embed(nickname_status, role_name, interaction.user.name, in_guild, public_only=True)
            
            # Send public summary to acceptance channel
            try:
                acceptance_channel = interaction.guild.get_channel(554892608409829422)  # ACCEPTANCE_CHANNEL_ID
                if acceptance_channel:
                    await acceptance_channel.send(embed=public_embed)
            except Exception as e:
                print(f"[WARN] Failed to send to acceptance channel: {e}")

            # Persist the mapping between the accepted Discord user and the entered username
            try:
                save_username_match(self.user.id, self.username, self.uuid)
            except Exception as e:
                # Log the error but don't interrupt a successful accept flow
                print(f"[WARN] Failed to persist username match for {self.user.id}: {e}")
            
            # Update the original public message with only the summary
            await interaction.edit_original_response(embed=public_embed, view=None)

            # Send the full accept message / invite / recruit info ephemerally to the executor
            followup_sent = False
            try:
                await interaction.followup.send(embed=full_embed, ephemeral=True)
                followup_sent = True
            except Exception as e:
                print(f"[WARN] Failed to send ephemeral accept details: {e}")
                import traceback
                traceback.print_exc()
            
            # If ephemeral followup failed, send as a second followup with a note
            if not followup_sent:
                try:
                    # Try again with a simpler message
                    await interaction.followup.send(
                        content=f"**Accept Details (ephemeral failed):**\n{full_embed.description[:1900] if full_embed.description else 'See above'}",
                        ephemeral=True
                    )
                except Exception as e2:
                    print(f"[WARN] Failed to send fallback accept details: {e2}")
            
            # This ensures it always runs when accept succeeds
            if self.source_message_id:
                try:
                    # Shared ticket helpers live in ticket_handler
                    from ticket_handler import (
                        load_forwarded_apps,
                        save_forwarded_apps,
                        calculate_threshold,
                        ApplicationMixedView,
                        ApplicationVoteView,
                    )

                    apps = load_forwarded_apps()
                    
                    if str(self.source_message_id) in apps:
                        app_data = apps[str(self.source_message_id)]
                        
                        # Mark as accepted instead of removing
                        apps[str(self.source_message_id)]['status'] = 'accepted'
                        apps[str(self.source_message_id)]['buttons_enabled'] = False
                        save_forwarded_apps(apps)
                        
                        # Get the message and disable its buttons
                        try:
                            channel_id = app_data['channel_id']
                            
                            # Try to get as a regular channel first
                            channel = interaction.guild.get_channel(channel_id)
                            
                            # If not found as a channel, try to get as a thread
                            if not channel:
                                channel = interaction.guild.get_thread(channel_id)
                            
                            if channel:
                                message = await channel.fetch_message(self.source_message_id)
                                
                                # Check if message already has a view
                                if message.components:
                                    # Get the current view to determine type
                                    threshold = calculate_threshold(interaction.guild)
                                    approve_count = app_data.get('approve_count', 0)
                                    deny_count = app_data.get('deny_count', 0)
                                    
                                    # Check if it's showing action buttons (Accept/Deny) or vote buttons
                                    is_mixed_view = False
                                    for action_row in message.components:
                                        for button in action_row.children:
                                            if hasattr(button, 'label') and ('Accept Application' in button.label or 'Deny Application' in button.label):
                                                is_mixed_view = True
                                                break
                                    
                                    if is_mixed_view:
                                        # Recreate ApplicationMixedView
                                        disabled_view = ApplicationMixedView(
                                            app_data,
                                            approve_count,
                                            deny_count,
                                            show_approve_action=(approve_count >= threshold or app_data.get('approve_notified', False)),
                                            show_deny_action=(deny_count >= threshold or app_data.get('deny_notified', False)),
                                            threshold=threshold
                                        )
                                    else:
                                        # Recreate ApplicationVoteView (used for envoy and initial forwarding)
                                        disabled_view = ApplicationVoteView(
                                            app_data,
                                            approve_count,
                                            deny_count,
                                            threshold=threshold
                                        )
                                    
                                    # Disable all buttons in the view
                                    for item in disabled_view.children:
                                        item.disabled = True

                                    await message.edit(view=disabled_view)
                                else:
                                    print(f"[DEBUG] Message has no components to disable")
                            else:
                                print(f"[DEBUG] Channel/thread {channel_id} not found in guild")
                        except discord.NotFound:
                            print(f"[DEBUG] Message {self.source_message_id} not found (deleted)")
                        except discord.Forbidden:
                            print(f"[DEBUG] Missing permissions to edit message {self.source_message_id}")
                        except Exception as e:
                            print(f"[DEBUG] Error disabling buttons: {e}")
                            import traceback
                            traceback.print_exc()
                    else:
                        print(f"[DEBUG] Application {self.source_message_id} not found in forwarded_applications.json")
                except Exception as e:
                    print(f"[DEBUG] Error handling forwarded app cleanup: {e}")
                    import traceback
                    traceback.print_exc()
            else:
                print(f"[DEBUG] No source_message_id provided, skipping forwarded app cleanup")
                
        except discord.Forbidden as e:
            error_embed = discord.Embed(
                title="Permission Error",
                description=f"Bot lacks permission to **{failed_action}**\n\n"
                        f"Please check:\n"
                        f"â€¢ Bot has **Manage Roles** permission\n"
                        f"â€¢ Bot has **Manage Nicknames** permission\n"
                        f"â€¢ Bot's role is higher than the roles being assigned\n\n"
                        f"Error details: {str(e)}",
                color=0xFF0000
            )
            await interaction.edit_original_response(embed=error_embed, view=None)
        except Exception as e:
            error_embed = discord.Embed(
                title="Error",
                description=f"An unexpected error occurred: {str(e)}",
                color=0xFF0000
            )
            await interaction.edit_original_response(embed=error_embed, view=None)
    
    async def cancel_callback(self, interaction: discord.Interaction):
        """Handle cancellation of the accept action"""
        # Check if user is the one who initiated the accept command
        if interaction.user.id != self.executor_id:
            await interaction.response.defer(ephemeral=True)
            error_embed = discord.Embed(
                title="Permission Denied",
                description="Only the user who used the `/accept` command can cancel this action.",
                color=0xFF0000
            )
            await interaction.edit_original_response(embed=error_embed)
            return
        
        embed = discord.Embed(
            title="Cancelled",
            description="Accept operation was cancelled.",
            color=0x999999
        )
        await interaction.response.edit_message(embed=embed, view=None)
    
    async def set_nickname(self, interaction):
        """Set nickname and return status"""
        if not self.nickname.strip():
            return "No nickname set"
        
        if not interaction.guild.me.guild_permissions.manage_nicknames:
            return "Bot lacks Manage Nicknames permission"
        
        if self.user == interaction.guild.owner:
            return "Cannot change server owner's nickname"
        
        if self.user.top_role >= interaction.guild.me.top_role:
            return "Cannot change - user's role is too high"
        
        try:
            await self.user.edit(
                nick=self.nickname,
                reason=f"Nickname set via /accept {self.rank_key} by {interaction.user.name}"
            )
            return "Set successfully"
        except:
            return "Failed to set nickname"
    
    def create_success_embed(self, nickname_status, role_name, executor_name, in_guild=None, public_only: bool = False):
        """Create the success embed.

        When public_only is True, only include the rank assignment line so it can be posted publically
        without leaking the accept message, invite command, or recruit command.
        """
        rank_config = RANK_CONFIGS[self.rank_key]

        # This header line is always safe to show publicly
        base_description = f"Successfully assigned **{rank_config['display_name']}** rank to {self.user.mention}"

        # If we only want the public summary, stop here
        if public_only:
            description = base_description
        else:
            # Use real newlines in the embed description
            description = base_description + "\n\n"

            if self.rank_key == "envoy":
                description += (
                    f"```Congratulations {self.user.mention}, your application for Envoy has been accepted, welcome to the ESI community!"
                    f" Head on over to <#606712832716832778> to socialise with other members, or <#1380986002419748905> "
                    f"to participate in our regular community events. You now also have access to some of our other text "
                    f"and voice channels, so feel free to check them out!```"
                )
            elif role_name in ("veteran", "ex_citizen"):
                description += f"**Accept message:**\n"
                if self.queued:
                    description += (
                        f"```Hello there {self.user.mention}, welcome back! You have been accepted into the guild! "
                        f"However, the guild is currently at full capacity so you have been placed in a waiting queue "
                        f"(position #{self.queue_position}). We will notify you as soon as a slot opens up! "
                        f"Feel free to check out <#606712832716832778>, <#1023821930404532286> and <#1381293188630843502> "
                        f"to catch up on what you've missed.\n\nIf you want to show off with "
                        f"guild badges, make sure your API is on so we can track your achievements and rank you up! "
                        f"(API is on by default)```"
                    )
                    description += f"\n\n**Guild invite command:**\n```/gu invite {self.username}```"
                else:
                    description += (
                        f"```Hello there {self.user.mention}, welcome back! You have been accepted into the guild! "
                        f"Feel free to check out <#606712832716832778>, <#1023821930404532286> and <#1381293188630843502> "
                        f"to catch up on what you've missed. You can join the guild in-game by typing '/gu join ESI' "
                        f"the next time you log on.\n\nIf you want to show off with "
                        f"guild badges, make sure your API is on so we can track your achievements and rank you up! "
                        f"(API is on by default)```"
                    )
                    description += f"\n\n**Guild invite command:**\n```/gu invite {self.username}```"
            else:
                description += f"**Accept message:**\n"
                mention_nova = (
                    f"(and thank Nova for his amazing job on the handbook). "
                    if self.executor_id == 967867229410574340
                    else ""
                )
                if self.queued:
                    description += (
                        f"```Congrats {self.user.mention}, your application has been accepted! However, the guild is currently "
                        f"at full capacity so you have been placed in a waiting queue (position #{self.queue_position}). "
                        f"We will notify you as soon as a slot opens up! In the meantime, feel free to check out "
                        f"the new channels you have access to such as <#1381289736903065662>, <#1381836409689870406> and "
                        f"<#1369575540675313674> if you want to grab some special roles. Also don't hesitate to head over "
                        f"to <#1381293188630843502> for more details on the guild! {mention_nova}"
                        f"If you want to show off with guild badges, make sure your API is on so we can track your "
                        f"achievements and rank you up! (API is on by default)```"
                    )
                    description += f"\n\n**Guild invite command:**\n```/gu invite {self.username}```"
                else:
                    description += (
                        f"```Congrats {self.user.mention}, your application has been accepted! Feel free to check out "
                        f"the new channels you have access to such as <#1381289736903065662>, <#1381836409689870406> and "
                        f"<#1369575540675313674> if you want to grab some special roles. Also don't hesitate to head over "
                        f"to <#1381293188630843502> for more details on the guild! {mention_nova}"
                    )
                    if in_guild:
                        description += (
                            f"Please make sure to leave your current guild so we can invite you and let us know when you do!\n\nIf you want to show off with "
                            f"guild badges, make sure your API is on so we can track your achievements and rank you up! (API is on by default)```"
                        )
                    else:
                        description += (
                            f"You can join the guild in-game by typing '/gu join ESI' the next time you log on.\n\nIf you want to show off with "
                            f"guild badges, make sure your API is on so we can track your achievements and rank you up! (API is on by default)```"
                        )
                    description += f"\n\n**Guild invite command:**\n```/gu invite {self.username}```"
                description += (
                    f"\n**Remember to add a recruitment point to the mentioned player in the app if needed using "
                    f"`/recruitment action:add recruiter:username recruit:{self.username}`**"
                )

        embed = discord.Embed(
            title="Action Complete",
            description=description,
            color=0x00FF00,
            timestamp=datetime.utcnow()
        )
        
        embed.set_thumbnail(url=self.user.display_avatar.url)
        embed.set_footer(text=f"Executed by {executor_name}")
        
        return embed

async def show_confirmation_embed(interaction: discord.Interaction, user: discord.Member, username: str, rank_key: str, pronoun_value: str = None, source_message_id: int = None, is_alt: bool = False, main_username: str = None, uuid: str = None):
    """Show the confirmation embed with all the rank assignment details"""
    
    rank_config = RANK_CONFIGS[rank_key]
    
    # Check if username is blacklisted
    blacklisted, blacklist_reason = is_blacklisted(username)
    
    # Get user's current role IDs
    user_role_ids = {role.id for role in user.roles}
    
    # Check for demotion
    current_rank = get_user_highest_rank(user_role_ids)
    if is_demotion(current_rank, rank_config["main_role"]):
        demotion_embed = discord.Embed(
            title="âŒ Demotion Not Allowed",
            description=f"{user.mention} currently has the **{current_rank[1]}** rank.\n\n"
                    f"Cannot assign **{rank_config['display_name']}** as it would be a demotion.",
            color=0xFF0000,
            timestamp=datetime.utcnow()
        )
        demotion_embed.set_footer(text=f"Requested by {interaction.user.name}")
        await interaction.response.edit_message(content=None, embed=demotion_embed, view=None)
        return
    
    # Determine which roles they need and which should be removed
    roles_to_add = []
    roles_to_add_mentions = []
    roles_already_have = []
    roles_to_remove = []
    roles_to_remove_mentions = []
    
    for role_id in rank_config["required_roles"]:
        role = interaction.guild.get_role(role_id)
        if role:
            if role_id in user_role_ids:
                roles_already_have.append(role.mention)
            else:
                roles_to_add.append(role)
                roles_to_add_mentions.append(role.mention)
    
    for role_id in rank_config["roles_to_remove"]:
        if role_id in user_role_ids:
            role = interaction.guild.get_role(role_id)
            if role:
                roles_to_remove.append(role)
                roles_to_remove_mentions.append(role.mention)
    
    # Determine the title and nickname
    if rank_key == "envoy":
        if pronoun_value == "she/her":
            title = "Lady"
        elif pronoun_value == "he/him":
            title = "Sir"
        else:
            title = "Envoy"
    elif rank_key == "viscount":
        if pronoun_value == "she/her":
            title = "Viscountess"
        elif pronoun_value == "he/him":
            title = "Viscount"
        else:
            title = "Viscount"
    else:
        title = rank_config['display_name']
    
    if is_alt and main_username:
        new_nickname = f"{title} {username} ({main_username})"
    else:
        new_nickname = f"{title} {username}"

    # Create embed with blacklist warning if applicable
    if blacklisted:
        embed = discord.Embed(
            title="BLACKLIST WARNING - Confirm Rank Assignment",
            description="**WARNING: This user is blacklisted!**\n\nPlease confirm the following rank assignment:",
            color=0xFF0000,
            timestamp=datetime.utcnow()
        )
    else:
        embed = discord.Embed(
            title="Confirm Rank Assignment",
            description="Please confirm the following rank assignment:",
            color=0xFFA500,
            timestamp=datetime.utcnow()
        )
    
    embed.set_thumbnail(url=user.display_avatar.url)
    
    # Add blacklist warning at the top if blacklisted
    if blacklisted:
        blacklist_text = f"ðŸš« **BLACKLISTED USER DETECTED**\n"
        blacklist_text += f"**Reason:** {blacklist_reason if blacklist_reason else 'No reason provided'}\n"
        blacklist_text += f"**NameMC Profile:** [View Profile](https://namemc.com/search?q={username})\n"
        blacklist_text += f"\n**Please verify this is intentional before proceeding!**\n**You must click the confirm button TWICE.**"
        embed.add_field(name="Blacklist Status", value=blacklist_text, inline=False)

    embed.add_field(name="Target Member", value=f"{user.mention}", inline=True)
    embed.add_field(name="Rank", value=rank_config['display_name'], inline=True)
    embed.add_field(name="Username", value=f"`{username}`", inline=True)
    
    if pronoun_value:
        if rank_key == "viscount":
            pronoun_display = f"{pronoun_value} â†’ {'Viscountess' if pronoun_value == 'she/her' else 'Viscount'}"
        elif rank_key == "envoy":
            if pronoun_value == "she/her":
                pronoun_display = f"{pronoun_value} â†’ Lady"
            elif pronoun_value == "he/him":
                pronoun_display = f"{pronoun_value} â†’ Sir"
            else:
                pronoun_display = f"{pronoun_value} â†’ Envoy"
        else:
            pronoun_display = f"{pronoun_value}"
        embed.add_field(name="Pronoun", value=pronoun_display, inline=False)
    
    if roles_to_add_mentions:
        embed.add_field(name="Roles to Add", value="\n".join([f"â€¢ {role}" for role in roles_to_add_mentions]), inline=False)
    
    if roles_to_remove_mentions:
        embed.add_field(name="Roles to Remove", value="\n".join([f"â€¢ {role}" for role in roles_to_remove_mentions]), inline=False)
    
    embed.add_field(name="New Nickname", value=f"`{new_nickname}`", inline=False)
    embed.add_field(name="Important", value="This action cannot be undone. Please verify all information is correct before confirming.", inline=False)
    
    # Create the confirmation view with actual role objects
    view = AcceptConfirmView(
        user, username, rank_key, pronoun_value, 
        roles_to_add, roles_to_remove, new_nickname, 
        blacklisted, interaction.user.id,
        source_message_id=source_message_id,
        is_alt=is_alt,
        main_username=main_username,
        uuid=uuid
    )
    
    # Edit the ephemeral message and send the embed publicly with buttons
    await interaction.response.edit_message(content="âœ… Rank assignment details posted!", view=None)

    # If we're in a thread, send to the parent channel instead
    target_channel = interaction.channel
    if isinstance(interaction.channel, discord.Thread):
        target_channel = interaction.channel.parent

    await target_channel.send(embed=embed, view=view)

def get_user_highest_rank(user_role_ids):
    """Get the highest rank a user currently has"""
    for rank_id, rank_name in RANK_HIERARCHY:
        if rank_id in user_role_ids:
            return (rank_id, rank_name)
    return None

def is_demotion(current_rank, target_rank):
    """Check if giving target_rank would be a demotion"""
    if current_rank is None:
        return False
    
    current_index = next((i for i, (rid, _) in enumerate(RANK_HIERARCHY) if rid == current_rank[0]), None)
    target_index = next((i for i, (rid, _) in enumerate(RANK_HIERARCHY) if rid == target_rank), None)
    
    if current_index is None or target_index is None:
        return False
    
    return target_index > current_index  # Higher index = lower rank

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    @bot.tree.command(
        name="accept",
        description="Check which roles a user needs for a specific rank"
    )
    @app_commands.describe(
        user="The user to check"
    )
    async def accept(
        interaction: discord.Interaction,
        user: discord.Member
    ):
        """Check which roles a user needs for a specific rank"""

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
        
        # Create modal for username input
        username_modal = UsernameModal(user)
        await interaction.response.send_modal(username_modal)
    
    @bot.tree.context_menu(name="Accept User")
    async def accept_user(interaction: discord.Interaction, user: discord.Member):
        """Accept a user via context menu"""
        
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
        
        # Create modal for username input
        username_modal = UsernameModal(user)
        await interaction.response.send_modal(username_modal)

    print("[OK] Loaded accept user context menu")
    print("[OK] Loaded accept command")
