import discord
from discord import app_commands
from discord.ext import tasks
import os
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from utils.permissions import has_roles

REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0,
    1356674258390225076 # Admin
]

# Path to the JSON file (shared with standalone tracker)
DATA_FILE = Path(__file__).parent.parent.parent / "data/guild_territories.json"

notification_channel_id = 1056604456877297746  # Hardcoded channel
bot = None
notifications_enabled = False  # Toggle for sending notifications
last_notified_timestamp = None  # Track the last event we sent a notification for


def load_data_file():
    """Load data from the shared JSON file written by the standalone tracker"""
    try:
        if DATA_FILE.exists():
            with open(DATA_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        print(f"[CLAIM NOTIFY] Failed to read data file: {e}")
    return None


def save_notification_settings():
    """Save notification enabled/disabled state back to the shared JSON file"""
    try:
        if DATA_FILE.exists():
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
            data["notifications_enabled"] = notifications_enabled
            data["notification_channel_id"] = notification_channel_id
            with open(DATA_FILE, "w") as f:
                json.dump(data, f, indent=4)
    except Exception as e:
        print(f"[CLAIM NOTIFY] Failed to save notification settings: {e}")


async def send_claim_notification(channel, event_info):
    """Send Discord notification for a territory change"""
    try:
        event_type = event_info.get("type", "")
        territory = event_info.get("territory", "Unknown")
        from_guild = event_info.get("from_guild", "Unknown")
        to_guild = event_info.get("to_guild", "Unknown")
        held_for = event_info.get("held_for", "Unknown")
        
        # Calculate cooldown end (10 minutes from now)
        cooldown_end = datetime.now(timezone.utc) + timedelta(minutes=10)
        cooldown_timestamp = f"<t:{int(cooldown_end.timestamp())}:R>"
        
        if event_type == "Territory Lost":
            embed = discord.Embed(
                title="🔴 Territory Lost",
                color=0xFF0000,
                timestamp=datetime.now(timezone.utc)
            )
            embed.description = f"**{territory}**\n{from_guild} -> {to_guild}\nHeld for: **{held_for}**\nOff cooldown {cooldown_timestamp}"
        elif event_type == "Territory Captured":
            embed = discord.Embed(
                title="🟢 Territory Captured",
                color=0x00FF00,
                timestamp=datetime.now(timezone.utc)
            )
            embed.description = f"**{territory}**\n{from_guild} -> {to_guild}\nHeld for: **{held_for}**\nOff cooldown {cooldown_timestamp}"
        else:
            return
        
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[CLAIM NOTIFY] Failed to send notification: {e}")


def teardown(bot_instance):
    """Cleanup function called before reload"""
    if hasattr(bot_instance, '_claim_watcher_task') and bot_instance._claim_watcher_task is not None:
        if bot_instance._claim_watcher_task.is_running():
            print("[TEARDOWN] Stopping claim notification watcher...")
            bot_instance._claim_watcher_task.stop()
            print("[TEARDOWN] Claim notification watcher stopped")
        bot_instance._claim_watcher_task = None


def setup(bot_instance, has_required_role, config):
    """Setup function for bot integration"""
    global bot, notifications_enabled, last_notified_timestamp
    
    bot = bot_instance
    
    # Stop existing watcher if running
    if hasattr(bot, '_claim_watcher_task') and bot._claim_watcher_task is not None:
        if bot._claim_watcher_task.is_running():
            print("[RELOAD] Stopping existing claim notification watcher...")
            bot._claim_watcher_task.stop()
    
    # Load notification settings and set baseline from existing history
    data = load_data_file()
    if data:
        notifications_enabled = data.get("notifications_enabled", False)
        history = data.get("history", [])
        # Set baseline to latest history entry so we don't re-notify old events on startup
        if history:
            last_notified_timestamp = history[-1].get("timestamp")
        print(f"[OK] Claim notifier ready (notifications: {'enabled' if notifications_enabled else 'disabled'}, {len(history)} history entries)")
    else:
        print("[OK] Claim notifier ready (no data file found, waiting for standalone tracker)")
    
    # Background task to watch the shared JSON file for new history entries
    @tasks.loop(seconds=5)
    async def claim_notification_watcher():
        """Watch the shared JSON file for new history entries and send notifications"""
        global last_notified_timestamp
        
        if not notifications_enabled:
            return
        
        try:
            data = load_data_file()
            if not data:
                return
            
            history = data.get("history", [])
            if not history:
                return
            
            # If we have no baseline yet, set it now without notifying
            if last_notified_timestamp is None:
                last_notified_timestamp = history[-1].get("timestamp")
                return
            
            # Find events newer than last notified
            new_events = [
                event for event in history
                if event.get("timestamp") and event["timestamp"] > last_notified_timestamp
            ]
            
            if not new_events:
                return
            
            channel = bot.get_channel(notification_channel_id)
            if not channel:
                print(f"[CLAIM NOTIFY] Channel not found: {notification_channel_id}")
                return
            
            for event in new_events:
                await send_claim_notification(channel, event)
                print(f"[CLAIM NOTIFY] Sent: {event.get('type')} - {event.get('territory')}")
            
            last_notified_timestamp = new_events[-1].get("timestamp")
            
        except Exception as e:
            print(f"[CLAIM NOTIFY] Error: {e}")
            import traceback
            traceback.print_exc()
    
    @claim_notification_watcher.before_loop
    async def before_claim_watcher():
        await bot.wait_until_ready()
    
    # Start watcher and store on bot object (survives reloads)
    bot._claim_watcher_task = claim_notification_watcher
    bot._claim_watcher_task.start()
    print("[OK] Started claim notification watcher (checking file every 5 seconds)")
    
    @bot.tree.command(
        name="claim_tracker",
        description="Toggle territory tracking notifications for ESI"
    )
    async def claim_tracker_toggle(interaction: discord.Interaction):
        """Command to toggle territory tracking notifications"""
        global notifications_enabled, last_notified_timestamp

        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000,
                timestamp=datetime.now(timezone.utc)
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return
        
        # Toggle notifications
        notifications_enabled = not notifications_enabled
        
        if notifications_enabled:
            # Set baseline so we only notify for events that happen after enabling
            data = load_data_file()
            if data:
                history = data.get("history", [])
                if history:
                    last_notified_timestamp = history[-1].get("timestamp")
            
            embed = discord.Embed(
                title="Claim Tracker Enabled",
                description=f"Notifications will be sent to <#{notification_channel_id}>",
                color=0x00FF00,
                timestamp=datetime.now(timezone.utc)
            )
        else:
            embed = discord.Embed(
                title="Claim Tracker Disabled",
                description="Notifications have been disabled",
                color=0xFF0000,
                timestamp=datetime.now(timezone.utc)
            )
        
        save_notification_settings()
        await interaction.response.send_message(embed=embed)
        print(f"[OK] Claim tracker notifications {'enabled' if notifications_enabled else 'disabled'}")
    
    print("[OK] Loaded claim tracking commands")
