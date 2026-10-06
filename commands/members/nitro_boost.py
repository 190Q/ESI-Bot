import random
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from utils.nitro_roles import (
    BOOST_ANNOUNCE_CHANNEL_ID,
    BOT_ROLE_ID,
    COLOUR_COMMAND_NAME,
    RETENTION_DAYS,
    build_placement_payload,
    clear_boost_ended,
    drop_entry,
    expiry_due,
    get_entry,
    load_store,
    mark_boost_ended,
    random_colour,
    role_name_for,
    save_store,
    upsert_entry,
)

BOOST_MESSAGES = [
    "Thanks for boosting, {user}! Use /{placeholder_command} to customise your coloured role.",
    "Hey {user}, thanks for the boost! You can customise your coloured role with /{placeholder_command}.",
    "Thank you for boosting the server, {user}! Run /{placeholder_command} to set up your coloured role.",
    "Hey there {user}, we appreciate the boost! Use /{placeholder_command} to customise your role colours.",
    "Thanks a lot for the boost, {user}! To personalise your coloured role, use /{placeholder_command}.",
    "Hi {user}, thanks for boosting! If you'd like to change your role colours, run /{placeholder_command}.",
    "Boost received, thanks {user}! You can customise your coloured role anytime with /{placeholder_command}.",
    "Hey {user}, thank you for supporting the server! Use /{placeholder_command} to make your coloured role your own.",
]

REASON = "Server boost reward"

_listener = None


def _boost_started(before: discord.Member, after: discord.Member) -> bool:
    """True only on the transition into boosting, so the reward fires once."""
    return before.premium_since is None and after.premium_since is not None


def _boost_ended(before: discord.Member, after: discord.Member) -> bool:
    return before.premium_since is not None and after.premium_since is None


async def _announce(bot, member: discord.Member) -> None:
    """Post a random thank-you message. Sent on every fresh boost, even a repeat."""
    channel = bot.get_channel(BOOST_ANNOUNCE_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(BOOST_ANNOUNCE_CHANNEL_ID)
        except discord.HTTPException as exc:
            print(f"[Nitro Boost] Could not reach channel {BOOST_ANNOUNCE_CHANNEL_ID}: {exc}")
            return

    # Plain replace rather than str.format so a stray brace can never raise.
    message = random.choice(BOOST_MESSAGES)
    message = message.replace("{user}", member.mention)
    message = message.replace("{placeholder_command}", COLOUR_COMMAND_NAME)

    try:
        await channel.send(message)
    except discord.Forbidden:
        print(f"[Nitro Boost] Missing permission to post in {BOOST_ANNOUNCE_CHANNEL_ID}")
    except discord.HTTPException as exc:
        print(f"[Nitro Boost] Failed to post boost message: {exc}")


def _ceiling_role(guild: discord.Guild) -> discord.Role | None:
    """The highest role the colour role is allowed to sit below.

    Role ``BOT_ROLE_ID`` is the intended anchor, but it is lowered to the bot's
    own top role when that sits further down, otherwise the move would 403.
    """
    ceiling = guild.get_role(BOT_ROLE_ID)
    top_role = guild.me.top_role if guild.me is not None else None

    if ceiling is None:
        return top_role
    if top_role is not None and ceiling.position > top_role.position:
        return top_role
    return ceiling


async def _create_colour_role(bot, member: discord.Member) -> discord.Role | None:
    """Create the member's colour role and slot it above their highest role."""
    guild = member.guild

    try:
        role = await guild.create_role(
            name=role_name_for(member),
            colour=discord.Colour(random_colour()),
            permissions=discord.Permissions.none(),
            hoist=False,
            mentionable=False,
            reason=REASON,
        )
    except discord.HTTPException as exc:
        print(f"[Nitro Boost] Failed to create a role for {member} ({member.id}): {exc}")
        return None

    try:
        raw_roles = await bot.http.get_roles(guild.id)
        ceiling = _ceiling_role(guild)
        payload = build_placement_payload(
            raw_roles,
            role.id,
            member.top_role.id,
            ceiling.id if ceiling is not None else None,
        )
        if payload:
            await bot.http.move_role_position(guild.id, payload, reason=REASON)
    except Exception as exc:
        # A badly placed role is still better than no role, so keep it.
        print(f"[Nitro Boost] Could not position {role.name} ({role.id}): {exc}")

    print(f"[Nitro Boost] Created {role.name} ({role.id}) for {member} ({member.id})")
    return role


async def _grant(bot, member: discord.Member) -> None:
    """Hand the member their colour role, creating it the first time only."""
    guild = member.guild
    store = load_store()
    entry = get_entry(store, member.id)

    if entry is not None:
        existing = guild.get_role(int(entry.get("role_id") or 0))
        if existing is not None:
            # They boosted before: give the same role back and keep its colours.
            clear_boost_ended(store, member.id)
            save_store(store)
            if existing not in member.roles:
                try:
                    await member.add_roles(existing, reason=REASON)
                except discord.HTTPException as exc:
                    print(f"[Nitro Boost] Failed to re-assign {existing.name}: {exc}")
            print(f"[Nitro Boost] Re-assigned {existing.name} ({existing.id}) to {member} ({member.id})")
            return

        # The stored role is gone, so fall through and build a fresh one.
        drop_entry(store, member.id)

    role = await _create_colour_role(bot, member)
    if role is None:
        return

    upsert_entry(store, member.id, role.id, member.name)
    save_store(store)

    try:
        await member.add_roles(role, reason=REASON)
    except discord.HTTPException as exc:
        print(f"[Nitro Boost] Failed to assign {role.name} to {member} ({member.id}): {exc}")


async def _revoke(member: discord.Member) -> None:
    """Take the role away and start the retention timer."""
    guild = member.guild
    store = load_store()
    entry = get_entry(store, member.id)
    if entry is None:
        return

    role = guild.get_role(int(entry.get("role_id") or 0))
    if role is not None and role in member.roles:
        try:
            await member.remove_roles(role, reason="Server boost ended")
        except discord.HTTPException as exc:
            print(f"[Nitro Boost] Failed to remove {role.name} from {member} ({member.id}): {exc}")

    mark_boost_ended(store, member.id)
    save_store(store)
    print(
        f"[Nitro Boost] Boost ended for {member} ({member.id}); "
        f"role deleted in {RETENTION_DAYS} days unless they boost again"
    )


@tasks.loop(hours=24)
async def _expire_roles(bot) -> None:
    """Delete colour roles whose retention window has passed."""
    await bot.wait_until_ready()

    store = load_store()
    now = datetime.now(timezone.utc)
    users = store.get("users", {})
    deleted = 0

    for user_id in list(users):
        entry = users.get(user_id)
        if entry is None or not expiry_due(entry, now):
            continue

        role_id = int(entry.get("role_id") or 0)
        guild = next((g for g in bot.guilds if g.get_role(role_id) is not None), None)
        if guild is None:
            # Role already gone, so the record is stale.
            drop_entry(store, user_id)
            continue

        member = guild.get_member(int(user_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(user_id))
            except discord.HTTPException:
                member = None

        if member is not None and member.premium_since is not None:
            # They are boosting again but the listener missed it, so keep the role.
            clear_boost_ended(store, user_id)
            continue

        try:
            await guild.get_role(role_id).delete(
                reason=f"Boost reward unused for {RETENTION_DAYS} days"
            )
        except discord.HTTPException as exc:
            print(f"[Nitro Boost] Failed to delete role {role_id}: {exc}")
            continue

        drop_entry(store, user_id)
        deleted += 1

    if deleted:
        save_store(store)
        print(f"[Nitro Boost] Deleted {deleted} expired colour role(s)")


def teardown(bot) -> None:
    """Remove the listener and stop the sweep so a reload does not stack them."""
    global _listener

    if _listener is not None:
        try:
            bot.remove_listener(_listener, "on_member_update")
        except Exception as exc:
            print(f"[Nitro Boost] Failed to remove listener: {exc}")
        _listener = None

    if _expire_roles.is_running():
        _expire_roles.cancel()
        print("[Nitro Boost] Cancelled the expiry sweep")


def setup(bot) -> None:
    global _listener

    teardown(bot)

    async def on_member_update_nitro_boost(before: discord.Member, after: discord.Member):
        if after.bot:
            return

        if _boost_started(before, after):
            await _announce(bot, after)
            await _grant(bot, after)
        elif _boost_ended(before, after):
            await _revoke(after)

    _listener = on_member_update_nitro_boost
    bot.add_listener(_listener, "on_member_update")

    _expire_roles.start(bot)

    print(f"[Nitro Boost] Loaded - boosters get a colour role, editable with /{COLOUR_COMMAND_NAME}")
