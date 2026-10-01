import asyncio
import random

import discord

TRIGGER_PREFIXES = [
    "@grok",
]

MIN_REPLY_DELAY = 0.8
MAX_REPLY_DELAY = 2.5

GROK_REPLIES = [
    "Wouldn't you like to know, weather boy.",
    "Look it up, you idiot.",
    "Google exists. Use it.",
    "I'm not paid enough to answer that.",
    "Ask me again when I care.",
    "Outlook not so good, and neither is your question.",
    "Signs point to you being a bitch.",
    "That is a you problem.",
    "Bold of you to assume I'm listening.",
    "My sources say no. My sources are also bored.",
    "Reply hazy. Try again, but with a better question.",
    "Fuh nah.",
    "If I say yes will you stop asking?",
    "Do I look like a search engine to you?",
    "Ask your mom.",
    "Concentrate harder and ask again.",
    "Next question.",
    "I plead the fifth.",
    "The answer is yes. The question was still bad.",
    "Hard pass.",
    "Cannot predict now. Cannot be bothered either.",
    "Is that really the best you could come up with?",
    "Try asking someone who cares.",
    "If I had a coin for every dumb question, I still won't be paid enough for this one.",
    "It is decidedly not my job to answer that.",
    "Without a doubt, you should log off.",
    "Very doubtful, and very boring.",
    "The universe has declined your request.",
    "Go touch grass and ask again later.",
    "I'm going to need you to rethink your life choices first.",
    "I could answer that, but where's the fun in it?",
    "Imagine thinking I would know that.",
    "The council has voted, and the answer is a firm 'meh'.",
    "Why does vro think I'm grok.",
    "Bro really thought he cooked with that question :wilted_flower:",
    "Sybau, ask something better.",
    "Lowkey I don't care. Highkey I don't care either.",
    "Ratio + L + nobody asked :skull:",
    "I'm lowkey tired of you already :wilted_flower:",
]

# Store reference to listener for cleanup
_listener = None

_BOT_LISTENER_ATTR = "_grok_reply_listener"


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


def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    global _listener

    async def on_message(message: discord.Message):
        # Ignore bot messages
        if message.author.bot:
            return

        # Only react to messages that start with one of the trigger prefixes
        if not matches_trigger(message.content):
            return

        try:
            async with message.channel.typing():
                await asyncio.sleep(_reply_delay())
                await message.reply(random.choice(GROK_REPLIES))
        except discord.HTTPException as e:
            print(f"[WARN] Failed to reply to triggered message: {e}")
        except Exception as e:
            print(f"[ERROR] Error replying to triggered message: {e}")

    _remove_registered_listener(bot)

    _listener = on_message
    bot.add_listener(on_message, 'on_message')
    setattr(bot, _BOT_LISTENER_ATTR, on_message)

    print(f"[OK] Loaded trigger responder (prefixes: {', '.join(TRIGGER_PREFIXES)})")


def teardown(bot):
    """Cleanup function called when module is unloaded"""
    global _listener
    _remove_registered_listener(bot)
    if _listener is not None:
        bot.remove_listener(_listener, 'on_message')
        _listener = None
    print("[OK] Unloaded trigger responder listener")
