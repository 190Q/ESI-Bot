import discord
import re
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse


PROTECTED_ROLE_IDS = {}
CHANNEL_RESTRICTED_ROLES = {
    1357064338615304412: [1330955133261189230],
    1491387160039919777: [1330955133261189230],
    1370477368057008220: [1330955133261189230],
    1384811398667702344: [1330955133261189230],
    1370477190524833902: [1330955133261189230],
}
ALERT_CHANNELS = {}
AUTO_ALERT_FIRST_DETECTION_CHANNEL = True
AUTO_ALERT_MESSAGE = "Spam/scam activity detected. Staff has been notified."

LOG_CHANNEL_ID = 1447167603951927347
SIGNAL_THRESHOLD = 3
ACCOUNT_AGE_DAYS_THRESHOLD = 21
MESSAGE_BURST_COUNT = 2
MESSAGE_BURST_WINDOW_SECONDS = 15
TIMEOUT_MINUTES = 10080

IMAGE_FILE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tiff",
    ".svg",
}
URL_REGEX = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)
DISCORD_CDN_HOST_SUFFIXES = (
    "discordapp.com",
    "discordapp.net",
    "discord.com",
)

_listener = None
_message_windows = defaultdict(deque)
_first_detection_channel_id = None
LISTENER_ATTR = "_spam_detection_listeners"


def _trim_user_message_window(user_id: int, now: datetime):
    window = _message_windows[user_id]
    window.append(now)
    cutoff = now - timedelta(seconds=MESSAGE_BURST_WINDOW_SECONDS)
    while window and window[0] < cutoff:
        window.popleft()
    return len(window)

def _is_image_attachment(attachment: discord.Attachment) -> bool:
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    filename = attachment.filename.lower()
    return any(filename.endswith(ext) for ext in IMAGE_FILE_EXTENSIONS)

def _extract_urls(text: str):
    return [match.group(1).strip("()[]{}<>.,!?\"'") for match in URL_REGEX.finditer(text)]

def _is_external_image_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
    except Exception:
        return False

    if not host:
        return False

    has_image_extension = any(path.endswith(ext) for ext in IMAGE_FILE_EXTENSIONS)
    if not has_image_extension:
        return False

    is_discord_cdn = host.endswith(DISCORD_CDN_HOST_SUFFIXES)
    return not is_discord_cdn

def _calculate_signal_score(message: discord.Message, now: datetime):
    triggered_signals = []

    if message.mention_everyone:
        triggered_signals.append("everyone_or_here_ping")

    mentioned_role_ids = {role.id for role in message.role_mentions}
    protected_role_ids_only = PROTECTED_ROLE_IDS - set(CHANNEL_RESTRICTED_ROLES.keys())
    if protected_role_ids_only and any(role_id in protected_role_ids_only for role_id in mentioned_role_ids):
        triggered_signals.append("protected_role_ping")

    if any(_is_image_attachment(attachment) for attachment in message.attachments):
        triggered_signals.append("image_attachment")

    urls = _extract_urls(message.content or "")
    if any(_is_external_image_url(url) for url in urls):
        triggered_signals.append("external_image_url")

    account_age_days = (now - message.author.created_at).total_seconds() / 86400
    if account_age_days < ACCOUNT_AGE_DAYS_THRESHOLD:
        triggered_signals.append("new_account")

    burst_count = _trim_user_message_window(message.author.id, now)
    if burst_count >= MESSAGE_BURST_COUNT:
        triggered_signals.append("message_burst")

    restricted_role_violated = any(
        role_id in CHANNEL_RESTRICTED_ROLES and message.channel.id not in CHANNEL_RESTRICTED_ROLES[role_id]
        for role_id in mentioned_role_ids
    )
    if restricted_role_violated:
        triggered_signals.append("restricted_role_outside_allowed_channel")

    return len(triggered_signals), triggered_signals

async def _send_log(bot: discord.Client, message: discord.Message, signal_count: int, triggered_signals):
    if not LOG_CHANNEL_ID:
        return

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel is None:
        try:
            log_channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        except Exception:
            return

    embed = discord.Embed(
        title="Spam/Scam Enforcement Triggered",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="User", value=f"{message.author} ({message.author.id})", inline=False)
    embed.add_field(name="Channel", value=message.channel.mention, inline=False)
    embed.add_field(name="Signals Triggered", value=str(signal_count), inline=True)
    if triggered_signals:
        embed.add_field(name="Signal Keys", value=", ".join(triggered_signals), inline=False)

    try:
        await log_channel.send(embed=embed)
    except Exception:
        pass

async def _send_alert_messages(bot: discord.Client, message: discord.Message):
    global _first_detection_channel_id

    if AUTO_ALERT_FIRST_DETECTION_CHANNEL and _first_detection_channel_id is None:
        _first_detection_channel_id = message.channel.id
    for channel_id, alert_message in ALERT_CHANNELS.items():
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except Exception:
                continue

        try:
            await channel.send(alert_message)
        except Exception:
            continue

    if (
        AUTO_ALERT_FIRST_DETECTION_CHANNEL
        and _first_detection_channel_id
        and _first_detection_channel_id not in ALERT_CHANNELS
    ):
        auto_channel = bot.get_channel(_first_detection_channel_id)
        if auto_channel is None:
            try:
                auto_channel = await bot.fetch_channel(_first_detection_channel_id)
            except Exception:
                auto_channel = None

        if auto_channel is not None:
            try:
                await auto_channel.send(AUTO_ALERT_MESSAGE)
            except Exception:
                pass

def setup(bot, has_required_role=None, config=None):
    global _listener

    tracked = getattr(bot, LISTENER_ATTR, [])
    for listener in tracked:
        try:
            bot.remove_listener(listener, "on_message")
        except Exception:
            pass
    setattr(bot, LISTENER_ATTR, [])

    extra_events = getattr(bot, "extra_events", {})
    existing_message_listeners = list(extra_events.get("on_message", []))
    for existing_listener in existing_message_listeners:
        if getattr(existing_listener, "__spam_detection_listener__", False):
            try:
                bot.remove_listener(existing_listener, "on_message")
            except Exception:
                pass

    async def on_message(message: discord.Message):
        if message.author.bot or not message.guild:
            return

        now = datetime.now(timezone.utc)
        signal_score, triggered_signals = _calculate_signal_score(message, now)
        if signal_score < SIGNAL_THRESHOLD:
            return

        try:
            await message.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass

        if isinstance(message.author, discord.Member):
            timeout_until = now + timedelta(minutes=TIMEOUT_MINUTES)
            try:
                await message.author.timeout(
                    timeout_until,
                    reason=f"Spam/scam detection triggered ({signal_score} signals)",
                )
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                pass

        await _send_log(bot, message, signal_score, triggered_signals)
        await _send_alert_messages(bot, message)

    on_message.__spam_detection_listener__ = True
    _listener = on_message
    bot.add_listener(on_message, "on_message")
    setattr(bot, LISTENER_ATTR, [on_message])
    print("[OK] Loaded spam detection module")

def teardown(bot):
    global _listener
    tracked = getattr(bot, LISTENER_ATTR, [])
    for listener in tracked:
        try:
            bot.remove_listener(listener, "on_message")
        except Exception:
            pass
    setattr(bot, LISTENER_ATTR, [])

    extra_events = getattr(bot, "extra_events", {})
    existing_message_listeners = list(extra_events.get("on_message", []))
    for existing_listener in existing_message_listeners:
        if getattr(existing_listener, "__spam_detection_listener__", False):
            try:
                bot.remove_listener(existing_listener, "on_message")
            except Exception:
                pass
    if _listener is not None:
        try:
            bot.remove_listener(_listener, "on_message")
        except Exception:
            pass
        _listener = None
    print("[OK] Unloaded spam detection module")
