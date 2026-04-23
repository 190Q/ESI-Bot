import discord
from discord import app_commands
from discord.ext import tasks
import os
import aiohttp
import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path
import sys
from utils.permissions import has_roles

# Add tickets directory to path for guild_queue imports
_TICKETS_DIR = str(Path(__file__).parent.parent / "tickets")
if _TICKETS_DIR not in sys.path:
    sys.path.insert(0, _TICKETS_DIR)

TICKET_PANELS_FILE = Path(__file__).parent.parent.parent / "data/ticket_panels.json"

REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0,
    1356674258390225076 # Admin
]

WYNNCRAFT_API_KEY = os.getenv('WYNNCRAFT_KEY_7')

# Path to username matches DB (maps Discord user ID -> Wynncraft username)
USERNAME_MATCH_DB_PATH = Path(__file__).parent.parent.parent / "data/username_matches.json"

DELAY = 30  # Check interval (same as standalone tracker)

# Path to the JSON file (shared with standalone tracker)
DATA_FILE = Path(__file__).parent.parent.parent / "data/tracked_guild.json"

tracked_guild = "ESI"  # Always track ESI
previous_guild_data = {}
member_history = {}
notification_channel_id = None
notification_thread_id = 1462881693865218150  # Hardcoded thread
bot = None
is_prefix_tracked = True  # Always use prefix
last_notified_event_timestamp = None  # Track timestamp of last notified event
notifications_enabled = False  # Toggle for sending notifications

# Task is stored on bot object as bot._guild_watcher_task to survive reloads

# Rank hierarchy (lower index = higher rank)
RANK_HIERARCHY = ["owner", "chief", "strategist", "captain", "recruiter", "recruit"]

def get_rank_level(rank):
    """Get the hierarchical level of a rank (lower = higher rank)"""
    try:
        return RANK_HIERARCHY.index(rank.lower())
    except ValueError:
        return 999  # Unknown rank

def load_tracked_guild():
    """Load tracked guild data from JSON file"""
    try:
        if DATA_FILE.exists():
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
                return (
                    data.get("guild_identifier"),
                    data.get("is_prefix"),
                    data.get("previous_data", {}),
                    data.get("member_history", {}),
                    data.get("event_history", []),
                    data.get("notification_channel_id"),
                    data.get("notification_thread_id"),
                    data.get("notifications_enabled", True)
                )
    except Exception as e:
        print(f"[ERROR] Failed to load tracked guild: {e}")
    return None, False, {}, {}, [], None, None, False

def save_guild_data(guild_identifier, is_prefix, guild_data, member_history_data):
    """Save guild data to JSON file"""
    # Load existing data to preserve event_history from standalone tracker
    existing_data = {}
    try:
        if DATA_FILE.exists():
            with open(DATA_FILE, "r") as f:
                existing_data = json.load(f)
    except:
        pass
    
    data = {
        "guild_identifier": guild_identifier,
        "is_prefix": is_prefix,
        "last_update": datetime.now(timezone.utc).isoformat(),
        "previous_data": guild_data,
        "member_history": member_history_data,
        "event_history": existing_data.get("event_history", []),
        "notification_channel_id": notification_channel_id,
        "notification_thread_id": notification_thread_id,
        "notifications_enabled": notifications_enabled
    }
    
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

async def fetch_guild_data(identifier, use_prefix=False):
    """Fetch guild data from Wynncraft API"""
    headers = {
        "apikey": WYNNCRAFT_API_KEY
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            if use_prefix:
                url = f"https://api.wynncraft.com/v3/guild/prefix/{identifier}"
            else:
                url = f"https://api.wynncraft.com/v3/guild/{identifier}"
            
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    print(f"[ERROR] Failed to fetch guild data: {response.status}")
                    return None
    except Exception as e:
        print(f"[ERROR] Request error: {e}")
        import traceback
        traceback.print_exc()
        return None

def extract_guild_info(data):
    """Extract the relevant guild information from API response"""
    if not data:
        return None
    
    guild_info = {
        "name": data.get("name"),
        "prefix": data.get("prefix"),
        "level": data.get("level"),
        "member_count": data.get("members", {}).get("total", 0),
        "members": {}
    }
    
    # Extract members by rank
    members_data = data.get("members", {})
    for rank in ["owner", "chief", "strategist", "captain", "recruiter", "recruit"]:
        if rank in members_data:
            guild_info["members"][rank] = []
            rank_members = members_data[rank]
            
            # Handle case where rank has a single member (owner)
            if isinstance(rank_members, dict):
                for username, member_data in rank_members.items():
                    guild_info["members"][rank].append({
                        "username": username,
                        "uuid": member_data.get("uuid"),
                        "contributed": member_data.get("contributed", 0),
                        "joined": member_data.get("joined")
                    })
            elif isinstance(rank_members, list):
                # If it's a list (shouldn't happen but just in case)
                for member_data in rank_members:
                    if isinstance(member_data, dict):
                        username = list(member_data.keys())[0]
                        data_obj = member_data[username]
                        guild_info["members"][rank].append({
                            "username": username,
                            "uuid": data_obj.get("uuid"),
                            "contributed": data_obj.get("contributed", 0),
                            "joined": data_obj.get("joined")
                        })
    
    return guild_info

def compare_guild_data(old_data, new_data):
    """Compare old and new guild data and return changes"""
    changes = []
    
    if not old_data:
        return changes
    
    # Check level change
    if old_data.get("level") != new_data.get("level"):
        changes.append({
            "type": "level_change",
            "old": old_data.get("level"),
            "new": new_data.get("level")
        })
    
    # Check member count change
    if old_data.get("member_count") != new_data.get("member_count"):
        changes.append({
            "type": "member_count_change",
            "old": old_data.get("member_count"),
            "new": new_data.get("member_count")
        })
    
    # Check for member additions/removals
    old_members = {}
    new_members = {}
    
    # Flatten old members
    for rank, members in old_data.get("members", {}).items():
        for member in members:
            old_members[member["uuid"]] = {
                "username": member["username"],
                "rank": rank,
                "contributed": member.get("contributed", 0),
                "joined": member.get("joined")
            }
    
    # Flatten new members
    for rank, members in new_data.get("members", {}).items():
        for member in members:
            new_members[member["uuid"]] = {
                "username": member["username"],
                "rank": rank,
                "contributed": member.get("contributed", 0),
                "joined": member.get("joined")
            }
    
    # Find removed members
    for uuid, member_data in old_members.items():
        if uuid not in new_members:
            changes.append({
                "type": "member_left",
                "username": member_data["username"],
                "uuid": uuid,
                "rank": member_data["rank"],
                "contributed": member_data["contributed"]
            })
    
    # Find added members
    for uuid, member_data in new_members.items():
        if uuid not in old_members:
            changes.append({
                "type": "member_joined",
                "username": member_data["username"],
                "uuid": uuid,
                "rank": member_data["rank"],
                "joined": member_data["joined"]
            })
    
    # Find rank changes
    for uuid, new_member in new_members.items():
        if uuid in old_members:
            old_member = old_members[uuid]
            if old_member["rank"] != new_member["rank"]:
                changes.append({
                    "type": "rank_change",
                    "username": new_member["username"],
                    "uuid": uuid,
                    "old_rank": old_member["rank"],
                    "new_rank": new_member["rank"]
                })
    
    return changes

def print_change(change_info):
    """Print change to console"""
    change_type = change_info["type"]
    guild_name = change_info["guild_name"]
    
    if change_type == "level_change":
        print(f"[LEVEL] {guild_name}: Level {change_info['old']} -> {change_info['new']}")
    elif change_type == "member_joined":
        print(f"[JOIN] {guild_name}: {change_info['username']} joined as {change_info['rank']}")
    elif change_type == "member_left":
        print(f"[LEAVE] {guild_name}: {change_info['username']} left ({change_info['rank']})")
    elif change_type == "rank_change":
        old_level = get_rank_level(change_info['old_rank'])
        new_level = get_rank_level(change_info['new_rank'])
        action = "promoted" if new_level < old_level else "demoted"
        print(f"[RANK] {guild_name}: {change_info['username']} {action} from {change_info['old_rank']} to {change_info['new_rank']}")

async def send_change_notification(channel, change_info):
    """Send Discord notification for a change"""
    try:
        change_type = change_info["type"]
        
        if change_type == "level_change":
            embed = discord.Embed(
                title="📊 Guild Level Up",
                description=f"**{change_info['guild_name']}** leveled up!\nLevel {change_info['old']} → Level {change_info['new']}",
                color=0xFFD700,
                timestamp=datetime.now(timezone.utc)
            )
        elif change_type == "rank_changes_batch":
            # Batch rank changes
            title = f"{change_info['guild_name']} [{change_info['guild_prefix']}]"
            description_lines = ["🎖️ Rank Update"]
            for change in change_info['changes']:
                old_level = get_rank_level(change['old_rank'])
                new_level = get_rank_level(change['new_rank'])
                action = "promoted" if new_level < old_level else "demoted"
                emoji = "▲" if action == "promoted" else "▼"
                escaped_username = discord.utils.escape_markdown(change['username'])
                description_lines.append(f"{emoji} **{escaped_username}** was {action} from `{change['old_rank'].upper()}` to `{change['new_rank'].upper()}`")
            embed = discord.Embed(
                title=title,
                description="\n".join(description_lines),
                color=0x0099FF,
                timestamp=datetime.now(timezone.utc)
            )
        elif change_type == "member_joins_batch":
            # Batch member joins
            title = f"{change_info['guild_name']} [{change_info['guild_prefix']}]"
            description_lines = ["▶️ Member Join"]
            for change in change_info['changes']:
                escaped_username = discord.utils.escape_markdown(change['username'])
                description_lines.append(escaped_username)
            embed = discord.Embed(
                title=title,
                description="\n".join(description_lines),
                color=0x00FF00,
                timestamp=datetime.now(timezone.utc)
            )
        elif change_type == "member_leaves_batch":
            # Batch member leaves
            title = f"{change_info['guild_name']} [{change_info['guild_prefix']}]"
            description_lines = ["◀️ Member Leave"]
            for change in change_info['changes']:
                highest_rank = change.get('highest_rank', change['rank'])
                escaped_username = discord.utils.escape_markdown(change['username'])
                description_lines.append(f"{escaped_username} ({change['rank'].upper()}) - Highest: {highest_rank.upper()}")
            embed = discord.Embed(
                title=title,
                description="\n".join(description_lines),
                color=0xFF0000,
                timestamp=datetime.now(timezone.utc)
            )
        else:
            return
        
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[ERROR] Failed to send notification: {e}")


def check_for_new_guild_events():
    """Check the JSON file for new events that haven't been notified"""
    global last_notified_event_timestamp
    
    try:
        if not DATA_FILE.exists():
            return []
        
        # Retry on JSON parse errors (race condition protection)
        max_retries = 5
        data = None
        for attempt in range(max_retries):
            try:
                with open(DATA_FILE, "r") as f:
                    data = json.load(f)
                break  # Success
            except (json.JSONDecodeError, IOError, OSError) as e:
                if attempt == max_retries - 1:
                    print(f"[GUILD NOTIFY] Failed to parse JSON after {max_retries} attempts: {e}")
                    return []
                # Exponential backoff: 0.2s, 0.4s, 0.8s, 1.6s
                time.sleep(0.2 * (2 ** attempt))
        
        if data is None:
            return []
        
        event_history = data.get("event_history", [])
        
        # Get new events since last notified timestamp
        new_events = []
        for event in event_history:
            event_ts = event.get("timestamp")
            if event_ts and (last_notified_event_timestamp is None or event_ts > last_notified_event_timestamp):
                new_events.append(event)
        
        if new_events:
            # Update timestamp to the latest event
            last_notified_event_timestamp = max(e.get("timestamp", "") for e in new_events)
        
        return new_events
    
    except Exception as e:
        print(f"[GUILD NOTIFY] Error checking for new events: {e}")
        return []


async def send_batched_notifications(channel, events):
    """Send Discord notifications for guild events, batching similar types together"""
    if not events:
        return
    
    # Group events by type
    joins = []
    leaves = []
    rank_changes = []
    level_changes = []
    
    for event in events:
        event_type = event.get("type", "")
        if event_type == "member_joined":
            joins.append(event)
        elif event_type == "member_left":
            leaves.append(event)
        elif event_type == "rank_change":
            rank_changes.append(event)
        elif event_type == "level_change":
            level_changes.append(event)
        # Skip member_count_change - redundant with join/leave
    
    # Get guild info from first event
    guild_name = events[0].get("guild_name", "Unknown")
    guild_prefix = events[0].get("guild_prefix", "?")
    
    try:
        # Send level changes
        for event in level_changes:
            embed = discord.Embed(
                title=f"{guild_name} [{guild_prefix}]",
                description=f"📊 **Guild Level Up**\nLevel {event.get('old')} → Level {event.get('new')}",
                color=0xFFD700,
                timestamp=datetime.now(timezone.utc)
            )
            await channel.send(embed=embed)
        
        # Send batched joins
        if joins:
            # Clear any pending invites for players who just joined the guild
            try:
                from guild_queue import (
                    remove_pending_invite_by_uuid, remove_pending_invite_by_username,
                )
                for event in joins:
                    uuid = event.get("uuid")
                    uname = event.get("username")
                    cleared = False
                    if uuid:
                        cleared = remove_pending_invite_by_uuid(uuid)
                    if not cleared and uname:
                        cleared = remove_pending_invite_by_username(uname)
                    if cleared:
                        print(f"[PENDING] Cleared pending invite for {uname} (joined guild)")
            except Exception as e:
                print(f"[PENDING] Error clearing pending invites on join: {e}")

            lines = []
            for event in joins:
                escaped_username = discord.utils.escape_markdown(event.get("username", "Unknown"))
                lines.append(f"{escaped_username} joined as `{event.get('rank', 'recruit').upper()}`")
            embed = discord.Embed(
                title=f"{guild_name} [{guild_prefix}]",
                description=f"▶️ **Member Join{'s' if len(joins) > 1 else ''}**\n" + "\n".join(lines),
                color=0x00FF00,
                timestamp=datetime.now(timezone.utc)
            )
            await channel.send(embed=embed)
        
        # Send batched leaves
        if leaves:
            lines = []
            for event in leaves:
                escaped_username = discord.utils.escape_markdown(event.get("username", "Unknown"))
                lines.append(f"{escaped_username} left (`{event.get('rank', 'unknown').upper()}`)") 
            embed = discord.Embed(
                title=f"{guild_name} [{guild_prefix}]",
                description=f"◀️ **Member Leave{'s' if len(leaves) > 1 else ''}**\n" + "\n".join(lines),
                color=0xFF0000,
                timestamp=datetime.now(timezone.utc)
            )
            await channel.send(embed=embed)
            
            # Check queue for open slots after leaves
            await _check_queue_after_leaves(channel, len(leaves))
        
        # Send batched rank changes
        if rank_changes:
            lines = []
            for event in rank_changes:
                escaped_username = discord.utils.escape_markdown(event.get("username", "Unknown"))
                old_rank = event.get('old_rank', 'unknown')
                new_rank = event.get('new_rank', 'unknown')
                old_level = get_rank_level(old_rank)
                new_level = get_rank_level(new_rank)
                action = "promoted" if new_level < old_level else "demoted"
                emoji = "▲" if action == "promoted" else "▼"
                lines.append(f"{emoji} **{escaped_username}** was {action} from `{old_rank.upper()}` to `{new_rank.upper()}`")
            embed = discord.Embed(
                title=f"{guild_name} [{guild_prefix}]",
                description=f"🎖️ **Rank Update{'s' if len(rank_changes) > 1 else ''}**\n" + "\n".join(lines),
                color=0x0099FF,
                timestamp=datetime.now(timezone.utc)
            )
            await channel.send(embed=embed)
    
    except Exception as e:
        print(f"[GUILD NOTIFY] Failed to send notification: {e}")


def _load_forwarding_channel_ids():
    """Return the set of forwarding channel IDs declared in ticket panels."""
    forwarding_ids = set()
    try:
        if TICKET_PANELS_FILE.exists():
            with open(TICKET_PANELS_FILE, "r", encoding="utf-8") as f:
                panels = json.load(f)
            for panel_data in panels.values():
                fwd_id = panel_data.get("forwarding_channel_id")
                if fwd_id:
                    forwarding_ids.add(fwd_id)
    except Exception as e:
        print(f"[QUEUE] Failed to load ticket panels: {e}")
    return forwarding_ids


async def notify_slot_opened(bot_instance, open_slots: int) -> int:
    """Build and send the "🔓 Guild Slot(s) Opened" embed to every ticket
    forwarding channel.

    Returns the number of channels that were notified. Returns ``0`` when the
    queue is empty or there are no forwarding channels configured.

    This is the same embed produced by the real member‑leave flow; the debug
    capacity‑override path calls it directly so simulating a slot opening is
    indistinguishable from a genuine leave.
    """
    if open_slots <= 0 or bot_instance is None:
        return 0

    try:
        from guild_queue import load_queue
    except Exception as e:
        print(f"[QUEUE] notify_slot_opened: failed to import load_queue: {e}")
        return 0

    queue = load_queue()
    veteran_queue = queue.get("veteran", [])
    normal_queue = queue.get("normal", [])
    if not (veteran_queue or normal_queue):
        print("[QUEUE] notify_slot_opened: queue is empty, nothing to announce")
        return 0

    ordered = [(e, "veteran") for e in veteran_queue] + [(e, "normal") for e in normal_queue]
    invitable = ordered[:open_slots]
    if not invitable:
        return 0

    lines = []
    for entry, _qt in invitable:
        escaped = discord.utils.escape_markdown(entry.get("username", "Unknown"))
        lines.append(f"**{escaped}** (<@{entry['discord_id']}>)")

    embed = discord.Embed(
        title="🔓 Guild Slot(s) Opened",
        description=(
            f"**{open_slots}** slot(s) are now available.\n\n"
            + "\n".join(lines)
        ),
        color=0x00FF00,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Use /gu invite <username> to invite them")

    forwarding_ids = _load_forwarding_channel_ids()
    if not forwarding_ids:
        print("[QUEUE] notify_slot_opened: no forwarding channels configured")
        return 0

    sent = 0
    for ch_id in forwarding_ids:
        ch = bot_instance.get_channel(ch_id)
        if ch:
            await ch.send(embed=embed)
            sent += 1
        else:
            print(f"[QUEUE] notify_slot_opened: channel {ch_id} not found")

    print(f"[QUEUE] notify_slot_opened: notified {len(invitable)} player(s) in {sent} channel(s) (open_slots={open_slots})")
    return sent


async def _check_queue_after_leaves(notification_channel, num_leaves=1):
    """After member leaves, check if queue members can now be invited.
    Only notifies if the guild was previously at effective capacity.

    Pending invites (accepted players who have not joined yet) still count
    against the guild, so a slot doesn't "open" until the effective count
    drops below ``max_slots``.
    """
    try:
        from guild_queue import get_max_slots_for_level, get_pending_invites_count

        # Read current member count and level from tracked guild data
        if not DATA_FILE.exists():
            return
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
        guild_data = data.get("previous_data", {})
        member_count = guild_data.get("member_count")
        guild_level = guild_data.get("level")
        if member_count is None or guild_level is None:
            return

        pending = get_pending_invites_count()
        max_slots = get_max_slots_for_level(guild_level)
        open_slots = max_slots - member_count - pending
        if open_slots <= 0:
            return

        # Only notify if guild was previously at effective capacity
        previous_count = member_count + num_leaves + pending
        if previous_count < max_slots:
            return

        await notify_slot_opened(bot, open_slots)
    except Exception as e:
        print(f"[QUEUE] Error checking queue after leaves: {e}")
        import traceback
        traceback.print_exc()


async def _check_queue_after_joins(notification_channel):
    """After members join, check if approved apps that reached accept threshold need to be queued."""
    try:
        from guild_queue import (
            get_max_slots_for_level, add_to_queue, get_queue_position,
            extract_username_from_embeds, VETERAN_ROLE_ID as VET_ROLE_ID,
            get_pending_invites_count,
        )
        from ticket_handler import load_forwarded_apps, save_forwarded_apps, ApplicationMixedView

        # Read current member count and level from tracked guild data
        if not DATA_FILE.exists():
            return
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
        guild_data = data.get("previous_data", {})
        member_count = guild_data.get("member_count")
        guild_level = guild_data.get("level")
        if member_count is None or guild_level is None:
            return

        pending = get_pending_invites_count()
        max_slots = get_max_slots_for_level(guild_level)
        open_slots = max_slots - member_count - pending
        if open_slots > 0:
            return  # Guild still has room (after pending invites), no need to queue

        # Load forwarded apps and find approved-but-not-queued guild applications
        apps = load_forwarded_apps()
        unqueued_approved = []
        for msg_id, app_data in apps.items():
            if not app_data.get('approve_notified'):
                continue
            if app_data.get('queue_locked'):
                continue
            app_type = (app_data.get('app_type') or '').lower()
            if app_type not in ('guild member', 'ex-citizen'):
                continue
            if app_data.get('status') == 'denied':
                continue
            unqueued_approved.append((msg_id, app_data))

        if not unqueued_approved:
            return

        guild = notification_channel.guild if notification_channel else None
        queued_entries = []

        for msg_id, app_data in unqueued_approved:
            discord_id = app_data.get('user_id')
            if not discord_id or get_queue_position(discord_id):
                continue

            # Extract username from the forwarded message embeds
            username = "Unknown"
            try:
                ch = bot.get_channel(app_data['channel_id'])
                if ch:
                    msg = await ch.fetch_message(app_data['message_id'])
                    if msg and msg.embeds:
                        extracted = extract_username_from_embeds(msg.embeds)
                        if extracted:
                            username = extracted
            except Exception:
                pass

            # Check veteran status
            is_vet = False
            if guild:
                member = guild.get_member(discord_id)
                is_vet = member is not None and any(r.id == VET_ROLE_ID for r in member.roles)

            queue_pos, queue_type = add_to_queue(username, None, discord_id, is_veteran=is_vet)

            # Lock the app
            app_data['queue_locked'] = True
            app_data['queue_position'] = queue_pos
            app_data['queue_type'] = queue_type
            # Persist the updated queue fields before notifying
            save_forwarded_apps(apps)
            queued_entries.append((msg_id, app_data, queue_pos, queue_type, username, discord_id))

            # Update the forwarded message view to show locked state
            try:
                ch = bot.get_channel(app_data['channel_id'])
                if ch:
                    msg = await ch.fetch_message(app_data['message_id'])
                    if msg:
                        threshold = app_data.get('threshold', 5)
                        approve_count = app_data.get('approve_count', 0)
                        deny_count = app_data.get('deny_count', 0)
                        show_deny = deny_count >= threshold or app_data.get('deny_notified', False)
                        locked_view = ApplicationMixedView(
                            app_data, approve_count, deny_count,
                            show_approve_action=True,
                            show_deny_action=show_deny,
                            threshold=threshold,
                        )
                        await msg.edit(view=locked_view)
            except Exception as e:
                print(f"[QUEUE] Failed to lock message view for {discord_id}: {e}")

            # Notify the user in their ticket channel
            try:
                if guild:
                    from ticket_handler import notify_applicant_queued
                    await notify_applicant_queued(guild, msg_id)
            except Exception as e:
                print(f"[QUEUE] Failed to notify ticket channel for {discord_id}: {e}")

        if not queued_entries:
            return

        save_forwarded_apps(apps)

        # Notify forwarding channels
        lines = []
        for msg_id, app_data, queue_pos, queue_type, username, discord_id in queued_entries:
            queue_label = "⭐ Veteran" if queue_type == "veteran" else "Normal"
            escaped = discord.utils.escape_markdown(username)
            lines.append(f"**{escaped}** (<@{discord_id}>) → {queue_label} queue #{queue_pos}")

        embed = discord.Embed(
            title="📋 Auto-Queued Approved Applications",
            description=(
                f"The guild is at **{member_count}/{max_slots}** members "
                f"({open_slots} slot{'s' if open_slots != 1 else ''} remaining).\n"
                f"The following approved applications have been queued:\n\n"
                + "\n".join(lines)
            ),
            color=0xFFA500,
            timestamp=datetime.now(timezone.utc),
        )

        forwarding_ids = _load_forwarding_channel_ids()

        for ch_id in forwarding_ids:
            ch = bot.get_channel(ch_id)
            if ch:
                await ch.send(embed=embed)

        print(f"[QUEUE] Auto-queued {len(queued_entries)} approved app(s) after guild join(s)")
    except Exception as e:
        print(f"[QUEUE] Error checking queue after joins: {e}")
        import traceback
        traceback.print_exc()


def teardown(bot_instance):
    """Cleanup function called before reload"""
    # Stop the watcher task if it exists on the bot
    if hasattr(bot_instance, '_guild_watcher_task') and bot_instance._guild_watcher_task is not None:
        if bot_instance._guild_watcher_task.is_running():
            print("[TEARDOWN] Stopping guild notification watcher...")
            bot_instance._guild_watcher_task.stop()
            print("[TEARDOWN] Guild notification watcher stopped")
        bot_instance._guild_watcher_task = None


def setup(bot_instance, has_required_role, config):
    """Setup function for bot integration"""
    global tracked_guild, previous_guild_data, member_history, bot, notification_channel_id, notification_thread_id, is_prefix_tracked, last_notified_event_timestamp, notifications_enabled
    
    bot = bot_instance
    
    # Stop existing watcher if it's running (check on bot object)
    if hasattr(bot, '_guild_watcher_task') and bot._guild_watcher_task is not None:
        if bot._guild_watcher_task.is_running():
            print("[RELOAD] Stopping existing guild notification watcher...")
            bot._guild_watcher_task.stop()
    
    # Load previously tracked guild on startup
    loaded_identifier, loaded_is_prefix, loaded_data, loaded_member_history, loaded_event_history, loaded_channel_id, loaded_thread_id, loaded_notifications_enabled = load_tracked_guild()
    
    # Always load the notifications state
    notifications_enabled = loaded_notifications_enabled
    
    if loaded_identifier:
        tracked_guild = loaded_identifier
        is_prefix_tracked = loaded_is_prefix
        member_history = loaded_member_history
        previous_guild_data = loaded_data
        # Initialize last_notified_event_timestamp to latest event timestamp to avoid re-sending old events
        if loaded_event_history:
            last_notified_event_timestamp = max(e.get("timestamp", "") for e in loaded_event_history if e.get("timestamp"))
        else:
            last_notified_event_timestamp = None
        guild_name = loaded_data.get('name', loaded_identifier)
        print(f"[OK] Loaded guild tracking data for {guild_name} (notifications: {'enabled' if notifications_enabled else 'disabled'})")
    else:
        print(f"[OK] Guild tracker ready (notifications: {'enabled' if notifications_enabled else 'disabled'})")
    
    # Always use hardcoded thread
    notification_thread_id = 1462881693865218150
    notification_channel_id = None
    
    # --- Discord member leave handler ---
    @bot.event
    async def on_member_remove(member: discord.Member):
        """When a user leaves the Discord, check if they are in the guild and notify with a kick command."""
        if not notifications_enabled:
            return
        
        try:
            # Load username matches to find their Wynncraft username
            try:
                with open(USERNAME_MATCH_DB_PATH, "r", encoding="utf-8") as f:
                    username_db = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                return
            
            wynncraft_entry = username_db.get(str(member.id))
            if not wynncraft_entry:
                return
            # Entry can be a plain string or a dict with 'username' key
            wynncraft_username = wynncraft_entry['username'] if isinstance(wynncraft_entry, dict) else wynncraft_entry
            
            # Check if this username is currently in the guild
            guild_data = previous_guild_data
            if not guild_data or not guild_data.get("members"):
                return
            
            in_guild = False
            member_rank = None
            for rank, members in guild_data.get("members", {}).items():
                for m in members:
                    if m.get("username", "").lower() == wynncraft_username.lower():
                        in_guild = True
                        member_rank = rank
                        break
                if in_guild:
                    break
            
            if not in_guild:
                return
            
            # Send notification to the guild tracker thread
            channel = bot.get_channel(1462881693865218150)
            if not channel:
                print(f"[GUILD NOTIFY] Thread not found for Discord leave notification")
                return
            
            escaped_username = discord.utils.escape_markdown(wynncraft_username)
            embed = discord.Embed(
                title=f"{guild_data.get('name', 'Unknown')} [{guild_data.get('prefix', '?')}]",
                description=(
                    f"⚠️ **Discord Leave**\n"
                    f"{escaped_username} (`{member_rank.upper()}`) left the Discord server.\n"
                    f"Discord: {member} (`{member.id}`)\n\n"
                    f"Kick command:"
                ),
                color=0xFF6600,
                timestamp=datetime.now(timezone.utc)
            )
            await channel.send(embed=embed, content=f"```\n/gu kick {wynncraft_username}\n```")
            print(f"[GUILD NOTIFY] Sent Discord leave kick notification for {wynncraft_username} (Discord: {member})")
        
        except Exception as e:
            print(f"[GUILD NOTIFY] Error in on_member_remove handler: {e}")
            import traceback
            traceback.print_exc()
    
    # Background task to watch for new events from standalone tracker
    @tasks.loop(seconds=5)
    async def guild_notification_watcher():
        """Check for new guild events and send notifications"""
        if not notifications_enabled:
            return
        
        try:
            # Drop any pending-invite entries that are older than the TTL so
            # their reserved slots return to the pool even if nothing else
            # triggers a capacity check.
            try:
                from guild_queue import prune_expired_pending_invites
                prune_expired_pending_invites()
            except Exception as e:
                print(f"[PENDING] Failed to prune expired pending invites: {e}")

            new_events = check_for_new_guild_events()
            
            if new_events:
                channel = bot.get_channel(1462881693865218150)
                if channel:
                    await send_batched_notifications(channel, new_events)
                else:
                    print(f"[GUILD NOTIFY] Thread not found: 1462881693865218150")
        except Exception as e:
            print(f"[GUILD NOTIFY] Watcher error: {e}")
    
    @guild_notification_watcher.before_loop
    async def before_guild_watcher():
        await bot.wait_until_ready()
    
    # Start the watcher and store reference on bot object (survives reloads)
    bot._guild_watcher_task = guild_notification_watcher
    bot._guild_watcher_task.start()
    print("[OK] Started guild notification watcher")
    
    @bot.tree.command(
        name="guild_tracker",
        description="Toggle guild member tracking notifications for ESI"
    )
    async def guild_tracker_toggle(interaction: discord.Interaction):
        """Command to toggle guild tracking notifications"""
        global tracked_guild, previous_guild_data, member_history, notification_channel_id, notification_thread_id, is_prefix_tracked, notifications_enabled

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
            # Set up tracking for ESI with hardcoded thread
            tracked_guild = "ESI"
            is_prefix_tracked = True
            notification_thread_id = 1462881693865218150  # Hardcoded thread
            notification_channel_id = None
            
            embed = discord.Embed(
                title="Guild Tracker Enabled",
                description=f"Notifications will be sent to <#1462881693865218150>",
                color=0x00FF00,
                timestamp=datetime.now(timezone.utc)
            )
        else:
            embed = discord.Embed(
                title="Guild Tracker Disabled",
                description="Notifications have been disabled",
                color=0xFF0000,
                timestamp=datetime.now(timezone.utc)
            )
        
        # Save the state
        save_guild_data(tracked_guild, is_prefix_tracked, previous_guild_data, member_history)
        
        await interaction.response.send_message(embed=embed)
        print(f"[OK] Guild tracker notifications {'enabled' if notifications_enabled else 'disabled'}")
    
    print("[OK] Loaded guild tracking commands")
