import discord
from discord import app_commands
from discord.ui import Select, View
import os
from datetime import datetime
from pathlib import Path
import json
import sys
current_dir = Path(__file__).parent
if str(current_dir) not in sys.path:
    sys.path.insert(0, str(current_dir))

from ticket_handler import (
    ApplicationVoteView, 
    ApplicationMixedView, 
    ApplicationActionView,
    DenyReasonModal,
    calculate_threshold,
    save_forwarded_apps,
    load_pending_apps,
    save_pending_apps,
)
from guild_queue import (
    get_queue_position,
    add_to_queue,
    remove_from_queue,
    load_queue,
    get_guild_capacity,
    get_capacity_override,
    set_capacity_override,
    clear_capacity_override,
    extract_username_from_embeds,
    VETERAN_ROLE_ID,
)
from utils.permissions import has_roles

REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0,
    600185623474601995, # Parliament
    954566591520063510  # Jurors
]

# Roles that have restricted access (can view but not manage votes or send reminders)
RESTRICTED_ROLES = [
    954566591520063510  # Jurors
]

_ROOT = Path(__file__).resolve().parent.parent.parent
FORWARDED_APPS_FILE = _ROOT / 'data' / 'forwarded_applications.json'
PENDING_APPS_FILE = _ROOT / 'data' / 'pending_applications.json'

def load_pending_apps():
    """Load pending applications from JSON file"""
    if PENDING_APPS_FILE.exists():
        with open(PENDING_APPS_FILE, 'r') as f:
            return json.load(f)
    return {}

def load_forwarded_apps():
    """Load forwarded applications from JSON file"""
    if FORWARDED_APPS_FILE.exists():
        with open(FORWARDED_APPS_FILE, 'r') as f:
            return json.load(f)
    return {}

def is_restricted_user(user):
    """Check if user has restricted access (can view but not manage)"""
    user_role_ids = [role.id for role in user.roles]
    # User is restricted if they ONLY have restricted roles and not parliament/owner
    has_parliament = 600185623474601995 in user_role_ids
    has_owner = user.id == int(os.getenv('OWNER_ID', '0'))
    has_restricted = any(role_id in user_role_ids for role_id in RESTRICTED_ROLES)
    return has_restricted and not has_parliament and not has_owner


def is_bot_owner(user) -> bool:
    """Return True when ``user`` is the bot owner (``OWNER_ID`` env var)."""
    try:
        owner_id = int(os.getenv('OWNER_ID', '0'))
    except (TypeError, ValueError):
        return False
    return bool(owner_id) and getattr(user, 'id', None) == owner_id

class EmbedBuilder:
    """Utility class for building ticket-related embeds"""
    
    @staticmethod
    def build_vote_display(voters, threshold, vote_type="Approve"):
        """Build formatted voter list with admin vote handling"""
        mentions = []
        for voter_id in voters:
            if voter_id < 0:
                mentions.append("**[Admin Vote]**")
            else:
                mentions.append(f"<@{voter_id}>")
        
        text = ", ".join(mentions) if mentions else "*None*"
        if len(voters) > 10:
            text = ", ".join(mentions[:10])
            text += f"\n*... and {len(voters) - 10} more*"
        
        emoji = "✅" if vote_type == "Approve" else "❌"
        return {
            "name": f"{emoji} {vote_type} Votes ({len(voters)}/{threshold})",
            "value": text,
            "inline": False
        }
    
    @staticmethod
    def build_ticket_embed(app_data, guild, title="Manage Ticket"):
        """Build standard ticket information embed"""
        
        # Get status
        status = app_data.get('status', 'pending')
        status_emoji = {
            'pending': '⏳',
            'accepted': '✅',
            'denied': '❌'
        }.get(status, '⏳')
        
        title = f"{status_emoji} {title} - {status}"
        
        # Get user info
        user = guild.get_member(app_data['user_id'])
        user_mention = user.mention if user else f"<@{app_data['user_id']}>"
        
        # Get channel info
        channel = guild.get_channel(app_data['ticket_channel_id'])
        if not channel:
            channel = guild.get_thread(app_data['ticket_channel_id'])
        channel_mention = channel.mention if channel else f"<#{app_data['ticket_channel_id']}>"
        
        status_text = status.capitalize()
        
        embed = discord.Embed(
            title=title,
            description=f"**Application Type:** {app_data['app_type']}\n**Applicant:** {user_mention}",
            color=0x5865F2,
            timestamp=datetime.utcfromtimestamp(app_data.get('timestamp', 0))
        )
        
        embed.add_field(name="Channel", value=channel_mention, inline=True)
        embed.add_field(name="Message ID", value=f"`{app_data['message_id']}`", inline=True)
        embed.add_field(name="User ID", value=f"`{app_data['user_id']}`", inline=True)
        
        # Add timestamp field
        timestamp = app_data.get('timestamp', 0)
        if timestamp:
            embed.add_field(
                name="Submitted",
                value=f"<t:{int(timestamp)}:F> (<t:{int(timestamp)}:R>)",
                inline=False
            )
        
        return embed
    
    @staticmethod
    def add_queue_field(embed, app_data):
        """Add guild queue position field to embed if applicable"""
        guild_app = app_data.get('app_type', '').lower() == 'guild member' or app_data.get('app_type', '').lower() == 'ex-citizen'
        if guild_app:
            try:
                result = get_queue_position(app_data['user_id'])
                if result is not None:
                    queue_pos, queue_type = result
                    embed.add_field(
                        name="\u23f3 Guild Queue",
                        value=f"Position **#{queue_pos}** - guild is at full capacity",
                        inline=False
                    )
            except Exception:
                pass

class VoteManager:
    """Utility class for managing votes and updating message views"""
    
    @staticmethod
    async def update_message_view(interaction, app_data):
        """Update the view on the original forwarded message"""
        try:
            channel = interaction.guild.get_channel(app_data['channel_id'])
            if not channel:
                channel = interaction.guild.get_thread(app_data['channel_id'])
            
            if not channel:
                return
            
            message = await channel.fetch_message(app_data['message_id'])
            
            
            threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
            approve_count = app_data.get('approve_count', 0)
            deny_count = app_data.get('deny_count', 0)
            
            approve_threshold_met = approve_count >= threshold
            deny_threshold_met = deny_count >= threshold
            
            buttons_enabled = app_data.get('buttons_enabled', True)
            
            # Reset notified flags if counts drop below threshold
            if approve_count < threshold and app_data.get('approve_notified', False):
                apps = load_forwarded_apps()
                apps[str(app_data['message_id'])]['approve_notified'] = False
                save_forwarded_apps(apps)
                app_data['approve_notified'] = False
                
            if deny_count < threshold and app_data.get('deny_notified', False):
                apps = load_forwarded_apps()
                apps[str(app_data['message_id'])]['deny_notified'] = False
                save_forwarded_apps(apps)
                app_data['deny_notified'] = False
            
            if approve_threshold_met or deny_threshold_met:
                new_view = ApplicationMixedView(
                    app_data,
                    approve_count,
                    deny_count,
                    show_approve_action=approve_threshold_met,
                    show_deny_action=deny_threshold_met,
                    threshold=threshold
                )
            else:
                new_view = ApplicationVoteView(
                    app_data,
                    approve_count,
                    deny_count,
                    threshold=threshold
                )
            
            if not buttons_enabled:
                for item in new_view.children:
                    item.disabled = True
            
            await message.edit(view=new_view)
        except Exception as e:
            print(f"Error updating message view: {e}")
    
    @staticmethod
    def generate_admin_vote_id(approve_voters, deny_voters):
        """Generate unique negative ID for admin votes"""
        import random
        admin_vote_id = -random.randint(1000000, 9999999)
        while admin_vote_id in approve_voters or admin_vote_id in deny_voters:
            admin_vote_id = -random.randint(1000000, 9999999)
        return admin_vote_id

class VoteModificationView(View):
    """View for modifying individual votes"""
    def __init__(self, message_id, tickets_data, guild):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.tickets_data = tickets_data
        self.guild = guild
        
        # Back button FIRST
        back_button = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.secondary,
            emoji="◀️"
        )
        back_button.callback = self.back_callback
        self.add_item(back_button)
        
        # Add admin approve vote button
        add_approve_button = discord.ui.Button(
            label="Add Approve Vote",
            style=discord.ButtonStyle.success,
            emoji="➕"
        )
        add_approve_button.callback = self.add_approve_callback
        self.add_item(add_approve_button)
        
        # Add admin deny vote button
        add_deny_button = discord.ui.Button(
            label="Add Deny Vote",
            style=discord.ButtonStyle.danger,
            emoji="➕"
        )
        add_deny_button.callback = self.add_deny_callback
        self.add_item(add_deny_button)
        
        # Remove votes button
        remove_button = discord.ui.Button(
            label="Remove Votes",
            style=discord.ButtonStyle.secondary,
            emoji="➖"
        )
        remove_button.callback = self.remove_votes_callback
        self.add_item(remove_button)
        
        # Auto-fill votes button
        autofill_button = discord.ui.Button(
            label="Fill to Threshold",
            style=discord.ButtonStyle.success,
            emoji="⚡"
        )
        autofill_button.callback = self.autofill_votes_callback
        self.add_item(autofill_button)
    
    async def autofill_votes_callback(self, interaction: discord.Interaction):
        """Auto-fill votes to reach threshold"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        approve_count = len(approve_voters)
        deny_count = len(deny_voters)
        
        # Check if either is already at threshold
        if approve_count >= threshold and deny_count >= threshold:
            await interaction.response.send_message(
                "⚠️ Both approve and deny votes are already at or above threshold!",
                ephemeral=True
            )
            return
        
        # Calculate how many votes needed
        approve_needed = max(0, threshold - approve_count)
        deny_needed = max(0, threshold - deny_count)
        
        if approve_needed == 0 and deny_needed == 0:
            await interaction.response.send_message(
                "⚠️ Both vote types are already at threshold!",
                ephemeral=True
            )
            return
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "⚡ Auto-fill Votes to Threshold")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(approve_voters, threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(deny_voters, threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = AutofillSelectionView(self.message_id, self.tickets_data, self.guild, approve_needed, deny_needed, threshold)
        await interaction.response.edit_message(embed=embed, view=view)
    
    async def add_approve_callback(self, interaction: discord.Interaction):
        """Add admin approve vote"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        # Generate admin vote ID using utility
        admin_vote_id = VoteManager.generate_admin_vote_id(approve_voters, deny_voters)
        
        # Add admin vote
        approve_voters.append(admin_vote_id)
        app_data['approve_count'] = len(approve_voters)
        app_data['approve_voters'] = approve_voters
        apps[self.message_id] = app_data
        
        save_forwarded_apps(apps)
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "Manage Ticket")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(approve_voters, threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(app_data.get('deny_voters', []), threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = VoteModificationView(self.message_id, self.tickets_data, self.guild)
        await interaction.response.edit_message(embed=embed, view=view)
        
        # Update the message view using utility
        await VoteManager.update_message_view(interaction, app_data)
    
    async def add_deny_callback(self, interaction: discord.Interaction):
        """Add admin deny vote"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        # Generate admin vote ID using utility
        admin_vote_id = VoteManager.generate_admin_vote_id(approve_voters, deny_voters)
        
        # Add admin vote
        deny_voters.append(admin_vote_id)
        app_data['deny_count'] = len(deny_voters)
        app_data['deny_voters'] = deny_voters
        apps[self.message_id] = app_data
        
        save_forwarded_apps(apps)
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "Manage Ticket")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(app_data.get('approve_voters', []), threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(deny_voters, threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = VoteModificationView(self.message_id, self.tickets_data, self.guild)
        await interaction.response.edit_message(embed=embed, view=view)
        
        # Update the message view using utility
        await VoteManager.update_message_view(interaction, app_data)
    
    async def remove_votes_callback(self, interaction: discord.Interaction):
        """Show interface to remove specific votes"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        if not approve_voters and not deny_voters:
            await interaction.response.send_message(
                "⚠️ No votes to remove!",
                ephemeral=True
            )
            return
        
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "➖ Remove Votes")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(approve_voters, threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(deny_voters, threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = RemoveVotesView(self.message_id, self.tickets_data, self.guild, approve_voters, deny_voters)
        await interaction.response.edit_message(embed=embed, view=view)
    
    async def back_callback(self, interaction: discord.Interaction):
        """Go back to vote management"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "Manage Ticket")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(approve_voters, threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(deny_voters, threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = VoteManagementView(self.message_id, self.tickets_data, self.guild)
        await interaction.response.edit_message(embed=embed, view=view)

class TicketDetailView(View):
    """View for ticket details with action buttons"""
    def __init__(self, message_id, tickets_data, guild, user=None):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.tickets_data = tickets_data
        self.guild = guild
        self.user = user
        
        # Check if user is restricted
        is_restricted = is_restricted_user(user) if user else False
        
        # Back button
        back_button = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.secondary,
            emoji="◀️"
        )
        back_button.callback = self.back_callback
        self.add_item(back_button)
        
        # Jump to message button
        apps = load_forwarded_apps()
        app_data = apps.get(message_id)
        if app_data:
            jump_button = discord.ui.Button(
                label="Jump to Message",
                url=f"https://discord.com/channels/{guild.id}/{app_data['channel_id']}/{app_data['message_id']}",
                emoji="🔗"
            )
            self.add_item(jump_button)
        
        # Send reminder button (hidden for restricted users)
        if not is_restricted:
            reminder_button = discord.ui.Button(
                label="Send Reminder",
                style=discord.ButtonStyle.secondary,
                emoji="🔔"
            )
            reminder_button.callback = self.send_reminder_callback
            self.add_item(reminder_button)
        
        # Manage votes button (hidden for restricted users)
        if not is_restricted:
            manage_button = discord.ui.Button(
                label="Manage Ticket",
                style=discord.ButtonStyle.primary,
                emoji="⚙️"
            )
            manage_button.callback = self.manage_votes_callback
            self.add_item(manage_button)

        # Owner‑only debug button for end‑to‑end ticket + queue testing
        if is_bot_owner(user):
            debug_button = discord.ui.Button(
                label="Debug",
                style=discord.ButtonStyle.secondary,
                emoji="🛠️",
            )
            debug_button.callback = self.debug_callback
            self.add_item(debug_button)
    
    async def send_reminder_callback(self, interaction: discord.Interaction):
        """Send a reminder to vote on the application"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Get the channel and message
            channel = interaction.guild.get_channel(app_data['channel_id'])
            if not channel:
                channel = interaction.guild.get_thread(app_data['channel_id'])
            
            if not channel:
                await interaction.followup.send("❌ Channel not found!", ephemeral=True)
                return
            
            # If it's a thread, use the parent_message_id to get the message in the main channel
            if isinstance(channel, discord.Thread) and app_data.get('parent_message_id'):
                # Get the parent channel
                parent_channel = channel.parent
                try:
                    message = await parent_channel.fetch_message(app_data['parent_message_id'])
                except discord.NotFound:
                    await interaction.followup.send("❌ Parent message not found!", ephemeral=True)
                    return
            else:
                # Regular channel or no parent message ID
                try:
                    message = await channel.fetch_message(app_data['message_id'])
                except discord.NotFound:
                    await interaction.followup.send("❌ Message not found!", ephemeral=True)
                    return
            
            # Calculate threshold and current votes
            threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
            approve_count = app_data.get('approve_count', 0)
            deny_count = app_data.get('deny_count', 0)
            
            # Get applicant mention
            applicant = interaction.guild.get_member(app_data['user_id'])
            applicant_mention = applicant.mention if applicant else f"<@{app_data['user_id']}>"
            
            # Send reminder
            reminder_text = f"Reminder: {applicant_mention}'s **{app_data['app_type']}** application needs your votes!"
            reminder_text += f" Current votes: **{approve_count}/{threshold}** approve, **{deny_count}/{threshold}** deny. Please review and vote!"
            
            await message.reply(reminder_text)
            
        except discord.NotFound:
            await interaction.response.send_message("❌ Message not found!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error sending reminder: {e}", ephemeral=True)
    
    async def back_callback(self, interaction: discord.Interaction):
        """Go back to ticket list"""
        # Load fresh data to include pending apps
        tickets = load_forwarded_apps()
        pending_apps = load_pending_apps()
        
        list_embed = discord.Embed(
            title="🎫 Manage Tickets",
            description=f"Found **{len(tickets) + len(pending_apps)}** application(s) ({len(tickets)} submitted, {len(pending_apps)} pending).\nSelect a ticket from the dropdown below to view details.",
            color=0x5865F2,
            timestamp=datetime.utcnow()
        )
        new_view = TicketSelectorView(tickets, interaction.guild, pending_apps)
        await interaction.response.edit_message(
            embed=list_embed,
            view=new_view
        )
    
    async def manage_votes_callback(self, interaction: discord.Interaction):
        """Show vote management interface"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message(
                "❌ Ticket data not found!",
                ephemeral=True
            )
            return
        
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "Manage Ticket")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(approve_voters, threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(deny_voters, threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = VoteManagementView(self.message_id, self.tickets_data, self.guild)
        await interaction.response.edit_message(embed=embed, view=view)

    async def debug_callback(self, interaction: discord.Interaction):
        """Open the owner‑only debug panel for this ticket."""
        if not is_bot_owner(interaction.user):
            await interaction.response.send_message(
                "❌ Debug tools are restricted to the bot owner.", ephemeral=True
            )
            return

        embed, _ = build_debug_embed(self.message_id, interaction.guild)
        if embed is None:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return

        view = DebugTicketView(self.message_id, self.tickets_data, self.guild, interaction.user)
        await interaction.response.edit_message(embed=embed, view=view)

class TicketDetailViewStandalone(View):
    """Standalone view for ticket details (accessed from vote buttons)"""
    def __init__(self, message_id, tickets_data, guild, user=None):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.tickets_data = tickets_data
        self.guild = guild
        self.user = user
        
        # Check if user is restricted
        is_restricted = is_restricted_user(user) if user else False
        
        # Jump to message button
        apps = load_forwarded_apps()
        app_data = apps.get(message_id)
        if app_data:
            jump_button = discord.ui.Button(
                label="Jump to Message",
                url=f"https://discord.com/channels/{guild.id}/{app_data['channel_id']}/{app_data['message_id']}",
                emoji="🔗"
            )
            self.add_item(jump_button)
        
        # Send reminder button (hidden for restricted users)
        if not is_restricted:
            reminder_button = discord.ui.Button(
                label="Send Reminder",
                style=discord.ButtonStyle.secondary,
                emoji="🔔"
            )
            reminder_button.callback = self.send_reminder_callback
            self.add_item(reminder_button)
        
        # Manage votes button (hidden for restricted users)
        if not is_restricted:
            manage_button = discord.ui.Button(
                label="Manage Ticket",
                style=discord.ButtonStyle.primary,
                emoji="⚙️"
            )
            manage_button.callback = self.manage_votes_callback
            self.add_item(manage_button)
    
    async def send_reminder_callback(self, interaction: discord.Interaction):
        """Send a reminder to vote on the application"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Get the channel and message
            channel = interaction.guild.get_channel(app_data['channel_id'])
            if not channel:
                channel = interaction.guild.get_thread(app_data['channel_id'])
            
            if not channel:
                await interaction.followup.send("❌ Channel not found!", ephemeral=True)
                return
            
            # If it's a thread, use the parent_message_id to get the message in the main channel
            if isinstance(channel, discord.Thread) and app_data.get('parent_message_id'):
                # Get the parent channel
                parent_channel = channel.parent
                try:
                    message = await parent_channel.fetch_message(app_data['parent_message_id'])
                except discord.NotFound:
                    await interaction.followup.send("❌ Parent message not found!", ephemeral=True)
                    return
            else:
                # Regular channel or no parent message ID
                try:
                    message = await channel.fetch_message(app_data['message_id'])
                except discord.NotFound:
                    await interaction.followup.send("❌ Message not found!", ephemeral=True)
                    return
            
            # Calculate threshold and current votes
            threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
            approve_count = app_data.get('approve_count', 0)
            deny_count = app_data.get('deny_count', 0)
            
            # Get applicant mention
            applicant = interaction.guild.get_member(app_data['user_id'])
            applicant_mention = applicant.mention if applicant else f"<@{app_data['user_id']}>"
            
            # Send reminder
            reminder_text = f"Reminder: {applicant_mention}'s **{app_data['app_type']}** application needs your votes!"
            reminder_text += f" Current votes: **{approve_count}/{threshold}** approve, **{deny_count}/{threshold}** deny. Please review and vote!"
            
            await message.reply(reminder_text)
            await interaction.followup.send("✅ Reminder sent!", ephemeral=True)
            
        except Exception as e:
            await interaction.followup.send(f"❌ Error sending reminder: {e}", ephemeral=True)
    
    async def manage_votes_callback(self, interaction: discord.Interaction):
        """Show vote management interface"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message(
                "❌ Ticket data not found!",
                ephemeral=True
            )
            return
        
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "Manage Ticket")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(approve_voters, threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(deny_voters, threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = VoteManagementViewStandalone(self.message_id, self.tickets_data, self.guild, self.user)
        await interaction.response.edit_message(embed=embed, view=view)

class VoteManagementView(View):
    """View for managing votes on a ticket"""
    def __init__(self, message_id, tickets_data, guild):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.tickets_data = tickets_data
        self.guild = guild
        
        # Back button FIRST
        back_button = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.secondary,
            emoji="◀️"
        )
        back_button.callback = self.back_to_details
        self.add_item(back_button)
        
        # Add toggle buttons button
        toggle_button = discord.ui.Button(
            label="Toggle Buttons",
            style=discord.ButtonStyle.secondary,
            emoji="🔁"
        )
        toggle_button.callback = self.toggle_buttons_callback
        self.add_item(toggle_button)
        
        # Modified: Single button to access vote modification options
        modify_votes_button = discord.ui.Button(
            label="Modify Votes",
            style=discord.ButtonStyle.primary,
            emoji="⚙️"
        )
        modify_votes_button.callback = self.modify_votes_callback
        self.add_item(modify_votes_button)
        
        # Reload application button
        reload_button = discord.ui.Button(
            label="Reload Application",
            style=discord.ButtonStyle.success,
            emoji="🛠️"
        )
        reload_button.callback = self.reload_application_callback
        self.add_item(reload_button)
    
    async def reload_application_callback(self, interaction: discord.Interaction):
        """Reload the application message view"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Check thresholds and reset status if needed
            threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
            approve_count = app_data.get('approve_count', 0)
            deny_count = app_data.get('deny_count', 0)
            
            # If neither threshold is met, clear the status
            if approve_count < threshold and deny_count < threshold:
                if 'status' in app_data:
                    del app_data['status']
                apps[self.message_id] = app_data
                save_forwarded_apps(apps)
            
            # Update the message view using utility
            await VoteManager.update_message_view(interaction, app_data)
        except Exception as e:
            await interaction.followup.send(f"❌ Error reloading application: {e}", ephemeral=True)
    
    async def modify_votes_callback(self, interaction: discord.Interaction):
        """Show vote modification options"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "Manage Ticket")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(approve_voters, threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(deny_voters, threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = VoteModificationView(self.message_id, self.tickets_data, self.guild)
        await interaction.response.edit_message(embed=embed, view=view)
        
    async def toggle_buttons_callback(self, interaction: discord.Interaction):
        """Toggle buttons on the forwarded application"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        # Defer the interaction immediately
        await interaction.response.defer()
        
        try:
            channel = interaction.guild.get_channel(app_data['channel_id'])
            if not channel:
                channel = interaction.guild.get_thread(app_data['channel_id'])
            
            if not channel:
                await interaction.followup.send("❌ Channel not found!", ephemeral=True)
                return
            
            message = await channel.fetch_message(app_data['message_id'])
            
            # Check current state
            current_state = app_data.get('buttons_enabled', True)
            new_state = not current_state
            
            # Update the state in database AND clear status if re-enabling
            apps[self.message_id]['buttons_enabled'] = new_state
            if new_state and 'status' in apps[self.message_id]:
                del apps[self.message_id]['status']  # Clear accepted/denied status when re-enabling
            save_forwarded_apps(apps)

            apps = load_forwarded_apps()
            app_data = apps[self.message_id]

            # Get current view
            threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
            approve_count = app_data.get('approve_count', 0)
            deny_count = app_data.get('deny_count', 0)

            # Check if notified flags are set OR if counts meet threshold
            approve_threshold_met = approve_count >= threshold or app_data.get('approve_notified', False)
            deny_threshold_met = deny_count >= threshold or app_data.get('deny_notified', False)
            
            # Create appropriate view
            if approve_threshold_met or deny_threshold_met:
                new_view = ApplicationMixedView(
                    app_data,
                    approve_count,
                    deny_count,
                    show_approve_action=approve_threshold_met,
                    show_deny_action=deny_threshold_met,
                    threshold=threshold
                )
            else:
                new_view = ApplicationVoteView(
                    app_data,
                    approve_count,
                    deny_count,
                    threshold=threshold
                )
            
            # Disable or enable all buttons based on new state
            for item in new_view.children:
                item.disabled = not new_state
            
            await message.edit(view=new_view)
            
        except Exception as e:
            print(f"Error toggling buttons: {e}")
            await interaction.followup.send(f"❌ Error toggling buttons: {e}", ephemeral=True)
    
    async def add_approve_callback(self, interaction: discord.Interaction):
        """Add admin approve vote"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        # Generate admin vote ID using utility
        admin_vote_id = VoteManager.generate_admin_vote_id(approve_voters, deny_voters)
        
        # Add admin vote
        approve_voters.append(admin_vote_id)
        app_data['approve_count'] = len(approve_voters)
        app_data['approve_voters'] = approve_voters
        
        
        # Add admin vote
        approve_voters.append(admin_vote_id)
        app_data['approve_count'] = len(approve_voters)
        app_data['approve_voters'] = approve_voters
        
        # Check thresholds and reset status if needed
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        approve_count = len(approve_voters)
        deny_count = len(app_data.get('deny_voters', []))
        
        if approve_count < threshold and deny_count < threshold:
            if 'status' in app_data:
                del app_data['status']
        
        apps[self.message_id] = app_data
        
        save_forwarded_apps(apps)
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "Manage Ticket")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(approve_voters, threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(app_data.get('deny_voters', []), threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = VoteManagementView(self.message_id, self.tickets_data, self.guild)
        await interaction.response.edit_message(embed=embed, view=view)
        
        # Update the message view using utility
        await VoteManager.update_message_view(interaction, app_data)
    
    async def add_deny_callback(self, interaction: discord.Interaction):
        """Add admin deny vote"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        # Generate admin vote ID using utility
        admin_vote_id = VoteManager.generate_admin_vote_id(approve_voters, deny_voters)
        
        # Add admin vote
        deny_voters.append(admin_vote_id)
        app_data['deny_count'] = len(deny_voters)
        app_data['deny_voters'] = deny_voters
        
        # Check thresholds and reset status if needed
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        approve_count = len(approve_voters)
        deny_count = len(app_data.get('deny_voters', []))
        
        if approve_count < threshold and deny_count < threshold:
            if 'status' in app_data:
                del app_data['status']
        
        apps[self.message_id] = app_data
        
        save_forwarded_apps(apps)
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "Manage Ticket")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(app_data.get('approve_voters', []), threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(deny_voters, threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = VoteManagementView(self.message_id, self.tickets_data, self.guild)
        await interaction.response.edit_message(embed=embed, view=view)
        
        # Update the message view using utility
        await VoteManager.update_message_view(interaction, app_data)
    
    async def remove_votes_callback(self, interaction: discord.Interaction):
        """Show interface to remove specific votes"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        if not approve_voters and not deny_voters:
            await interaction.response.send_message(
                "⚠️ No votes to remove!",
                ephemeral=True
            )
            return
        
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "➖ Remove Votes")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(approve_voters, threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(deny_voters, threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = RemoveVotesView(self.message_id, self.tickets_data, self.guild, approve_voters, deny_voters)
        await interaction.response.edit_message(embed=embed, view=view)
    
    async def back_to_details(self, interaction: discord.Interaction):
        """Go back to ticket details"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "Manage Ticket")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(app_data.get('approve_voters', []), threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(app_data.get('deny_voters', []), threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        detail_view = TicketDetailView(self.message_id, self.tickets_data, self.guild, interaction.user)
        await interaction.response.edit_message(embed=embed, view=detail_view)
    
    async def update_message_view(self, interaction: discord.Interaction, app_data):
        """Update the view on the original forwarded message"""
        try:
            apps = load_forwarded_apps()
            app_data = apps.get(str(app_data['message_id']))
            
            if not app_data:
                return
            
            channel = interaction.guild.get_channel(app_data['channel_id'])
            if not channel:
                channel = interaction.guild.get_thread(app_data['channel_id'])
            
            if channel:
                message = await channel.fetch_message(app_data['message_id'])
                
                current_dir = Path(__file__).parent
                if str(current_dir) not in sys.path:
                    sys.path.insert(0, str(current_dir))
                
                threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
                approve_count = app_data.get('approve_count', 0)
                deny_count = app_data.get('deny_count', 0)
                
                # Check if thresholds are CURRENTLY met (not just if they were notified before)
                approve_threshold_met = approve_count >= threshold
                deny_threshold_met = deny_count >= threshold
                
                # Check buttons_enabled state
                buttons_enabled = app_data.get('buttons_enabled', True)
                
                # Reset notified flags if counts drop below threshold
                if approve_count < threshold and app_data.get('approve_notified', False):
                    apps = load_forwarded_apps()
                    apps[str(app_data['message_id'])]['approve_notified'] = False
                    save_forwarded_apps(apps)
                    app_data['approve_notified'] = False
                    
                if deny_count < threshold and app_data.get('deny_notified', False):
                    apps = load_forwarded_apps()
                    apps[str(app_data['message_id'])]['deny_notified'] = False
                    save_forwarded_apps(apps)
                    app_data['deny_notified'] = False
                
                if approve_threshold_met or deny_threshold_met:
                    # Use mixed view
                    new_view = ApplicationMixedView(
                        app_data,
                        approve_count,
                        deny_count,
                        show_approve_action=approve_threshold_met,
                        show_deny_action=deny_threshold_met,
                        threshold=threshold
                    )
                else:
                    # Use vote view
                    new_view = ApplicationVoteView(
                        app_data,
                        approve_count,
                        deny_count,
                        threshold=threshold
                    )
                
                # Apply buttons_enabled state
                if not buttons_enabled:
                    for item in new_view.children:
                        item.disabled = True
                
                await message.edit(view=new_view)
        except Exception as e:
            print(f"Error updating message view: {e}")

class VoteManagementViewStandalone(View):
    """Standalone view for managing votes on a ticket"""
    def __init__(self, message_id, tickets_data, guild, user=None):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.tickets_data = tickets_data
        self.guild = guild
        self.user = user
        
        # Back button FIRST
        back_button = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.secondary,
            emoji="◀️"
        )
        back_button.callback = self.back_to_details
        self.add_item(back_button)
        
        # Add toggle buttons button
        toggle_button = discord.ui.Button(
            label="Toggle Buttons",
            style=discord.ButtonStyle.secondary,
            emoji="🔁"
        )
        toggle_button.callback = self.toggle_buttons_callback
        self.add_item(toggle_button)
        
        # Modified: Single button to access vote modification options
        modify_votes_button = discord.ui.Button(
            label="Modify Votes",
            style=discord.ButtonStyle.primary,
            emoji="⚙️"
        )
        modify_votes_button.callback = self.modify_votes_callback
        self.add_item(modify_votes_button)
        
        # Reload application button
        reload_button = discord.ui.Button(
            label="Reload Application",
            style=discord.ButtonStyle.success,
            emoji="🛠️"
        )
        reload_button.callback = self.reload_application_callback
        self.add_item(reload_button)
    
    async def back_to_details(self, interaction: discord.Interaction):
        """Go back to ticket details view"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "Ticket Details")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(app_data.get('approve_voters', []), threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(app_data.get('deny_voters', []), threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = TicketDetailViewStandalone(self.message_id, self.tickets_data, self.guild, self.user)
        await interaction.response.edit_message(embed=embed, view=view)
    
    async def reload_application_callback(self, interaction: discord.Interaction):
        """Reload the application message view"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Check thresholds and reset status if needed
            threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
            approve_count = app_data.get('approve_count', 0)
            deny_count = app_data.get('deny_count', 0)
            
            # If neither threshold is met, clear the status
            if approve_count < threshold and deny_count < threshold:
                if 'status' in app_data:
                    del app_data['status']
                apps[self.message_id] = app_data
                save_forwarded_apps(apps)
            
            # Update the message view using utility
            await VoteManager.update_message_view(interaction, app_data)
            await interaction.followup.send("✅ Application reloaded!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error reloading application: {e}", ephemeral=True)
    
    async def modify_votes_callback(self, interaction: discord.Interaction):
        """Show vote modification options"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "Manage Ticket")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(approve_voters, threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(deny_voters, threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = VoteModificationViewStandalone(self.message_id, self.tickets_data, self.guild, self.user)
        await interaction.response.edit_message(embed=embed, view=view)
    
    async def toggle_buttons_callback(self, interaction: discord.Interaction):
        """Toggle buttons on the forwarded application"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        # Defer the interaction immediately
        await interaction.response.defer()
        
        try:
            channel = interaction.guild.get_channel(app_data['channel_id'])
            if not channel:
                channel = interaction.guild.get_thread(app_data['channel_id'])
            
            if not channel:
                await interaction.followup.send("❌ Channel not found!", ephemeral=True)
                return
            
            message = await channel.fetch_message(app_data['message_id'])
            
            # Check current state
            current_state = app_data.get('buttons_enabled', True)
            new_state = not current_state
            
            # Update the state in database AND clear status if re-enabling
            apps[self.message_id]['buttons_enabled'] = new_state
            if new_state and 'status' in apps[self.message_id]:
                del apps[self.message_id]['status']  # Clear accepted/denied status when re-enabling
            save_forwarded_apps(apps)

            apps = load_forwarded_apps()
            app_data = apps[self.message_id]

            # Get current view
            threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
            approve_count = app_data.get('approve_count', 0)
            deny_count = app_data.get('deny_count', 0)

            # Check if notified flags are set OR if counts meet threshold
            approve_threshold_met = approve_count >= threshold or app_data.get('approve_notified', False)
            deny_threshold_met = deny_count >= threshold or app_data.get('deny_notified', False)
            
            # Create appropriate view
            if approve_threshold_met or deny_threshold_met:
                new_view = ApplicationMixedView(
                    app_data,
                    approve_count,
                    deny_count,
                    show_approve_action=approve_threshold_met,
                    show_deny_action=deny_threshold_met,
                    threshold=threshold
                )
            else:
                new_view = ApplicationVoteView(
                    app_data,
                    approve_count,
                    deny_count,
                    threshold=threshold
                )
            
            # Disable or enable all buttons based on new state
            for item in new_view.children:
                item.disabled = not new_state
            
            await message.edit(view=new_view)
            await interaction.followup.send(f"✅ Buttons {'enabled' if new_state else 'disabled'}!", ephemeral=True)
            
        except Exception as e:
            print(f"Error toggling buttons: {e}")
            await interaction.followup.send(f"❌ Error toggling buttons: {e}", ephemeral=True)

class VoteModificationViewStandalone(View):
    """Standalone view for modifying individual votes"""
    def __init__(self, message_id, tickets_data, guild, user=None):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.tickets_data = tickets_data
        self.guild = guild
        self.user = user
        
        # Back button FIRST
        back_button = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.secondary,
            emoji="◀️"
        )
        back_button.callback = self.back_to_management
        self.add_item(back_button)
        
        # Add admin approve vote button
        add_approve_button = discord.ui.Button(
            label="Add Approve Vote",
            style=discord.ButtonStyle.success,
            emoji="➕"
        )
        add_approve_button.callback = self.add_approve_callback
        self.add_item(add_approve_button)
        
        # Add admin deny vote button
        add_deny_button = discord.ui.Button(
            label="Add Deny Vote",
            style=discord.ButtonStyle.danger,
            emoji="➕"
        )
        add_deny_button.callback = self.add_deny_callback
        self.add_item(add_deny_button)
        
        # Remove votes button
        remove_button = discord.ui.Button(
            label="Remove Votes",
            style=discord.ButtonStyle.secondary,
            emoji="➖"
        )
        remove_button.callback = self.remove_votes_callback
        self.add_item(remove_button)
        
        # Auto-fill votes button
        autofill_button = discord.ui.Button(
            label="Fill to Threshold",
            style=discord.ButtonStyle.success,
            emoji="⚡"
        )
        autofill_button.callback = self.autofill_votes_callback
        self.add_item(autofill_button)
    
    async def back_to_management(self, interaction: discord.Interaction):
        """Go back to vote management view"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "Manage Ticket")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(app_data.get('approve_voters', []), threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(app_data.get('deny_voters', []), threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = VoteManagementViewStandalone(self.message_id, self.tickets_data, self.guild, self.user)
        await interaction.response.edit_message(embed=embed, view=view)
    
    async def autofill_votes_callback(self, interaction: discord.Interaction):
        """Auto-fill votes to reach threshold"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        approve_count = len(approve_voters)
        deny_count = len(deny_voters)
        
        # Check if either is already at threshold
        if approve_count >= threshold and deny_count >= threshold:
            await interaction.response.send_message(
                "⚠️ Both approve and deny votes are already at or above threshold!",
                ephemeral=True
            )
            return
        
        # Calculate how many votes needed
        approve_needed = max(0, threshold - approve_count)
        deny_needed = max(0, threshold - deny_count)
        
        if approve_needed == 0 and deny_needed == 0:
            await interaction.response.send_message(
                "⚠️ Both vote types are already at threshold!",
                ephemeral=True
            )
            return
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "⚡ Auto-fill Votes to Threshold")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(approve_voters, threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(deny_voters, threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = AutofillSelectionViewStandalone(self.message_id, self.tickets_data, self.guild, approve_needed, deny_needed, threshold, self.user)
        await interaction.response.edit_message(embed=embed, view=view)
    
    async def add_approve_callback(self, interaction: discord.Interaction):
        """Add admin approve vote"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        # Generate admin vote ID using utility
        admin_vote_id = VoteManager.generate_admin_vote_id(approve_voters, deny_voters)
        
        # Add admin vote
        approve_voters.append(admin_vote_id)
        app_data['approve_count'] = len(approve_voters)
        app_data['approve_voters'] = approve_voters
        apps[self.message_id] = app_data
        
        save_forwarded_apps(apps)
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "Manage Ticket")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(approve_voters, threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(app_data.get('deny_voters', []), threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = VoteModificationViewStandalone(self.message_id, self.tickets_data, self.guild, self.user)
        await interaction.response.edit_message(embed=embed, view=view)
        
        # Update the message view using utility
        await VoteManager.update_message_view(interaction, app_data)
    
    async def add_deny_callback(self, interaction: discord.Interaction):
        """Add admin deny vote"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        # Generate admin vote ID using utility
        admin_vote_id = VoteManager.generate_admin_vote_id(approve_voters, deny_voters)
        
        # Add admin vote
        deny_voters.append(admin_vote_id)
        app_data['deny_count'] = len(deny_voters)
        app_data['deny_voters'] = deny_voters
        apps[self.message_id] = app_data
        
        save_forwarded_apps(apps)
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "Manage Ticket")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(app_data.get('approve_voters', []), threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(deny_voters, threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = VoteModificationViewStandalone(self.message_id, self.tickets_data, self.guild, self.user)
        await interaction.response.edit_message(embed=embed, view=view)
        
        # Update the message view using utility
        await VoteManager.update_message_view(interaction, app_data)
    
    async def remove_votes_callback(self, interaction: discord.Interaction):
        """Show interface to remove specific votes"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        if not approve_voters and not deny_voters:
            await interaction.response.send_message(
                "⚠️ No votes to remove!",
                ephemeral=True
            )
            return
        
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "➖ Remove Votes")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(approve_voters, threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(deny_voters, threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = RemoveVotesViewStandalone(self.message_id, self.tickets_data, self.guild, approve_voters, deny_voters, self.user)
        await interaction.response.edit_message(embed=embed, view=view)

class AutofillSelectionViewStandalone(View):
    """Standalone view for selecting which vote type to auto-fill"""
    def __init__(self, message_id, tickets_data, guild, approve_needed, deny_needed, threshold, user=None):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.tickets_data = tickets_data
        self.guild = guild
        self.approve_needed = approve_needed
        self.deny_needed = deny_needed
        self.threshold = threshold
        self.user = user
        
        # Back button FIRST
        back_button = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.secondary,
            emoji="◀️"
        )
        back_button.callback = self.back_to_modification
        self.add_item(back_button)
        
        # Add approve button if needed
        if approve_needed > 0:
            approve_button = discord.ui.Button(
                label=f"Fill Approve (+{approve_needed})",
                style=discord.ButtonStyle.success,
                emoji="✅"
            )
            approve_button.callback = self.fill_approve_callback
            self.add_item(approve_button)
        
        # Add deny button if needed
        if deny_needed > 0:
            deny_button = discord.ui.Button(
                label=f"Fill Deny (+{deny_needed})",
                style=discord.ButtonStyle.danger,
                emoji="❌"
            )
            deny_button.callback = self.fill_deny_callback
            self.add_item(deny_button)
        
        # Add both button if both needed
        if approve_needed > 0 and deny_needed > 0:
            both_button = discord.ui.Button(
                label=f"Fill Both (+{approve_needed + deny_needed})",
                style=discord.ButtonStyle.primary,
                emoji="⚡"
            )
            both_button.callback = self.fill_both_callback
            self.add_item(both_button)
    
    async def back_to_modification(self, interaction: discord.Interaction):
        """Go back to vote modification view"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "Manage Ticket")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(app_data.get('approve_voters', []), threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(app_data.get('deny_voters', []), threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = VoteModificationViewStandalone(self.message_id, self.tickets_data, self.guild, self.user)
        await interaction.response.edit_message(embed=embed, view=view)
    
    async def fill_approve_callback(self, interaction: discord.Interaction):
        """Fill approve votes to threshold"""
        await self.fill_votes(interaction, approve=True, deny=False)
    
    async def fill_deny_callback(self, interaction: discord.Interaction):
        """Fill deny votes to threshold"""
        await self.fill_votes(interaction, approve=False, deny=True)
    
    async def fill_both_callback(self, interaction: discord.Interaction):
        """Fill both approve and deny votes to threshold"""
        await self.fill_votes(interaction, approve=True, deny=True)
    
    async def fill_votes(self, interaction: discord.Interaction, approve: bool, deny: bool):
        """Fill votes with admin votes"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        added_approve = 0
        added_deny = 0
        
        # Add approve votes using utility
        if approve:
            for _ in range(self.approve_needed):
                admin_vote_id = VoteManager.generate_admin_vote_id(approve_voters, deny_voters)
                approve_voters.append(admin_vote_id)
                added_approve += 1
        
        # Add deny votes using utility
        if deny:
            for _ in range(self.deny_needed):
                admin_vote_id = VoteManager.generate_admin_vote_id(approve_voters, deny_voters)
                deny_voters.append(admin_vote_id)
                added_deny += 1
        
        # Update app data
        app_data['approve_voters'] = approve_voters
        app_data['approve_count'] = len(approve_voters)
        app_data['deny_voters'] = deny_voters
        app_data['deny_count'] = len(deny_voters)
        apps[self.message_id] = app_data
        
        save_forwarded_apps(apps)
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        # Build updated embed
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "⚡ Votes Auto-filled")
        
        approve_field = EmbedBuilder.build_vote_display(approve_voters, threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(deny_voters, threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = VoteManagementViewStandalone(self.message_id, self.tickets_data, self.guild, self.user)
        await interaction.edit_original_response(embed=embed, view=view)
        
        # Update the message view using utility
        await VoteManager.update_message_view(interaction, app_data)

class RemoveVotesViewStandalone(View):
    """Standalone view for removing specific votes"""
    def __init__(self, message_id, tickets_data, guild, approve_voters, deny_voters, user=None):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.tickets_data = tickets_data
        self.guild = guild
        self.user = user
        
        # Back button FIRST
        back_button = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.secondary,
            emoji="◀️"
        )
        back_button.callback = self.back_to_modification
        self.add_item(back_button)
        
        # Create approve voters selector if there are any
        if approve_voters:
            approve_select = Select(
                placeholder="Select approve voters to remove...",
                min_values=1,
                max_values=min(len(approve_voters), 25),
                custom_id="remove_approve_votes"
            )
            
            for voter_id in approve_voters[:25]:  # Discord limit
                if voter_id < 0:
                    # Admin vote
                    name = "Admin Vote"
                    description = f"Admin ID: {voter_id}"
                else:
                    # Real user vote
                    member = guild.get_member(voter_id)
                    name = member.name if member else f"User {voter_id}"
                    description = f"ID: {voter_id}"
                
                approve_select.add_option(
                    label=name,
                    value=str(voter_id),
                    description=description
                )
            
            approve_select.callback = self.remove_approve_votes
            self.add_item(approve_select)
        
        # Create deny voters selector if there are any
        if deny_voters:
            deny_select = Select(
                placeholder="Select deny voters to remove...",
                min_values=1,
                max_values=min(len(deny_voters), 25),
                custom_id="remove_deny_votes"
            )
            
            for voter_id in deny_voters[:25]:  # Discord limit
                if voter_id < 0:
                    # Admin vote
                    name = "Admin Vote"
                    description = f"Admin ID: {voter_id}"
                else:
                    # Real user vote
                    member = guild.get_member(voter_id)
                    name = member.name if member else f"User {voter_id}"
                    description = f"ID: {voter_id}"
                
                deny_select.add_option(
                    label=name,
                    value=str(voter_id),
                    description=description
                )
            
            deny_select.callback = self.remove_deny_votes
            self.add_item(deny_select)
    
    async def back_to_modification(self, interaction: discord.Interaction):
        """Go back to vote modification view"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "Manage Ticket")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(app_data.get('approve_voters', []), threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(app_data.get('deny_voters', []), threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = VoteModificationViewStandalone(self.message_id, self.tickets_data, self.guild, self.user)
        await interaction.response.edit_message(embed=embed, view=view)
    
    async def remove_approve_votes(self, interaction: discord.Interaction):
        """Remove selected approve votes"""
        await self._remove_votes(interaction, vote_type="approve")
    
    async def remove_deny_votes(self, interaction: discord.Interaction):
        """Remove selected deny votes"""
        await self._remove_votes(interaction, vote_type="deny")
    
    async def _remove_votes(self, interaction: discord.Interaction, vote_type: str):
        """Generic method to remove votes"""
        selected_voters = [int(v) for v in interaction.data['values']]
        
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        # Remove selected voters based on vote type
        if vote_type == "approve":
            for voter_id in selected_voters:
                if voter_id in approve_voters:
                    approve_voters.remove(voter_id)
            app_data['approve_voters'] = approve_voters
            app_data['approve_count'] = len(approve_voters)
        else:  # deny
            for voter_id in selected_voters:
                if voter_id in deny_voters:
                    deny_voters.remove(voter_id)
            app_data['deny_voters'] = deny_voters
            app_data['deny_count'] = len(deny_voters)
        
        apps[self.message_id] = app_data
        save_forwarded_apps(apps)
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "➖ Remove Votes")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(approve_voters, threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(deny_voters, threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = RemoveVotesViewStandalone(self.message_id, self.tickets_data, self.guild, approve_voters, deny_voters, self.user)
        await interaction.response.edit_message(embed=embed, view=view)
        
        # Update the message view using utility
        await VoteManager.update_message_view(interaction, app_data)

class AutofillSelectionView(View):
    """View for selecting which vote type to auto-fill"""
    def __init__(self, message_id, tickets_data, guild, approve_needed, deny_needed, threshold):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.tickets_data = tickets_data
        self.guild = guild
        self.approve_needed = approve_needed
        self.deny_needed = deny_needed
        self.threshold = threshold
        
        # Back button
        back_button = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.secondary,
            emoji="◀️"
        )
        back_button.callback = self.back_callback
        self.add_item(back_button)
        
        # Add approve button if needed
        if approve_needed > 0:
            approve_button = discord.ui.Button(
                label=f"Fill Approve (+{approve_needed})",
                style=discord.ButtonStyle.success,
                emoji="✅"
            )
            approve_button.callback = self.fill_approve_callback
            self.add_item(approve_button)
        
        # Add deny button if needed
        if deny_needed > 0:
            deny_button = discord.ui.Button(
                label=f"Fill Deny (+{deny_needed})",
                style=discord.ButtonStyle.danger,
                emoji="❌"
            )
            deny_button.callback = self.fill_deny_callback
            self.add_item(deny_button)
        
        # Add both button if both needed
        if approve_needed > 0 and deny_needed > 0:
            both_button = discord.ui.Button(
                label=f"Fill Both (+{approve_needed + deny_needed})",
                style=discord.ButtonStyle.primary,
                emoji="⚡"
            )
            both_button.callback = self.fill_both_callback
            self.add_item(both_button)
    
    async def fill_approve_callback(self, interaction: discord.Interaction):
        """Fill approve votes to threshold"""
        await self.fill_votes(interaction, approve=True, deny=False)
    
    async def fill_deny_callback(self, interaction: discord.Interaction):
        """Fill deny votes to threshold"""
        await self.fill_votes(interaction, approve=False, deny=True)
    
    async def fill_both_callback(self, interaction: discord.Interaction):
        """Fill both approve and deny votes to threshold"""
        await self.fill_votes(interaction, approve=True, deny=True)
    
    async def fill_votes(self, interaction: discord.Interaction, approve: bool, deny: bool):
        """Fill votes with admin votes"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        added_approve = 0
        added_deny = 0
        
        # Add approve votes using utility
        if approve:
            for _ in range(self.approve_needed):
                admin_vote_id = VoteManager.generate_admin_vote_id(approve_voters, deny_voters)
                approve_voters.append(admin_vote_id)
                added_approve += 1
        
        # Add deny votes using utility
        if deny:
            for _ in range(self.deny_needed):
                admin_vote_id = VoteManager.generate_admin_vote_id(approve_voters, deny_voters)
                deny_voters.append(admin_vote_id)
                added_deny += 1
        
        # Update app data
        app_data['approve_voters'] = approve_voters
        app_data['approve_count'] = len(approve_voters)
        app_data['deny_voters'] = deny_voters
        app_data['deny_count'] = len(deny_voters)
        
        # Check thresholds and reset status if needed
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        approve_count = len(approve_voters)
        deny_count = len(app_data.get('deny_voters', []))
        
        if approve_count < threshold and deny_count < threshold:
            if 'status' in app_data:
                del app_data['status']
        
        apps[self.message_id] = app_data
        
        save_forwarded_apps(apps)
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        # Build updated embed
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "⚡ Votes Auto-filled")
        
        approve_field = EmbedBuilder.build_vote_display(approve_voters, threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(deny_voters, threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = VoteManagementView(self.message_id, self.tickets_data, self.guild)
        await interaction.edit_original_response(embed=embed, view=view)
        
        # Update the message view using utility
        await VoteManager.update_message_view(interaction, app_data)
    
    async def back_callback(self, interaction: discord.Interaction):
        """Go back to vote management"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "Manage Ticket")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(approve_voters, threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(deny_voters, threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = VoteManagementView(self.message_id, self.tickets_data, self.guild)
        await interaction.response.edit_message(embed=embed, view=view)
    
    # Delete the entire update_message_view method and replace with:
    async def update_message_view(self, interaction: discord.Interaction, app_data):
        """Update the view on the original forwarded message"""
        await VoteManager.update_message_view(interaction, app_data)

class RemoveVotesView(View):
    """View for removing specific votes"""
    def __init__(self, message_id, tickets_data, guild, approve_voters, deny_voters):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.tickets_data = tickets_data
        self.guild = guild
        
        # Create approve voters selector if there are any
        if approve_voters:
            approve_select = Select(
                placeholder="Select approve voters to remove...",
                min_values=1,
                max_values=min(len(approve_voters), 25),
                custom_id="remove_approve_votes"
            )
            
            for voter_id in approve_voters[:25]:  # Discord limit
                if voter_id < 0:
                    # Admin vote
                    name = "Admin Vote"
                    description = f"Admin ID: {voter_id}"
                else:
                    # Real user vote
                    member = guild.get_member(voter_id)
                    name = member.name if member else f"User {voter_id}"
                    description = f"ID: {voter_id}"
                
                approve_select.add_option(
                    label=name,
                    value=str(voter_id),
                    description=description
                )
            
            approve_select.callback = self.remove_approve_votes
            self.add_item(approve_select)
        
        # Create deny voters selector if there are any
        if deny_voters:
            deny_select = Select(
                placeholder="Select deny voters to remove...",
                min_values=1,
                max_values=min(len(deny_voters), 25),
                custom_id="remove_deny_votes"
            )
            
            for voter_id in deny_voters[:25]:  # Discord limit
                if voter_id < 0:
                    # Admin vote
                    name = "Admin Vote"
                    description = f"Admin ID: {voter_id}"
                else:
                    # Real user vote
                    member = guild.get_member(voter_id)
                    name = member.name if member else f"User {voter_id}"
                    description = f"ID: {voter_id}"
                
                deny_select.add_option(
                    label=name,
                    value=str(voter_id),
                    description=description
                )
            
            deny_select.callback = self.remove_deny_votes
            self.add_item(deny_select)
        
        # Back button
        back_button = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.secondary,
            emoji="◀️"
        )
        back_button.callback = self.back_callback
        self.add_item(back_button)
    
    async def remove_approve_votes(self, interaction: discord.Interaction):
        """Remove selected approve votes"""
        await self._remove_votes(interaction, vote_type="approve")
    
    async def remove_deny_votes(self, interaction: discord.Interaction):
        """Remove selected deny votes"""
        await self._remove_votes(interaction, vote_type="deny")
    
    async def _remove_votes(self, interaction: discord.Interaction, vote_type: str):
        """Generic method to remove votes"""
        selected_voters = [int(v) for v in interaction.data['values']]
        
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        # Remove selected voters based on vote type
        if vote_type == "approve":
            for voter_id in selected_voters:
                if voter_id in approve_voters:
                    approve_voters.remove(voter_id)
            app_data['approve_voters'] = approve_voters
            app_data['approve_count'] = len(approve_voters)
        else:  # deny
            for voter_id in selected_voters:
                if voter_id in deny_voters:
                    deny_voters.remove(voter_id)
            app_data['deny_voters'] = deny_voters
            app_data['deny_count'] = len(deny_voters)
        
        # Check thresholds and reset status if needed
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        approve_count = len(approve_voters)
        deny_count = len(app_data.get('deny_voters', []))
        
        if approve_count < threshold and deny_count < threshold:
            if 'status' in app_data:
                del app_data['status']
        
        apps[self.message_id] = app_data
        
        save_forwarded_apps(apps)
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "➖ Remove Votes")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(approve_voters, threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(deny_voters, threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = RemoveVotesView(self.message_id, self.tickets_data, self.guild, approve_voters, deny_voters)
        await interaction.response.edit_message(embed=embed, view=view)
        
        # Update the message view using utility
        await VoteManager.update_message_view(interaction, app_data)
    
    async def back_callback(self, interaction: discord.Interaction):
        """Go back to vote management"""
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        # Build embed using utility class
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "Manage Ticket")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(approve_voters, threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(deny_voters, threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        view = VoteManagementView(self.message_id, self.tickets_data, self.guild)
        await interaction.response.edit_message(embed=embed, view=view)
    
    # Delete the entire update_message_view method and replace with:
    async def update_message_view(self, interaction: discord.Interaction, app_data):
        """Update the view on the original forwarded message"""
        await VoteManager.update_message_view(interaction, app_data)

class TicketSelectorView(View):
    def __init__(self, tickets_data, guild, pending_apps=None):
        super().__init__(timeout=None)
        self.guild = guild
        self.tickets_data = tickets_data
        self.pending_apps = pending_apps or {}

        # Combine tickets and pending apps for display
        all_items = []
        
        # Add forwarded tickets
        for message_id, app_data in tickets_data.items():
            all_items.append(('forwarded', message_id, app_data))
        
        # Add pending apps
        for pending_key, pending_data in self.pending_apps.items():
            all_items.append(('pending', pending_key, pending_data))
        
        # Create first selector (up to 25 items)
        if len(all_items) > 0:
            first_chunk = all_items[:25]
            first_select = Select(
                placeholder="Select a ticket (1-25)...",
                min_values=1,
                max_values=1,
                custom_id="ticket_select_1"
            )
            
            for item_type, item_id, item_data in first_chunk:
                if item_type == 'pending':
                    # Pending application
                    user = guild.get_member(item_data['user_id'])
                    user_name = user.name if user else f"User {item_data['user_id']}"
                    
                    # Get channel name
                    channel = guild.get_channel(item_data['channel_id'])
                    channel_name = channel.name if channel else f"Channel {item_data['channel_id']}"
                    
                    # Count answered questions
                    answered = len([a for a in item_data['answers'].values() if a])
                    total = len(item_data['answers'])
                    
                    answered = len([a for a in item_data['answers'].values() if a])
                    total_questions = item_data.get('total_questions', len(item_data['answers']))
                    total_pages = item_data.get('total_pages', (total_questions + 4) // 5)

                    first_select.add_option(
                        label=f"📝 {user_name} - {item_data['application_name']} (Pending)",
                        value=f"pending_{item_id}",
                        description=f"In Progress | {answered}/{total_questions} answers | Page {item_data['current_page'] + 1}/{total_pages}"
                    )
                else:
                    # Forwarded application
                    user = guild.get_member(item_data['user_id'])
                    user_name = user.name if user else f"User {item_data['user_id']}"
                    
                    # Get channel name
                    channel = guild.get_channel(item_data['ticket_channel_id'])
                    if not channel:
                        channel = guild.get_thread(item_data['ticket_channel_id'])
                    channel_name = channel.name if channel else f"Channel {item_data['ticket_channel_id']}"
                    
                    # Get threshold from this specific ticket's data
                    threshold = item_data.get('threshold', calculate_threshold(guild))
                    
                    # Get vote counts
                    approve_count = item_data.get('approve_count', 0)
                    deny_count = item_data.get('deny_count', 0)
                    
                    # Get status
                    status = item_data.get('status', 'pending')
                    status_emoji = {
                        'pending': '⏳',
                        'accepted': '✅',
                        'denied': '❌'
                    }.get(status, '⏳')
                    
                    first_select.add_option(
                        label=f"{status_emoji} {user_name} - {item_data['app_type']}",
                        value=item_id,
                        description=f"{status.capitalize()} | {approve_count}/{threshold} approve, {deny_count}/{threshold} deny"
                    )
            
            first_select.callback = self.select_callback
            self.add_item(first_select)
        
        # Create second selector if more than 25 items
        if len(all_items) > 25:
            second_chunk = all_items[25:50]
            second_select = Select(
                placeholder="Select a ticket (26-50)...",
                min_values=1,
                max_values=1,
                custom_id="ticket_select_2"
            )
            
            for item_type, item_id, item_data in second_chunk:
                if item_type == 'pending':
                    # Pending application
                    user = guild.get_member(item_data['user_id'])
                    user_name = user.name if user else f"User {item_data['user_id']}"
                    
                    # Get channel name
                    channel = guild.get_channel(item_data['channel_id'])
                    channel_name = channel.name if channel else f"Channel {item_data['channel_id']}"
                    
                    # Count answered questions
                    answered = len([a for a in item_data['answers'].values() if a])
                    total = len(item_data['answers'])
                    
                    answered = len([a for a in item_data['answers'].values() if a])
                    total_questions = item_data.get('total_questions', len(item_data['answers']))
                    total_pages = item_data.get('total_pages', (total_questions + 4) // 5)

                    second_select.add_option(
                        label=f"📝 {user_name} - {item_data['application_name']} (Pending)",
                        value=f"pending_{item_id}",
                        description=f"In Progress | {answered}/{total_questions} answers | Page {item_data['current_page'] + 1}/{total_pages}"
                    )
                else:
                    # Forwarded application
                    user = guild.get_member(item_data['user_id'])
                    user_name = user.name if user else f"User {item_data['user_id']}"
                    
                    # Get channel name
                    channel = guild.get_channel(item_data['ticket_channel_id'])
                    if not channel:
                        channel = guild.get_thread(item_data['ticket_channel_id'])
                    channel_name = channel.name if channel else f"Channel {item_data['ticket_channel_id']}"
                    
                    # Get vote counts
                    approve_count = item_data.get('approve_count', 0)
                    deny_count = item_data.get('deny_count', 0)
                    
                    # Get threshold from this specific ticket's data
                    threshold = item_data.get('threshold', calculate_threshold(guild))
                    
                    # Get status
                    status = item_data.get('status', 'pending')
                    status_emoji = {
                        'pending': '⏳',
                        'accepted': '✅',
                        'denied': '❌'
                    }.get(status, '⏳')
                    
                    second_select.add_option(
                        label=f"{status_emoji} {user_name} - {item_data['app_type']}",
                        value=item_id,
                        description=f"{status.capitalize()} | {approve_count}/{threshold} approve, {deny_count}/{threshold} deny"
                    )
                    
            second_select.callback = self.select_callback
            self.add_item(second_select)
    
    async def select_callback(self, interaction: discord.Interaction):
        """Handle ticket selection"""
        selected_value = interaction.data['values'][0]
        
        # Check if it's a pending app
        if selected_value.startswith('pending_'):
            pending_key = selected_value.replace('pending_', '')
            pending_apps = load_pending_apps()
            pending_data = pending_apps.get(pending_key)
            
            if not pending_data:
                await interaction.response.send_message(
                    "❌ Pending application data not found!",
                    ephemeral=True
                )
                return
            
            # Create info embed for pending app
            user = interaction.guild.get_member(pending_data['user_id'])
            user_mention = user.mention if user else f"<@{pending_data['user_id']}>"
            
            # Get channel mention
            channel = interaction.guild.get_channel(pending_data['channel_id'])
            channel_mention = channel.mention if channel else f"<#{pending_data['channel_id']}>"
            
            embed = discord.Embed(
                title="📝 Pending Application (In Progress)",
                description=f"**Application Type:** {pending_data['application_name']}\n**Applicant:** {user_mention}",
                color=0xFFA500,
                timestamp=datetime.utcfromtimestamp(pending_data.get('timestamp', 0))
            )
            
            embed.add_field(name="Channel", value=channel_mention, inline=True)

            # Get stored values or calculate fallback
            total_questions = pending_data.get('total_questions', len(pending_data['answers']))
            total_pages = pending_data.get('total_pages', (total_questions + 4) // 5)

            embed.add_field(name="Current Page", value=f"{pending_data['current_page'] + 1}/{total_pages}", inline=True)

            answered = len([a for a in pending_data['answers'].values() if a])
            embed.add_field(name="Answers Provided", value=f"{answered}/{total_questions}", inline=True)
                        
            # Add timestamp field
            timestamp = pending_data.get('timestamp', 0)
            if timestamp:
                embed.add_field(
                    name="Started",
                    value=f"<t:{int(timestamp)}:F> (<t:{int(timestamp)}:R>)",
                    inline=False
                )
            
            embed.set_footer(text="Application not yet submitted")
            
            # Create simple back button view
            back_view = View(timeout=None)
            back_button = discord.ui.Button(
                label="Back",
                style=discord.ButtonStyle.secondary,
                emoji="◀️"
            )
            
            async def back_callback(inter):
                """Go back to ticket list"""
                # Load fresh data to include pending apps
                tickets = load_forwarded_apps()
                pending_apps = load_pending_apps()
                
                list_embed = discord.Embed(
                    title="🎫 Manage Tickets",
                    description=f"Found **{len(tickets) + len(pending_apps)}** application(s) ({len(tickets)} submitted, {len(pending_apps)} pending).\nSelect a ticket from the dropdown below to view details.",
                    color=0x5865F2,
                    timestamp=datetime.utcnow()
                )
                new_view = TicketSelectorView(tickets, inter.guild, pending_apps)
                await inter.response.edit_message(
                    embed=list_embed,
                    view=new_view
                )
            
            back_button.callback = back_callback
            back_view.add_item(back_button)
            
            await interaction.response.edit_message(embed=embed, view=back_view)
            return
        
        # Original forwarded ticket logic
        selected_message_id = selected_value
        
        # Load fresh data
        apps = load_forwarded_apps()
        app_data = apps.get(selected_message_id)
        
        if not app_data:
            await interaction.response.send_message(
                "❌ Ticket data not found!",
                ephemeral=True
            )
            return
        
        # Create info embed
        user = interaction.guild.get_member(app_data['user_id'])
        user_mention = user.mention if user else f"<@{app_data['user_id']}>"
        
        # Get channel mention
        channel = interaction.guild.get_channel(app_data['ticket_channel_id'])
        if not channel:
            channel = interaction.guild.get_thread(app_data['ticket_channel_id'])
        channel_mention = channel.mention if channel else f"<#{app_data['ticket_channel_id']}>"
        
        status = app_data.get('status', 'pending')
        status_emoji = {
            'pending': '⏳',
            'accepted': '✅',
            'denied': '❌'
        }.get(status, '⏳')
        
        title = f"{status_emoji} Manage Ticket - {status}"
        
        embed = discord.Embed(
            title=title,
            description=f"**Application Type:** {app_data['app_type']}\n**Applicant:** {user_mention}",
            color=0x5865F2,
            timestamp=datetime.utcfromtimestamp(app_data.get('timestamp', 0))
        )
        
        embed.add_field(name="Channel", value=channel_mention, inline=True)
        embed.add_field(name="Message ID", value=f"`{app_data['message_id']}`", inline=True)
        embed.add_field(name="User ID", value=f"`{app_data['user_id']}`", inline=True)
        
        # Add timestamp field
        timestamp = app_data.get('timestamp', 0)
        if timestamp:
            embed.add_field(
                name="Submitted",
                value=f"<t:{int(timestamp)}:F> (<t:{int(timestamp)}:R>)",
                inline=False
            )
        
        # Calculate threshold
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))

        # Approve votes
        approve_voters = app_data.get('approve_voters', [])
        approve_mentions = []
        for voter_id in approve_voters:
            if voter_id < 0:
                approve_mentions.append("**[Admin Vote]**")
            else:
                approve_mentions.append(f"<@{voter_id}>")
        approve_text = ", ".join(approve_mentions) if approve_mentions else "*None*"
        if len(approve_voters) > 10:
            approve_text = ", ".join(approve_mentions[:10])
            approve_text += f"\n*... and {len(approve_voters) - 10} more*"

        embed.add_field(
            name=f"✅ Approve Votes ({app_data.get('approve_count', 0)}/{threshold})",
            value=approve_text,
            inline=False
        )

        # Deny votes
        deny_voters = app_data.get('deny_voters', [])
        deny_mentions = []
        for voter_id in deny_voters:
            if voter_id < 0:
                deny_mentions.append("**[Admin Vote]**")
            else:
                deny_mentions.append(f"<@{voter_id}>")
        deny_text = ", ".join(deny_mentions) if deny_mentions else "*None*"
        if len(deny_voters) > 10:
            deny_text = ", ".join(deny_mentions[:10])
            deny_text += f"\n*... and {len(deny_voters) - 10} more*"

        embed.add_field(
            name=f"❌ Deny Votes ({app_data.get('deny_count', 0)}/{threshold})",
            value=deny_text,
            inline=False
        )

        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        # Create view with buttons
        detail_view = TicketDetailView(selected_message_id, self.tickets_data, self.guild, interaction.user)
        
        # Edit the original message instead of sending a new one
        await interaction.response.edit_message(
            embed=embed,
            view=detail_view
        )

# Owner‑only debug panel for end‑to‑end ticket and queue testing

def build_debug_embed(message_id, guild):
    """Build the debug panel embed for a ticket.

    Returns ``(embed, app_data)`` or ``(None, None)`` if the ticket is missing.
    """
    apps = load_forwarded_apps()
    app_data = apps.get(message_id)
    if not app_data:
        return None, None

    threshold = app_data.get('threshold', calculate_threshold(guild))
    approve_count = app_data.get('approve_count', 0)
    deny_count = app_data.get('deny_count', 0)
    status = app_data.get('status', 'pending')
    buttons_enabled = app_data.get('buttons_enabled', True)

    embed = EmbedBuilder.build_ticket_embed(app_data, guild, "🛠️ Debug Ticket")
    embed.color = 0xE67E22

    state_lines = [
        f"**Status:** `{status}`",
        f"**Buttons enabled:** `{buttons_enabled}`",
        f"**Approve notified:** `{app_data.get('approve_notified', False)}`",
        f"**Deny notified:** `{app_data.get('deny_notified', False)}`",
        f"**Approve votes:** `{approve_count}/{threshold}`",
        f"**Deny votes:** `{deny_count}/{threshold}`",
    ]
    embed.add_field(name="Ticket state", value="\n".join(state_lines), inline=False)

    # Queue summary
    queue_lines = []
    try:
        capacity = get_guild_capacity()
        player_count = capacity.get('player_count')
        max_slots = capacity.get('max_slots')
        is_full = capacity.get('is_full')
        queue_lines.append(
            f"**Guild capacity:** `{player_count}/{max_slots}` (full: `{is_full}`)"
        )
        if capacity.get('capacity_overridden'):
            override_open = capacity.get('override', {}).get('open_slots')
            queue_lines.append(
                f"**⚠️ Capacity override active:** simulating `{override_open}` open slot(s)"
            )
    except Exception as e:
        queue_lines.append(f"**Guild capacity:** *unavailable ({e})*")

    try:
        result = get_queue_position(app_data['user_id'])
        if result is None:
            queue_lines.append("**Queue position:** *not queued*")
        else:
            pos, qt = result
            queue_lines.append(f"**Queue position:** `#{pos}` in `{qt}` queue")
    except Exception as e:
        queue_lines.append(f"**Queue position:** *error ({e})*")

    if app_data.get('queued'):
        queue_lines.append(
            f"**Stored queue:** `#{app_data.get('queue_position')}` in `{app_data.get('queue_type')}` (locked: `{app_data.get('queue_locked', False)}`)"
        )

    embed.add_field(name="Queue", value="\n".join(queue_lines), inline=False)
    embed.set_footer(text="Owner debug panel - changes are applied immediately")
    return embed, app_data


async def _edit_forwarded_message_view(interaction, app_data, forced_view=None):
    """Re‑render the forwarded application message with an up‑to‑date view."""
    try:
        channel = interaction.guild.get_channel(app_data['channel_id'])
        if not channel:
            channel = interaction.guild.get_thread(app_data['channel_id'])
        if not channel:
            return False
        message = await channel.fetch_message(app_data['message_id'])

        if forced_view is not None:
            view = forced_view
        else:
            threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
            approve_count = app_data.get('approve_count', 0)
            deny_count = app_data.get('deny_count', 0)
            approve_met = approve_count >= threshold or app_data.get('approve_notified', False)
            deny_met = deny_count >= threshold or app_data.get('deny_notified', False)
            if approve_met or deny_met:
                view = ApplicationMixedView(
                    app_data, approve_count, deny_count,
                    show_approve_action=approve_met,
                    show_deny_action=deny_met,
                    threshold=threshold,
                )
            else:
                view = ApplicationVoteView(app_data, approve_count, deny_count, threshold=threshold)
            if not app_data.get('buttons_enabled', True):
                for item in view.children:
                    item.disabled = True

        await message.edit(view=view)
        return True
    except Exception as e:
        print(f"[DEBUG] Failed to re‑render forwarded message view: {e}")
        return False


class DebugTicketView(View):
    """Owner‑only debug panel for a ticket.

    Exposes direct hooks into the ticket state and the guild queue so the bot
    owner can drive every state transition without needing real voters, real
    applicants, or a full/empty guild.
    """

    def __init__(self, message_id, tickets_data, guild, user):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.tickets_data = tickets_data
        self.guild = guild
        self.user = user

        # Row 0 - navigation + diagnostics
        back_button = discord.ui.Button(
            label="Back", style=discord.ButtonStyle.secondary, emoji="◀️", row=0,
        )
        back_button.callback = self.back_callback
        self.add_item(back_button)

        refresh_button = discord.ui.Button(
            label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄", row=0,
        )
        refresh_button.callback = self.refresh_callback
        self.add_item(refresh_button)

        raw_button = discord.ui.Button(
            label="Show Raw JSON", style=discord.ButtonStyle.secondary, emoji="📄", row=0,
        )
        raw_button.callback = self.show_raw_callback
        self.add_item(raw_button)

        # Row 1 - ticket state resets and triggers
        reset_button = discord.ui.Button(
            label="Reset Ticket", style=discord.ButtonStyle.danger, emoji="🧹", row=1,
        )
        reset_button.callback = self.reset_callback
        self.add_item(reset_button)

        force_approve = discord.ui.Button(
            label="Trigger Approval", style=discord.ButtonStyle.success, emoji="⚡", row=1,
        )
        force_approve.callback = self.trigger_approval_callback
        self.add_item(force_approve)

        force_deny = discord.ui.Button(
            label="Trigger Denial", style=discord.ButtonStyle.danger, emoji="⚡", row=1,
        )
        force_deny.callback = self.trigger_denial_callback
        self.add_item(force_deny)

        # Row 2 - real accept / deny modal flows
        sim_accept = discord.ui.Button(
            label="Simulate Accept Button", style=discord.ButtonStyle.success, emoji="🟢", row=2,
        )
        sim_accept.callback = self.simulate_accept_callback
        self.add_item(sim_accept)

        sim_deny = discord.ui.Button(
            label="Simulate Deny Button", style=discord.ButtonStyle.danger, emoji="🔴", row=2,
        )
        sim_deny.callback = self.simulate_deny_callback
        self.add_item(sim_deny)

        # Row 3 - queue controls
        queue_add = discord.ui.Button(
            label="Force Queue Add", style=discord.ButtonStyle.primary, emoji="⏳", row=3,
        )
        queue_add.callback = self.force_queue_add_callback
        self.add_item(queue_add)

        queue_add_vet = discord.ui.Button(
            label="Force Queue Add (Veteran)", style=discord.ButtonStyle.primary, emoji="⭐", row=3,
        )
        queue_add_vet.callback = self.force_queue_add_vet_callback
        self.add_item(queue_add_vet)

        queue_remove = discord.ui.Button(
            label="Remove from Queue", style=discord.ButtonStyle.secondary, emoji="➖", row=3,
        )
        queue_remove.callback = self.force_queue_remove_callback
        self.add_item(queue_remove)

        # Row 4 - guild capacity override (simulate slots opening)
        set_override_btn = discord.ui.Button(
            label="Set Capacity Override", style=discord.ButtonStyle.primary, emoji="🚧", row=4,
        )
        set_override_btn.callback = self.set_capacity_override_callback
        self.add_item(set_override_btn)

        open_one_btn = discord.ui.Button(
            label="Open +1 Slot", style=discord.ButtonStyle.success, emoji="➕", row=4,
        )
        open_one_btn.callback = self.open_one_slot_callback
        self.add_item(open_one_btn)

        force_full_btn = discord.ui.Button(
            label="Force Guild Full", style=discord.ButtonStyle.danger, emoji="🚫", row=4,
        )
        force_full_btn.callback = self.force_full_callback
        self.add_item(force_full_btn)

        clear_override_btn = discord.ui.Button(
            label="Clear Override", style=discord.ButtonStyle.secondary, emoji="♻️", row=4,
        )
        clear_override_btn.callback = self.clear_capacity_override_callback
        self.add_item(clear_override_btn)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _guard(self, interaction) -> bool:
        if is_bot_owner(interaction.user):
            return True
        return False

    async def _reject_non_owner(self, interaction):
        await interaction.response.send_message(
            "❌ Debug tools are restricted to the bot owner.", ephemeral=True
        )

    async def _refresh_panel(self, interaction, note: str | None = None):
        embed, _ = build_debug_embed(self.message_id, self.guild)
        if embed is None:
            if interaction.response.is_done():
                await interaction.followup.send("❌ Ticket data not found!", ephemeral=True)
            else:
                await interaction.response.send_message(
                    "❌ Ticket data not found!", ephemeral=True
                )
            return
        if note:
            embed.description = (embed.description or "") + f"\n\nℹ️ {note}"
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    # Callbacks
    async def back_callback(self, interaction: discord.Interaction):
        if not self._guard(interaction):
            await self._reject_non_owner(interaction)
            return

        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return

        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "Manage Ticket")
        approve_field = EmbedBuilder.build_vote_display(app_data.get('approve_voters', []), threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(app_data.get('deny_voters', []), threshold, "Deny")
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text="Application submitted")

        view = TicketDetailView(self.message_id, self.tickets_data, self.guild, interaction.user)
        await interaction.response.edit_message(embed=embed, view=view)

    async def refresh_callback(self, interaction: discord.Interaction):
        if not self._guard(interaction):
            await self._reject_non_owner(interaction)
            return
        await self._refresh_panel(interaction, note="Panel refreshed.")

    async def show_raw_callback(self, interaction: discord.Interaction):
        if not self._guard(interaction):
            await self._reject_non_owner(interaction)
            return

        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return

        raw = json.dumps(app_data, indent=2, ensure_ascii=False)
        # Discord message limit is 2000 chars; wrap in a code block with truncation.
        content = f"```json\n{raw[:1900]}{'\n...[truncated]' if len(raw) > 1900 else ''}\n```"
        await interaction.response.send_message(content, ephemeral=True)

    async def reset_callback(self, interaction: discord.Interaction):
        if not self._guard(interaction):
            await self._reject_non_owner(interaction)
            return

        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return

        await interaction.response.defer()

        app_data['approve_voters'] = []
        app_data['deny_voters'] = []
        app_data['approve_count'] = 0
        app_data['deny_count'] = 0
        app_data['approve_notified'] = False
        app_data['deny_notified'] = False
        app_data['buttons_enabled'] = True
        for key in ('status', 'queued', 'queue_position', 'queue_type', 'queue_locked'):
            app_data.pop(key, None)
        apps[self.message_id] = app_data
        save_forwarded_apps(apps)

        # Also clear any queue entry for this applicant.
        try:
            remove_from_queue(app_data['user_id'])
        except Exception as e:
            print(f"[DEBUG] Queue remove during reset failed: {e}")

        await _edit_forwarded_message_view(interaction, app_data)
        await self._refresh_panel(interaction, note="Ticket state reset - votes, status, notified flags, and queue entry cleared.")

    async def trigger_approval_callback(self, interaction: discord.Interaction):
        """Fill approvals to threshold and run the full approve‑threshold flow."""
        if not self._guard(interaction):
            await self._reject_non_owner(interaction)
            return

        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return

        await interaction.response.defer()

        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        approve_voters = app_data.get('approve_voters', [])
        while len(approve_voters) < threshold:
            approve_voters.append(VoteManager.generate_admin_vote_id(approve_voters, app_data.get('deny_voters', [])))
        app_data['approve_voters'] = approve_voters
        app_data['approve_count'] = len(approve_voters)
        app_data['approve_notified'] = True
        app_data.pop('status', None)
        apps[self.message_id] = app_data
        save_forwarded_apps(apps)

        await _edit_forwarded_message_view(interaction, app_data)
        await self._refresh_panel(
            interaction,
            note="Forced approval - votes filled to threshold and `approve_notified` set. Use `Force Queue Add` to simulate the full‑guild path.",
        )

    async def trigger_denial_callback(self, interaction: discord.Interaction):
        """Fill denials to threshold and run the full deny‑threshold flow."""
        if not self._guard(interaction):
            await self._reject_non_owner(interaction)
            return

        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return

        await interaction.response.defer()

        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        deny_voters = app_data.get('deny_voters', [])
        while len(deny_voters) < threshold:
            deny_voters.append(VoteManager.generate_admin_vote_id(app_data.get('approve_voters', []), deny_voters))
        app_data['deny_voters'] = deny_voters
        app_data['deny_count'] = len(deny_voters)
        app_data['deny_notified'] = True
        app_data.pop('status', None)
        apps[self.message_id] = app_data
        save_forwarded_apps(apps)

        await _edit_forwarded_message_view(interaction, app_data)
        await self._refresh_panel(
            interaction,
            note="Forced denial - votes filled to threshold and `deny_notified` set.",
        )

    async def simulate_accept_callback(self, interaction: discord.Interaction):
        """Invoke the real accept modal for this ticket as if the owner pressed the button on the forwarded message."""
        if not self._guard(interaction):
            await self._reject_non_owner(interaction)
            return

        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return

        try:
            channel = interaction.guild.get_channel(app_data['channel_id']) or interaction.guild.get_thread(app_data['channel_id'])
            target_message = await channel.fetch_message(app_data['message_id']) if channel else None
        except Exception:
            target_message = None

        if target_message is None:
            await interaction.response.send_message(
                "❌ Could not fetch the forwarded application message to replay the accept flow.",
                ephemeral=True,
            )
            return

        # Proxy the interaction so the callback sees the forwarded message as ``interaction.message``.
        proxy = _InteractionMessageProxy(interaction, target_message)
        view = ApplicationActionView({**app_data, 'message_id': app_data['message_id']}, show_deny=False)
        await view.accept_callback(proxy)

    async def simulate_deny_callback(self, interaction: discord.Interaction):
        """Open the real deny‑reason modal as if the owner pressed the deny button on the forwarded message."""
        if not self._guard(interaction):
            await self._reject_non_owner(interaction)
            return

        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return

        try:
            channel = interaction.guild.get_channel(app_data['channel_id']) or interaction.guild.get_thread(app_data['channel_id'])
            target_message = await channel.fetch_message(app_data['message_id']) if channel else None
        except Exception:
            target_message = None

        if target_message is None:
            await interaction.response.send_message(
                "❌ Could not fetch the forwarded application message to replay the deny flow.",
                ephemeral=True,
            )
            return

        modal = DenyReasonModal(app_data, target_message)
        await interaction.response.send_modal(modal)

    async def force_queue_add_callback(self, interaction: discord.Interaction):
        await self._do_queue_add(interaction, is_veteran=False)

    async def force_queue_add_vet_callback(self, interaction: discord.Interaction):
        await self._do_queue_add(interaction, is_veteran=True)

    async def _do_queue_add(self, interaction: discord.Interaction, is_veteran: bool):
        if not self._guard(interaction):
            await self._reject_non_owner(interaction)
            return

        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return

        await interaction.response.defer()

        # Try to resolve the applicant's in‑game username from the forwarded embed.
        username = "Unknown"
        try:
            ch = interaction.guild.get_channel(app_data['channel_id']) or interaction.guild.get_thread(app_data['channel_id'])
            if ch:
                msg = await ch.fetch_message(app_data['message_id'])
                extracted = extract_username_from_embeds(msg.embeds)
                if extracted:
                    username = extracted
        except Exception as e:
            print(f"[DEBUG] Could not extract username for queue add: {e}")

        # If the caller didn't request veteran explicitly, still respect the veteran role.
        if not is_veteran:
            member = interaction.guild.get_member(app_data['user_id'])
            is_veteran = bool(member) and any(r.id == VETERAN_ROLE_ID for r in member.roles)

        try:
            pos, qt = add_to_queue(username, None, app_data['user_id'], is_veteran=is_veteran)
        except Exception as e:
            await self._refresh_panel(interaction, note=f"Queue add failed: `{e}`")
            return

        app_data['queued'] = True
        app_data['queue_position'] = pos
        app_data['queue_type'] = qt
        apps[self.message_id] = app_data
        save_forwarded_apps(apps)

        await _edit_forwarded_message_view(interaction, app_data)
        await self._refresh_panel(
            interaction,
            note=f"Forced add to `{qt}` queue at position `#{pos}` (veteran: `{is_veteran}`).",
        )

    async def force_queue_remove_callback(self, interaction: discord.Interaction):
        if not self._guard(interaction):
            await self._reject_non_owner(interaction)
            return

        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return

        await interaction.response.defer()

        removed = False
        try:
            removed = remove_from_queue(app_data['user_id'])
        except Exception as e:
            await self._refresh_panel(interaction, note=f"Queue remove failed: `{e}`")
            return

        for key in ('queued', 'queue_position', 'queue_type', 'queue_locked'):
            app_data.pop(key, None)
        apps[self.message_id] = app_data
        save_forwarded_apps(apps)

        await _edit_forwarded_message_view(interaction, app_data)
        await self._refresh_panel(
            interaction,
            note="Removed from queue." if removed else "No queue entry existed for this applicant.",
        )

    # Guild capacity override
    async def set_capacity_override_callback(self, interaction: discord.Interaction):
        """Open a modal to set the number of simulated open slots."""
        if not self._guard(interaction):
            await self._reject_non_owner(interaction)
            return

        current = get_capacity_override() or {}
        default_value = str(current.get('open_slots', 0))
        await interaction.response.send_modal(
            CapacityOverrideModal(
                message_id=self.message_id,
                tickets_data=self.tickets_data,
                guild=self.guild,
                user=self.user,
                default_value=default_value,
            )
        )

    async def open_one_slot_callback(self, interaction: discord.Interaction):
        """Bump the simulated open‑slot count by +1 (creates an override if none yet)."""
        if not self._guard(interaction):
            await self._reject_non_owner(interaction)
            return

        await interaction.response.defer()

        current = get_capacity_override() or {}
        new_open = int(current.get('open_slots', 0)) + 1
        set_capacity_override(new_open)

        # Re‑render the forwarded message since the buttons' disabled state depends on capacity.
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        if app_data:
            await _edit_forwarded_message_view(interaction, app_data)

        await self._refresh_panel(
            interaction,
            note=f"Capacity override set to `{new_open}` open slot(s). `get_guild_capacity()` now reports `is_full = False`.",
        )

    async def force_full_callback(self, interaction: discord.Interaction):
        """Override the reported capacity to 0 open slots (guild is full)."""
        if not self._guard(interaction):
            await self._reject_non_owner(interaction)
            return

        await interaction.response.defer()

        set_capacity_override(0)

        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        if app_data:
            await _edit_forwarded_message_view(interaction, app_data)

        await self._refresh_panel(
            interaction,
            note="Capacity override set to `0` open slots — simulating a full guild.",
        )

    async def clear_capacity_override_callback(self, interaction: discord.Interaction):
        """Remove any active capacity override and fall back to the real tracked data."""
        if not self._guard(interaction):
            await self._reject_non_owner(interaction)
            return

        await interaction.response.defer()

        cleared = clear_capacity_override()

        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        if app_data:
            await _edit_forwarded_message_view(interaction, app_data)

        await self._refresh_panel(
            interaction,
            note="Capacity override cleared — real guild capacity is back in effect." if cleared
            else "No capacity override was active.",
        )


class CapacityOverrideModal(discord.ui.Modal, title="Simulate Guild Capacity"):
    """Modal for setting the simulated number of open guild slots."""

    open_slots_input = discord.ui.TextInput(
        label="Open slots to simulate (0 = full)",
        placeholder="e.g. 1 for one slot open, 0 to force full",
        required=True,
        max_length=4,
    )

    def __init__(self, message_id, tickets_data, guild, user, default_value: str = "0"):
        super().__init__()
        self.message_id = message_id
        self.tickets_data = tickets_data
        self.guild = guild
        self.user = user
        self.open_slots_input.default = default_value

    async def on_submit(self, interaction: discord.Interaction):
        if not is_bot_owner(interaction.user):
            await interaction.response.send_message(
                "❌ Debug tools are restricted to the bot owner.", ephemeral=True
            )
            return

        raw = (self.open_slots_input.value or "").strip()
        try:
            open_slots = int(raw)
        except ValueError:
            await interaction.response.send_message(
                f"❌ `{raw}` is not a valid integer.", ephemeral=True
            )
            return

        stored = set_capacity_override(open_slots)

        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        if app_data:
            await _edit_forwarded_message_view(interaction, app_data)

        embed, _ = build_debug_embed(self.message_id, self.guild)
        if embed is None:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return

        embed.description = (embed.description or "") + (
            f"\n\nℹ️ Capacity override set to `{stored['open_slots']}` open slot(s)."
        )
        view = DebugTicketView(self.message_id, self.tickets_data, self.guild, self.user)
        await interaction.response.edit_message(embed=embed, view=view)


class _InteractionMessageProxy:
    """Wrap a ``discord.Interaction`` so ``interaction.message`` points at a
    different message than the one that originally triggered it.

    Used by the debug panel to replay ``ApplicationActionView.accept_callback``
    against the forwarded application message while preserving the owner's
    current interaction context.
    """

    def __init__(self, inner, message):
        self._inner = inner
        self.message = message

    def __getattr__(self, item):
        return getattr(self._inner, item)


def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    @bot.tree.command(
        name="manage_tickets",
        description="Manage forwarded application tickets"
    )
    async def manage_tickets(interaction: discord.Interaction):
        """Manage tickets command with selectors"""

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
        
        # Load tickets and pending apps
        tickets = load_forwarded_apps()
        pending_apps = load_pending_apps()
        
        total_count = len(tickets) + len(pending_apps)
        
        if total_count == 0:
            await interaction.response.send_message(
                "❌ No tickets or pending applications found!",
                ephemeral=True
            )
            return
        
        # Create embed
        embed = discord.Embed(
            title="🎫 Manage Tickets",
            description=f"Found **{total_count}** application(s) (**{len(tickets)}** submitted, **{len(pending_apps)}** pending).\nSelect a ticket from the dropdown below to view details.",
            color=0x5865F2,
            timestamp=datetime.utcnow()
        )
        
        # Create view with selectors
        view = TicketSelectorView(tickets, interaction.guild, pending_apps)
        
        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True
        )
    
    print("[OK] Loaded manage tickets command")