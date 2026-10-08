import asyncio
import json
import random

import discord

from utils import errors
from utils.paths import CONFIG_DIR
from utils.usage import record_feature
from collections import deque

TRIGGER_PREFIXES = [
    "@grok",
]

MIN_REPLY_DELAY = 0.8
MAX_REPLY_DELAY = 2.5
RECENT_REPLY_MEMORY = 15
_RECENT_REPLIES = deque(maxlen=RECENT_REPLY_MEMORY)

# Chance that a message ending in "?" gets answered even without a trigger word.
UNTRIGGERED_QUESTION_CHANCE = 0.003

# Chance that a nonsense reply is sent instead of a real one.
NONSENSE_REPLY_CHANCE = 0.01

REASON_TRIGGER = "trigger"
REASON_REPLY = "reply"
REASON_PING = "ping"
REASON_QUESTION = "question"

REPLIES_PATH = CONFIG_DIR / "grok_replies.json"

REPLY_POOL_NAMES = (
    "shared",
    "empty_trigger",
    "question",
    "statement",
    "reply_to_bot",
    "ping",
    "nonsense",
)


def _parse_reply(entry) -> str | errors.CommandError | None:
    """Turn one JSON entry into a reply, or None if it cannot be understood.

    Entries are normally plain strings. An entry shaped like
    ``{"type": "error", "title": ..., "description": ...}`` sends one of the
    bot's error embeds instead of text.
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and entry.get("type") == "error":
        return errors.custom(entry.get("title", "Error"), entry.get("description", ""))
    print(f"[WARN] Skipping unsupported reply entry in {REPLIES_PATH.name}: {entry!r}")
    return None


def load_reply_pools() -> dict[str, list]:
    """Read every reply pool from the JSON file.

    Pools that are missing, malformed or unreadable come back empty, and the
    responder then stays quiet for them instead of crashing.
    """
    pools = {name: [] for name in REPLY_POOL_NAMES}
    try:
        with open(REPLIES_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[WARN] Failed to load {REPLIES_PATH}: {e}")
        return pools

    if not isinstance(raw, dict):
        print(f"[WARN] {REPLIES_PATH.name} must contain a JSON object of reply pools")
        return pools

    for name in REPLY_POOL_NAMES:
        entries = raw.get(name, [])
        if not isinstance(entries, list):
            print(f"[WARN] Reply pool '{name}' in {REPLIES_PATH.name} is not a list; ignoring it")
            continue
        pools[name] = [reply for entry in entries if (reply := _parse_reply(entry)) is not None]
    return pools

# Store reference to listener for cleanup
_listener = None

_BOT_LISTENER_ATTR = "_grok_reply_listener"


def _reply_key(reply) -> tuple:
    """Stable identity for recency tracking.

    Error replies are rebuilt from the JSON on every load, so they have to be
    compared by their title and description rather than by object identity.
    """
    if isinstance(reply, errors.CommandError):
        return ("error", reply.title, reply.description)
    return ("text", reply)


def _pick_fresh(pool: list) -> str | errors.CommandError | None:
    """Pick a reply from *pool*, preferring ones that were not used recently."""
    if not pool:
        return None
    recent = set(_RECENT_REPLIES)
    fresh = [reply for reply in pool if _reply_key(reply) not in recent]
    choice = random.choice(fresh or pool)
    _RECENT_REPLIES.append(_reply_key(choice))
    return choice

def _remove_registered_listener(bot):
    """Remove the on_message listener this module registered previously, if any.

    bot.add_listener only appends to bot.extra_events['on_message'], so
    registering twice would make the bot reply twice to every triggered
    message. remove_listener is a no-op when the listener is already gone.
    """
    previous = getattr(bot, _BOT_LISTENER_ATTR, None)
    if previous is not None:
        bot.remove_listener(previous, 'on_message')
        setattr(bot, _BOT_LISTENER_ATTR, None)


def _reply_delay() -> float:
    """Random human-ish pause before answering."""
    return random.uniform(MIN_REPLY_DELAY, MAX_REPLY_DELAY)


def matches_trigger(content: str) -> bool:
    """Return True if the message starts with any configured trigger prefix."""
    stripped = content.lstrip().lower()
    return any(stripped.startswith(prefix.lower()) for prefix in TRIGGER_PREFIXES)


def _body_after_trigger(content: str) -> str:
    """Everything the user typed after the matched trigger prefix."""
    stripped = content.lstrip()
    lowered = stripped.lower()
    for prefix in TRIGGER_PREFIXES:
        if lowered.startswith(prefix.lower()):
            return stripped[len(prefix):]
    return ""


def _is_ping(message: discord.Message, bot_user: discord.ClientUser) -> bool:
    """Return True if the message mentions the bot."""
    return any(user.id == bot_user.id for user in message.mentions)


async def _is_reply_to_bot(message: discord.Message, bot_user: discord.ClientUser) -> bool:
    """Return True if the message replies to one of the bot's messages."""
    reference = message.reference
    if reference is None:
        return False

    resolved = reference.resolved
    if resolved is None:
        try:
            resolved = await message.channel.fetch_message(reference.message_id)
        except discord.HTTPException:
            return False

    author = getattr(resolved, "author", None)
    return author is not None and author.id == bot_user.id


def _should_answer_untriggered_question(content: str) -> bool:
    """Rarely answer a question mark message that never used a trigger word."""
    if not content.rstrip().endswith("?"):
        return False
    return random.random() < UNTRIGGERED_QUESTION_CHANCE


async def detect_reply_reason(message: discord.Message, bot_user: discord.ClientUser) -> str | None:
    """Return the single reason to reply, or None to stay quiet.

    Reasons are checked in priority order, so a message that matches several
    conditions still produces exactly one reason - and one reply.
    """
    if matches_trigger(message.content):
        return REASON_TRIGGER
    if await _is_reply_to_bot(message, bot_user):
        return REASON_REPLY
    if _is_ping(message, bot_user):
        return REASON_PING
    if _should_answer_untriggered_question(message.content):
        return REASON_QUESTION
    return None


def select_reply(content: str, reason: str) -> str | errors.CommandError | None:
    """Pick a single reply from the pool matching the winning reason.

    Rarely, a nonsense reply is returned instead of a real one. Returns None
    when the reply file has nothing usable for the chosen pool.
    """
    pools = load_reply_pools()

    if random.random() < NONSENSE_REPLY_CHANCE:
        return _pick_fresh(pools["nonsense"])

    if reason == REASON_TRIGGER:
        body = _body_after_trigger(content).strip()
        if not body:
            return _pick_fresh(pools["empty_trigger"])
        pool = pools["question"] if "?" in body else pools["statement"]
    elif reason == REASON_REPLY:
        pool = pools["reply_to_bot"]
    elif reason == REASON_PING:
        pool = pools["ping"]
    else:
        pool = pools["question"]
    return _pick_fresh(pool + pools["shared"])


def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    global _listener

    async def on_message(message: discord.Message):
        # Ignore bot messages
        if message.author.bot:
            return

        try:
            # At most one reason comes back, so only one reply is ever sent.
            reason = await detect_reply_reason(message, bot.user)
            if reason is None:
                return

            async with message.channel.typing():
                await asyncio.sleep(_reply_delay())
                reply = select_reply(message.content, reason)
                if reply is None:
                    print(f"[WARN] No replies available in {REPLIES_PATH.name}; skipping reply")
                    return
                if isinstance(reply, errors.CommandError):
                    await message.reply(embed=reply.build_embed())
                else:
                    await message.reply(reply)

            record_feature(
                "Grok replies",
                user_id=message.author.id,
                guild_id=message.guild.id if message.guild else None,
            )
        except discord.HTTPException as e:
            print(f"[WARN] Failed to reply to triggered message: {e}")
        except Exception as e:
            print(f"[ERROR] Error replying to triggered message: {e}")

    pools = load_reply_pools()
    total_replies = sum(len(replies) for replies in pools.values())
    if total_replies:
        print(f"[OK] Loaded {total_replies} replies from {REPLIES_PATH.name}")
    else:
        print(f"[WARN] No replies loaded from {REPLIES_PATH} - the responder will stay quiet")

    _remove_registered_listener(bot)

    _listener = on_message
    bot.add_listener(on_message, 'on_message')
    setattr(bot, _BOT_LISTENER_ATTR, on_message)

    print(f"[OK] Loaded trigger responder (prefixes: {', '.join(TRIGGER_PREFIXES)}; replies to bot messages and pings)")


def teardown(bot):
    """Cleanup function called when module is unloaded"""
    global _listener
    _remove_registered_listener(bot)
    if _listener is not None:
        bot.remove_listener(_listener, 'on_message')
        _listener = None
    print("[OK] Unloaded trigger responder listener")
