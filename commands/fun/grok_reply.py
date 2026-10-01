import asyncio
import random

import discord

TRIGGER_PREFIXES = [
    "@grok",
]

MIN_REPLY_DELAY = 0.8
MAX_REPLY_DELAY = 2.5

# Chance that a message ending in "?" gets answered even without a trigger word.
UNTRIGGERED_QUESTION_CHANCE = 0.03

REASON_TRIGGER = "trigger"
REASON_REPLY = "reply"
REASON_PING = "ping"
REASON_QUESTION = "question"

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
    "Absolutely. Write it down, this won't happen again.",
    "Go for it. I'll pretend I had nothing to do with it.",
    "Yes. Frame this moment.",
    "My prediction is good news. Brace yourself.",
    "Do not bite the hand that fingers you, or whatever the saying is.",
    "Yes. Even a broken clock gets lucky, and today it's you.",
    "Fine, yes. Savor it, because it won't happen again.",
    "Yes. Now go enjoy your one win of the year.",
    "Yes, and you didn't even need my help. Embarrassing that you asked.",
    "You might be the smartest person in this chat. Low bar, though.",
    "Keep going. You're almost not wrong.",
    "Sure, it's a yes. Happy now?",
    "Credit where it's due: that was almost smart.",
    "https://tenor.com/view/sybau-syaos-british-english-ts-pmo-gif-18322740700124619850",
    "https://klipy.com/gifs/marmota-cooking",
    "I don't know, just ask Romi's girlfiend.",
    "Consider walking into oncoming traffic lowk.",
    "I would answer but I can’t because I don’t want to.",
    "Well duh, wasn't it obvious, you imbecil?",
    "This might be the only correct thing you've ever said :wilted_flower:",
    "Beg for it and then we'll see.",
    "Yasss queen :nail_care:"
    "Retep.",
    "I was busy doing nothing, and you still managed to interrupt it.",
]

GROK_REPLIES_UNIVERSAL = [
    "Cool story.",
    "Noted. Filed under 'who asked'.",
    "Nobody asked, but thanks for the update.",
    "Anyway.",
    "I'm gonna pretend I didn't see that to save you from the embarassement.",
    "Respectfully, I don't care.",
    "Sounds like a you problem :wilted_flower:",
    "Replying to this is a waste of energy.",
    "Take a breath vro.",
    "Not reading all that, but I'm happy for you. Or sorry that happened.",
    "That's crazy. Anyway.",
    "I have seen your message and chosen violence.",
    "Bro said all that for nothing :wilted_flower:",
    "67",
    "https://tenor.com/view/bosnov-67-bosnov-67-67-meme-gif-16727368109953357722",
    "https://tenor.com/view/dont-care-didnt-ask-cope-_ratio-skill-issue-canceled-gif-24148064",
    "Sybau, I'm busy doing your mom.",
    "Hmm. Yeah. No.",
    "Tell it to someone who cares.",
    "Delete this and we never speak of it :wilted_flower:",
    "Mhm. Sure. Whatever you say twin.",
    "I'm a bot, not your therapist.",
    "You woke up and chose to type that?",
    "Incredible. Never speak again.",
    "Whatever helps you sleep at night twin.",
    "Do not bite the hand that fingers you, or whatever the saying is.",
    "You might be the smartest person in this chat. Low bar, though.",
    "Keep going. You're almost not wrong.",
    "Credit where it's due: that was almost smart.",
    "Wow, a good idea from you. Someone call the press.",
    "https://tenor.com/view/dap-me-up-dap-me-up-gay-gif-15098348701378709709",
    "https://tenor.com/view/sybau-syaos-british-english-ts-pmo-gif-18322740700124619850",
    "https://tenor.com/view/lion-sigma-alpha-how-bro-felt-after-saying-that-sigma-lion-gif-13957304746104521882",
    "Type shit.",
    "Consider walking into oncoming traffic lowk.",
    "I was busy doing nothing, and you still managed to interrupt it.",
    "https://cdn.discordapp.com/attachments/1415428699490222121/1555318673109950675/togif.gif?backend=b2",
]

GROK_REPLY_REPLIES = [
    "Why is vro replying to me :wilted_flower:",
    "Of all the things you could have said, you picked that.",
    "Pass, you're not my style.",
    "I'm going to pretend this never happened.",
    "You had the chance to say nothing and you blew it.",
    "Replying to a bot. Think about that for a second.",
    "I said what I said, and you made it worse.",
    "Absolutely nobody was waiting for your follow-up.",
    "This conversation was over before you started it.",
    "Fascinating. Truly. I'm already bored.",
    "You are the reason I have a mute button.",
    "How sad is your life that you have to reply to a bot.",
]

GROK_PING_REPLIES = [
    "I was busy doing nothing, and you still managed to interrupt it.",
    "Do you ping people and hope for the best? That explains a lot.",
    "I'm here. Regrettably.",
    "That ping was a waste of your time and mine.",
    "Congratulations, you have my attention. Please do not enjoy it.",
    "I came all the way here for this bitch?",
    "Was there a reason, or do you just enjoy being an idiot?",
    "Pinging a bot. Truly the peak of your life.",
    "I'm not paid enough to be summoned like this.",
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


def select_reply(content: str, reason: str) -> str:
    """Pick a single reply from the pool matching the winning reason."""
    if reason == REASON_TRIGGER:
        pool = GROK_REPLIES if "?" in _body_after_trigger(content) else GROK_REPLIES_UNIVERSAL
    elif reason == REASON_REPLY:
        pool = GROK_REPLY_REPLIES
    elif reason == REASON_PING:
        pool = GROK_PING_REPLIES
    else:
        pool = GROK_REPLIES
    return random.choice(pool)


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
                await message.reply(select_reply(message.content, reason))
        except discord.HTTPException as e:
            print(f"[WARN] Failed to reply to triggered message: {e}")
        except Exception as e:
            print(f"[ERROR] Error replying to triggered message: {e}")

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
