import discord
from discord import app_commands
from discord.ui import Select, Button, View, Modal, TextInput
import json
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
import re
import io
import asyncio
from collections import defaultdict
import sys
import aiohttp

# Add parent directory to path to import blacklist
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from suscard import calculate_suspiciousness, SusCardImageGenerator, WynncraftAPI
from blacklist import is_blacklisted
from guild_queue import get_guild_capacity, add_to_queue, get_queue_position, remove_from_queue, extract_username_from_embeds, VETERAN_ROLE_ID
from utils.permissions import has_roles

_ROOT = Path(__file__).resolve().parent.parent.parent
NOTIFICATION_FILE = _ROOT / 'data' / 'app_notifications.json'
FORWARDED_APPS_FILE = _ROOT / 'data' / 'forwarded_applications.json'
CHANNEL_OPENERS_FILE = _ROOT / 'data' / 'channel_openers.json'
PENDING_APPS_FILE = _ROOT / 'data' / 'pending_applications.json'

# Load 7th Wynncraft API key for username verification
WYNNCRAFT_VERIFICATION_KEY = os.getenv('WYNNCRAFT_KEY_7')

PANEL_REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0,
    600185623474601995 # Parliament
]

async def refresh_all_panels_and_buttons(bot):
    """Refresh all ticket panels and control buttons across all guilds"""
    try:
        panels = load_panels()
        channel_openers = load_channel_openers()
        
        refreshed_panels = 0
        refreshed_buttons = 0
        
        # Refresh ticket panels
        for panel_id, panel_data in panels.items():
            try:
                channel = bot.get_channel(panel_data.get('channel_id'))
                if channel:
                    try:
                        panel_message = await channel.fetch_message(int(panel_id))
                        
                        class PersistentTicketView(View):
                            def __init__(self, applications, panel_data):
                                super().__init__(timeout=None)
                                self.panel_data = panel_data
                                
                                for app in applications:
                                    button = Button(
                                        label=app['name'],
                                        style=discord.ButtonStyle(app['style']),
                                        emoji=app.get('emoji'),
                                        custom_id=f"ticket_{app['name'].lower().replace(' ', '_')}"
                                    )
                                    
                                    def create_callback(app_name):
                                        async def callback(interaction: discord.Interaction):
                                            await create_application_channel(interaction, app_name, self.panel_data)
                                        return callback
                                    
                                    button.callback = create_callback(app['name'])
                                    self.add_item(button)
                        
                        view = PersistentTicketView(panel_data['applications'], panel_data)
                        await panel_message.edit(view=view)
                        refreshed_panels += 1
                    except discord.NotFound:
                        print(f"[REFRESH] Panel message {panel_id} not found")
                    except Exception as e:
                        print(f"[REFRESH] Error refreshing panel {panel_id}: {e}")
            except Exception as e:
                print(f"[REFRESH] Error processing panel {panel_id}: {e}")
        
        # Refresh ticket control buttons
        for channel_id, opener_id in channel_openers.items():
            try:
                channel = bot.get_channel(int(channel_id))
                if channel:
                    submitted = False
                    async for message in channel.history(limit=100):
                        if message.author == channel.guild.me and message.embeds:
                            for embed in message.embeds:
                                if "Application Submitted" in embed.title:
                                    submitted = True
                                    break
                        if submitted:
                            break
                    
                    control_message = None
                    async for message in channel.history(limit=50):
                        if message.author == channel.guild.me and message.embeds:
                            if "Application" in message.embeds[0].title and "Submitted" not in message.embeds[0].title:
                                control_message = message
                                break
                    
                    if control_message:
                        view = ApplicationControlView(opener_id=opener_id)
                        
                        if submitted:
                            for item in view.children:
                                if isinstance(item, Button) and item.custom_id == "fill_application":
                                    item.disabled = True
                                    item.label = "✅ Application Submitted"
                                    item.style = discord.ButtonStyle.success
                        
                        await control_message.edit(view=view)
                        refreshed_buttons += 1
            except Exception as e:
                print(f"[REFRESH] Error refreshing buttons in channel {channel_id}: {e}")
        
        if refreshed_panels > 0 or refreshed_buttons > 0:
            print(f"[REFRESH] Refreshed {refreshed_panels} panel(s) and {refreshed_buttons} ticket button(s)")
    
    except Exception as e:
        print(f"[REFRESH] Error during refresh: {e}")

def load_pending_apps():
    """Load pending applications from JSON file"""
    if PENDING_APPS_FILE.exists():
        with open(PENDING_APPS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_pending_apps(apps):
    """Save pending applications to JSON file"""
    with open(PENDING_APPS_FILE, 'w') as f:
        json.dump(apps, f, indent=4)

def save_pending_app(user_id: int, channel_id: int, application_name: str, answers: dict, page: int, questions: list):
    """Save a pending application"""
    apps = load_pending_apps()
    key = f"{user_id}_{channel_id}"
    total_pages = (len(questions) + 4) // 5
    apps[key] = {
        'user_id': user_id,
        'channel_id': channel_id,
        'application_name': application_name,
        'answers': answers,
        'current_page': page,
        'total_questions': len(questions),
        'total_pages': total_pages,
        'timestamp': datetime.now(timezone.utc).timestamp()
    }
    save_pending_apps(apps)

def remove_pending_app(user_id: int, channel_id: int):
    """Remove a pending application"""
    apps = load_pending_apps()
    key = f"{user_id}_{channel_id}"
    if key in apps:
        del apps[key]
        save_pending_apps(apps)

def load_channel_openers():
    """Load channel opener mappings"""
    if CHANNEL_OPENERS_FILE.exists():
        with open(CHANNEL_OPENERS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_channel_openers(openers):
    """Save channel opener mappings"""
    with open(CHANNEL_OPENERS_FILE, 'w') as f:
        json.dump(openers, f, indent=4)

def load_notification_users():
    """Load users who want app notifications"""
    if NOTIFICATION_FILE.exists():
        with open(NOTIFICATION_FILE, 'r') as f:
            return set(json.load(f))
    return set()

def load_panels():
    """Load ticket panels from JSON file"""
    panels_file = _ROOT / 'data' / 'ticket_panels.json'
    if panels_file.exists():
        with open(panels_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_panels(panels):
    """Save ticket panels to JSON file"""
    with open(_ROOT / 'data' / 'ticket_panels.json', 'w', encoding='utf-8') as f:
        json.dump(panels, f, indent=4, ensure_ascii=False)

def get_panel_data_from_channel(channel):
    """Get panel data associated with this channel"""
    panels = load_panels()
    
    for panel_id, panel_data in panels.items():
        # Check if this channel is in the same category as the panel
        if channel.category_id == panel_data.get('ticket_category_id'):
            return panel_id, panel_data
    
    return None, None

def get_next_application_id(panel_message_id):
    """Get the next application ID for a panel"""
    panels = load_panels()
    
    if panel_message_id not in panels:
        return 1
    
    # Initialize application counter if it doesn't exist
    if 'application_counter' not in panels[panel_message_id]:
        panels[panel_message_id]['application_counter'] = 0
    
    # Increment and save
    panels[panel_message_id]['application_counter'] += 1
    save_panels(panels)
    
    return panels[panel_message_id]['application_counter']

def format_channel_name(template, user, application_id):
    """Format channel name with variables"""
    replacements = {
        '%user%': user.name.lower(),
        '%id%': str(application_id)
    }
    
    result = template
    for var, value in replacements.items():
        result = result.replace(var, value)
    
    # Clean up
    result = result.replace(' ', '-')
    result = result.lower()
    
    # Remove invalid characters
    result = re.sub(r'[^\w\-]', '', result)
    
    return result

def check_close_permissions(user, panel_data: dict, application_name: str) -> bool:
    """Check if user has permission to close the application"""
    permissions = panel_data.get('permissions', {}).get(application_name, {})
    
    # Check if user is in allowed users
    if user.id in permissions.get('users', []):
        return True
    
    # Check if user has any of the allowed roles
    user_role_ids = [role.id for role in user.roles]
    for role_id in permissions.get('roles', []):
        if role_id in user_role_ids:
            return True
    
    return False

async def create_transcript(channel):
    """Create a transcript of all messages in the channel"""
    transcript = []
    
    async for message in channel.history(limit=None, oldest_first=True):
        timestamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        author = f"{message.author.name}#{message.author.discriminator}" if message.author.discriminator != "0" else message.author.name
        content = message.content if message.content else "[No text content]"
        
        transcript_line = f"[{timestamp}] {author}: {content}"
        
        # Add attachment info
        if message.attachments:
            for attachment in message.attachments:
                transcript_line += f"\n  [Attachment: {attachment.filename} - {attachment.url}]"
        
        # Add detailed embed info
        if message.embeds:
            for i, embed in enumerate(message.embeds, 1):
                transcript_line += f"\n  [Embed #{i}]"
                if embed.title:
                    transcript_line += f"\n    Title: {embed.title}"
                if embed.description:
                    transcript_line += f"\n    Description: {embed.description}"
                if embed.url:
                    transcript_line += f"\n    URL: {embed.url}"
                if embed.color:
                    transcript_line += f"\n    Color: #{embed.color.value:06x}"
                if embed.footer:
                    transcript_line += f"\n    Footer: {embed.footer.text}"
                if embed.author:
                    transcript_line += f"\n    Author: {embed.author.name}"
                if embed.fields:
                    transcript_line += f"\n    Fields:"
                    for field in embed.fields:
                        transcript_line += f"\n      - {field.name}: {field.value}"
                if embed.image:
                    transcript_line += f"\n    Image: {embed.image.url}"
                if embed.thumbnail:
                    transcript_line += f"\n    Thumbnail: {embed.thumbnail.url}"
        
        transcript.append(transcript_line)
    
    return "\n".join(transcript)

def load_forwarded_apps():
    """Load forwarded applications from JSON file"""
    if FORWARDED_APPS_FILE.exists():
        with open(FORWARDED_APPS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_forwarded_apps(apps):
    """Save forwarded applications to JSON file"""
    with open(FORWARDED_APPS_FILE, 'w') as f:
        json.dump(apps, f, indent=4)

def save_forwarded_app(channel_id: int, message_id: int, user_id: int, app_type: str, parent_message_id: int = None, ticket_channel_id: int = None, threshold: int = None):
    """Save a forwarded application to the database"""
    apps = load_forwarded_apps()
    
    if threshold is None:
        threshold = 5
    
    apps[str(message_id)] = {
        'channel_id': channel_id,
        'message_id': message_id,
        'user_id': user_id,
        'app_type': app_type,
        'parent_message_id': parent_message_id,
        'ticket_channel_id': ticket_channel_id,
        'notified': False,
        'approve_notified': False,
        'deny_notified': False,
        'approve_count': 0,
        'deny_count': 0,
        'approve_voters': [],
        'deny_voters': [],
        'buttons_enabled': True,
        'last_stale_notification': 0,
        'threshold': threshold,
        'timestamp': datetime.now(timezone.utc).timestamp()
    }
    
    save_forwarded_apps(apps)

def calculate_threshold(guild):
    """Calculate the voting threshold based on number of jurors"""
    if guild.id == 1442126799369670770:
        return 1
    try:
        juror_role = guild.get_role(954566591520063510)
        if juror_role:
            juror_count = len(juror_role.members)
            threshold = max(1, min(8, juror_count // 4))
            return threshold
    except:
        pass
    return 5  # Default threshold

async def check_stale_applications(bot):
    """Check for applications that haven't met threshold after 24 hours"""
    while True:
        try:
            await asyncio.sleep(3600)  # Check every hour
            
            apps = load_forwarded_apps()
            current_time = datetime.now(timezone.utc).timestamp()
            
            for message_id, app_data in list(apps.items()):
                # Get the channel and guild to calculate threshold
                try:
                    channel = bot.get_channel(app_data['channel_id'])
                    if not channel:
                        continue
                    
                    threshold = app_data.get('threshold', calculate_threshold(channel.guild))
                    
                    # Check if thresholds are met
                    approve_count = app_data.get('approve_count', 0)
                    deny_count = app_data.get('deny_count', 0)
                    
                    # Skip if either threshold is met
                    if approve_count >= threshold or deny_count >= threshold:
                        continue
                    
                    # Get timestamp of app submission
                    app_timestamp = app_data.get('timestamp', 0)
                    
                    # Get timestamp of last stale notification (if any)
                    last_stale_notification = app_data.get('last_stale_notification', 0)
                    
                    # Calculate hours since submission
                    hours_since_submission = (current_time - app_timestamp) / 3600
                    
                    # Calculate hours since last notification
                    hours_since_last_notification = (current_time - last_stale_notification) / 3600 if last_stale_notification > 0 else hours_since_submission
                    
                    # Send reminder every 24 hours starting from first 24 hours after submission
                    if hours_since_submission >= 24 and hours_since_last_notification >= 24:
                        # Get the message
                        try:
                            message = await channel.fetch_message(app_data['message_id'])
                            
                            # Calculate how many days old this application is
                            days_old = int(hours_since_submission / 24)
                            
                            # Get applicant mention
                            applicant = channel.guild.get_member(app_data['user_id'])
                            applicant_mention = applicant.mention if applicant else f"<@{app_data['user_id']}>"

                            # Reply to the message
                            if days_old == 1:
                                reminder_text = f"Reminder: {applicant_mention}'s **{app_data['app_type']}** application needs your votes! (pending for 24 hours)"
                            else:
                                reminder_text = f"Reminder: {applicant_mention}'s **{app_data['app_type']}** application needs your votes! (pending for {days_old} days)"

                            reminder_text += f" Current votes: **{approve_count}/{threshold}** approve, **{deny_count}/{threshold}** deny. Please review and vote!"

                            # Check if we're in a thread
                            if isinstance(channel, discord.Thread):
                                # If in a thread, reply to the parent message in the main channel
                                if app_data.get('parent_message_id'):
                                    try:
                                        parent_channel = channel.parent
                                        parent_message = await parent_channel.fetch_message(app_data['parent_message_id'])
                                        await parent_message.reply(reminder_text)
                                    except Exception as e:
                                        print(f"Error replying to parent message: {e}")
                                        # Fallback to replying in thread
                                        await message.reply(reminder_text)
                                else:
                                    # No parent message ID, reply in thread
                                    await message.reply(reminder_text)
                            else:
                                # Regular channel - reply to the application message
                                await message.reply(reminder_text)
                            
                            # Update last notification timestamp
                            apps[message_id]['last_stale_notification'] = current_time
                            save_forwarded_apps(apps)
                            
                        except discord.NotFound:
                            # Message was deleted, remove from tracking
                            del apps[message_id]
                            save_forwarded_apps(apps)
                        except Exception as e:
                            print(f"Error sending stale reminder for {message_id}: {e}")
                    
                except Exception as e:
                    print(f"Error processing stale application {message_id}: {e}")
                    
        except Exception as e:
            print(f"Error in check_stale_applications: {e}")
            await asyncio.sleep(300)  # Wait 5 minutes before retrying on error

class ApplicationVoteView(View):
    """View for voting on applications with approve/deny buttons"""
    def __init__(self, app_data, approve_count=0, deny_count=0, threshold=None):
        super().__init__(timeout=None)
        self.app_data = app_data
        
        # Add approve button
        if threshold is not None:
            approve_label = f"✅ Approve ({approve_count}/{threshold})"
            deny_label = f"❌ Deny ({deny_count}/{threshold})"
        else:
            approve_label = f"✅ Approve ({approve_count})"
            deny_label = f"❌ Deny ({deny_count})"
        
        approve_button = Button(
            label=approve_label,
            style=discord.ButtonStyle.success,
            custom_id=f"app_vote_approve_{app_data['message_id']}"
        )
        approve_button.callback = self.approve_callback
        self.add_item(approve_button)
        
        # Add deny button
        deny_button = Button(
            label=deny_label,
            style=discord.ButtonStyle.danger,
            custom_id=f"app_vote_deny_{app_data['message_id']}"
        )
        deny_button.callback = self.deny_callback
        self.add_item(deny_button)
        
        # Add View Details button
        view_details_button = Button(
            label="📋 View Details",
            style=discord.ButtonStyle.secondary,
            custom_id=f"app_view_details_{app_data['message_id']}"
        )
        view_details_button.callback = self.view_details_callback
        self.add_item(view_details_button)
        
        # Check if buttons should be disabled
        if not app_data.get('buttons_enabled', True):
            for item in self.children:
                item.disabled = True
    
    async def approve_callback(self, interaction: discord.Interaction):
        """Handle approve vote"""
        apps = load_forwarded_apps()
        app_data = apps.get(str(self.app_data['message_id']))
        
        if not app_data:
            await interaction.response.send_message("❌ Application data not found!", ephemeral=True)
            return
        
        user_id = interaction.user.id
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        # Check if already voted approve, if so, remove the vote
        if user_id in approve_voters:
            approve_voters.remove(user_id)
            app_data['approve_count'] = max(0, len(approve_voters))
            app_data['approve_voters'] = approve_voters
            apps[str(self.app_data['message_id'])] = app_data
            save_forwarded_apps(apps)
            await interaction.response.defer()
            await self.check_and_update_view(interaction, app_data)
            return
        
        # Remove from deny voters if switching vote
        if user_id in deny_voters:
            deny_voters.remove(user_id)
            app_data['deny_count'] = max(0, app_data.get('deny_count', 0) - 1)
        
        # Add to approve voters
        approve_voters.append(user_id)
        app_data['approve_voters'] = approve_voters
        app_data['deny_voters'] = deny_voters
        app_data['approve_count'] = len(approve_voters)
        
        apps[str(self.app_data['message_id'])] = app_data
        save_forwarded_apps(apps)
        
        await interaction.response.defer()
        
        # Check threshold and update view
        await self.check_and_update_view(interaction, app_data)
    
    async def deny_callback(self, interaction: discord.Interaction):
        """Handle deny vote"""
        apps = load_forwarded_apps()
        app_data = apps.get(str(self.app_data['message_id']))
        
        if not app_data:
            await interaction.response.send_message("❌ Application data not found!", ephemeral=True)
            return
        
        user_id = interaction.user.id
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        # Check if already voted deny, if so, remove the vote
        if user_id in deny_voters:
            deny_voters.remove(user_id)
            app_data['deny_count'] = max(0, len(deny_voters))
            app_data['deny_voters'] = deny_voters
            apps[str(self.app_data['message_id'])] = app_data
            save_forwarded_apps(apps)
            await interaction.response.defer()
            await self.check_and_update_view(interaction, app_data)
            return
        
        # Remove from approve voters if switching vote
        if user_id in approve_voters:
            approve_voters.remove(user_id)
            app_data['approve_count'] = max(0, app_data.get('approve_count', 0) - 1)
        
        # Add to deny voters
        deny_voters.append(user_id)
        app_data['approve_voters'] = approve_voters
        app_data['deny_voters'] = deny_voters
        app_data['deny_count'] = len(deny_voters)
        
        apps[str(self.app_data['message_id'])] = app_data
        save_forwarded_apps(apps)
        
        await interaction.response.defer()
        
        # Check threshold and update view
        await self.check_and_update_view(interaction, app_data)
    
    async def check_and_update_view(self, interaction: discord.Interaction, app_data):
        """Check if threshold is met and update the view"""
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        approve_count = app_data.get('approve_count', 0)
        deny_count = app_data.get('deny_count', 0)
        
        approve_threshold_reached = approve_count >= threshold and not app_data.get('approve_notified', False)
        deny_threshold_reached = deny_count >= threshold and not app_data.get('deny_notified', False)
        
        
        # If threshold reached, create a mixed view with action buttons and vote buttons
        if approve_threshold_reached or deny_threshold_reached:
            # Create a mixed view
            mixed_view = ApplicationMixedView(
                app_data,
                approve_count,
                deny_count,
                show_approve_action=approve_threshold_reached or app_data.get('approve_notified', False),
                show_deny_action=deny_threshold_reached or app_data.get('deny_notified', False),
                threshold=threshold
            )
            
            try:
                # Update the message with the mixed view
                await interaction.message.edit(view=mixed_view)
                
                # Mark as notified
                apps = load_forwarded_apps()
                if approve_threshold_reached:
                    apps[str(app_data['message_id'])]['approve_notified'] = True
                if deny_threshold_reached:
                    apps[str(app_data['message_id'])]['deny_notified'] = True
                save_forwarded_apps(apps)
                
                # Add to guild queue if guild member app and guild is full
                if approve_threshold_reached and app_data.get('app_type', '').lower() != 'envoy':
                    try:
                        capacity = get_guild_capacity()
                        if capacity['is_full']:
                            username = extract_username_from_embeds(interaction.message.embeds)
                            discord_id = app_data['user_id']
                            member = interaction.guild.get_member(discord_id)
                            is_veteran = False
                            if member:
                                is_veteran = any(role.id == VETERAN_ROLE_ID for role in member.roles)
                            pos, qt = add_to_queue(username or "Unknown", None, discord_id, is_veteran)
                            apps = load_forwarded_apps()
                            apps[str(app_data['message_id'])]['queued'] = True
                            apps[str(app_data['message_id'])]['queue_position'] = pos
                            apps[str(app_data['message_id'])]['queue_type'] = qt
                            save_forwarded_apps(apps)
                            print(f"[QUEUE] Added {username} to {qt} queue at position {pos} (approve threshold reached, guild full)")
                            # Notify in the application channel
                            try:
                                queue_msg = f"⏳ <@{discord_id}> has been placed in the guild waiting queue (position #{pos}) as the guild is currently at full capacity."
                                
                                # Try multiple approaches to send notification
                                notification_sent = False
                                
                                # Approach 1: Try to reply to parent message if it exists
                                if app_data.get('parent_message_id'):
                                    try:
                                        parent_channel = interaction.channel.parent
                                        if parent_channel:
                                            parent_message = await parent_channel.fetch_message(app_data['parent_message_id'])
                                            await parent_message.reply(queue_msg)
                                            notification_sent = True
                                    except (discord.NotFound, discord.HTTPException) as e:
                                        print(f"Could not reply to parent message: {e}")
                                
                                # Approach 2: Try to get starter message
                                if not notification_sent:
                                    try:
                                        starter_message = interaction.channel.starter_message
                                        if starter_message:
                                            await starter_message.reply(queue_msg)
                                            notification_sent = True
                                    except (discord.NotFound, discord.HTTPException, AttributeError) as e:
                                        print(f"Could not use starter message: {e}")
                                
                                # Approach 3: Fallback to sending in thread
                                if not notification_sent:
                                    await interaction.channel.send(queue_msg)

                                # Send DM notifications
                                    await self.send_threshold_notifications(interaction, app_data, approve_threshold_reached, deny_threshold_reached, approve_count, deny_count, threshold)
                            except Exception as notify_err:
                                print(f"[WARN] Failed to send queue notification: {notify_err}")
                    except Exception as e:
                        print(f"[WARN] Failed to add player to queue at threshold: {e}")
                
                if not notification_sent:
                    try:
                        applicant = interaction.guild.get_member(app_data['user_id'])
                        applicant_mention = applicant.mention if applicant else f"<@{app_data['user_id']}>"

                        # Build notification messages
                        approve_msg = f"✅ **Approval threshold reached** for {applicant_mention}'s application!"
                        deny_msg = f"❌ **Denial threshold reached** for {applicant_mention}'s application!"
                        
                        # Check if we're in a thread
                        if isinstance(interaction.channel, discord.Thread):
                            # Try multiple approaches to send notification
                            notification_sent = False
                            
                            # Approach 1: Try to reply to parent message if it exists
                            if app_data.get('parent_message_id'):
                                try:
                                    parent_channel = interaction.channel.parent
                                    if parent_channel:
                                        parent_message = await parent_channel.fetch_message(app_data['parent_message_id'])
                                        if approve_threshold_reached:
                                            await parent_message.reply(approve_msg)
                                        if deny_threshold_reached:
                                            await parent_message.reply(deny_msg)
                                        notification_sent = True
                                except (discord.NotFound, discord.HTTPException) as e:
                                    print(f"Could not reply to parent message: {e}")
                            
                            # Approach 2: Try to get starter message
                            if not notification_sent:
                                try:
                                    starter_message = interaction.channel.starter_message
                                    if starter_message:
                                        if approve_threshold_reached:
                                            await starter_message.reply(approve_msg)
                                        if deny_threshold_reached:
                                            await starter_message.reply(deny_msg)
                                        notification_sent = True
                                except (discord.NotFound, discord.HTTPException, AttributeError) as e:
                                    print(f"Could not use starter message: {e}")
                            
                            # Approach 3: Fallback to sending in thread
                            if not notification_sent:
                                if approve_threshold_reached:
                                    await interaction.channel.send(approve_msg)
                                if deny_threshold_reached:
                                    await interaction.channel.send(deny_msg)
                        else:
                            # Regular channel - reply to the application message
                            if approve_threshold_reached:
                                await interaction.message.reply(approve_msg)
                            if deny_threshold_reached:
                                await interaction.message.reply(deny_msg)
                    except Exception as e:
                        print(f"Error sending threshold notification to channel: {e}")
                    
                    # Send DM notifications
                    await self.send_threshold_notifications(interaction, app_data, approve_threshold_reached, deny_threshold_reached, approve_count, deny_count, threshold)
            except Exception as e:
                print(f"Error updating message with mixed buttons: {e}")
                import traceback
                traceback.print_exc()
        else:
            # Update vote counts on buttons
            updated_view = ApplicationVoteView(app_data, approve_count, deny_count, threshold)
            
            try:
                await interaction.message.edit(view=updated_view)
            except:
                pass
    
    async def view_details_callback(self, interaction: discord.Interaction):
        """Show ticket details view"""
        await interaction.response.defer(ephemeral=True)
        
        from manage_tickets import TicketDetailViewStandalone, EmbedBuilder, calculate_threshold as calc_threshold
        
        apps = load_forwarded_apps()
        app_data = apps.get(str(self.app_data['message_id']))
        
        if not app_data:
            await interaction.followup.send("❌ Ticket data not found!", ephemeral=True)
            return
        
        threshold = app_data.get('threshold', calc_threshold(interaction.guild))
        
        # Build embed using utility class from manage_tickets
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "Ticket Details")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(app_data.get('approve_voters', []), threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(app_data.get('deny_voters', []), threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        # Create standalone detail view (no back button)
        detail_view = TicketDetailViewStandalone(str(self.app_data['message_id']), {}, interaction.guild, interaction.user)
        
        await interaction.followup.send(embed=embed, view=detail_view, ephemeral=True)
    
    async def send_threshold_notifications(self, interaction, app_data, approve_threshold_reached, deny_threshold_reached, approve_count, deny_count, threshold):
        """Send DM notifications when threshold is reached"""
        users = load_notification_users()
        if not users:
            return
        
        guild = interaction.guild
        applicant = guild.get_member(app_data['user_id'])
        applicant_name = applicant.name if applicant else f"User {app_data['user_id']}"
        
        for user_id in users:
            try:
                user = await interaction.client.fetch_user(user_id)
                
                # Determine notification type
                if approve_threshold_reached:
                    notification_type = "approval"
                    color = 0x00FF00  # Green
                    emoji = "✅"
                else:
                    notification_type = "denial"
                    color = 0xFF0000  # Red
                    emoji = "❌"
                
                dm_embed = discord.Embed(
                    title=f"{emoji} Application {notification_type.title()} Threshold Reached",
                    description=f"**{applicant_name}**'s **{app_data['app_type']}** application has reached the {notification_type} threshold.",
                    color=color,
                    timestamp=datetime.utcnow()
                )
                dm_embed.add_field(name="Channel", value=interaction.channel.mention, inline=True)
                dm_embed.add_field(name="Applicant", value=f"<@{app_data['user_id']}>", inline=True)
                dm_embed.add_field(name="Application Type", value=app_data['app_type'], inline=True)
                dm_embed.add_field(name="Approve Votes", value=f"{approve_count}/{threshold}", inline=True)
                dm_embed.add_field(name="Deny Votes", value=f"{deny_count}/{threshold}", inline=True)
                
                view_dm = discord.ui.View()
                view_dm.add_item(discord.ui.Button(
                    label="Jump to Application", 
                    url=f"https://discord.com/channels/{guild.id}/{interaction.channel.id}/{app_data['message_id']}", 
                    emoji="🔗"
                ))
                
                await user.send(embed=dm_embed, view=view_dm)
            except Exception as e:
                print(f"Failed to send threshold notification DM to user {user_id}: {e}")

class DenyReasonModal(Modal, title="Deny Reason"):
    reason_input = TextInput(
        label="Reason for denial",
        placeholder="...decided to refuse it due to <reason>.",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000
    )
    
    def __init__(self, app_data, interaction_message):
        super().__init__()
        self.app_data = app_data
        self.interaction_message = interaction_message
    
    async def send_threshold_notifications(self, interaction, app_data, approve_threshold_reached, deny_threshold_reached, approve_count, deny_count, threshold):
        """Send DM notifications when threshold is reached"""
        users = load_notification_users()
        if not users:
            return
        
        guild = interaction.guild
        applicant = guild.get_member(app_data['user_id'])
        applicant_name = applicant.name if applicant else f"User {app_data['user_id']}"
        
        for user_id in users:
            try:
                user = await interaction.client.fetch_user(user_id)
                
                # Determine notification type
                if approve_threshold_reached:
                    notification_type = "approval"
                    color = 0x00FF00  # Green
                    emoji = "✅"
                else:
                    notification_type = "denial"
                    color = 0xFF0000  # Red
                    emoji = "❌"
                
                dm_embed = discord.Embed(
                    title=f"{emoji} Application {notification_type.title()} Threshold Reached",
                    description=f"**{applicant_name}**'s **{app_data['app_type']}** application has reached the {notification_type} threshold.",
                    color=color,
                    timestamp=datetime.utcnow()
                )
                dm_embed.add_field(name="Channel", value=interaction.channel.mention, inline=True)
                dm_embed.add_field(name="Applicant", value=f"<@{app_data['user_id']}>", inline=True)
                dm_embed.add_field(name="Application Type", value=app_data['app_type'], inline=True)
                dm_embed.add_field(name="Approve Votes", value=f"{approve_count}/{threshold}", inline=True)
                dm_embed.add_field(name="Deny Votes", value=f"{deny_count}/{threshold}", inline=True)
                
                view_dm = discord.ui.View()
                view_dm.add_item(discord.ui.Button(
                    label="Jump to Application", 
                    url=f"https://discord.com/channels/{guild.id}/{interaction.channel.id}/{app_data['message_id']}", 
                    emoji="🔗"
                ))
                
                await user.send(embed=dm_embed, view=view_dm)
            except Exception as e:
                print(f"Failed to send threshold notification DM to user {user_id}: {e}")
    
    async def on_submit(self, interaction: discord.Interaction):
        user = interaction.guild.get_member(self.app_data['user_id'])
        app_type = self.app_data['app_type']
        reason = self.reason_input.value
        
        if not user:
            user_mention = f"<@{self.app_data['user_id']}>"
        else:
            user_mention = user.mention
        
        # Replace <reason> with actual reason
        deny_message = f"Hello there {user_mention}! Unfortunately after reviewing your {app_type} application we have decided to refuse it due to {reason}. We thank you for your time and we hope you stay motivated and continue enjoying Wynncraft. We wish you the best, and perhaps in the future you may apply again when you meet the requirements."

        await interaction.response.send_message(
            F"**Deny message for {user_mention}. Make sure to verify the message before notifying the applicant:**\n\n```{deny_message}```",
            ephemeral=True
        )
        
        apps = load_forwarded_apps()
        
        if str(self.app_data['message_id']) in apps:
            app_data = apps[str(self.app_data['message_id'])]
            threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
            
            approve_count = app_data.get('approve_count', 0)
            deny_count = app_data.get('deny_count', 0)
            
            save_forwarded_apps(apps)
            
            if str(self.app_data['message_id']) in apps:
                apps[str(self.app_data['message_id'])]['status'] = "denied"
                apps[str(self.app_data['message_id'])]['buttons_enabled'] = False
                save_forwarded_apps(apps)
            
            # Remove from guild queue if queued
            applicant_id = self.app_data.get('user_id')
            if applicant_id:
                removed = remove_from_queue(applicant_id)
                if removed:
                    print(f"[QUEUE] Removed user {applicant_id} from guild queue (application denied)")
            
            app_data = apps.get(str(self.app_data['message_id']), self.app_data)
            threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
            approve_count = app_data.get('approve_count', 0)
            deny_count = app_data.get('deny_count', 0)
            
            disabled_view = ApplicationMixedView(
                app_data,
                approve_count,
                deny_count,
                show_approve_action=(approve_count >= threshold or app_data.get('approve_notified', False)),
                show_deny_action=(deny_count >= threshold or app_data.get('deny_notified', False)),
                threshold=threshold
            )
            
            for i, item in enumerate(disabled_view.children):
                item.disabled = True
            
            try:
                await self.interaction_message.edit(view=disabled_view)
            except:
                print(f"[DEBUG] Failed to edit message {self.interaction_message.id}")

class ApplicationMixedView(View):
    """View that shows a mix of vote buttons and action buttons"""
    def __init__(self, app_data, approve_count=0, deny_count=0, show_approve_action=False, show_deny_action=False, threshold=None):
        super().__init__(timeout=None)
        self.app_data = app_data

        # is guild full
        is_guild_full = False
        if app_data.get('app_type', '').lower() != 'envoy':
            try:
                capacity = get_guild_capacity()
                if capacity['is_full']:
                    is_guild_full = True
            except Exception:
                pass
        
        # Add approve button (either vote or action)
        if show_approve_action:
            # Show action button
            accept_button = Button(
                label="✅ Accept Application",
                style=discord.ButtonStyle.success,
                custom_id=f"app_accept_{app_data['message_id']}"
            )
            # Disable accept if guild is at max capacity
            if is_guild_full:
                accept_button.disabled = True
                accept_button.label = "⏳ Guild Full"
            accept_button.callback = self.accept_callback
            self.add_item(accept_button)
        else:
            # Show vote button
            if threshold is not None:
                approve_label = f"✅ Approve ({approve_count}/{threshold})"
            else:
                approve_label = f"✅ Approve ({approve_count})"
            
            approve_button = Button(
                label=approve_label,
                style=discord.ButtonStyle.success,
                custom_id=f"app_vote_approve_{app_data['message_id']}"
            )
            approve_button.callback = self.approve_callback
            self.add_item(approve_button)
        
        # Add deny button (either vote or action)
        if show_deny_action:
            # Show action button
            deny_button = Button(
                label="❌ Deny Application",
                style=discord.ButtonStyle.danger,
                custom_id=f"app_deny_{app_data['message_id']}"
            )
            deny_button.callback = self.deny_callback_action
            self.add_item(deny_button)
        else:
            # Show vote button
            if threshold is not None:
                deny_label = f"❌ Deny ({deny_count}/{threshold})"
            else:
                deny_label = f"❌ Deny ({deny_count})"
            
            deny_button = Button(
                label=deny_label,
                style=discord.ButtonStyle.danger,
                custom_id=f"app_vote_deny_{app_data['message_id']}"
            )
            # Disable deny if guild is at max capacity
            if is_guild_full:
                deny_button.disabled = True
            deny_button.callback = self.deny_callback
            self.add_item(deny_button)
        
        # Add View Details button
        view_details_button = Button(
            label="📋 View Details",
            style=discord.ButtonStyle.secondary,
            custom_id=f"app_view_details_{app_data['message_id']}"
        )
        view_details_button.callback = self.view_details_callback
        self.add_item(view_details_button)
    
    async def view_details_callback(self, interaction: discord.Interaction):
        """Show ticket details view"""
        from manage_tickets import TicketDetailViewStandalone, EmbedBuilder, calculate_threshold as calc_threshold
        
        apps = load_forwarded_apps()
        app_data = apps.get(str(self.app_data['message_id']))
        
        if not app_data:
            await interaction.response.send_message("❌ Ticket data not found!", ephemeral=True)
            return
        
        threshold = app_data.get('threshold', calc_threshold(interaction.guild))
        
        # Build embed using utility class from manage_tickets
        embed = EmbedBuilder.build_ticket_embed(app_data, interaction.guild, "Ticket Details")
        
        # Add vote fields
        approve_field = EmbedBuilder.build_vote_display(app_data.get('approve_voters', []), threshold, "Approve")
        deny_field = EmbedBuilder.build_vote_display(app_data.get('deny_voters', []), threshold, "Deny")
        
        embed.add_field(**approve_field)
        embed.add_field(**deny_field)
        EmbedBuilder.add_queue_field(embed, app_data)
        embed.set_footer(text=f"Application submitted")
        
        # Create standalone detail view (no back button)
        detail_view = TicketDetailViewStandalone(str(self.app_data['message_id']), {}, interaction.guild, interaction.user)
        
        await interaction.response.send_message(embed=embed, view=detail_view, ephemeral=True)
    
    # Copy the approve and deny voting callbacks
    async def approve_callback(self, interaction: discord.Interaction):
        """Handle approve vote"""
        apps = load_forwarded_apps()
        app_data = apps.get(str(self.app_data['message_id']))
        
        if not app_data:
            await interaction.response.send_message("❌ Application data not found!", ephemeral=True)
            return
        
        user_id = interaction.user.id
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        # Check if already voted approve, if so, remove the vote
        if user_id in approve_voters:
            approve_voters.remove(user_id)
            app_data['approve_count'] = max(0, len(approve_voters))
            app_data['approve_voters'] = approve_voters
            apps[str(self.app_data['message_id'])] = app_data
            save_forwarded_apps(apps)
            await interaction.response.defer()
            await self.check_and_update_mixed_view(interaction, app_data)
            return
        
        if user_id in deny_voters:
            deny_voters.remove(user_id)
            app_data['deny_count'] = max(0, app_data.get('deny_count', 0) - 1)
        
        approve_voters.append(user_id)
        app_data['approve_voters'] = approve_voters
        app_data['deny_voters'] = deny_voters
        app_data['approve_count'] = len(approve_voters)
        
        apps[str(self.app_data['message_id'])] = app_data
        save_forwarded_apps(apps)
        
        await interaction.response.defer()
        
        # Check threshold - recreate view with updated counts
        await self.check_and_update_mixed_view(interaction, app_data)
    
    async def deny_callback(self, interaction: discord.Interaction):
        """Handle deny vote"""
        apps = load_forwarded_apps()
        app_data = apps.get(str(self.app_data['message_id']))
        
        if not app_data:
            await interaction.response.send_message("❌ Application data not found!", ephemeral=True)
            return
        
        user_id = interaction.user.id
        approve_voters = app_data.get('approve_voters', [])
        deny_voters = app_data.get('deny_voters', [])
        
        # Check if already voted deny, if so, remove the vote
        if user_id in deny_voters:
            deny_voters.remove(user_id)
            app_data['deny_count'] = max(0, len(deny_voters))
            app_data['deny_voters'] = deny_voters
            apps[str(self.app_data['message_id'])] = app_data
            save_forwarded_apps(apps)
            await interaction.response.defer()
            await self.check_and_update_mixed_view(interaction, app_data)
            return
        
        # Remove from approve voters if switching vote
        if user_id in approve_voters:
            approve_voters.remove(user_id)
            app_data['approve_count'] = max(0, app_data.get('approve_count', 0) - 1)
        
        # Add to deny voters
        deny_voters.append(user_id)
        app_data['approve_voters'] = approve_voters
        app_data['deny_voters'] = deny_voters
        app_data['deny_count'] = len(deny_voters)
        
        apps[str(self.app_data['message_id'])] = app_data
        save_forwarded_apps(apps)
        
        await interaction.response.defer()
        
        # Check threshold and update view
        await self.check_and_update_mixed_view(interaction, app_data)
    
    async def check_and_update_mixed_view(self, interaction: discord.Interaction, app_data):
        """Check if threshold is met and update the mixed view"""
        threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
        approve_count = app_data.get('approve_count', 0)
        deny_count = app_data.get('deny_count', 0)
        
        approve_threshold_reached = approve_count >= threshold and not app_data.get('approve_notified', False)
        deny_threshold_reached = deny_count >= threshold and not app_data.get('deny_notified', False)

        # Determine which buttons to show as actions
        # Show action button if threshold is currently met OR was previously met and still above threshold
        show_approve_action = (approve_count >= threshold) or (app_data.get('approve_notified', False) and approve_count >= threshold)
        show_deny_action = (deny_count >= threshold) or (app_data.get('deny_notified', False) and deny_count >= threshold)

        # If count drops below threshold, reset the notified flag
        if approve_count < threshold:
            apps = load_forwarded_apps()
            apps[str(app_data['message_id'])]['approve_notified'] = False
            save_forwarded_apps(apps)
            app_data['approve_notified'] = False
            
        if deny_count < threshold:
            apps = load_forwarded_apps()
            apps[str(app_data['message_id'])]['deny_notified'] = False
            save_forwarded_apps(apps)
            app_data['deny_notified'] = False
        
        # Create updated mixed view
        updated_view = ApplicationMixedView(
            app_data,
            approve_count,
            deny_count,
            show_approve_action,
            show_deny_action,
            threshold
        )
        
        try:
            await interaction.message.edit(view=updated_view)
            
            # Mark as notified and send DMs if threshold just reached
            if approve_threshold_reached or deny_threshold_reached:
                apps = load_forwarded_apps()
                if approve_threshold_reached:
                    apps[str(app_data['message_id'])]['approve_notified'] = True
                if deny_threshold_reached:
                    apps[str(app_data['message_id'])]['deny_notified'] = True
                save_forwarded_apps(apps)
                
                # Add to guild queue if guild member app and guild is full
                if approve_threshold_reached and app_data.get('app_type', '').lower() != 'envoy':
                    try:
                        capacity = get_guild_capacity()
                        if capacity['is_full']:
                            username = extract_username_from_embeds(interaction.message.embeds)
                            discord_id = app_data['user_id']
                            member = interaction.guild.get_member(discord_id)
                            is_veteran = False
                            if member:
                                is_veteran = any(role.id == VETERAN_ROLE_ID for role in member.roles)
                            pos, qt = add_to_queue(username or "Unknown", None, discord_id, is_veteran)
                            apps = load_forwarded_apps()
                            apps[str(app_data['message_id'])]['queued'] = True
                            apps[str(app_data['message_id'])]['queue_position'] = pos
                            apps[str(app_data['message_id'])]['queue_type'] = qt
                            save_forwarded_apps(apps)
                            print(f"[QUEUE] Added {username} to {qt} queue at position {pos} (approve threshold reached, guild full)")
                            # Notify in the application channel
                            try:
                                queue_msg = f"⏳ <@{discord_id}> has been placed in the guild waiting queue (position #{pos}) as the guild is currently at full capacity."
                                
                                # Try multiple approaches to send notification
                                notification_sent = False
                                
                                # Approach 1: Try to reply to parent message if it exists
                                if app_data.get('parent_message_id'):
                                    try:
                                        parent_channel = interaction.channel.parent
                                        if parent_channel:
                                            parent_message = await parent_channel.fetch_message(app_data['parent_message_id'])
                                            await parent_message.reply(queue_msg)
                                            notification_sent = True
                                    except (discord.NotFound, discord.HTTPException) as e:
                                        print(f"Could not reply to parent message: {e}")
                                
                                # Approach 2: Try to get starter message
                                if not notification_sent:
                                    try:
                                        starter_message = interaction.channel.starter_message
                                        if starter_message:
                                            await starter_message.reply(queue_msg)
                                            notification_sent = True
                                    except (discord.NotFound, discord.HTTPException, AttributeError) as e:
                                        print(f"Could not use starter message: {e}")
                                
                                # Approach 3: Fallback to sending in thread
                                if not notification_sent:
                                    await interaction.channel.send(queue_msg)
                            except Exception as notify_err:
                                print(f"[WARN] Failed to send queue notification: {notify_err}")
                    except Exception as e:
                        print(f"[WARN] Failed to add player to queue at threshold: {e}")
                
                try:
                    applicant = interaction.guild.get_member(app_data['user_id'])
                    applicant_mention = applicant.mention if applicant else f"<@{app_data['user_id']}>"

                    # Build notification messages
                    approve_msg = f"✅ **Approval threshold reached** for {applicant_mention}'s application!"
                    deny_msg = f"❌ **Denial threshold reached** for {applicant_mention}'s application!"
                    
                    # Check if we're in a thread
                    if isinstance(interaction.channel, discord.Thread):
                        # Try multiple approaches to send notification
                        notification_sent = False
                        
                        # Approach 1: Try to reply to parent message if it exists
                        if app_data.get('parent_message_id'):
                            try:
                                parent_channel = interaction.channel.parent
                                if parent_channel:
                                    parent_message = await parent_channel.fetch_message(app_data['parent_message_id'])
                                    if approve_threshold_reached:
                                        await parent_message.reply(approve_msg)
                                    if deny_threshold_reached:
                                        await parent_message.reply(deny_msg)
                                    notification_sent = True
                            except (discord.NotFound, discord.HTTPException) as e:
                                print(f"Could not reply to parent message: {e}")
                        
                        # Approach 2: Try to get starter message
                        if not notification_sent:
                            try:
                                starter_message = interaction.channel.starter_message
                                if starter_message:
                                    if approve_threshold_reached:
                                        await starter_message.reply(approve_msg)
                                    if deny_threshold_reached:
                                        await starter_message.reply(deny_msg)
                                    notification_sent = True
                            except (discord.NotFound, discord.HTTPException, AttributeError) as e:
                                print(f"Could not use starter message: {e}")
                        
                        # Approach 3: Fallback to sending in thread
                        if not notification_sent:
                            if approve_threshold_reached:
                                await interaction.channel.send(approve_msg)
                            if deny_threshold_reached:
                                await interaction.channel.send(deny_msg)
                    else:
                        # Regular channel - reply to the application message
                        if approve_threshold_reached:
                            await interaction.message.reply(approve_msg)
                        if deny_threshold_reached:
                            await interaction.message.reply(deny_msg)
                except Exception as e:
                    print(f"Error sending threshold notification to channel: {e}")
                
                # Import send_threshold_notifications from ApplicationVoteView
                vote_view = ApplicationVoteView(app_data)
                await vote_view.send_threshold_notifications(interaction, app_data, approve_threshold_reached, deny_threshold_reached, approve_count, deny_count, threshold)
        except Exception as e:
            print(f"Error updating mixed view: {e}")
    
    # Copy the accept and deny action callbacks from ApplicationActionView
    async def accept_callback(self, interaction: discord.Interaction):
        user = interaction.guild.get_member(self.app_data['user_id'])
        
        if not user:
            await interaction.response.send_message("❌ User not found in this server!", ephemeral=True)
            return
        
        username = None
        detected_pronoun = None
        
        try:
            message = interaction.message
            if message.embeds:
                for embed in message.embeds:
                    for field in embed.fields:
                        field_name_lower = field.name.lower()
                        
                        if any(keyword in field_name_lower for keyword in ['username', 'in game name', 'nickname', 'in-game name']):
                            username = field.value.replace('`', '').strip()
                            if username and username != "*No answer provided*":
                                continue
                        
                        if 'pronoun' in field_name_lower:
                            pronoun_value = field.value.replace('`', '').strip().lower()
                            # Normalize the pronoun value by removing common separators and extra words
                            normalized = pronoun_value.replace('/', ' ').replace('and', ' ').replace(',', ' ')
                            # Remove extra whitespace
                            normalized = ' '.join(normalized.split())
                            
                            # Define pronoun patterns to match
                            pronoun_patterns = {
                                'she/her': ['she her', 'she', 'her'],
                                'he/him': ['he him', 'he', 'him'],
                                'they/them': ['they them', 'they', 'them'],
                                'it/its': ['it its', 'it', 'its']
                            }
                            
                            # Try to match the normalized pronoun value
                            for standard_form, patterns in pronoun_patterns.items():
                                for pattern in patterns:
                                    if pattern == normalized or pattern in normalized:
                                        detected_pronoun = standard_form
                                        break
                                if detected_pronoun:
                                    break
        except Exception as e:
            print(f"Error getting username/pronouns from embed: {e}")
        
        import sys
        import os
        from pathlib import Path
        
        current_dir = Path(__file__).parent
        if str(current_dir) not in sys.path:
            sys.path.insert(0, str(current_dir))
        
        from accept import VETERAN_ID, EX_CITIZEN_ID, UsernameModal, UsernameEditModal
        
        app_type = self.app_data['app_type']
        
        user_role_ids = [role.id for role in user.roles]
        is_ex_member = VETERAN_ID in user_role_ids or EX_CITIZEN_ID in user_role_ids
        
        # Determine rank and pronoun before the ex-member check
        if app_type.lower() == 'ex-citizen' or is_ex_member:
            # For ex-members, show rank selection modal instead of auto-assigning
            from accept import UsernameModal
            source_msg_id = self.app_data.get('message_id')
            modal = UsernameModal(user, default_username=username, source_message_id=source_msg_id, is_ex_citizen=True, from_application=True)
            await interaction.response.send_modal(modal)
            return
        elif app_type.lower() == 'guild member':
            rank_key = 'squire'
            needs_pronoun = False
        elif app_type.lower() == 'envoy':
            rank_key = 'envoy'
            needs_pronoun = True
        else:
            rank_key = 'squire'
            needs_pronoun = False
        
        source_msg_id = self.app_data.get('message_id')
        print(f"[DEBUG] Creating UsernameEditModal with source_message_id={source_msg_id}")
        modal = UsernameEditModal(user, username, rank_key, needs_pronoun, interaction.user.id, detected_pronoun, source_message_id=source_msg_id)
        await interaction.response.send_modal(modal)
        
        # Mark as accepted instead of removing
        apps = load_forwarded_apps()
        if str(self.app_data['message_id']) in apps:
            apps[str(self.app_data['message_id'])]['status'] = 'accepted'
            save_forwarded_apps(apps)
    
    async def deny_callback_action(self, interaction: discord.Interaction):
        # Show modal for deny reason
        modal = DenyReasonModal(self.app_data, interaction.message)
        await interaction.response.send_modal(modal)

class ApplicationActionView(View):
    def __init__(self, app_data, show_accept=True, show_deny=True):
        super().__init__(timeout=None)
        self.app_data = app_data
        
        if show_accept:
            accept_button = Button(
                label="✅ Accept Application",
                style=discord.ButtonStyle.success,
                custom_id=f"app_accept_{app_data['message_id']}"
            )
            accept_button.callback = self.accept_callback
            self.add_item(accept_button)
        
        if show_deny:
            deny_button = Button(
                label="❌ Deny Application",
                style=discord.ButtonStyle.danger,
                custom_id=f"app_deny_{app_data['message_id']}"
            )
            deny_button.callback = self.deny_callback
            self.add_item(deny_button)
    
    async def accept_callback(self, interaction: discord.Interaction):
        user = interaction.guild.get_member(self.app_data['user_id'])
        
        if not user:
            await interaction.response.send_message("❌ User not found in this server!", ephemeral=True)
            return
        
        username = None
        detected_pronoun = None
        
        try:
            message = interaction.message
            if message.embeds:
                for embed in message.embeds:
                    for field in embed.fields:
                        field_name_lower = field.name.lower()
                        
                        if any(keyword in field_name_lower for keyword in ['username', 'in game name', 'nickname', 'in-game name']):
                            username = field.value.replace('`', '').strip()
                            if username and username != "*No answer provided*":
                                continue
                        
                        if 'pronoun' in field_name_lower:
                            pronoun_value = field.value.replace('`', '').strip().lower()
                            # Normalize the pronoun value by removing common separators and extra words
                            normalized = pronoun_value.replace('/', ' ').replace('and', ' ').replace(',', ' ')
                            # Remove extra whitespace
                            normalized = ' '.join(normalized.split())
                            
                            # Define pronoun patterns to match
                            pronoun_patterns = {
                                'she/her': ['she her', 'she', 'her'],
                                'he/him': ['he him', 'he', 'him'],
                                'they/them': ['they them', 'they', 'them'],
                                'it/its': ['it its', 'it', 'its']
                            }
                            
                            # Try to match the normalized pronoun value
                            for standard_form, patterns in pronoun_patterns.items():
                                for pattern in patterns:
                                    if pattern == normalized or pattern in normalized:
                                        detected_pronoun = standard_form
                                        break
                                if detected_pronoun:
                                    break
        except Exception as e:
            print(f"Error getting username/pronouns from embed: {e}")
        
        import sys
        import os
        from pathlib import Path
        
        current_dir = Path(__file__).parent
        if str(current_dir) not in sys.path:
            sys.path.insert(0, str(current_dir))
        
        from accept import VETERAN_ID, EX_CITIZEN_ID, UsernameModal, UsernameEditModal
        
        app_type = self.app_data['app_type']
        
        # Check if user is ex-member by role OR by application type
        user_role_ids = [role.id for role in user.roles]
        is_ex_member = VETERAN_ID in user_role_ids or EX_CITIZEN_ID in user_role_ids
        
        # Also treat as ex-member if the application type indicates it
        if app_type.lower() == 'ex-citizen':
            is_ex_member = True
        
        if is_ex_member:
            from accept import UsernameModal
            source_msg_id = self.app_data.get('message_id')
            modal = UsernameModal(user, default_username=username, source_message_id=source_msg_id, is_ex_citizen=True, from_application=True)
            await interaction.response.send_modal(modal)
            return
        elif app_type.lower() == 'guild member':
            rank_key = 'squire'
            needs_pronoun = False
        elif app_type.lower() == 'envoy':
            rank_key = 'envoy'
            needs_pronoun = True
        else:
            rank_key = 'squire'
            needs_pronoun = False
        
        source_msg_id = self.app_data.get('message_id')
        print(f"[DEBUG] Creating UsernameEditModal with source_message_id={source_msg_id}")
        modal = UsernameEditModal(user, username, rank_key, needs_pronoun, interaction.user.id, detected_pronoun, source_message_id=source_msg_id)
        await interaction.response.send_modal(modal)
        
        # Mark as accepted instead of removing
        apps = load_forwarded_apps()
        if str(self.app_data['message_id']) in apps:
            apps[str(self.app_data['message_id'])]['approve_notified'] = True
            apps[str(self.app_data['message_id'])]['deny_notified'] = True
            save_forwarded_apps(apps)
        
        await interaction.message.edit(view=None)
        
    async def deny_callback(self, interaction: discord.Interaction):
        user = interaction.guild.get_member(self.app_data['user_id'])
        app_type = self.app_data['app_type']
        
        if not user:
            user_mention = f"<@{self.app_data['user_id']}>"
        else:
            user_mention = user.mention
        
        deny_message = f"Hello there {user_mention}! Unfortunately after reviewing your {app_type} application we have decided to refuse it due to <reason>. We thank you for your time and we hope you stay motivated and continue enjoying Wynncraft. We wish you the best, and perhaps in the future you may apply again when you meet the requirements."

        await interaction.response.send_message(
            f"**Deny message for {user_mention}:**\n\n```{deny_message}```",
            ephemeral=True
        )
        
        # Mark as denied instead of removing
        apps = load_forwarded_apps()
        if str(self.app_data['message_id']) in apps:
            apps[str(self.app_data['message_id'])]['approve_notified'] = True
            apps[str(self.app_data['message_id'])]['deny_notified'] = True
            apps[str(self.app_data['message_id'])]['status'] = 'denied'
            save_forwarded_apps(apps)
        
        # Remove user from guild queue if they were queued
        user_id = self.app_data.get('user_id')
        if user_id:
            removed = remove_from_queue(user_id)
            if removed:
                print(f"[DEBUG] Removed user {user_id} from guild queue (application denied)")
        
        await interaction.message.edit(view=None)

class ConfirmCloseView(View):
    def __init__(self, original_interaction, application_name, panel_data, panel_id, reason=None):
        super().__init__(timeout=None)
        self.original_interaction = original_interaction
        self.application_name = application_name
        self.panel_data = panel_data
        self.panel_id = panel_id
        self.reason = reason
    
    @discord.ui.button(label="Confirm Close", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: Button):
        await self.close_application_logic(interaction)
    
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: Button):
        try:
            await interaction.message.delete()
        except discord.NotFound:
            pass
    
    async def close_application_logic(self, interaction: discord.Interaction):
        from datetime import timezone
        
        channel = interaction.channel
        
        # Get channel creation time from the first message or channel itself
        created_at = channel.created_at
        closed_at = datetime.now(timezone.utc)
        
        # Calculate open duration
        duration = closed_at - created_at
        hours, remainder = divmod(int(duration.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        duration_str = f"{hours}h {minutes}m {seconds}s"
        
        # Find who opened the ticket (first user mentioned or channel creator)
        opened_by = None
        async for message in channel.history(limit=10, oldest_first=True):
            if message.embeds and message.embeds[0].description:
                # Try to find user mention in the embed
                mentions = re.findall(r'<@!?(\d+)>', message.embeds[0].description)
                if mentions:
                    opened_by = interaction.guild.get_member(int(mentions[0]))
                    break
        
        if not opened_by:
            # Fallback: check channel permissions for non-staff users
            for overwrite in channel.overwrites:
                if isinstance(overwrite, discord.Member) and overwrite != interaction.guild.me:
                    opened_by = overwrite
                    break
        
        # Create transcript
        await interaction.response.send_message("📝 Creating transcript...", ephemeral=True)
        transcript_text = await create_transcript(channel)
        
        # Create transcript file
        transcript_bytes = io.BytesIO(transcript_text.encode('utf-8'))
        transcript_file = discord.File(
            fp=transcript_bytes,
            filename=f"transcript-{channel.name}-{int(closed_at.timestamp())}.txt"
        )
        
        # Send to transcript/logging channel
        if 'logging_channel_id' in self.panel_data:
            log_channel = interaction.guild.get_channel(self.panel_data['logging_channel_id'])
            if log_channel:
                close_embed = discord.Embed(
                    title="Application Closed",
                    color=0x5865F2,
                    timestamp=closed_at
                )
                close_embed.add_field(name="Application Type", value=self.application_name, inline=True)
                close_embed.add_field(name="Channel", value=channel.name, inline=True)
                close_embed.add_field(name="Opened By", value=opened_by.mention if opened_by else "Unknown", inline=True)
                close_embed.add_field(name="Closed By", value=interaction.user.mention, inline=True)
                close_embed.add_field(name="Open Duration", value=duration_str, inline=True)
                close_embed.add_field(name="Reason", value=self.reason if self.reason else "No reason provided", inline=True)
                close_embed.set_footer(text=f"Closed at")
                
                try:
                    await log_channel.send(embed=close_embed, file=transcript_file)
                except discord.Forbidden:
                    print(f"Missing permissions to send log to channel {log_channel.id}")
                except Exception as e:
                    print(f"Error sending log message: {e}")
        
        # Send DM if enabled
        settings = self.panel_data.get('settings', {}).get(self.application_name, {})
        dm_enabled = settings.get('dm_on_close', True)
        
        if dm_enabled and opened_by:
            try:
                dm_embed = discord.Embed(
                    title=f"🔒 Your {self.application_name} Application Has Been Closed",
                    description=f"Your application in **{interaction.guild.name}** has been closed.",
                    color=0xFF0000,
                    timestamp=closed_at
                )
                dm_embed.add_field(name="Closed By", value=interaction.user.name, inline=True)
                dm_embed.add_field(name="Open Duration", value=duration_str, inline=True)
                dm_embed.add_field(name="Reason", value=self.reason if self.reason else "No reason provided", inline=False)
                
                await opened_by.send(embed=dm_embed)
            except discord.Forbidden:
                pass  # User has DMs disabled
        
        # Clean up the channel opener mapping
        channel_openers = load_channel_openers()
        if str(channel.id) in channel_openers:
            del channel_openers[str(channel.id)]
            save_channel_openers(channel_openers)
            
        # Remove any forwarded applications associated with this ticket channel
        apps = load_forwarded_apps()
        apps_to_remove = []
        for message_id, app_data in apps.items():
            if app_data.get('ticket_channel_id') == channel.id:
                apps_to_remove.append(message_id)
                
                # Disable buttons on the forwarded application message
                try:
                    # Check if the channel is a thread
                    forwarded_channel_or_thread = interaction.guild.get_channel_or_thread(app_data['channel_id'])
                    if forwarded_channel_or_thread:
                        try:
                            forwarded_message = await forwarded_channel_or_thread.fetch_message(app_data['message_id'])
                            
                            # Determine which view to create based on current state
                            threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
                            approve_count = app_data.get('approve_count', 0)
                            deny_count = app_data.get('deny_count', 0)
                            
                            # Check if we need mixed view or vote view
                            if app_data.get('approve_notified') or app_data.get('deny_notified'):
                                disabled_view = ApplicationMixedView(
                                    app_data,
                                    approve_count,
                                    deny_count,
                                    show_approve_action=app_data.get('approve_notified', False),
                                    show_deny_action=app_data.get('deny_notified', False),
                                    threshold=threshold
                                )
                            else:
                                disabled_view = ApplicationVoteView(
                                    app_data,
                                    approve_count,
                                    deny_count,
                                    threshold
                                )
                            
                            # Disable all buttons
                            for item in disabled_view.children:
                                item.disabled = True
                            
                            await forwarded_message.edit(view=disabled_view)
                            print(f"[DEBUG] Disabled buttons for forwarded application {message_id}")
                        except discord.NotFound:
                            print(f"[DEBUG] Forwarded message {message_id} not found")
                        except Exception as e:
                            print(f"[DEBUG] Error disabling buttons for forwarded application {message_id}: {e}")
                except Exception as e:
                    print(f"[DEBUG] Error processing forwarded application {message_id}: {e}")

        for message_id in apps_to_remove:
            # Remove user from guild queue if they were queued
            user_id = apps[message_id].get('user_id')
            if user_id:
                removed = remove_from_queue(user_id)
                if removed:
                    print(f"[DEBUG] Removed user {user_id} from guild queue (ticket closed)")
            del apps[message_id]
            print(f"[DEBUG] Removed application {message_id} from forwarded_applications.json (ticket closed)")

        if apps_to_remove:
            save_forwarded_apps(apps)
        
        # Remove any pending applications associated with this ticket channel
        pending_apps = load_pending_apps()
        pending_keys_to_remove = []
        for pending_key, pending_data in pending_apps.items():
            if pending_data.get('channel_id') == channel.id:
                pending_keys_to_remove.append(pending_key)

        for pending_key in pending_keys_to_remove:
            del pending_apps[pending_key]
            print(f"[DEBUG] Removed pending application {pending_key} from pending_applications.json (ticket closed)")

        if pending_keys_to_remove:
            save_pending_apps(pending_apps)
        
        # Delete the channel
        await channel.delete(reason=f"Application closed by {interaction.user}")

class CloseReasonModal(Modal, title="Close Application"):
    reason_input = TextInput(
        label="Reason for closing",
        placeholder="Enter the reason for closing this application...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000
    )
    
    def __init__(self, application_name, panel_data, panel_id):
        super().__init__()
        self.application_name = application_name
        self.panel_data = panel_data
        self.panel_id = panel_id
    
    async def on_submit(self, interaction: discord.Interaction):
        reason = self.reason_input.value
        # Close immediately without confirmation when reason is provided
        view = ConfirmCloseView(interaction, self.application_name, self.panel_data, self.panel_id, reason)
        await view.close_application_logic(interaction)

class ContinueApplicationView(View):
    def __init__(self, modal, page_info=None):
        super().__init__(timeout=None)
        self.modal = modal
        self.page_info = page_info
    
    @discord.ui.button(label="Continue Application", style=discord.ButtonStyle.primary)
    async def continue_button(self, interaction: discord.Interaction, button: Button):
        # Save as pending when they start
        save_pending_app(
            interaction.user.id,
            interaction.channel.id,
            self.modal.application_name,
            self.modal.answers,
            self.modal.page,
            self.modal.all_questions
        )
        
        # Store the interaction reference before showing modal
        self.modal.initial_interaction = interaction
        await interaction.response.send_modal(self.modal)

class ApplicationNavigationView(View):
    def __init__(self, application_name, questions, current_page, answers, progress_message=None, target_user=None):
        super().__init__(timeout=None)
        self.application_name = application_name
        self.questions = questions
        self.current_page = current_page
        self.answers = answers
        self.total_pages = (len(questions) + 4) // 5
        self.progress_message = progress_message
        self.initial_interaction = None
        self.target_user = target_user  # The user the application is being filled for
        
        # Add page selector dropdown
        options = []
        for page_num in range(self.total_pages):
            start_idx = page_num * 5
            end_idx = min(start_idx + 5, len(questions))
            page_questions = questions[start_idx:end_idx]
            
            # Check if all required questions on this page are answered
            all_answered = all(
                not q.get('required', True) or answers.get(start_idx + i)
                for i, q in enumerate(page_questions)
            )
            
            label = f"Page {page_num + 1}"
            if all_answered:
                label += " ✓"
            
            options.append(discord.SelectOption(
                label=label,
                value=str(page_num),
                description=f"Questions {start_idx + 1}-{end_idx}",
                default=(page_num == current_page)
            ))
        
        page_select = Select(
            placeholder=f"Select Page (Currently: Page {current_page + 1})",
            options=options,
            custom_id="page_selector"
        )
        page_select.callback = self.page_select_callback
        self.add_item(page_select)
        
        # Add edit button
        edit_button = Button(
            label="✏️ Edit Current Page",
            style=discord.ButtonStyle.primary,
            custom_id="edit_page"
        )
        edit_button.callback = self.edit_page
        self.add_item(edit_button)
        
        # Check if all required questions are answered
        all_required_answered = all(
            not q.get('required', True) or answers.get(idx)
            for idx, q in enumerate(questions)
        )
        
        # Add confirm button if all required questions are filled
        if all_required_answered:
            confirm_button = Button(
                label="✅ Preview Application",
                style=discord.ButtonStyle.success,
                custom_id="confirm_submit"
            )
            confirm_button.callback = self.preview_and_submit
            self.add_item(confirm_button)
    
    async def page_select_callback(self, interaction: discord.Interaction):
        # Get the select component to access its values
        select = [item for item in self.children if isinstance(item, Select)][0]
        selected_page = int(select.values[0])
        
        # Use target_user if set, otherwise use interaction user
        user_id = self.target_user.id if self.target_user else interaction.user.id
        
        # Save current state with the selected page
        save_pending_app(
            user_id,
            interaction.channel.id,
            self.application_name,
            self.answers,
            selected_page,
            self.questions
        )
        
        # Update the navigation view to show the new page
        self.current_page = selected_page
        updated_view = ApplicationNavigationView(
            self.application_name,
            self.questions,
            selected_page,
            self.answers,
            self.progress_message,
            target_user=self.target_user
        )
        updated_view.initial_interaction = self.initial_interaction
        updated_view.progress_message = self.progress_message or self.initial_interaction
        
        # Create progress message for the new page
        if self.target_user and self.target_user.id != interaction.user.id:
            progress_msg = f"**Filling application on behalf of {self.target_user.mention}**\n\n"
        else:
            progress_msg = ""
        progress_msg += updated_view._create_progress_message(selected_page)
        
        # Update the message to show the new page
        if self.initial_interaction:
            await self.initial_interaction.edit(content=progress_msg, embed=None, view=updated_view)
            await interaction.response.defer()
        else:
            await interaction.response.edit_message(content=progress_msg, embed=None, view=updated_view)

    async def edit_page(self, interaction: discord.Interaction):
        # Get panel data to check if we need to clear username
        panels = load_panels()
        panel_data = None
        for pid, pdata in panels.items():
            if interaction.channel.category_id == pdata.get('ticket_category_id'):
                panel_data = pdata
                break
        
        # Use target_user if set, otherwise use interaction user
        user_id = self.target_user.id if self.target_user else interaction.user.id
        
        # This handles the case where keys might be strings in JSON
        pending_apps = load_pending_apps()
        pending_key = f"{user_id}_{interaction.channel.id}"
        pending_app = pending_apps.get(pending_key)
        
        if pending_app:
            # Convert string keys to integers
            saved_answers = {int(k) if isinstance(k, str) else k: v for k, v in pending_app.get('answers', {}).items()}
            # Update self.answers with the saved state
            self.answers = saved_answers
        
        # Create a copy of answers for the modal
        modal_answers = self.answers.copy()
        
        # If full_forward is enabled, clear the username field if it exists on this page
        if panel_data:
            settings = panel_data.get('settings', {}).get(self.application_name, {})
            forward_full = settings.get('forward_full', False)
            
            if forward_full:
                # Check if username field is on the current page
                start_idx = self.current_page * 5
                end_idx = min(start_idx + 5, len(self.questions))
                
                for idx in range(start_idx, end_idx):
                    question = self.questions[idx]
                    if 'username' in question['label'].lower() or 'in game name' in question['label'].lower() or 'nickname' in question['label'].lower():
                        # Clear the username field in the modal answers
                        modal_answers[idx] = ''
                        break
        
        # Create and show modal with modified answers
        modal = ApplicationFormModal(
            self.application_name,
            self.questions,
            self.current_page,
            modal_answers,  # Use modified answers
            self.progress_message,
            target_user=self.target_user
        )
        modal.initial_interaction = self.initial_interaction or self.progress_message
        await interaction.response.send_modal(modal)
    
    def _create_progress_message(self, page):
        """Helper to create progress message"""
        total_answered = sum(1 for ans in self.answers.values() if ans)
        total_required = sum(1 for q in self.questions if q.get('required', True))
        
        progress_msg = f"**Application Progress: {total_answered}/{len(self.questions)} questions answered**\n"
        progress_msg += f"**Required: {sum(1 for idx, q in enumerate(self.questions) if q.get('required', True) and self.answers.get(idx))}/{total_required}**\n\n"
        
        # Show current page info
        start_idx = page * 5
        end_idx = min(start_idx + 5, len(self.questions))
        progress_msg += f"Currently viewing **Page {page + 1}** (Questions {start_idx + 1}-{end_idx})\n\n"
        
        # List questions on current page with status
        for i, q_idx in enumerate(range(start_idx, end_idx)):
            question = self.questions[q_idx]
            is_answered = bool(self.answers.get(q_idx))
            status = "✓" if is_answered else ("⚠️" if question.get('required', True) else "○")
            progress_msg += f"{status} **{question['label']}**"
            if question.get('required', True):
                progress_msg += " *(required)*"
            progress_msg += "\n"
        
        all_required_answered = all(
            not q.get('required', True) or self.answers.get(idx)
            for idx, q in enumerate(self.questions)
        )
        
        if all_required_answered:
            progress_msg += "\n✅ All required questions completed! Click 'Preview Application' when ready."
        else:
            remaining = sum(1 for idx, q in enumerate(self.questions) if q.get('required', True) and not self.answers.get(idx))
            progress_msg += f"\n⚠️ {remaining} required question(s) remaining."
        
        return progress_msg
    
    async def preview_and_submit(self, interaction: discord.Interaction):
        # Defer the interaction first
        if not interaction.response.is_done():
            await interaction.response.defer()
        
        # Use target_user if set, otherwise use interaction user
        applicant = self.target_user if self.target_user else interaction.user
        
        # Create preview embed
        embed = discord.Embed(
            title=f"📋 {self.application_name} Application Preview",
            description=f"**Applicant:** {applicant.mention}\n\nPlease review your answers before submitting:",
            color=0xFFA500,
            timestamp=datetime.utcnow()
        )
        
        # Add all answers as fields
        for idx, question in enumerate(self.questions):
            answer = self.answers.get(idx, "*No answer provided*")
            # Truncate long answers for preview
            display_answer = answer if len(answer) <= 100 else answer[:97] + "..."
            embed.add_field(
                name=question['label'],
                value=f"`{display_answer}`" if display_answer else "*No answer provided*",
                inline=False
            )
        
        embed.set_footer(text="Click 'Confirm Submit' to finalize your application or 'Back' to modify")
        
        # Create confirmation view with back button
        confirm_view = ConfirmSubmitView(
            self.application_name, 
            self.questions, 
            self.answers,
            self.current_page,
            self.progress_message or self.initial_interaction,
            target_user=self.target_user
        )
        confirm_view.initial_interaction = self.initial_interaction or self.progress_message
        
        # Edit using the original interaction
        try:
            if self.initial_interaction:
                await self.initial_interaction.edit(content=None, embed=embed, view=confirm_view)
            else:
                await interaction.followup.send(embed=embed, view=confirm_view, ephemeral=True)
                confirm_view.initial_interaction = interaction
        except:
            await interaction.followup.send(embed=embed, view=confirm_view, ephemeral=True)
            confirm_view.initial_interaction = interaction

class ConfirmSubmitView(View):
    def __init__(self, application_name, questions, answers, last_page=0, progress_message=None, target_user=None):
        super().__init__(timeout=None)
        self.application_name = application_name
        self.questions = questions
        self.answers = answers
        self.last_page = last_page
        self.progress_message = progress_message
        self.initial_interaction = None
        self.target_user = target_user  # The user the application is being filled for
        
        # Add Back button first
        back_button = Button(
            label="◀️ Back",
            style=discord.ButtonStyle.secondary,
            custom_id="back_to_form"
        )
        back_button.callback = self.back_button
        self.add_item(back_button)
        
        # Add Confirm Submit button
        confirm_button = Button(
            label="✅ Confirm Submit",
            style=discord.ButtonStyle.success,
            custom_id="confirm_submit"
        )
        confirm_button.callback = self.confirm_button
        self.add_item(confirm_button)
        
        # Add Cancel button
        cancel_button = Button(
            label="❌ Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id="cancel_submit"
        )
        cancel_button.callback = self.cancel_button
        self.add_item(cancel_button)
    
    async def back_button(self, interaction: discord.Interaction):
        """Go back to the navigation view"""
        # Create navigation view
        nav_view = ApplicationNavigationView(
            self.application_name,
            self.questions,
            self.last_page,
            self.answers,
            self.progress_message,
            target_user=self.target_user
        )   
        nav_view.initial_interaction = self.initial_interaction or self.progress_message
        nav_view.progress_message = self.initial_interaction or self.progress_message
        
        # Create progress message
        if self.target_user and self.target_user.id != interaction.user.id:
            progress_msg = f"**Filling application on behalf of {self.target_user.mention}**\n\n"
        else:
            progress_msg = ""
        progress_msg += nav_view._create_progress_message(self.last_page)
        
        # Edit to show navigation
        try:
            if self.initial_interaction:
                await self.initial_interaction.edit(content=progress_msg, embed=None, view=nav_view)
                if not interaction.response.is_done():
                    await interaction.response.defer()
            else:
                await interaction.response.edit_message(content=progress_msg, embed=None, view=nav_view)
        except:
            if not interaction.response.is_done():
                await interaction.response.edit_message(content=progress_msg, embed=None, view=nav_view)
    
    async def confirm_button(self, interaction: discord.Interaction):
        if interaction.response.is_done():
            return
        
        # Use target_user if set, otherwise use interaction user
        applicant = self.target_user if self.target_user else interaction.user
        
        # Username is valid or no username field - continue with submission
        remove_pending_app(applicant.id, interaction.channel.id)
        
        await interaction.response.defer()
        
        # Delete using the original interaction
        try:
            if self.initial_interaction:
                await self.initial_interaction.delete()
            else:
                pass
        except:
            pass
        
        # Get panel data to check for forwarding channel
        panels = load_panels()
        panel_id = None
        panel_data = None

        for pid, pdata in panels.items():
            if interaction.channel.category_id == pdata.get('ticket_category_id'):
                panel_id = pid
                panel_data = pdata
                break
        
        # Create final submission embed
        embed = discord.Embed(
            title=f"📋 {self.application_name} Application Submitted",
            description=f"**Submitted by:** {applicant.mention}\n**Time:** <t:{int(datetime.now().timestamp())}:F>",
            color=0x00FF00,
            timestamp=datetime.utcnow()
        )
        
        # Add all answers as fields
        for idx, question in enumerate(self.questions):
            answer = self.answers.get(idx, "*No answer provided*")
            embed.add_field(
                name=question['label'],
                value=f"`{answer}`" if answer else "*No answer provided*",
                inline=False
            )
        
        embed.set_thumbnail(url=applicant.display_avatar.url)
        embed.set_footer(text=f"Application by {applicant}", icon_url=applicant.display_avatar.url)
        
        # Update the "Fill Out Application" button
        view = ApplicationControlView()
        for item in view.children:
            if isinstance(item, Button) and item.custom_id == "fill_application":
                item.disabled = True
                item.label = "✅ Application Submitted"
                item.style = discord.ButtonStyle.success
        
        # Find and edit the original message with control buttons
        async for message in interaction.channel.history(limit=50):
            if message.author == interaction.guild.me and message.embeds:
                if "Application" in message.embeds[0].title and "Submitted" not in message.embeds[0].title:
                    try:
                        await message.edit(view=view)
                    except:
                        pass
                    break
        
        # Send the submission to the channel
        submitted_msg = await interaction.channel.send(embed=embed)
        
        # Send DMs to registered users about new application
        try:
            users = load_notification_users()
            print(f"Loaded {len(users)} notification users")
            if users:
                # Determine the channel to link to
                if panel_data and 'forwarding_channel_id' in panel_data:
                    forwarding_channel = interaction.guild.get_channel(panel_data['forwarding_channel_id'])
                    notification_channel_id = forwarding_channel.id if forwarding_channel else interaction.channel.id
                else:
                    notification_channel_id = interaction.channel.id
            if users:
                for user_id in users:
                    try:
                        user = await interaction.client.fetch_user(user_id)
                        
                        dm_embed = discord.Embed(
                            title="📝 New Application Submitted",
                            description=f"**{applicant.name}** has submitted a **{self.application_name}** application",
                            color=0x0099ff,
                            timestamp=datetime.utcnow()
                        )
                        dm_embed.add_field(name="Channel", value=interaction.channel.mention, inline=True)
                        dm_embed.add_field(name="Applicant", value=applicant.mention, inline=True)
                        dm_embed.set_footer(text=f"Application submitted • {datetime.utcnow().strftime('%d/%m/%Y, %H:%M')}")
                        
                        view = discord.ui.View()
                        view.add_item(discord.ui.Button(
                            label="Jump to Application", 
                            url=f"https://discord.com/channels/{interaction.guild.id}/{notification_channel_id}", 
                            emoji="🔗"
                        ))
                        
                        await user.send(embed=dm_embed, view=view)
                        print(f"Sent notification DM to user {user_id}")
                    except Exception as e:
                        print(f"Failed to send notification DM to user {user_id}: {e}")
        except Exception as e:
            print(f"Error sending notification DMs: {e}")
        
        # Forward to forwarding channel if configured
        if panel_data and 'forwarding_channel_id' in panel_data:
            forwarding_channel = interaction.guild.get_channel(panel_data['forwarding_channel_id'])
            if forwarding_channel:
                # Check if full forwarding is enabled for this specific application
                settings = panel_data.get('settings', {}).get(self.application_name, {})
                forward_full = settings.get('forward_full', False)
                print(f'Forwarding application to {forwarding_channel.mention} (full: {forward_full})')
                
                if forward_full:
                    # Import recruitment functions
                    try:
                        import sys
                        import os
                        from pathlib import Path
                        
                        # Add recruitment module to path
                        recruitment_path = Path(__file__).parent
                        if str(recruitment_path) not in sys.path:
                            sys.path.insert(0, str(recruitment_path))
                        
                        from recruitment import (
                            RecruitmentAPI,
                            PlaytimeGraphGenerator,
                            generate_player_card,
                        )
                        import sqlite3
                        import glob
                        from collections import defaultdict
                        
                        # Try to extract username from application
                        username = None
                        for idx, question in enumerate(self.questions):
                            if 'username' in question['label'].lower() or 'in game name' in question['label'].lower() or 'nickname' in question['label'].lower():
                                username = self.answers.get(idx, '').strip()
                                if username:
                                    break
                        
                        if username:
                            # Fetch player data
                            player_data = await RecruitmentAPI.fetch_player_data(username)
                            
                            if player_data and not player_data.get('error'):
                                # Handle multiple players
                                if player_data.get('multiple'):
                                    players_data = player_data.get('objects', {})
                                    if players_data:
                                        selected_uuid = await RecruitmentAPI.select_highest_playtime_player(players_data)
                                        if selected_uuid:
                                            player_data = await RecruitmentAPI.fetch_player_data(selected_uuid)
                                
                                if player_data and not player_data.get('multiple'):
                                    # Get player UUID and fetch skin
                                    selected_uuid = player_data.get('uuid')
                                    skin_bytes = await RecruitmentAPI.fetch_player_skin(selected_uuid)
                                    
                                    # Generate player card
                                    player_card_bytes = await generate_player_card(player_data, skin_bytes)
                                    
                                    # Send player card and create thread
                                    player_card_file = discord.File(player_card_bytes, filename=f"player_{player_data.get('username', 'player')}.png")
                                    main_message = await forwarding_channel.send(
                                        content=f"New {self.application_name} application submitted!",
                                        file=player_card_file
                                    )
                                    
                                    # Create thread
                                    safe_username = player_data.get('username', username).replace('_', ' ')
                                    thread = await main_message.create_thread(
                                        name=f"{self.application_name} - {safe_username}",
                                        auto_archive_duration=1440
                                    )
                                    
                                    # Update main message with thread link
                                    await main_message.edit(content=f"New {self.application_name} application for {applicant.mention}! Go to {thread.mention} to review.")
                                    
                                    # Calculate and send suspiciousness with sus card
                                    sus_data = calculate_suspiciousness(player_data)
                                    if sus_data:
                                        # Generate sus card image
                                        selected_uuid = player_data.get('uuid')
                                        skin_bytes = await WynncraftAPI.fetch_player_skin(selected_uuid)
                                        
                                        try:
                                            sus_card_bytes = await SusCardImageGenerator.generate(sus_data, skin_bytes)
                                            sus_card_file = discord.File(sus_card_bytes, filename=f"suscard_{sus_data['username']}.png")
                                            await thread.send(file=sus_card_file)
                                        except Exception as e:
                                            print(f"Error generating sus card: {e}")
                                    
                                    # Fetch playtime graph using the same logic as /playtime command
                                    try:
                                        import importlib.util as _ilu
                                        import statistics as _stats
                                        _pt_path = _ROOT / 'commands' / 'tracking' / 'playtime.py'
                                        _pt_spec = _ilu.spec_from_file_location('playtime', _pt_path)
                                        _pt_mod = _ilu.module_from_spec(_pt_spec)
                                        _pt_spec.loader.exec_module(_pt_mod)
                                        get_daily_playtime_data = _pt_mod.get_daily_playtime_data
                                        create_playtime_graph = _pt_mod.create_playtime_graph
                                        
                                        player_username = player_data.get('username', username)
                                        daily_data, user_found = get_daily_playtime_data(player_username, 7)
                                        
                                        if user_found and daily_data:
                                            total_pt = sum(d['playtime_seconds'] for d in daily_data)
                                            avg_hours = (total_pt / len(daily_data)) / 3600 if daily_data else 0
                                            median_hours = _stats.median([d['playtime_seconds'] for d in daily_data]) / 3600
                                            
                                            graph_buf = create_playtime_graph(player_username, daily_data, 7, avg_hours, median_hours)
                                            await thread.send(file=discord.File(graph_buf, filename='playtime.png'))
                                    except Exception as e:
                                        print(f"Error fetching playtime: {e}")
                                    
                                    # Send application answers in thread
                                    app_embed = discord.Embed(
                                        title=f"📋 Application Answers",
                                        description=f"**Submitted by:** {applicant.mention}\n**Channel:** {interaction.channel.mention}",
                                        color=0x5865F2,
                                        timestamp=datetime.utcnow()
                                    )
                                    
                                    # Check for blacklist status
                                    blacklisted, blacklist_reason = is_blacklisted(username)
                                    if blacklisted:
                                        blacklist_warning = "🚫 **BLACKLISTED USER DETECTED**\n"
                                        blacklist_warning += f"**Reason:** {blacklist_reason if blacklist_reason else 'No reason provided'}\n"
                                        blacklist_warning += f"**NameMC Profile:** [View Profile](https://namemc.com/search?q={username})"
                                        app_embed.add_field(name="⚠️ Blacklist Status", value=blacklist_warning, inline=False)
                                        # Change embed color to red if blacklisted
                                        app_embed.color = 0xFF0000
                                    
                                    for idx, question in enumerate(self.questions):
                                        answer = self.answers.get(idx, "*No answer provided*")
                                        display_answer = answer if len(answer) <= 1024 else answer[:1021] + "..."
                                        app_embed.add_field(
                                            name=question['label'],
                                            value=f"`{display_answer}`" if display_answer else "*No answer provided*",
                                            inline=False
                                        )
                                    
                                    app_embed.set_footer(text=f"Application by {applicant}", icon_url=applicant.display_avatar.url)
                                    app_msg = await thread.send(embed=app_embed)
                                    
                                    # Save forwarded application
                                    save_forwarded_app(
                                        thread.id,
                                        app_msg.id,
                                        applicant.id,
                                        self.application_name,
                                        main_message.id,
                                        ticket_channel_id=interaction.channel.id,
                                        threshold=calculate_threshold(interaction.guild)
                                    )
                                    
                                    # Add vote buttons
                                    vote_view = ApplicationVoteView(
                                        {'message_id': app_msg.id, 'user_id': applicant.id, 'app_type': self.application_name},
                                        approve_count=0,
                                        deny_count=0,
                                        threshold=calculate_threshold(interaction.guild)
                                    )
                                    await app_msg.edit(view=vote_view)
                                else:
                                    # Fallback to regular forwarding if player data failed
                                    raise Exception("Failed to get single player data")
                            else:
                                # Fallback to regular forwarding if player not found
                                raise Exception("Player not found or API error")
                        else:
                            # Fallback to regular forwarding if no username found
                            raise Exception("No username field found in application")
                    
                    except Exception as e:
                        print(f"Error with full forwarding: {e}")
                        import traceback
                        traceback.print_exc()
                        # Fallback to regular forwarding
                        forward_full = False
                
                if not forward_full:
                    # Original forwarding logic (existing code)
                    forward_embed = discord.Embed(
                        title=f"📋 New {self.application_name} Application",
                        description=f"**Submitted by:** {applicant.mention}\n**Channel:** {interaction.channel.mention}\n**Time:** <t:{int(datetime.now().timestamp())}:F>",
                        color=0x5865F2,
                        timestamp=datetime.utcnow()
                    )
                    forward_embed.set_thumbnail(url=applicant.display_avatar.url)
                    
                    # Check for username and blacklist status
                    username = None
                    for idx, question in enumerate(self.questions):
                        if 'username' in question['label'].lower() or 'in game name' in question['label'].lower() or 'nickname' in question['label'].lower():
                            username = self.answers.get(idx, '').strip()
                            if username:
                                break
                    
                    # Add blacklist warning if username is blacklisted
                    if username:
                        blacklisted, blacklist_reason = is_blacklisted(username)
                        if blacklisted:
                            blacklist_warning = "🚫 **BLACKLISTED USER DETECTED**\n"
                            blacklist_warning += f"**Reason:** {blacklist_reason if blacklist_reason else 'No reason provided'}\n"
                            blacklist_warning += f"**NameMC Profile:** [View Profile](https://namemc.com/search?q={username})"
                            forward_embed.add_field(name="⚠️ Blacklist Status", value=blacklist_warning, inline=False)
                            # Change embed color to red if blacklisted
                            forward_embed.color = 0xFF0000
                    
                    for idx, question in enumerate(self.questions):
                        answer = self.answers.get(idx, "*No answer provided*")
                        display_answer = answer if len(answer) <= 1024 else answer[:1021] + "..."
                        forward_embed.add_field(
                            name=question['label'],
                            value=f"`{display_answer}`" if display_answer else "*No answer provided*",
                            inline=False
                        )
                    
                    forward_embed.set_footer(text=f"Application by {applicant}", icon_url=applicant.display_avatar.url)
                    
                    try:
                        forward_msg = await forwarding_channel.send(embed=forward_embed)
                        
                        # Save forwarded application
                        save_forwarded_app(
                            forwarding_channel.id,
                            forward_msg.id,
                            applicant.id,
                            self.application_name,
                            ticket_channel_id=interaction.channel.id,
                            threshold=calculate_threshold(interaction.guild)
                        )
                        
                        # Add vote buttons
                        vote_view = ApplicationVoteView(
                            {'message_id': forward_msg.id, 'user_id': applicant.id, 'app_type': self.application_name},
                            approve_count=0,
                            deny_count=0,
                            threshold=calculate_threshold(interaction.guild)
                        )
                        await forward_msg.edit(view=vote_view)
                                
                    except Exception as e:
                        print(f"Error forwarding application: {e}")
        
        # Notify the applicant AFTER forwarding is complete
        await submitted_msg.reply(
            "Your application has been sent to our Jurors and is now being reviewed! You can expect to hear back from us within a day."
        )
    
    async def cancel_button(self, interaction: discord.Interaction):
        # Edit using the original interaction
        try:
            if self.initial_interaction:
                await self.initial_interaction.edit(
                    content="❌ **Application submission cancelled.**",
                    embed=None,
                    view=None
                )
                if not interaction.response.is_done():
                    await interaction.response.defer()
            else:
                await interaction.response.send_message("Application submission cancelled.", ephemeral=True)
        except:
            if not interaction.response.is_done():
                await interaction.response.send_message("Application submission cancelled.", ephemeral=True)

class ApplicationFormModal(Modal):
    def __init__(self, application_name, questions, page=0, answers=None, progress_message=None, target_user=None):
        super().__init__(title=f"{application_name} - Page {page + 1}")
        self.application_name = application_name
        self.all_questions = questions
        self.page = page
        self.answers = answers if answers else {}
        self.progress_message = progress_message
        self.initial_message = None
        self.initial_interaction = None
        self.target_user = target_user  # The user the application is being filled for
        
        # Calculate which questions to show (5 per page)
        start_idx = page * 5
        end_idx = min(start_idx + 5, len(questions))
        current_questions = questions[start_idx:end_idx]
        
        # Add text inputs for current page
        for i, question in enumerate(current_questions):
            question_idx = start_idx + i
            
            # Truncate label to 45 characters (Discord's limit)
            label = question['label']
            if len(label) > 45:
                label = label[:42] + "..."
            
            # Get the saved answer for this question
            saved_answer = self.answers.get(question_idx, '')
            
            text_input = TextInput(
                label=label,
                placeholder=question.get('placeholder', ''),
                style=discord.TextStyle.paragraph if question.get('style') == 'paragraph' else discord.TextStyle.short,
                required=question.get('required', True),
                max_length=question.get('max_length', 1000),
                default=saved_answer
            )
            self.add_item(text_input)
    
    async def on_submit(self, interaction: discord.Interaction):
        # Defer immediately to avoid 3-second timeout
        await interaction.response.defer(ephemeral=True)
        
        # Use target_user if set, otherwise use interaction user
        user_id = self.target_user.id if self.target_user else interaction.user.id
        
        # Save answers from current page
        start_idx = self.page * 5
        for i, item in enumerate(self.children):
            if isinstance(item, TextInput):
                question_idx = start_idx + i
                self.answers[question_idx] = item.value
        
        save_pending_app(
            user_id,
            interaction.channel.id,
            self.application_name,
            self.answers,
            self.page,
            self.all_questions
        )
        
        username = None
        username_question_idx = None
        for idx, question in enumerate(self.all_questions):
            if 'username' in question['label'].lower() or 'in game name' in question['label'].lower() or 'nickname' in question['label'].lower():
                username = self.answers.get(idx, '').strip()
                username_question_idx = idx
                if username:
                    break
        
        # Check if this application has full forwarding enabled
        panels = load_panels()
        panel_id = None
        panel_data = None
        for pid, pdata in panels.items():
            if interaction.channel.category_id == pdata.get('ticket_category_id'):
                panel_id = pid
                panel_data = pdata
                break
        
        # Only verify username if full_forward is enabled
        if panel_data:
            settings = panel_data.get('settings', {}).get(self.application_name, {})
            forward_full = settings.get('forward_full', False)
            
            # If username was just filled in on this page, verify it
            if forward_full and username and username_question_idx is not None:
                # Check if this username field was on the current page
                start_idx_page = self.page * 5
                end_idx_page = min(start_idx_page + 5, len(self.all_questions))
                
                if start_idx_page <= username_question_idx < end_idx_page:
                    # Username was just entered on this page
                    headers = {}
                    if WYNNCRAFT_VERIFICATION_KEY:
                        headers['Authorization'] = f'Bearer {WYNNCRAFT_VERIFICATION_KEY}'
                    
                    async with aiohttp.ClientSession() as session:
                        async with session.get(f"https://api.wynncraft.com/v3/player/{username}", headers=headers) as response:
                            if response.status == 300:
                                # Multiple usernames found - pick the most recent one
                                data = await response.json()
                                # The API returns: {"error": "...", "objects": {"uuid1": {...}, "uuid2": {...}}}
                                players_dict = data.get('objects', {})
                                
                                print(f"[INFO] Multiple usernames found for {username}: {data}")
                                
                                if not players_dict:
                                    # No valid players found
                                    del self.answers[username_question_idx]
                                    save_pending_app(
                                        user_id,
                                        interaction.channel.id,
                                        self.application_name,
                                        self.answers,
                                        self.page,
                                        self.all_questions
                                    )
                                    
                                    error_embed = discord.Embed(
                                        title="❌ Invalid Username",
                                        description=f"The username `{username}` was not found on Wynncraft.\n\nPlease correct your username and try again. If you think this is a mistake, please contact support using `/contact_support`.",
                                        color=0xFF0000,
                                        timestamp=datetime.utcnow()
                                    )
                                    
                                    await interaction.followup.send(embed=error_embed, ephemeral=True)
                                    
                                    # THEN show navigation view with error message
                                    nav_view = ApplicationNavigationView(
                                        self.application_name,
                                        self.all_questions,
                                        self.page,
                                        self.answers,
                                        None,
                                        target_user=self.target_user
                                    )
                                    
                                    progress_msg = nav_view._create_progress_message(self.page)
                                    
                                    if self.initial_interaction:
                                        await self.initial_interaction.edit(content=progress_msg, embed=None, view=nav_view)
                                        nav_view.initial_interaction = self.initial_interaction
                                        nav_view.progress_message = self.initial_interaction
                                    else:
                                        nav_view.initial_interaction = interaction
                                        nav_view.progress_message = interaction
                                    
                                    return
                                
                                # Fetch full data for each UUID to get lastJoin
                                most_recent_player = None
                                most_recent_last_join = None
                                
                                for player_uuid in players_dict.keys():
                                    try:
                                        async with session.get(f"https://api.wynncraft.com/v3/player/{player_uuid}", headers=headers) as player_response:
                                            if player_response.status == 200:
                                                player_data = await player_response.json()
                                                last_join = player_data.get('lastJoin')
                                                
                                                if last_join and (most_recent_last_join is None or last_join > most_recent_last_join):
                                                    most_recent_last_join = last_join
                                                    most_recent_player = player_data
                                    except Exception as e:
                                        print(f"[WARN] Error fetching player data for UUID {player_uuid}: {e}")
                                        continue
                                
                                if most_recent_player:
                                    print(f"[INFO] Ticket: Selected player {most_recent_player.get('username')} with UUID {most_recent_player.get('uuid')} (most recent lastJoin: {most_recent_last_join})")
                                    # Username is valid - continue normally
                                else:
                                    # Could not determine the correct player
                                    del self.answers[username_question_idx]
                                    save_pending_app(
                                        user_id,
                                        interaction.channel.id,
                                        self.application_name,
                                        self.answers,
                                        self.page,
                                        self.all_questions
                                    )
                                    
                                    error_embed = discord.Embed(
                                        title="❌ Invalid Username",
                                        description=f"Could not determine the correct player for username `{username}`.\n\nPlease correct your username and try again. If you think this is a mistake, please contact support using `/contact_support`.",
                                        color=0xFF0000,
                                        timestamp=datetime.utcnow()
                                    )
                                    
                                    await interaction.followup.send(embed=error_embed, ephemeral=True)
                                    
                                    nav_view = ApplicationNavigationView(
                                        self.application_name,
                                        self.all_questions,
                                        self.page,
                                        self.answers,
                                        None,
                                        target_user=self.target_user
                                    )
                                    
                                    progress_msg = nav_view._create_progress_message(self.page)
                                    
                                    if self.initial_interaction:
                                        await self.initial_interaction.edit(content=progress_msg, embed=None, view=nav_view)
                                        nav_view.initial_interaction = self.initial_interaction
                                        nav_view.progress_message = self.initial_interaction
                                    else:
                                        nav_view.initial_interaction = interaction
                                        nav_view.progress_message = interaction
                                    
                                    return
                            elif response.status != 200:
                                del self.answers[username_question_idx]
                                save_pending_app(
                                    user_id,
                                    interaction.channel.id,
                                    self.application_name,
                                    self.answers,
                                    self.page,
                                    self.all_questions
                                )
                                
                                # Username invalid - send error message FIRST
                                error_embed = discord.Embed(
                                    title="❌ Invalid Username",
                                    description=f"The username `{username}` was not found on Wynncraft.\n\nPlease correct your username and try again. If you think this is a mistake, please contact support using `/contact_support`.",
                                    color=0xFF0000,
                                    timestamp=datetime.utcnow()
                                )
                                
                                await interaction.followup.send(embed=error_embed, ephemeral=True)
                                
                                # THEN show navigation view with error message
                                nav_view = ApplicationNavigationView(
                                    self.application_name,
                                    self.all_questions,
                                    self.page,
                                    self.answers,
                                    None,
                                    target_user=self.target_user
                                )
                                
                                # Create progress message with error
                                progress_msg = nav_view._create_progress_message(self.page)
                                
                                # Edit existing message with navigation (use followup since we already responded)
                                if self.initial_interaction:
                                    await self.initial_interaction.edit(content=progress_msg, embed=None, view=nav_view)
                                    nav_view.initial_interaction = self.initial_interaction
                                    nav_view.progress_message = self.initial_interaction
                                else:
                                    # If no initial interaction, the error message becomes the initial interaction
                                    nav_view.initial_interaction = interaction
                                    nav_view.progress_message = interaction
                                
                                return
        
        # Create navigation view
        nav_view = ApplicationNavigationView(
            self.application_name,
            self.all_questions,
            self.page,
            self.answers,
            None,
            target_user=self.target_user
        )

        # Create progress message using the helper method
        progress_msg = nav_view._create_progress_message(self.page)

        # Try to edit existing message, fallback to new message if webhook expired
        if self.initial_interaction:
            try:
                await self.initial_interaction.edit(content=progress_msg, embed=None, view=nav_view)
                nav_view.initial_interaction = self.initial_interaction
                nav_view.progress_message = self.initial_interaction
            except (discord.errors.HTTPException, discord.errors.NotFound):
                # Webhook expired or message deleted, create new message via followup
                followup = await interaction.followup.send(progress_msg, view=nav_view, ephemeral=True, wait=True)
                nav_view.initial_interaction = followup
                nav_view.progress_message = followup
        else:
            # No initial interaction exists, send via followup
            followup = await interaction.followup.send(progress_msg, view=nav_view, ephemeral=True, wait=True)
            nav_view.initial_interaction = followup
            nav_view.progress_message = followup
        
class SelectTargetUserView(View):
    """View for owner to select which user to fill application for"""
    def __init__(self, application_name, questions, panel_data):
        super().__init__(timeout=300)
        self.application_name = application_name
        self.questions = questions
        self.panel_data = panel_data
        
        # User select dropdown
        user_select = discord.ui.UserSelect(
            placeholder="Select a user to fill the application for...",
            min_values=1,
            max_values=1
        )
        user_select.callback = self.user_selected
        self.add_item(user_select)
        
        # Cancel button
        cancel_button = Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary
        )
        cancel_button.callback = self.cancel
        self.add_item(cancel_button)
    
    async def user_selected(self, interaction: discord.Interaction):
        # Get the selected user from the select menu
        select = [item for item in self.children if isinstance(item, discord.ui.UserSelect)][0]
        target_user = select.values[0]
        
        # Start the application flow for the target user
        await start_application_flow(interaction, self.application_name, self.questions, self.panel_data, target_user)
    
    async def cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="❌ Cancelled.", view=None, embed=None)


class OwnerApplicationChoiceView(View):
    """View for owner to choose to fill application for themselves or someone else"""
    def __init__(self, application_name, questions, panel_data):
        super().__init__(timeout=300)
        self.application_name = application_name
        self.questions = questions
        self.panel_data = panel_data
        
        # Fill for myself button
        myself_button = Button(
            label="📝 Fill for myself",
            style=discord.ButtonStyle.primary
        )
        myself_button.callback = self.fill_for_myself
        self.add_item(myself_button)
        
        # Fill for someone else button
        other_button = Button(
            label="👤 Fill for someone else",
            style=discord.ButtonStyle.secondary
        )
        other_button.callback = self.fill_for_other
        self.add_item(other_button)
    
    async def fill_for_myself(self, interaction: discord.Interaction):
        # Start application flow normally (target_user = interaction.user)
        await start_application_flow(interaction, self.application_name, self.questions, self.panel_data, interaction.user)
    
    async def fill_for_other(self, interaction: discord.Interaction):
        # Show user select view
        embed = discord.Embed(
            title="Select Target User",
            description="Choose which user you want to fill the application for:",
            color=0x5865F2
        )
        view = SelectTargetUserView(self.application_name, self.questions, self.panel_data)
        await interaction.response.edit_message(embed=embed, view=view)


async def start_application_flow(interaction: discord.Interaction, application_name: str, questions: list, panel_data: dict, target_user: discord.Member):
    """Start the application flow for a target user"""
    # Check for pending application for the target user
    pending_apps = load_pending_apps()
    pending_key = f"{target_user.id}_{interaction.channel.id}"
    pending_app = pending_apps.get(pending_key)
    
    if pending_app:
        # Convert string keys to integers if needed
        answers = {int(k) if isinstance(k, str) else k: v for k, v in pending_app['answers'].items()}
        current_page = pending_app['current_page']
    else:
        # Start new application
        answers = {}
        current_page = 0
    
    # Save as pending for the target user
    save_pending_app(
        target_user.id,
        interaction.channel.id,
        application_name,
        answers,
        current_page,
        questions
    )
    
    # Create modal for the current page
    modal = ApplicationFormModal(
        application_name,
        questions,
        page=current_page,
        answers=answers,
        target_user=target_user
    )
    
    # If this is from the owner choice view, we need to respond differently
    if interaction.response.is_done():
        # Send modal via followup isn't possible, so we edit to show a "Continue" button
        nav_view = ApplicationNavigationView(
            application_name,
            questions,
            current_page,
            answers,
            None,
            target_user=target_user
        )
        
        if target_user.id != interaction.user.id:
            progress_msg = f"**Filling application on behalf of {target_user.mention}**\n\n"
        else:
            progress_msg = ""
        progress_msg += nav_view._create_progress_message(current_page)
        
        await interaction.edit_original_response(content=progress_msg, embed=None, view=nav_view)
        nav_view.initial_interaction = interaction
        nav_view.progress_message = interaction
    else:
        # Show the modal first
        await interaction.response.send_modal(modal)
        
        # Send initial navigation message as followup
        nav_view = ApplicationNavigationView(
            application_name,
            questions,
            current_page,
            answers,
            None,
            target_user=target_user
        )
        
        if target_user.id != interaction.user.id:
            progress_msg = f"**Filling application on behalf of {target_user.mention}**\n\n"
        else:
            progress_msg = ""
        progress_msg += nav_view._create_progress_message(current_page)
        
        followup = await interaction.followup.send(progress_msg, view=nav_view, ephemeral=True, wait=True)
        
        # Store reference for editing later
        modal.initial_interaction = followup
        nav_view.initial_interaction = followup
        nav_view.progress_message = followup


class ApplicationControlView(View):
    def __init__(self, opener_id=None):
        super().__init__(timeout=None)
        self.opener_id = opener_id
        
        # Fill out application button
        fill_button = Button(
            label="📝 Fill Out Application",
            style=discord.ButtonStyle.primary,
            custom_id="fill_application"
        )
        fill_button.callback = self.fill_application
        self.add_item(fill_button)
        
        # Close button
        close_button = Button(
            label="🔒 Close",
            style=discord.ButtonStyle.danger,
            custom_id="close_application"
        )
        close_button.callback = self.close_application
        self.add_item(close_button)
        
        # Close with reason button
        close_reason_button = Button(
            label="🔒 Close with Reason",
            style=discord.ButtonStyle.danger,
            custom_id="close_application_reason"
        )
        close_reason_button.callback = self.close_with_reason
        self.add_item(close_reason_button)
    
    async def fill_application(self, interaction: discord.Interaction):
        # Check if the user is the bot owner
        owner_id = os.getenv('OWNER_ID')
        is_owner = owner_id and interaction.user.id == int(owner_id)
        
        # Check if the user is the ticket opener (unless they're the owner)
        print(f"Opener ID: {self.opener_id}, User ID: {interaction.user.id}, Is Owner: {is_owner}")
        if not is_owner and self.opener_id and interaction.user.id != self.opener_id:
            await interaction.response.send_message(
                "❌ Only the ticket opener can fill out the application!",
                ephemeral=True
            )
            return

        # Check if application was already submitted in this channel
        submitted = False
        async for message in interaction.channel.history(limit=100):
            if message.author == interaction.guild.me and message.embeds:
                for embed in message.embeds:
                    if "Application Submitted" in embed.title:
                        submitted = True
                        break
            if submitted:
                break
        
        if submitted:
            await interaction.response.send_message(
                "❌ An application has already been submitted in this channel!",
                ephemeral=True
            )
            return
        
        # Get panel data for this channel
        panels = load_panels()
        panel_id, panel_data = get_panel_data_from_channel(interaction.channel)
        
        if not panel_data:
            await interaction.response.send_message(
                "❌ Could not find panel data for this channel!",
                ephemeral=True
            )
            return
        
        # Determine application type from channel
        application_name = None
        channel_name = interaction.channel.name.lower()

        for app in panel_data.get('applications', []):
            app_name = app['name']

            settings_entry = panel_data.get('settings', {}).get(app_name, {})
            template = settings_entry.get('channel_name', '').lower()

            if not template:
                return

            regex_pattern = (
                "^" + re.escape(template).replace(r"%user%", r".+") + "$"
            )

            if re.match(regex_pattern, channel_name):
                application_name = app_name
                break
        
        if not application_name and panel_data.get('applications'):
            application_name = panel_data['applications'][0]['name']
        
        # Get questions for this application
        questions = panel_data.get('questions', {}).get(application_name, [])
        
        if not questions:
            await interaction.response.send_message(
                "❌ No questions have been configured for this application type!",
                ephemeral=True
            )
            return
        
        # If the user is the owner, show them a choice
        if is_owner:
            embed = discord.Embed(
                title="Fill Application",
                description="Would you like to fill this application for yourself or on behalf of someone else?",
                color=0x5865F2
            )
            view = OwnerApplicationChoiceView(application_name, questions, panel_data)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return
        
        # Normal flow for non-owners - start application for themselves
        await start_application_flow(interaction, application_name, questions, panel_data, interaction.user)
        
    async def close_application(self, interaction: discord.Interaction):
        # Get panel data for this channel
        panel_id, panel_data = get_panel_data_from_channel(interaction.channel)
        
        if not panel_data:
            await interaction.response.send_message(
                "❌ Could not find panel data for this channel!",
                ephemeral=True
            )
            return
        
        # Determine application type from channel
        application_name = None
        for app in panel_data.get('applications', []):
            # Try to match application name in channel name or topic
            if app['name'].lower().replace(' ', '-') in interaction.channel.name.lower():
                application_name = app['name']
                break
        
        if not application_name and panel_data.get('applications'):
            # Fallback to first application if can't determine
            application_name = panel_data['applications'][0]['name']
            
        # Check permissions to close this application
        if not check_close_permissions(interaction.user, panel_data, application_name):
            await interaction.response.send_message(
                "❌ You do not have permission to close this application!",
                ephemeral=True
            )
            return
        
        settings = panel_data.get('settings', {}).get(application_name, {})
        confirm_close = settings.get('confirm_close', True)
        
        if confirm_close:
            # Show confirmation
            confirm_embed = discord.Embed(
                title="Confirm Close",
                description="Are you sure you want to close this application?",
                color=0xFFA500
            )
            view = ConfirmCloseView(interaction, application_name, panel_data, panel_id)
            await interaction.response.send_message(embed=confirm_embed, view=view, ephemeral=True)
        else:
            # Close immediately
            view = ConfirmCloseView(interaction, application_name, panel_data, panel_id)
            await view.close_application_logic(interaction)
    
    async def close_with_reason(self, interaction: discord.Interaction):
        # Get panel data for this channel
        panel_id, panel_data = get_panel_data_from_channel(interaction.channel)
        
        if not panel_data:
            await interaction.response.send_message(
                "❌ Could not find panel data for this channel!",
                ephemeral=True
            )
            return
        
        # Determine application type from channel
        application_name = None
        for app in panel_data.get('applications', []):
            if app['name'].lower().replace(' ', '-') in interaction.channel.name.lower():
                application_name = app['name']
                break
        
        if not application_name and panel_data.get('applications'):
            application_name = panel_data['applications'][0]['name']
        
        # Check permissions to close this application
        if not check_close_permissions(interaction.user, panel_data, application_name):
            await interaction.response.send_message(
                "❌ You do not have permission to close this application!",
                ephemeral=True
            )
            return
        
        # Show modal for reason
        modal = CloseReasonModal(application_name, panel_data, panel_id)
        await interaction.response.send_modal(modal)

async def create_application_channel(interaction: discord.Interaction, application_name: str, panel_data: dict):
    """Create a new application channel with proper permissions"""
    
    try:
        # Get the category
        category = interaction.guild.get_channel(panel_data['ticket_category_id'])
        if not category:
            await interaction.response.send_message("❌ Ticket category not found!", ephemeral=True)
            return
        
        # Get settings for this application
        settings = panel_data.get('settings', {}).get(application_name, {})
        channel_name_template = settings.get('channel_name', 'application-%user%')
        
        # Generate a temporary ID (we'll use timestamp for now)
        temp_id = int(datetime.now().timestamp())
        
        # Format channel name
        channel_name = format_channel_name(channel_name_template, interaction.user, temp_id)
        
        # Create permission overwrites
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                create_public_threads=True,
                embed_links=True,
                attach_files=True,
                read_message_history=True,
                add_reactions=True,
                use_application_commands=True
            ),
            interaction.guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True
            )
        }
        
        # Add permissions for configured roles
        permissions = panel_data.get('permissions', {}).get(application_name, {})
        for role_id in permissions.get('roles', []):
            role = interaction.guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    create_public_threads=True,
                    embed_links=True,
                    attach_files=True,
                    read_message_history=True,
                    add_reactions=True,
                    use_application_commands=True
                )
        
        # Add permissions for configured users
        for user_id in permissions.get('users', []):
            user = interaction.guild.get_member(user_id)
            if user:
                overwrites[user] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    create_public_threads=True,
                    embed_links=True,
                    attach_files=True,
                    read_message_history=True,
                    add_reactions=True,
                    use_application_commands=True
                )
        
        # Create the channel
        channel = await category.create_text_channel(
            name=channel_name,
            overwrites=overwrites,
            reason=f"{application_name} application created by {interaction.user}"
        )
        
        # Create the embed
        embed = discord.Embed(
            title=f"🎫 {application_name} Application",
            description=f"{interaction.user.mention} has created a new {application_name} application.\n\n"
                       f"**Created:** <t:{int(datetime.now().timestamp())}:F>\n"
                       f"**Channel:** {channel.mention}\n\n"
                       f"**If you need any assistance, please contact a staff member or use `/contact_support` to get in touch with the bot owner.**",
            color=0xA300FF,
            timestamp=datetime.utcnow()
        )
        embed.set_footer(text=f"Application by {interaction.user}", icon_url=interaction.user.display_avatar.url)
        
        # Send the embed in the new channel
        application_embed = await channel.send(embed=embed, view=ApplicationControlView(opener_id=interaction.user.id))
        await application_embed.reply('☝️ To begin, you can press the "Fill Out Application" button above.')
        
        # Save the channel opener
        channel_openers = load_channel_openers()
        channel_openers[str(channel.id)] = interaction.user.id
        save_channel_openers(channel_openers)
        
        # Respond to the user
        await interaction.response.send_message(
            f"✅ Your {application_name} application has been created: {channel.mention}",
            ephemeral=True
        )
        
        # Log to logging channel if configured
        if 'logging_channel_id' in panel_data:
            # Check if logging is enabled for this application
            log_enabled = settings.get('log_creation', True)  # Default to True if not set
            
            if log_enabled:
                log_channel = interaction.guild.get_channel(panel_data['logging_channel_id'])
                if log_channel:
                    try:
                        log_embed = discord.Embed(
                            title="📋 New Application Created",
                            description=f"**Type:** {application_name}\n"
                                    f"**User:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                                    f"**Channel:** {channel.mention}\n"
                                    f"**Created:** <t:{int(datetime.now().timestamp())}:F>",
                            color=0x00FF00,
                            timestamp=datetime.utcnow()
                        )
                        await log_channel.send(embed=log_embed)
                    except discord.Forbidden:
                        print(f"Missing permissions to send log to channel {log_channel.id}")
                    except Exception as e:
                        print(f"Error sending log message: {e}")
        
    except discord.Forbidden:
        # Check if already responded
        if interaction.response.is_done():
            await interaction.followup.send(
                "❌ I don't have permission to create channels in that category!",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "❌ I don't have permission to create channels in that category!",
                ephemeral=True
            )
    except Exception as e:
        # Check if already responded
        if interaction.response.is_done():
            await interaction.followup.send(
                f"❌ An error occurred while creating the application: {str(e)}",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"❌ An error occurred while creating the application: {str(e)}",
                ephemeral=True
            )

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    @bot.event
    async def on_ready():
        """Re-register persistent views when bot starts"""
        panels = load_panels()
        
        # Re-register vote views for forwarded applications
        apps = load_forwarded_apps()
        for message_id, app_data in apps.items():
            if app_data.get('approve_notified') or app_data.get('deny_notified'):
                mixed_view = ApplicationMixedView(
                    app_data,
                    approve_count=app_data.get('approve_count', 0),
                    deny_count=app_data.get('deny_count', 0),
                    show_approve_action=app_data.get('approve_notified', False),
                    show_deny_action=app_data.get('deny_notified', False),
                    threshold=0
                )
                bot.add_view(mixed_view)
            else:
                vote_view = ApplicationVoteView(
                    app_data,
                    approve_count=app_data.get('approve_count', 0),
                    deny_count=app_data.get('deny_count', 0),
                    threshold=0
                )
                bot.add_view(vote_view)
        
        # Re-register persistent ticket panel views
        for panel_id, panel_data in panels.items():
            class PersistentTicketView(View):
                def __init__(self, applications, panel_data):
                    super().__init__(timeout=None)
                    self.panel_data = panel_data
                    
                    for app in applications:
                        button = Button(
                            label=app['name'],
                            style=discord.ButtonStyle(app['style']),
                            emoji=app.get('emoji'),
                            custom_id=f"ticket_{app['name'].lower().replace(' ', '_')}"
                        )
                        
                        def create_callback(app_name):
                            async def callback(interaction: discord.Interaction):
                                await create_application_channel(interaction, app_name, self.panel_data)
                            return callback
                        
                        button.callback = create_callback(app['name'])
                        self.add_item(button)
            
            view = PersistentTicketView(panel_data['applications'], panel_data)
            bot.add_view(view)
        
        # Re-register control views for all tracked ticket channels
        channel_openers = load_channel_openers()
        for channel_id, opener_id in channel_openers.items():
            bot.add_view(ApplicationControlView(opener_id=opener_id))
        
        print(f"[OK] Registered {len(panels)} persistent ticket panel(s)")
        print(f"[OK] Registered {len(channel_openers)} ticket control view(s)")
        print(f"[OK] Registered {len(apps)} forwarded application view(s)")
        
        # Refresh all ticket panels and buttons
        await refresh_all_panels_and_buttons(bot)
        
        # Restore support ticket views
        if hasattr(bot, '_restore_support_ticket_views'):
            try:
                restored, failed = await bot._restore_support_ticket_views()
                print(f"[OK] Restored {restored} support ticket(s), {failed} failed")
            except Exception as e:
                print(f"[WARNING] Failed to restore support tickets: {e}")
        
        # Start the stale application checker
        bot.loop.create_task(check_stale_applications(bot))
        print("[OK] Started stale application checker")
    
    @bot.tree.command(name="refresh_ticket_buttons", description="Refresh all ticket control buttons")
    @app_commands.describe(
        channel="The ticket channel to refresh buttons in (leave empty to refresh all)"
    )
    async def refresh_ticket_buttons(interaction: discord.Interaction, channel: discord.TextChannel = None):
        """Refresh ticket control buttons in one or all ticket channels"""
        
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
        channel_openers = load_channel_openers()
        
        channels_to_reload = []
        
        if channel:
            # Reload specific channel
            if str(channel.id) in channel_openers:
                channels_to_reload.append(channel)
            else:
                await interaction.followup.send(f"❌ {channel.mention} is not a tracked ticket channel!", ephemeral=True)
                return
        else:
            # Reload all ticket channels
            for channel_id in channel_openers.keys():
                ch = interaction.guild.get_channel(int(channel_id))
                if ch:
                    channels_to_reload.append(ch)
        
        if not channels_to_reload:
            await interaction.followup.send("❌ No ticket channels found to reload!", ephemeral=True)
            return
        
        reloaded_count = 0
        failed_count = 0
        
        for ch in channels_to_reload:
            try:
                opener_id = channel_openers.get(str(ch.id))
                
                # Check if application was already submitted
                submitted = False
                async for message in ch.history(limit=100):
                    if message.author == interaction.guild.me and message.embeds:
                        for embed in message.embeds:
                            if "Application Submitted" in embed.title:
                                submitted = True
                                break
                    if submitted:
                        break
                
                # Find the control buttons message
                control_message = None
                async for message in ch.history(limit=50):
                    if message.author == interaction.guild.me and message.embeds:
                        if "Application" in message.embeds[0].title and "Submitted" not in message.embeds[0].title:
                            control_message = message
                            break
                
                if control_message:
                    # Create new view with updated state
                    view = ApplicationControlView(opener_id=opener_id)
                    
                    # If submitted, disable the fill button
                    if submitted:
                        for item in view.children:
                            if isinstance(item, Button) and item.custom_id == "fill_application":
                                item.disabled = True
                                item.label = "✅ Application Submitted"
                                item.style = discord.ButtonStyle.success
                    
                    await control_message.edit(view=view)
                    reloaded_count += 1
                else:
                    failed_count += 1
                    
            except Exception as e:
                print(f"Error reloading buttons in channel {ch.id}: {e}")
                failed_count += 1
        
        result_msg = f"✅ Reloaded buttons in **{reloaded_count}** channel(s)"
        if failed_count > 0:
            result_msg += f"\n⚠️ Failed to reload **{failed_count}** channel(s)"
        
        await interaction.followup.send(result_msg, ephemeral=True)
    
    @bot.tree.command(name="refresh_vote_buttons", description="Refresh all application vote buttons")
    @app_commands.describe(
        message_id="The application message ID to refresh (leave empty to refresh all)"
    )
    async def refresh_vote_buttons(interaction: discord.Interaction, message_id: str = None):
        """Refresh vote buttons on one or all forwarded applications"""
        
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
        
        apps = load_forwarded_apps()
        
        apps_to_reload = []
        
        if message_id:
            # Reload specific application
            if message_id in apps:
                apps_to_reload.append((message_id, apps[message_id]))
            else:
                await interaction.followup.send(f"❌ Application with message ID `{message_id}` not found!", ephemeral=True)
                return
        else:
            # Reload all applications
            apps_to_reload = list(apps.items())
        
        if not apps_to_reload:
            await interaction.followup.send("❌ No forwarded applications found to reload!", ephemeral=True)
            return
        
        reloaded_count = 0
        failed_count = 0
        
        for msg_id, app_data in apps_to_reload:
            try:
                # Get the channel (could be regular channel or thread)
                channel = interaction.guild.get_channel_or_thread(app_data['channel_id'])
                if not channel:
                    failed_count += 1
                    continue
                
                # Fetch the message
                try:
                    message = await channel.fetch_message(app_data['message_id'])
                except discord.NotFound:
                    failed_count += 1
                    continue
                
                
                # Calculate threshold
                threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
                approve_count = app_data.get('approve_count', 0)
                deny_count = app_data.get('deny_count', 0)
                
                # Determine which view to create
                if app_data.get('approve_notified') or app_data.get('deny_notified'):
                    # Create mixed view
                    view = ApplicationMixedView(
                        app_data,
                        approve_count,
                        deny_count,
                        show_approve_action=app_data.get('approve_notified', False),
                        show_deny_action=app_data.get('deny_notified', False),
                        threshold=threshold
                    )
                else:
                    # Create vote view
                    view = ApplicationVoteView(
                        app_data,
                        approve_count,
                        deny_count,
                        threshold
                    )
                
                # Check if buttons should be disabled
                if not app_data.get('buttons_enabled', True):
                    for item in view.children:
                        item.disabled = True
                
                await message.edit(view=view)
                reloaded_count += 1
                
            except Exception as e:
                print(f"Error reloading vote buttons for application {msg_id}: {e}")
                failed_count += 1
        
        result_msg = f"✅ Reloaded vote buttons on **{reloaded_count}** application(s)"
        if failed_count > 0:
            result_msg += f"\n⚠️ Failed to reload **{failed_count}** application(s)"
        
        await interaction.followup.send(result_msg, ephemeral=True)
    
    print("[OK] Loaded application handler")
