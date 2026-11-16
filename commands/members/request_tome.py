import asyncio
import sqlite3
import uuid
from contextlib import closing

import aiohttp
import discord
from discord import app_commands

from commands.members.get_uniform import get_minecraft_username_from_discord
from utils import errors
from utils.paths import DB_DIR

CITIZEN_ROLE_ID = 554889169705500672
QUEUE_CHANNEL_ID = 815678688116867092
QUEUE_THREAD_ID = 1450163595923947663
APPROVE_EMOJI_ID = 820858331811020833
DB_PATH = DB_DIR / "tome_requests.db"

APPROVAL_NAMES = {
    "accept", "accepted", "approve", "approved", "approval",
    "check", "checkmark", "check_mark", "green_check", "green_checkmark",
    "green_check_mark", "checkmark_green", "check_mark_green", "green_tick",
    "tick", "tickmark", "tick_mark", "white_check_mark",
    "yes", "done", "thumbsup", "thumbs_up",
}
APPROVAL_UNICODE = {"✅", "✔", "☑", "✓", "🗸", "👍"}
LISTENERS_ATTR = "_tome_request_listeners"
STARTUP_TASK_ATTR = "_tome_request_startup_task"
LOCK_ATTR = "_tome_request_lock"


def _init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tome_requests ("
            "user_id INTEGER PRIMARY KEY, username TEXT NOT NULL, "
            "nonce TEXT NOT NULL UNIQUE, message_id INTEGER UNIQUE)"
        )


def _reserve_request(user_id, username, nonce):
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO tome_requests (user_id, username, nonce) VALUES (?, ?, ?)",
            (user_id, username, nonce),
        )
        return cursor.rowcount == 1


def _save_message_id(user_id, message_id):
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute(
            "UPDATE tome_requests SET message_id = ? WHERE user_id = ?",
            (message_id, user_id),
        )


def _save_message_from_event(nonce, username, message_id):
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute(
            "UPDATE tome_requests SET message_id = ? "
            "WHERE nonce = ? AND username = ? AND message_id IS NULL",
            (message_id, nonce, username),
        )


def _remove_unsent_request(user_id):
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute(
            "DELETE FROM tome_requests WHERE user_id = ? AND message_id IS NULL",
            (user_id,),
        )


def _approve_request(message_id):
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute("DELETE FROM tome_requests WHERE message_id = ?", (message_id,))


def _pending_message_ids():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return [
            row[0] for row in conn.execute(
                "SELECT message_id FROM tome_requests WHERE message_id IS NOT NULL"
            )
        ]


def _is_approval(emoji):
    emoji_id = getattr(emoji, "id", None)
    name = emoji if isinstance(emoji, str) else getattr(emoji, "name", "")
    if emoji_id is not None:
        return emoji_id == APPROVE_EMOJI_ID or (name or "").lower() in APPROVAL_NAMES
    name = (name or "").replace("\ufe0f", "")
    if name.startswith("👍") and len(name) == 2 and "\U0001f3fb" <= name[1] <= "\U0001f3ff":
        name = "👍"
    return name in APPROVAL_UNICODE


async def _get_queue_thread(bot):
    thread = bot.get_channel(QUEUE_THREAD_ID)
    if thread is None:
        thread = await bot.fetch_channel(QUEUE_THREAD_ID)
    if not isinstance(thread, discord.Thread) or thread.parent_id != QUEUE_CHANNEL_ID:
        raise ValueError("Tome Queue is not a thread in the expected channel")
    return thread


def teardown(bot):
    for name, listener in getattr(bot, LISTENERS_ATTR, []):
        bot.remove_listener(listener, name)
    setattr(bot, LISTENERS_ATTR, [])
    task = getattr(bot, STARTUP_TASK_ATTR, None)
    if task is not None and not task.done():
        task.cancel()
    setattr(bot, STARTUP_TASK_ATTR, None)


def setup(bot):
    _init_db()
    teardown(bot)
    request_lock = getattr(bot, LOCK_ATTR, None)
    if request_lock is None:
        request_lock = asyncio.Lock()
        setattr(bot, LOCK_ATTR, request_lock)
    reconcile_lock = asyncio.Lock()

    @bot.tree.command(name="request_tome", description="Request a tome")
    @app_commands.guild_only()
    async def request_tome(interaction: discord.Interaction):
        if not interaction.guild or not any(
            role.id == CITIZEN_ROLE_ID for role in getattr(interaction.user, "roles", ())
        ):
            await errors.NO_PERMISSION.send(interaction)
            return

        username = get_minecraft_username_from_discord(interaction.user.id)
        if not isinstance(username, str) or not username.strip():
            await errors.NO_LINKED_ACCOUNT.send(interaction)
            return
        username = username.strip()
        await interaction.response.defer(ephemeral=True)

        try:
            thread = await _get_queue_thread(bot)
            if thread.guild.id != interaction.guild.id:
                raise ValueError("Tome Queue is in a different server")
        except (aiohttp.ClientError, discord.HTTPException, OSError, ValueError) as exc:
            print(f"[TOME] Failed to find queue thread: {exc}")
            await errors.send_custom_error(
                interaction, "Queue Unavailable", "The tome queue is currently unavailable."
            )
            return

        async with request_lock:
            nonce = uuid.uuid4().hex[:24]
            try:
                reserved = _reserve_request(interaction.user.id, username, nonce)
            except sqlite3.Error as exc:
                print(f"[TOME] Failed to reserve request: {exc}")
                await errors.DATABASE_ERROR.send(interaction)
                return

            if not reserved:
                await errors.send_custom_error(
                    interaction, "Request Pending",
                    "You already have a tome request waiting for approval.",
                )
                return

            try:
                message = await thread.send(
                    username, nonce=nonce, allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.HTTPException as exc:
                if 400 <= exc.status < 500 and exc.status != 408:
                    _remove_unsent_request(interaction.user.id)
                    detail = "The bot could not post your request. Please try again later."
                else:
                    detail = "The request could not be confirmed. Contact support before trying again."
                print(f"[TOME] Failed to send request for {interaction.user.id}: {exc}")
                await errors.send_custom_error(
                    interaction, "Request Failed", detail,
                )
                return
            except (aiohttp.ClientError, OSError) as exc:
                print(f"[TOME] Request delivery is uncertain for {interaction.user.id}: {exc}")
                await errors.send_custom_error(
                    interaction, "Request Unconfirmed",
                    "The request could not be confirmed. Contact support before trying again.",
                )
                return

            try:
                _save_message_id(interaction.user.id, message.id)
            except sqlite3.Error as exc:
                print(f"[TOME] Failed to save message {message.id} for {interaction.user.id}: {exc}")
                await errors.DATABASE_ERROR.send(interaction)
                return

        await interaction.followup.send("Your tome request has been added to the queue.", ephemeral=True)

    async def on_message(message: discord.Message):
        if message.channel.id != QUEUE_THREAD_ID or message.author.id != bot.user.id or message.nonce is None:
            return
        async with request_lock:
            try:
                _save_message_from_event(str(message.nonce), message.content, message.id)
            except sqlite3.Error as exc:
                print(f"[TOME] Failed to save message {message.id} from gateway event: {exc}")

    async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
        if payload.channel_id != QUEUE_THREAD_ID or not _is_approval(payload.emoji):
            return
        if payload.user_id == bot.user.id or (payload.member and payload.member.bot):
            return
        async with request_lock:
            try:
                _approve_request(payload.message_id)
            except sqlite3.Error as exc:
                print(f"[TOME] Failed to approve request: {exc}")

    async def reconcile_requests():
        async with reconcile_lock:
            try:
                message_ids = _pending_message_ids()
                if not message_ids:
                    return
                thread = await _get_queue_thread(bot)
            except (sqlite3.Error, aiohttp.ClientError, discord.HTTPException, OSError, ValueError) as exc:
                print(f"[TOME] Failed to reconcile requests: {exc}")
                return

            for message_id in message_ids:
                try:
                    message = await thread.fetch_message(message_id)
                    for reaction in message.reactions:
                        if not _is_approval(reaction.emoji):
                            continue
                        async for user in reaction.users(limit=None):
                            if not user.bot:
                                async with request_lock:
                                    _approve_request(message_id)
                                break
                        else:
                            continue
                        break
                except (sqlite3.Error, aiohttp.ClientError, discord.HTTPException, OSError) as exc:
                    print(f"[TOME] Failed to reconcile message {message_id}: {exc}")

    async def on_ready():
        await reconcile_requests()

    async def reconcile_when_ready():
        await bot.wait_until_ready()
        await reconcile_requests()

    bot.add_listener(on_message, "on_message")
    bot.add_listener(on_raw_reaction_add, "on_raw_reaction_add")
    bot.add_listener(on_ready, "on_ready")
    setattr(bot, LISTENERS_ATTR, [
        ("on_message", on_message),
        ("on_raw_reaction_add", on_raw_reaction_add),
        ("on_ready", on_ready),
    ])
    setattr(bot, STARTUP_TASK_ATTR, asyncio.create_task(reconcile_when_ready()))
