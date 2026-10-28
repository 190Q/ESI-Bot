"""
Ticket reminders.

Pings the applicant in their own application channel when they stall: once if
the application was never submitted, twice if they were accepted but never
joined the guild in-game, and then closes the ticket if they still have not
joined a day after the final reminder.
"""
import asyncio
import json
import random
from datetime import datetime, timezone

import discord

from utils.paths import DATA_DIR

CHANNEL_OPENERS_FILE = DATA_DIR / "channel_openers.json"
FORWARDED_APPS_FILE = DATA_DIR / "forwarded_applications.json"
PENDING_APPS_FILE = DATA_DIR / "pending_applications.json"
TRACKED_GUILD_FILE = DATA_DIR / "tracked_guild.json"
STATE_FILE = DATA_DIR / "ticket_reminders.json"

# Reminder timings, in hours
FILL_REMINDER_HOURS = 24
JOIN_REMINDER_HOURS = 48
JOIN_FINAL_REMINDER_HOURS = 72
JOIN_CLOSE_HOURS = 96
# Progress saved this recently means the applicant is still filling the form
FILL_ACTIVITY_GRACE_HOURS = 6

CHECK_INTERVAL_SECONDS = 1800
FIRST_CHECK_DELAY_SECONDS = 120

# Roles the member keeps when a ticket is closed for never joining
DEFAULT_ROLE_IDS = [968121600488656906, 554884562862211082]
CLOSE_REASON = "accepted but didn't join"

FILL_MESSAGES = [
    "Hello {mention}, we noticed that your application ticket has been opened but you haven't filled it out yet. Please submit your application so we can review it. Let us know if you have any questions or need assistance!",
    "Hey {mention}, just a heads up that your application is still empty. Please fill it out when you get a chance so we can move forward with the review process!",
    "Hi {mention}, your application ticket is open but we haven't received your responses yet. Don't forget to complete it so we can get you reviewed!",
    "Hello {mention}, friendly reminder that you opened a ticket but haven't submitted anything yet. Please fill it out at your earliest convenience!",
    "Hey there {mention}, we see your ticket is open but the application itself is still blank. Please submit your answers so our jurors can review it!",
    "Hi {mention}, just checking in, your application ticket has been created but we're still waiting on your submission. Let us know if you need help getting started!",
    "Hello {mention}, we noticed your application hasn't been filled out yet. Please complete it when you have time so we can begin the review process!",
    "Hey {mention}, your application ticket is active but empty. Please submit your responses so we can move forward with reviewing your application!",
]

JOIN_REMINDER_MESSAGES = [
    "Hey {mention}, just a friendly reminder to join the guild in-game by typing '/gu join ESI' the next time you log on.",
    "Hi {mention}, don't forget to join the guild in-game! Just type '/gu join ESI' when you're on Wynncraft.",
    "Hello {mention}, quick reminder to hop in-game and join us by typing '/gu join ESI' whenever you're ready!",
    "Hey there {mention}, just checking in, remember to join the guild in-game with '/gu join ESI' when you get the chance!",
    "Hi {mention}, friendly reminder to join ESI in-game by using the command '/gu join ESI' next time you're online!",
    "Hello {mention}, just a gentle nudge to join the guild in-game! Type '/gu join ESI' when you log on.",
    "Hey {mention}, reminder to complete your application by joining us in-game with '/gu join ESI'!",
    "Hi there {mention}, don't forget the final step, join the guild in-game by typing '/gu join ESI' when you're on!",
]

JOIN_FINAL_MESSAGES = [
    "Hello {mention}, this is a final reminder to join the guild in-game using '/gu join ESI'. If you don't join within the next 24 hours, we'll have to close your application. You can always reapply later!",
    "Hey {mention}, just a heads up that if you don't join the guild in-game with '/gu join ESI' within the next 24 hours, we'll need to close this application. You're welcome to reapply anytime after!",
    "Hi {mention}, this is your final reminder to join ESI in-game by typing '/gu join ESI'. We'll have to close your application in 24 hours if you haven't joined, but you can always apply again later!",
    "Hello {mention}, we'll need to close your application in 24 hours if you don't join the guild in-game using '/gu join ESI'. Don't worry though, you can reapply whenever you want!",
    "Hey there {mention}, last call to join the guild in-game with '/gu join ESI'! If we don't see you in the next 24 hours, we'll close this application. Feel free to reapply later!",
    "Hi {mention}, final reminder, please join ESI in-game by typing '/gu join ESI' within 24 hours or we'll have to close your application. You can always open a new one when you're available!",
    "Hello {mention}, just letting you know that your application will be closed in 24 hours if you don't join the guild using '/gu join ESI'. No worries if you can't make it, just reapply whenever you want!",
    "Hey {mention}, we'll need to close this application in 24 hours if you haven't joined via '/gu join ESI'. You're more than welcome to apply again whenever you'd like!",
]

_task = None


def _load_json(path, default):
    """Read a JSON object, falling back to ``default`` when it cannot be read."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        print(f"[REMINDERS] Could not read {path.name}: {e}")
        return default
    return data if isinstance(data, dict) else default


def _save_state(state):
    """Persist the reminder state, never raising into the loop."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4)
    except Exception as e:
        print(f"[REMINDERS] Could not save the reminder state: {e}")


def _hours_since(timestamp, now_ts):
    """Return how many hours ago ``timestamp`` was, or None when unusable."""
    try:
        return (now_ts - float(timestamp)) / 3600
    except (TypeError, ValueError):
        return None


def load_guild_members():
    """Return the usernames and UUIDs currently in the tracked guild."""
    data = _load_json(TRACKED_GUILD_FILE, {})
    members = (data.get("previous_data") or {}).get("members") or {}
    usernames, uuids = set(), set()
    for rank_members in members.values():
        if not isinstance(rank_members, list):
            continue
        for member in rank_members:
            if not isinstance(member, dict):
                continue
            username = member.get("username")
            uuid = member.get("uuid")
            if username:
                usernames.add(str(username).lower())
            if uuid:
                uuids.add(str(uuid).lower())
    return usernames, uuids


def _resolve_channel(bot, channel_id):
    """Find a ticket channel or thread by ID."""
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    for guild in bot.guilds:
        thread = guild.get_thread(channel_id)
        if thread is not None:
            return thread
    return None


def _pick(messages, user_id):
    """Pick one of the message variants and fill in the applicant mention."""
    return random.choice(messages).replace("{mention}", f"<@{user_id}>")


async def _send(channel, message):
    """Post a reminder, returning True when it went out."""
    try:
        await channel.send(message)
        return True
    except discord.Forbidden:
        print(f"[REMINDERS] Missing permission to post in {channel.name}")
    except Exception as e:
        print(f"[REMINDERS] Failed to post in {channel.name}: {e}")
    return False


def _applicant_id(openers, pending, channel_id):
    """The user the application is for, falling back to whoever opened the ticket."""
    for app in pending.values():
        if isinstance(app, dict) and app.get("channel_id") == channel_id:
            user_id = app.get("user_id")
            if user_id:
                return user_id
    return openers.get(str(channel_id))


def _recently_filling(pending, channel_id, now_ts):
    """True when progress was saved for this ticket a moment ago."""
    for app in pending.values():
        if not isinstance(app, dict) or app.get("channel_id") != channel_id:
            continue
        hours = _hours_since(app.get("timestamp"), now_ts)
        if hours is not None and hours < FILL_ACTIVITY_GRACE_HOURS:
            return True
    return False


async def _has_submission_embed(channel):
    """True when the bot already posted the submitted application embed here."""
    try:
        async for message in channel.history(limit=50):
            if message.author != channel.guild.me:
                continue
            for embed in message.embeds:
                if embed.title and "Application Submitted" in embed.title:
                    return True
    except Exception as e:
        print(f"[REMINDERS] Could not read the history of {channel.name}: {e}")
    return False


async def _check_fill_reminders(bot, openers, apps, pending, state, now_ts):
    """Nag applicants whose ticket is a day old and still has no application."""
    sent = 0
    submitted_channels = {
        app.get("ticket_channel_id") for app in apps.values() if app.get("ticket_channel_id")
    }

    for channel_id in list(openers):
        try:
            channel_id = int(channel_id)
        except (TypeError, ValueError):
            continue

        entry = state.setdefault(str(channel_id), {})
        if entry.get("fill_reminder_at") or entry.get("submitted_at"):
            continue

        channel = _resolve_channel(bot, channel_id)
        if channel is None:
            continue

        age_hours = _hours_since(channel.created_at.timestamp(), now_ts)
        if age_hours is None or age_hours < FILL_REMINDER_HOURS:
            continue
        if channel_id in submitted_channels:
            continue
        if _recently_filling(pending, channel_id, now_ts):
            continue
        if await _has_submission_embed(channel):
            entry["submitted_at"] = now_ts
            continue

        applicant_id = _applicant_id(openers, pending, channel_id)
        if not applicant_id:
            continue

        if await _send(channel, _pick(FILL_MESSAGES, applicant_id)):
            entry["fill_reminder_at"] = now_ts
            sent += 1
            print(f"[REMINDERS] Fill reminder sent in {channel.name} ({channel_id})")

    return sent


def _in_guild(app, usernames, uuids):
    """True when the accepted account already shows up in the tracked guild."""
    username = str(app.get("accepted_username") or "").lower()
    uuid = str(app.get("accepted_uuid") or "").lower()
    return bool(username and username in usernames) or bool(uuid and uuid in uuids)


async def _reset_member(member):
    """Strip the member's roles and nickname, leaving only the default roles."""
    guild = member.guild
    reason = "Accepted but did not join the guild"
    keep = set(DEFAULT_ROLE_IDS)

    removable = [
        role for role in member.roles
        if role.id not in keep
        and role != guild.default_role
        and not role.managed
        and role < guild.me.top_role
    ]
    try:
        if removable:
            await member.remove_roles(*removable, reason=reason)
            print(f"[REMINDERS] Removed {len(removable)} role(s) from {member}")
    except Exception as e:
        print(f"[REMINDERS] Failed to remove the roles of {member}: {e}")

    try:
        if member.nick:
            await member.edit(nick=None, reason=reason)
    except Exception as e:
        print(f"[REMINDERS] Failed to reset the nickname of {member}: {e}")

    default_roles = [role for role in (guild.get_role(rid) for rid in DEFAULT_ROLE_IDS) if role]
    try:
        missing = [role for role in default_roles if role not in member.roles]
        if missing:
            await member.add_roles(*missing, reason=reason)
    except Exception as e:
        print(f"[REMINDERS] Failed to add the default roles to {member}: {e}")


async def _close_stale_acceptance(channel, app, entry, now_ts):
    """Reset the member and close the ticket after the final reminder expired."""
    from ticket_handler import close_application_channel, get_panel_data_from_channel

    guild = channel.guild
    applicant_id = app.get("user_id")
    member = guild.get_member(applicant_id) if applicant_id else None

    if member is not None:
        await _reset_member(member)
    else:
        print(f"[REMINDERS] Applicant {applicant_id} is not in {guild.name}, closing without a role reset")

    _, panel_data = get_panel_data_from_channel(channel)

    try:
        await close_application_channel(
            channel=channel,
            guild=guild,
            application_name=app.get("app_type") or "Application",
            panel_data=panel_data or {},
            closed_by=guild.me,
            reason=CLOSE_REASON,
        )
        entry["closed_at"] = now_ts
        print(f"[REMINDERS] Closed {channel.name} ({CLOSE_REASON})")
    except Exception as e:
        print(f"[REMINDERS] Failed to close {channel.name}: {e}")


async def _check_join_reminders(bot, apps, state, now_ts, guild_members):
    """Nag accepted members who never joined, then close their ticket."""
    sent = 0
    usernames, uuids = guild_members

    for app in apps.values():
        if not isinstance(app, dict) or not app.get("join_reminders"):
            continue

        accepted_at = app.get("accepted_at")
        channel_id = app.get("ticket_channel_id")
        applicant_id = app.get("user_id")
        if not accepted_at or not channel_id or not applicant_id:
            continue

        try:
            channel_id = int(channel_id)
        except (TypeError, ValueError):
            continue

        entry = state.setdefault(str(channel_id), {})
        if entry.get("closed_at"):
            continue

        hours = _hours_since(accepted_at, now_ts)
        if hours is None or hours < JOIN_REMINDER_HOURS:
            continue
        if _in_guild(app, usernames, uuids):
            continue

        channel = _resolve_channel(bot, channel_id)
        if channel is None:
            continue

        if hours >= JOIN_CLOSE_HOURS:
            await _close_stale_acceptance(channel, app, entry, now_ts)
            sent += 1
            continue

        if hours >= JOIN_FINAL_REMINDER_HOURS and not entry.get("join_reminder_2_at"):
            if await _send(channel, _pick(JOIN_FINAL_MESSAGES, applicant_id)):
                entry["join_reminder_1_at"] = entry.get("join_reminder_1_at") or now_ts
                entry["join_reminder_2_at"] = now_ts
                sent += 1
                print(f"[REMINDERS] Final join reminder sent in {channel.name} ({channel_id})")
            continue

        if not entry.get("join_reminder_1_at"):
            if await _send(channel, _pick(JOIN_REMINDER_MESSAGES, applicant_id)):
                entry["join_reminder_1_at"] = now_ts
                sent += 1
                print(f"[REMINDERS] Join reminder sent in {channel.name} ({channel_id})")

    return sent


def _prune_state(state, openers, apps):
    """Forget tickets that no longer exist."""
    live = set(openers)
    live.update(str(app.get("ticket_channel_id")) for app in apps.values() if app.get("ticket_channel_id"))
    for channel_id in [cid for cid in state if cid not in live]:
        del state[channel_id]


async def run_reminder_check(bot):
    """Run one pass of the fill and join checks. Returns how many messages went out."""
    openers = _load_json(CHANNEL_OPENERS_FILE, {})
    apps = _load_json(FORWARDED_APPS_FILE, {})
    pending = _load_json(PENDING_APPS_FILE, {})
    state = _load_json(STATE_FILE, {})
    now_ts = datetime.now(timezone.utc).timestamp()

    sent = await _check_fill_reminders(bot, openers, apps, pending, state, now_ts)
    sent += await _check_join_reminders(bot, apps, state, now_ts, load_guild_members())

    _prune_state(state, openers, apps)
    _save_state(state)
    return sent


async def reminder_loop(bot):
    """Check for stalled tickets every half hour."""
    await bot.wait_until_ready()
    await asyncio.sleep(FIRST_CHECK_DELAY_SECONDS)
    while True:
        try:
            sent = await run_reminder_check(bot)
            if sent:
                print(f"[REMINDERS] Sent {sent} reminder(s)")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[REMINDERS] Check failed: {e}")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


def teardown(bot):
    """Stop the loop when the module is unloaded."""
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
        print("[OK] Unloaded ticket reminders")


def setup(bot, has_required_role, config):
    """Start the reminder loop."""
    global _task
    teardown(bot)

    _task = bot.loop.create_task(reminder_loop(bot))
    if hasattr(bot, "_background_tasks"):
        bot._background_tasks.append(_task)

    print("[OK] Loaded ticket reminders")
