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
    calculate_threshold,
    save_forwarded_apps,
    load_pending_apps,
    save_pending_apps
)
from guild_queue import get_queue_position
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
                        value=f"Position **#{queue_pos}** — guild is at full capacity",
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