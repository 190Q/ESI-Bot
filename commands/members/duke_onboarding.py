import json

import discord

from utils.paths import DATA_DIR

# Discord ranks
DUKE_ROLE_ID = 591765870272053261
HIGHER_RANK_ROLE_IDS = (
    1396112289832243282,  # Grand Duke
    554514823191199747,   # Arch Duke
    554506531949772812,   # Emperor
)
TRACKED_RANK_ROLE_IDS = (DUKE_ROLE_ID, *HIGHER_RANK_ROLE_IDS)

# #phoenix-keep
PHOENIX_KEEP_CHANNEL_ID = 815678688116867092
DUKE_ONBOARDING_THREAD_IDS = (
    1526319463123910868,  # Guild alliances
    1450163595923947663,  # Tome Queue
    1443193393328164964,  # Count Promotions
    1392834927329808535,  # Terr Boost Logs
    927375210695520296,   # Checkbook Balancing
    1402383960046043146,  # Viscount Promotions
)

TRACKED_USERS_FILE = DATA_DIR / "duke_first_time_pings.json"

_pinged_user_ids = set()
_loaded = False
_listener = None


def _load_tracked_users():
    global _pinged_user_ids, _loaded
    if _loaded:
        return
    try:
        if TRACKED_USERS_FILE.exists():
            with open(TRACKED_USERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            ids = data.get("pinged_user_ids", data if isinstance(data, list) else [])
            _pinged_user_ids = {int(uid) for uid in ids}
        else:
            _pinged_user_ids = set()
    except Exception as e:
        print(f"[Duke Onboarding] Failed to load tracked users: {e}")
        _pinged_user_ids = set()
    _loaded = True


def _save_tracked_users():
    try:
        TRACKED_USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TRACKED_USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "pinged_user_ids": sorted(_pinged_user_ids),
                },
                f,
                indent=2,
            )
    except Exception as e:
        print(f"[Duke Onboarding] Failed to save tracked users: {e}")


def _has_higher_rank(member):
    role_ids = {role.id for role in member.roles}
    return any(role_id in role_ids for role_id in HIGHER_RANK_ROLE_IDS)


def _gained_duke(before, after):
    before_ids = {role.id for role in before.roles}
    after_ids = {role.id for role in after.roles}
    return DUKE_ROLE_ID not in before_ids and DUKE_ROLE_ID in after_ids


async def _resolve_thread(bot, guild, thread_id):
    channel = bot.get_channel(thread_id)
    if channel is not None:
        return channel

    thread = guild.get_thread(thread_id)
    if thread is not None:
        return thread

    try:
        return await bot.fetch_channel(thread_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        print(f"[Duke Onboarding] Could not fetch thread {thread_id}: {e}")
        return None


async def _ping_new_duke(bot, member):
    message = (
        f"{member.mention} — first-time Duke onboarding ping. "
        f"Please review this thread."
    )

    sent = 0
    for thread_id in DUKE_ONBOARDING_THREAD_IDS:
        thread = await _resolve_thread(bot, member.guild, thread_id)
        if thread is None:
            print(f"[Duke Onboarding] Thread not found: {thread_id}")
            continue

        try:
            await thread.send(message)
            sent += 1
        except discord.Forbidden:
            print(f"[Duke Onboarding] Missing permission to send in thread {thread_id}")
        except discord.HTTPException as e:
            print(f"[Duke Onboarding] Failed to send in thread {thread_id}: {e}")

    if sent:
        print(
            f"[Duke Onboarding] Pinged {member} ({member.id}) in {sent}/"
            f"{len(DUKE_ONBOARDING_THREAD_IDS)} threads"
        )
    else:
        print(f"[Duke Onboarding] Failed to ping {member} ({member.id}) in any thread")


def _seed_existing_rank_holders(bot):
    """Mark current Duke+ holders so they are not treated as first-time Dukes."""
    added = 0
    for guild in bot.guilds:
        for member in guild.members:
            if any(role.id in TRACKED_RANK_ROLE_IDS for role in member.roles):
                if member.id not in _pinged_user_ids:
                    _pinged_user_ids.add(member.id)
                    added += 1
    return added


def teardown(bot):
    """Remove listener on reload so handlers do not stack."""
    global _listener
    if _listener is not None:
        try:
            bot.remove_listener(_listener, "on_member_update")
            print("[Duke Onboarding] Removed on_member_update listener")
        except Exception as e:
            print(f"[Duke Onboarding] Failed to remove listener: {e}")
        _listener = None


def setup(bot, has_required_role, config):
    """Listen for first-time Duke role grants and ping onboarding threads."""
    global _listener

    teardown(bot)
    _load_tracked_users()

    async def on_member_update_duke_onboarding(before, after):
        if not _gained_duke(before, after):
            return

        if _has_higher_rank(after):
            print(
                f"[Duke Onboarding] Skipping {after} ({after.id}) — already has a rank above Duke"
            )
            return

        _load_tracked_users()
        if after.id in _pinged_user_ids:
            print(
                f"[Duke Onboarding] Skipping {after} ({after.id}) — already received first-time Duke ping"
            )
            return

        # Record before sending so concurrent updates cannot double-ping
        _pinged_user_ids.add(after.id)
        _save_tracked_users()

        await _ping_new_duke(bot, after)

    _listener = on_member_update_duke_onboarding
    bot.add_listener(_listener, "on_member_update")

    async def seed_after_ready():
        await bot.wait_until_ready()
        for guild in bot.guilds:
            if guild.get_channel(PHOENIX_KEEP_CHANNEL_ID) is not None:
                if not guild.chunked:
                    try:
                        await guild.chunk()
                    except Exception as e:
                        print(f"[Duke Onboarding] Failed to chunk guild {guild.id}: {e}")
                break

        added = _seed_existing_rank_holders(bot)
        if added:
            _save_tracked_users()
            print(f"[Duke Onboarding] Seeded {added} existing Duke+ member(s)")

    bot.loop.create_task(seed_after_ready())

    print(
        f"[Duke Onboarding] Loaded — first-time Duke pings "
        f"({len(_pinged_user_ids)} user(s) already tracked)"
    )
