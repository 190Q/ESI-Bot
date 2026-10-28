import asyncio
import json
import random

import discord

from utils import errors
from utils.paths import CONFIG_DIR, DATA_DIR
from utils.usage import record_feature
from collections import deque

TRIGGER_PREFIXES = [
    "@grok",
]

MIN_REPLY_DELAY = 0.8
MAX_REPLY_DELAY = 4.0
CHARS_PER_SECOND = 18.0

RECENT_REPLY_MEMORY = 15
_RECENT_REPLIES = deque(maxlen=RECENT_REPLY_MEMORY)

# How many of our own sent replies stay eligible for reactions and follow-ups.
RECENT_SENT_MEMORY = 200
_SENT_REPLIES: dict[int, dict] = {}

# Recent traffic per channel, used to back off when the bot already talks a lot.
ACTIVITY_WINDOW = 20
MIN_REPLY_CHANCE = 1.0
_CHANNEL_ACTIVITY: dict[int, deque] = {}

# How many channels keep a mood, and how often answering our own reply keeps it.
CHANNEL_MEMORY = 500
CHAIN_MOOD_CHANCE = 0.7
_CHANNEL_MOODS: dict[int, str] = {}

# Learned weights: praise raises a reply, every use lowers it a little.
REACTION_REWARD = 1.0
REPLY_REWARD = 0.6
USE_COST = 0.15
MIN_REPLY_WEIGHT = 0.25
MAX_REPLY_WEIGHT = 6.0
FRESH_PENALTY = 0.2
_STATS: dict[str, dict] = {}

REASON_TRIGGER = "trigger"
REASON_REPLY = "reply"
REASON_PING = "ping"

REPLIES_PATH = CONFIG_DIR / "grok_replies.json"
MOODS_PATH = CONFIG_DIR / "grok_moods.json"
STATS_PATH = DATA_DIR / "grok_reply_stats.json"

REPLY_POOL_NAMES = (
    "shared",
    "empty_trigger",
    "question",
    "statement",
    "reply_to_bot",
    "ping",
    "nonsense",
)

# Every pool except nonsense is weighted per mood in grok_moods.json.
MOOD_POOL_NAMES = tuple(name for name in REPLY_POOL_NAMES if name != "nonsense")

DEFAULT_MOODS = {
    "moods": {
        "default": {
            "pools": {name: 1.0 for name in MOOD_POOL_NAMES},
            "nonsense": 0.02,
            "reply_chance": 1.0,
            "delay": 1.0,
            "stay": 1.0,
            "next": {},
        },
    },
}


def _number(value, default: float) -> float:
    """Read a number out of the config, falling back when it is not one."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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


def load_moods() -> dict:
    """Read the mood table from config/grok_moods.json.

    A missing or broken file falls back to a single flat mood, so the
    responder keeps answering instead of crashing.
    """
    try:
        with open(MOODS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[WARN] Failed to load {MOODS_PATH}: {e}")
        return DEFAULT_MOODS

    entries = raw.get("moods") if isinstance(raw, dict) else None
    if not isinstance(entries, dict) or not entries:
        print(f"[WARN] {MOODS_PATH.name} holds no moods; using the default mood")
        return DEFAULT_MOODS

    fallback_stay = _number(raw.get("stay_probability"), 0.7)
    moods = {}
    for name, entry in entries.items():
        if not isinstance(entry, dict):
            print(f"[WARN] Mood '{name}' in {MOODS_PATH.name} is not an object; ignoring it")
            continue
        weights = entry.get("pools") if isinstance(entry.get("pools"), dict) else {}
        drift = entry.get("next") if isinstance(entry.get("next"), dict) else {}
        moods[name] = {
            "pools": {pool: _number(weights.get(pool), 0.0) for pool in MOOD_POOL_NAMES},
            "nonsense": _number(entry.get("nonsense"), 0.0),
            "reply_chance": _number(entry.get("reply_chance"), 1.0),
            "delay": _number(entry.get("delay"), 1.0),
            "stay": _number(entry.get("stay"), fallback_stay),
            "next": {other: _number(drift.get(other), 0.0) for other in drift if other in entries},
        }

    if not moods:
        return DEFAULT_MOODS
    return {"moods": moods}


def load_reply_stats() -> dict:
    """Read the learned reply weights, starting empty when there are none yet."""
    try:
        with open(STATS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        print(f"[WARN] Failed to load {STATS_PATH}: {e}")
        return {}

    if not isinstance(raw, dict):
        return {}
    return {name: stats for name, stats in raw.items() if isinstance(stats, dict)}


def save_reply_stats(stats: dict) -> None:
    """Write the learned weights; a failure here never stops a reply."""
    try:
        STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(STATS_PATH, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"[WARN] Failed to save {STATS_PATH}: {e}")


_BOT_LISTENER_ATTR = "_grok_reply_listeners"


def _reply_id(reply) -> str:
    """Stable name for a reply, used for recency and for the learned weights.

    Error replies are rebuilt from the JSON on every load, so they are named by
    their title and description rather than by object identity.
    """
    if isinstance(reply, errors.CommandError):
        return f"error|{reply.title}|{reply.description}"
    return reply


def _reply_weight(reply) -> float:
    """How much a reply has earned so far, between the floor and the ceiling."""
    stats = _STATS.get(_reply_id(reply))
    if not stats:
        return 1.0
    rewards = _number(stats.get("rewards"), 0.0)
    uses = int(_number(stats.get("uses"), 0))
    return max(MIN_REPLY_WEIGHT, min(1.0 + rewards - uses * USE_COST, MAX_REPLY_WEIGHT))


def _freshness(reply) -> float:
    """A reply used a moment ago is still possible, just unlikely."""
    return FRESH_PENALTY if _reply_id(reply) in _RECENT_REPLIES else 1.0


def _reward_reply(reply_id: str, amount: float) -> None:
    """Add praise to a reply and save what has been learned so far."""
    stats = _STATS.setdefault(reply_id, {"rewards": 0.0, "uses": 0})
    stats["rewards"] = round(_number(stats.get("rewards"), 0.0) + amount, 3)
    save_reply_stats(_STATS)


def _record_use(reply) -> None:
    """Count one send of a reply, so untouched ones keep their weight."""
    stats = _STATS.setdefault(_reply_id(reply), {"rewards": 0.0, "uses": 0})
    stats["uses"] = int(_number(stats.get("uses"), 0)) + 1
    save_reply_stats(_STATS)


def _remember(mapping: dict, key, value) -> None:
    """Keep a per-channel value, dropping the oldest ones once there are many."""
    mapping[key] = value
    while len(mapping) > CHANNEL_MEMORY:
        mapping.pop(next(iter(mapping)))


def _record_activity(channel_id: int, is_bot: bool) -> None:
    """Track how much of a channel's recent traffic came from this bot."""
    activity = _CHANNEL_ACTIVITY.get(channel_id)
    if activity is None:
        activity = deque(maxlen=ACTIVITY_WINDOW)
        _CHANNEL_ACTIVITY[channel_id] = activity
        while len(_CHANNEL_ACTIVITY) > CHANNEL_MEMORY:
            _CHANNEL_ACTIVITY.pop(next(iter(_CHANNEL_ACTIVITY)))
    activity.append(is_bot)


def _should_reply(mood: dict, channel_id: int) -> bool:
    """Answer less often the more of the channel's recent traffic is ours."""
    activity = _CHANNEL_ACTIVITY.get(channel_id)
    bot_share = sum(activity) / len(activity) if activity else 0.0
    chance = _number(mood.get("reply_chance"), 1.0) * (1.0 - bot_share)
    return random.random() < max(chance, MIN_REPLY_CHANCE)


def _current_mood(channel_id: int, moods: dict) -> str:
    """The mood a channel is in, picked at random the first time it is seen."""
    mood = _CHANNEL_MOODS.get(channel_id)
    if mood in moods["moods"]:
        return mood

    mood = random.choice(list(moods["moods"]))
    _remember(_CHANNEL_MOODS, channel_id, mood)
    return mood


def _next_mood(current: str, moods: dict) -> str:
    """Keep the mood, or drift to another one with the weights from the file."""
    mood = moods["moods"].get(current, {})
    if random.random() < _number(mood.get("stay"), 0.7):
        return current

    weights = mood.get("next") or {}
    names = [name for name, weight in weights.items() if name != current and weight > 0]
    if not names:
        names = [name for name in moods["moods"] if name != current]
        weights = {name: 1.0 for name in names}
    if not names:
        return current
    return random.choices(names, weights=[weights[name] for name in names], k=1)[0]


def _remember_sent(message_id: int, reply_id: str, mood: str) -> None:
    """Remember a reply we sent, so reactions and follow-ups can find it."""
    _SENT_REPLIES[message_id] = {"reply": reply_id, "mood": mood, "engagers": set()}
    while len(_SENT_REPLIES) > RECENT_SENT_MEMORY:
        _SENT_REPLIES.pop(next(iter(_SENT_REPLIES)))


def _reward_engagement(message_id: int, user_id: int, amount: float) -> None:
    """Credit the reply behind a message, once per person."""
    entry = _SENT_REPLIES.get(message_id)
    if entry is None or user_id in entry["engagers"]:
        return
    entry["engagers"].add(user_id)
    _reward_reply(entry["reply"], amount)


def _carry_on_chain(message: discord.Message) -> None:
    """Answering one of our own replies usually keeps the mood it was sent in."""
    reference = message.reference
    entry = _SENT_REPLIES.get(reference.message_id) if reference else None
    if entry is None:
        return

    _reward_engagement(reference.message_id, message.author.id, REPLY_REWARD)
    if random.random() < CHAIN_MOOD_CHANCE:
        _remember(_CHANNEL_MOODS, message.channel.id, entry["mood"])


def _remove_registered_listeners(bot):
    """Remove the listeners this module registered previously, if any.

    bot.add_listener only appends to bot.extra_events, so registering twice
    would make the bot reply twice to every triggered message. remove_listener
    is a no-op when the listener is already gone.
    """
    for event_name, listener in getattr(bot, _BOT_LISTENER_ATTR, []):
        try:
            bot.remove_listener(listener, event_name)
        except Exception:
            pass
    setattr(bot, _BOT_LISTENER_ATTR, [])


def _reply_length(reply) -> int:
    """Roughly how much there is to type in a reply."""
    if isinstance(reply, str):
        return len(reply)
    return len(getattr(reply, "description", "") or "")


def _reply_delay(reply_length: int, mood: dict) -> float:
    """Type at a human-ish speed, slower in a slow mood and never forever."""
    typed = MIN_REPLY_DELAY + reply_length / CHARS_PER_SECOND
    return min(typed * _number(mood.get("delay"), 1.0), MAX_REPLY_DELAY)


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


async def _is_ping(message: discord.Message, bot_user: discord.ClientUser) -> bool:
    """Return True if the message mentions the bot on purpose.

    Replying to a message makes Discord add a mention of its author, so
    replying to any of the bot's messages (a command output, a panel, ...)
    would otherwise look like a ping. Those mentions do not count.
    """
    if not any(user.id == bot_user.id for user in message.mentions):
        return False

    reference = message.reference
    if reference is None:
        return True

    resolved = reference.resolved
    if resolved is None:
        try:
            resolved = await message.channel.fetch_message(reference.message_id)
        except discord.HTTPException:
            return True

    author = getattr(resolved, "author", None)
    return author is None or author.id != bot_user.id


def _is_reply_to_grok_reply(message: discord.Message) -> bool:
    """Return True if the message replies to a reply this responder sent.

    Only the replies sent since the bot started are remembered, so replying to
    any other bot output (a command result, a panel, a log message, ...) is
    ignored.
    """
    reference = message.reference
    if reference is None:
        return False
    return reference.message_id in _SENT_REPLIES


async def detect_reply_reason(message: discord.Message, bot_user: discord.ClientUser) -> str | None:
    """Return the single reason to reply, or None to stay quiet.

    Reasons are checked in priority order, so a message that matches several
    conditions still produces exactly one reason - and one reply.
    """
    if matches_trigger(message.content):
        return REASON_TRIGGER
    if _is_reply_to_grok_reply(message):
        return REASON_REPLY
    if await _is_ping(message, bot_user):
        return REASON_PING
    return None


def _pools_for_reason(content: str, reason: str) -> list[str]:
    """The pools a reason allows, before the mood weighs them."""
    if reason == REASON_TRIGGER:
        body = _body_after_trigger(content).strip()
        if not body:
            return ["empty_trigger"]
        return ["question", "shared"] if "?" in body else ["statement", "shared"]
    if reason == REASON_REPLY:
        return ["reply_to_bot", "shared"]
    return ["ping", "shared"]


def select_reply(content: str, reason: str, mood: dict) -> str | errors.CommandError | None:
    """Pick one reply, weighted by the mood, the reason and past reactions.

    Returns None when the reply file has nothing usable to offer.
    """
    pools = load_reply_pools()
    pool_weights = mood.get("pools", {})
    candidates = []
    weights = []

    for pool_name in _pools_for_reason(content, reason):
        pool_weight = _number(pool_weights.get(pool_name), 1.0)
        for reply in pools[pool_name]:
            candidates.append(reply)
            weights.append(pool_weight * _reply_weight(reply) * _freshness(reply))

    for reply in pools["nonsense"]:
        candidates.append(reply)
        weights.append(_number(mood.get("nonsense"), 0.0) * _reply_weight(reply) * _freshness(reply))

    if not candidates or sum(weights) <= 0:
        return None

    chosen = random.choices(candidates, weights=weights, k=1)[0]
    _RECENT_REPLIES.append(_reply_id(chosen))
    return chosen


def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    global _STATS

    _STATS = load_reply_stats()

    async def on_message(message: discord.Message):
        channel_id = message.channel.id
        _record_activity(channel_id, message.author.id == bot.user.id)

        # Ignore bot messages
        if message.author.bot:
            return

        try:
            # At most one reason comes back, so only one reply is ever sent.
            reason = await detect_reply_reason(message, bot.user)
            if reason is None:
                return

            if reason == REASON_REPLY:
                _carry_on_chain(message)

            moods = load_moods()
            mood_name = _current_mood(channel_id, moods)
            mood = moods["moods"][mood_name]

            if not _should_reply(mood, channel_id):
                return

            reply = select_reply(message.content, reason, mood)
            if reply is None:
                print(f"[WARN] No replies available in {REPLIES_PATH.name}; skipping reply")
                return

            async with message.channel.typing():
                await asyncio.sleep(_reply_delay(_reply_length(reply), mood))
                if isinstance(reply, errors.CommandError):
                    sent = await message.reply(embed=reply.build_embed())
                else:
                    sent = await message.reply(reply)

            _remember_sent(sent.id, _reply_id(reply), mood_name)
            _record_use(reply)
            _remember(_CHANNEL_MOODS, channel_id, _next_mood(mood_name, moods))

            record_feature(
                "Grok replies",
                user_id=message.author.id,
                guild_id=message.guild.id if message.guild else None,
            )
        except discord.HTTPException as e:
            print(f"[WARN] Failed to reply to triggered message: {e}")
        except Exception as e:
            print(f"[ERROR] Error replying to triggered message: {e}")

    async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
        """Any emoji from anyone else counts as a reaction to the reply."""
        if payload.user_id == bot.user.id:
            return
        _reward_engagement(payload.message_id, payload.user_id, REACTION_REWARD)

    pools = load_reply_pools()
    total_replies = sum(len(replies) for replies in pools.values())
    if total_replies:
        print(f"[OK] Loaded {total_replies} replies from {REPLIES_PATH.name}")
    else:
        print(f"[WARN] No replies loaded from {REPLIES_PATH} - the responder will stay quiet")

    moods = load_moods()
    print(f"[OK] Loaded {len(moods['moods'])} moods from {MOODS_PATH.name}")
    if _STATS:
        print(f"[OK] Loaded {len(_STATS)} learned reply weights from {STATS_PATH.name}")

    _remove_registered_listeners(bot)

    bot.add_listener(on_message, "on_message")
    bot.add_listener(on_raw_reaction_add, "on_raw_reaction_add")
    setattr(
        bot,
        _BOT_LISTENER_ATTR,
        [("on_message", on_message), ("on_raw_reaction_add", on_raw_reaction_add)],
    )

    print(f"[OK] Loaded trigger responder (prefixes: {', '.join(TRIGGER_PREFIXES)}; moods, reaction feedback, replies to its own replies)")


def teardown(bot):
    """Cleanup function called when module is unloaded"""
    _remove_registered_listeners(bot)
    print("[OK] Unloaded trigger responder listeners")
