import asyncio
import base64
import json

import discord
from discord import app_commands
from datetime import datetime, timezone
import aiohttp


PLAYTIME_ALT_THRESHOLD_HOURS = 24      # Below this playtime -> suspicious
PLAYTIME_MAIN_THRESHOLD_HOURS = 100    # Above this -> strong indicator of main
ACCOUNT_AGE_THRESHOLD_DAYS = 180       # Account older than this but low stats -> suspicious
TOTAL_LEVEL_ALT_THRESHOLD = 50         # Below this across all characters -> suspicious
QUESTS_ALT_THRESHOLD = 10              # Below this -> suspicious
LOGINS_ALT_THRESHOLD = 20              # Total logins across characters below this -> suspicious
DISCORD_AGE_THRESHOLD_DAYS = 90        # Discord account younger than this -> slightly suspicious

# Minecraft-specific thresholds
MC_ACCOUNT_AGE_YOUNG_DAYS = 90         # Minecraft account newer than this -> suspicious
MC_ACCOUNT_AGE_OLD_DAYS = 365          # Minecraft account older than this -> reduces suspicion
MC_NAME_CHANGE_RECENT_DAYS = 30        # Name change within this window -> suspicious
MC_DEFAULT_SKIN_PENALTY = 1            # Points for using a default skin
MC_CAPE_BONUS = 2                      # Points deducted for owning a cape
MC_NAME_CHANGES_MANY = 3               # Number of name changes that looks odd

# Default Minecraft skin texture hashes (Steve and Alex)
DEFAULT_SKIN_HASHES = {
    "1a4af718455d4aab511e29771a0033d85d07b4f0b7a218179edbee374b9e3526",  # Steve
    "83a7fbb4a6e2820b0d4d8b9ed8be91bb91fc4f1c824e9c5a9d8e0e5d3e0f5e9",  # Alex
}


def _days_since(iso_str: str) -> float:
    """Return the number of days elapsed since an ISO-8601 date string."""
    if not iso_str:
        return 0.0
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400


def _format_uuid(uuid: str | None) -> str | None:
    """Return a canonical dashed UUID; pass through invalid values unchanged."""
    if not uuid:
        return None
    cleaned = uuid.replace("-", "").lower()
    if len(cleaned) != 32:
        return uuid
    return f"{cleaned[:8]}-{cleaned[8:12]}-{cleaned[12:16]}-{cleaned[16:20]}-{cleaned[20:]}"


async def _fetch_json(session: aiohttp.ClientSession, url: str) -> dict | None:
    """Fetch JSON from a URL, returning None on any failure."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception:
        pass
    return None


async def _fetch_mojang_uuid(session: aiohttp.ClientSession, username: str) -> str | None:
    """Resolve a Minecraft username to a UUID via the Mojang API."""
    data = await _fetch_json(session, f"https://api.mojang.com/users/profiles/minecraft/{username}")
    return data.get("id") if isinstance(data, dict) else None


def _normalize_textures(textures: dict | None) -> dict:
    """Lower-case Mojang texture keys (SKIN/CAPE -> skin/cape) for uniform access."""
    if not isinstance(textures, dict):
        return {}
    return {k.lower(): v for k, v in textures.items() if isinstance(v, dict)}


async def _fetch_mojang_profile(session: aiohttp.ClientSession, uuid: str) -> dict | None:
    """Fetch a Minecraft profile (skin/cape) from Mojang's session server."""
    data = await _fetch_json(session, f"https://sessionserver.mojang.com/session/minecraft/profile/{uuid}")
    if not isinstance(data, dict):
        return None

    properties = data.get("properties", [])
    for prop in properties:
        if prop.get("name") == "textures":
            try:
                decoded = base64.b64decode(prop.get("value", "")).decode("utf-8")
                textures = json.loads(decoded)
            except Exception:
                textures = {}
            return {
                "uuid": uuid,
                "username": data.get("name"),
                "textures": _normalize_textures(textures.get("textures", {})),
                "profile_actions": textures.get("profileActions", []),
            }
    return {"uuid": uuid, "username": data.get("name"), "textures": {}, "profile_actions": []}


async def _fetch_ashcon_profile(session: aiohttp.ClientSession, username: str) -> dict | None:
    """Fetch a richer Minecraft profile from Ashcon (creation date, name history, skin/cape)."""
    return await _fetch_json(session, f"https://api.ashcon.app/mojang/v2/user/{username}")


async def _fetch_vrc_profile(session: aiohttp.ClientSession, username: str) -> dict | None:
    """Fetch a Minecraft profile from vrc.lol (skin/cape/bedrock link, name history)."""
    data = await _fetch_json(session, f"https://vrc.lol/api?username={username}")
    if not isinstance(data, dict) or not data.get("success"):
        return None
    return data


async def _fetch_geyser_link(session: aiohttp.ClientSession, uuid: str) -> dict | None:
    """Check whether this Java UUID is linked to a Bedrock account via GeyserMC."""
    return await _fetch_json(session, f"https://api.geysermc.org/v2/link/java/{uuid}")


async def _fetch_laby_uuid(session: aiohttp.ClientSession, username: str) -> dict | None:
    """Resolve a username to a UUID via laby.net."""
    data = await _fetch_json(session, f"https://laby.net/api/v3/user/{username}/uniqueId")
    if not isinstance(data, dict):
        return None
    uuid = data.get("uuid") or data.get("uniqueId")
    if not uuid:
        return None
    data.setdefault("uuid", uuid)
    return data


async def _fetch_laby_names(session: aiohttp.ClientSession, uuid: str) -> list[dict] | None:
    """Fetch the public username history laby.net has recorded for a UUID."""
    data = await _fetch_json(session, f"https://laby.net/api/v3/user/{uuid}/names")
    return data if isinstance(data, list) else None


async def _fetch_laby_textures(session: aiohttp.ClientSession, uuid: str) -> dict | None:
    """Fetch the public skin/cape history laby.net has recorded for a UUID."""
    data = await _fetch_json(session, f"https://laby.net/api/v3/user/{uuid}/textures")
    return data if isinstance(data, dict) else None


async def _gather_minecraft_data(username: str) -> dict:
    """
    Query multiple Minecraft databases and return a normalized dict.
    Tries Mojang, Ashcon, vrc.lol, and laby.net concurrently; falls back where one source fails.
    """
    async with aiohttp.ClientSession() as session:
        mojang_uuid_task = _fetch_mojang_uuid(session, username)
        ashcon_task = _fetch_ashcon_profile(session, username)
        vrc_task = _fetch_vrc_profile(session, username)
        laby_uuid_task = _fetch_laby_uuid(session, username)

        mojang_uuid, ashcon, vrc, laby_uuid = await asyncio.gather(
            mojang_uuid_task, ashcon_task, vrc_task, laby_uuid_task
        )

        uuid = None
        if ashcon and isinstance(ashcon, dict):
            uuid = ashcon.get("uuid")
        if not uuid and laby_uuid:
            uuid = laby_uuid.get("uuid")
        if not uuid and mojang_uuid:
            uuid = mojang_uuid
        if not uuid and isinstance(vrc, dict):
            java_data = vrc.get("java") or {}
            uuid = java_data.get("uuid") or java_data.get("uuid_formatted")

        uuid = _format_uuid(uuid)

        mojang_profile = None
        geyser = None
        laby_names = None
        laby_textures = None
        if uuid:
            mojang_profile_task = _fetch_mojang_profile(session, uuid)
            geyser_task = _fetch_geyser_link(session, uuid)
            laby_names_task = _fetch_laby_names(session, uuid)
            laby_textures_task = _fetch_laby_textures(session, uuid)
            mojang_profile, geyser, laby_names, laby_textures = await asyncio.gather(
                mojang_profile_task, geyser_task, laby_names_task, laby_textures_task
            )

    laby = {}
    if laby_uuid:
        laby["uuid"] = laby_uuid
    if laby_names:
        laby["names"] = laby_names
    if laby_textures:
        laby["textures"] = laby_textures
    if not laby:
        laby = None

    return {
        "uuid": uuid,
        "mojang_profile": mojang_profile,
        "ashcon": ashcon,
        "vrc": vrc,
        "geyser": geyser,
        "laby": laby,
    }


def _get_textures(mc_data: dict) -> dict:
    """Merge texture info from all available sources into a lowercase-keyed dict."""
    textures: dict = {}

    ashcon = mc_data.get("ashcon") or {}
    if isinstance(ashcon.get("textures"), dict):
        textures.update(_normalize_textures(ashcon["textures"]))

    mojang_profile = mc_data.get("mojang_profile") or {}
    if isinstance(mojang_profile.get("textures"), dict):
        textures.update(_normalize_textures(mojang_profile["textures"]))

    vrc = mc_data.get("vrc") or {}
    java = vrc.get("java") or {}
    if isinstance(java, dict):
        if java.get("skin_url"):
            skin_url = java["skin_url"]
            if skin_url.startswith("/"):
                skin_url = f"https://vrc.lol{skin_url}"
            textures.setdefault("skin", {"url": skin_url})
            textures["skin"]["is_default"] = java.get("skin_is_default", True)
        if java.get("cape_url"):
            cape_url = java["cape_url"]
            if cape_url.startswith("/"):
                cape_url = f"https://vrc.lol{cape_url}"
            textures.setdefault("cape", {"url": cape_url})

    laby = mc_data.get("laby") or {}
    laby_textures = laby.get("textures")
    if isinstance(laby_textures, dict):
        skins = laby_textures.get("SKIN") or []
        capes = laby_textures.get("CAPE") or []
        if isinstance(skins, list) and skins and "skin" not in textures:
            active = next(
                (s for s in skins if isinstance(s, dict) and s.get("active")),
                skins[0],
            )
            if isinstance(active, dict):
                textures.setdefault("skin", {"url": None, "is_default": False, "laby": active})
        if isinstance(capes, list) and capes and "cape" not in textures:
            textures.setdefault("cape", {"url": None, "laby": capes[0]})

    return textures


def _laby_name_to_standard(entry: dict) -> dict:
    """Convert a laby.net name-history entry to the internal format."""
    return {
        "username": entry.get("name"),
        "changed_at": entry.get("changed_at"),
        "accurate": entry.get("accurate"),
        "last_seen_at": entry.get("last_seen_at"),
    }


def _get_username_history(mc_data: dict) -> list[dict] | None:
    """Return a normalized username history list, or None if unavailable."""
    laby = (mc_data.get("laby") or {}).get("names")
    if isinstance(laby, list) and laby:
        return [_laby_name_to_standard(entry) for entry in laby if isinstance(entry, dict)]

    ashcon = mc_data.get("ashcon") or {}
    if isinstance(ashcon.get("username_history"), list) and ashcon["username_history"]:
        return ashcon["username_history"]

    vrc = mc_data.get("vrc") or {}
    java = vrc.get("java") or {}
    history = java.get("name_history") if isinstance(java, dict) else None
    if isinstance(history, list) and history:
        return [
            {
                "username": entry.get("name"),
                "changed_at": (
                    datetime.fromtimestamp(entry["changed_at"] / 1000, tz=timezone.utc).isoformat()
                    if isinstance(entry.get("changed_at"), (int, float))
                    else None
                ),
            }
            for entry in history
            if isinstance(entry, dict)
        ]

    return None


def _get_created_at(mc_data: dict) -> str | None:
    """Return a creation-date estimate from any source that has one."""
    ashcon = mc_data.get("ashcon") or {}
    if isinstance(ashcon, dict) and ashcon.get("created_at"):
        return ashcon["created_at"]
    return None


def _get_first_seen_estimate(mc_data: dict) -> str | None:
    """
    Return the earliest public timestamp laby.net has seen for this account.
    This is only a lower-bound estimate of account age when no creation date exists.
    """
    laby = mc_data.get("laby") or {}
    textures = laby.get("textures") or {}
    timestamps: list[str] = []

    for category in ("SKIN", "CAPE"):
        entries = textures.get(category) or []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("first_seen_at"):
                timestamps.append(entry["first_seen_at"])

    names = laby.get("names")
    if isinstance(names, list):
        for entry in names:
            if not isinstance(entry, dict):
                continue
            if entry.get("changed_at"):
                timestamps.append(entry["changed_at"])
            if entry.get("last_seen_at"):
                timestamps.append(entry["last_seen_at"])

    return min(timestamps) if timestamps else None


def _get_skin_count(mc_data: dict) -> int:
    """Return the number of distinct skins laby.net has recorded."""
    textures = (mc_data.get("laby") or {}).get("textures") or {}
    skins = textures.get("SKIN")
    return len(skins) if isinstance(skins, list) else 0


def _get_cape_count(mc_data: dict) -> int:
    """Return the number of distinct capes laby.net has recorded."""
    textures = (mc_data.get("laby") or {}).get("textures") or {}
    capes = textures.get("CAPE")
    return len(capes) if isinstance(capes, list) else 0


def _is_default_skin(skin_url: str | None) -> bool:
    """Return True if the skin URL matches a known default Steve/Alex texture."""
    if not skin_url:
        return True
    # Texture hashes are the last path segment of a textures.minecraft.net URL
    hash_part = skin_url.rstrip("/").rsplit("/", 1)[-1]
    return hash_part.lower() in DEFAULT_SKIN_HASHES


def _score_player(
    data: dict,
    discord_user: discord.User,
    mc_data: dict,
) -> tuple[int, list[str], list[str]]:
    """
    Analyse a Wynncraft full-stats API response plus Minecraft metadata and return:
        (score, red_flags, green_flags)
    Score ≥ 5  -> likely alt
    Score 2-4  -> uncertain
    Score ≤ 1  -> likely main
    """
    score = 0
    red_flags: list[str] = []
    green_flags: list[str] = []

    # Account age
    first_join = data.get("firstJoin")
    account_age_days = _days_since(first_join) if first_join else 0

    # Playtime
    playtime_hours: float = data.get("playtime", 0) or 0

    if playtime_hours < PLAYTIME_ALT_THRESHOLD_HOURS:
        score += 3
        red_flags.append(
            f"Very low playtime ({playtime_hours:.1f} h) - barely played"
        )
    elif playtime_hours >= PLAYTIME_MAIN_THRESHOLD_HOURS:
        score -= 2
        green_flags.append(f"Substantial playtime ({playtime_hours:.1f} h)")

    # Account age vs. activity
    if account_age_days > ACCOUNT_AGE_THRESHOLD_DAYS and playtime_hours < PLAYTIME_ALT_THRESHOLD_HOURS:
        score += 2
        red_flags.append(
            f"Old account ({account_age_days:.0f} days) but almost no playtime"
        )

    # Global stats
    global_data: dict = data.get("globalData", {}) or {}

    total_levels: int = global_data.get("totalLevel", 0) or 0
    if total_levels < TOTAL_LEVEL_ALT_THRESHOLD:
        score += 2
        red_flags.append(f"Very low combined character levels ({total_levels})")
    elif total_levels > 300:
        score -= 2
        green_flags.append(f"High combined character levels ({total_levels})")

    completed_quests: int = global_data.get("completedQuests", 0) or 0
    if completed_quests < QUESTS_ALT_THRESHOLD:
        score += 1
        red_flags.append(f"Almost no quests completed ({completed_quests})")
    elif completed_quests > 100:
        score -= 1
        green_flags.append(f"Many quests completed ({completed_quests})")

    raids_total: int = (global_data.get("raids") or {}).get("total", 0)
    dungeons_total: int = (global_data.get("dungeons") or {}).get("total", 0)
    if raids_total == 0 and dungeons_total == 0 and account_age_days > 60:
        score += 1
        red_flags.append("Zero raids and dungeons on a non-brand-new account")

    # Guild membership
    guild = data.get("guild")
    if not guild:
        score += 1
        red_flags.append("Not in any guild")
    else:
        green_flags.append(f"Member of guild **{guild['name']}** ({guild['rank']})")

    # Rank / veteran
    veteran: bool = data.get("veteran", False) or False
    if veteran:
        score -= 3
        green_flags.append("Has the Veteran badge")

    support_rank: str | None = data.get("supportRank")
    if support_rank:
        score -= 2
        green_flags.append(f"Has a support rank ({support_rank})")

    forum_link = data.get("forumLink")
    if not forum_link and account_age_days > ACCOUNT_AGE_THRESHOLD_DAYS:
        score += 1
        red_flags.append("No forum account linked on an old Wynncraft account")

    # Character-level login count
    characters: dict = data.get("characters", {}) or {}
    total_logins = sum(
        (char.get("logins") or 0) for char in characters.values()
    )
    if total_logins < LOGINS_ALT_THRESHOLD and len(characters) > 0:
        score += 2
        red_flags.append(
            f"Very few total logins across all characters ({total_logins})"
        )
    elif total_logins > 200:
        score -= 1
        green_flags.append(f"High total login count ({total_logins})")

    # Public profile hidden
    public_profile: bool = data.get("publicProfile", True)
    if not public_profile and playtime_hours < PLAYTIME_ALT_THRESHOLD_HOURS:
        score += 1
        red_flags.append(
            "Profile is hidden despite very low activity (nothing to hide on a main)"
        )

    # Discord account age
    discord_age_days = (
        datetime.now(timezone.utc) - discord_user.created_at
    ).total_seconds() / 86400

    if discord_age_days < DISCORD_AGE_THRESHOLD_DAYS:
        score += 1
        red_flags.append(
            f"Discord account is only {discord_age_days:.0f} days old"
        )
    elif discord_age_days > 730:
        score -= 1
        green_flags.append(
            f"Discord account is {discord_age_days:.0f} days old (well-established)"
        )

    # Minecraft metadata checks
    combined_textures = _get_textures(mc_data)

    # Account creation date (Ashcon gives an estimate)
    created_at = _get_created_at(mc_data)
    if created_at:
        mc_age_days = _days_since(created_at)
        if mc_age_days < MC_ACCOUNT_AGE_YOUNG_DAYS:
            score += 2
            red_flags.append(
                f"Minecraft account is only {mc_age_days:.0f} days old"
            )
        elif mc_age_days > MC_ACCOUNT_AGE_OLD_DAYS:
            score -= 1
            green_flags.append(
                f"Minecraft account is {mc_age_days:.0f} days old"
            )
    elif not mc_data.get("uuid"):
        score += 1
        red_flags.append("Could not verify the Minecraft account")

    # Skin: default skins are common on throwaway alts
    skin_info = combined_textures.get("skin") or {}
    skin_url = skin_info.get("url") if isinstance(skin_info, dict) else None
    skin_is_default = (
        skin_info.get("is_default") is True
        if isinstance(skin_info, dict) and "is_default" in skin_info
        else _is_default_skin(skin_url)
    )
    if skin_is_default:
        score += MC_DEFAULT_SKIN_PENALTY
        red_flags.append("Still using a default Minecraft skin (Steve/Alex)")
    else:
        green_flags.append("Custom Minecraft skin equipped")

    # Cape
    cape_info = combined_textures.get("cape")
    cape_count = _get_cape_count(mc_data)
    if cape_info or cape_count:
        score -= MC_CAPE_BONUS
        cape_suffix = f" ({cape_count})" if cape_count > 1 else ""
        green_flags.append(f"Minecraft cape equipped{cape_suffix}")

    skin_count = _get_skin_count(mc_data)
    if skin_count > 1:
        green_flags.append(f"Multiple Minecraft skins recorded ({skin_count})")

    # Username history
    username_history = _get_username_history(mc_data)
    if isinstance(username_history, list) and username_history:
        history_count = len(username_history)
        if history_count > MC_NAME_CHANGES_MANY:
            score += 1
            red_flags.append(f"Lots of username changes ({history_count})")

        last_change = None
        for entry in reversed(username_history):
            changed_at = entry.get("changed_at") if isinstance(entry, dict) else None
            if changed_at:
                last_change = changed_at
                break

        if last_change:
            change_age_days = _days_since(last_change)
            if change_age_days < MC_NAME_CHANGE_RECENT_DAYS:
                score += 1
                red_flags.append(
                    f"Username changed only {change_age_days:.0f} days ago"
                )
            else:
                green_flags.append(
                    f"No username changes in the last {MC_NAME_CHANGE_RECENT_DAYS:.0f} days"
                )
        elif history_count == 1:
            green_flags.append("Original Minecraft username (never changed)")

    # GeyserMC Bedrock link
    geyser = mc_data.get("geyser")
    vrc = mc_data.get("vrc") or {}
    java_is_linked = isinstance(geyser, dict) and (
        geyser.get("linked") or geyser.get("xuid")
    )
    bedrock_data = vrc.get("bedrock") if isinstance(vrc, dict) else None
    vrc_is_linked = isinstance(bedrock_data, dict) and (
        bedrock_data.get("xuid") or bedrock_data.get("xuid_decimal")
    )
    if java_is_linked or vrc_is_linked:
        score -= 1
        green_flags.append("Java account linked to a Bedrock (Geyser) account")

    return score, red_flags, green_flags


def _build_verdict_embed(
    score: int,
    red_flags: list[str],
    green_flags: list[str],
    wynn_data: dict,
    discord_user: discord.User,
    mc_username: str,
    mc_data: dict,
) -> discord.Embed:
    """Build the result embed from the scoring output."""

    if score >= 5:
        verdict = "Likely ALT account"
        color = 0xE74C3C
        verdict_emoji = "🔴"
    elif score >= 2:
        verdict = "Uncertain - could be an alt"
        color = 0xF39C12
        verdict_emoji = "🟡"
    else:
        verdict = "Likely MAIN account"
        color = 0x2ECC71
        verdict_emoji = "🟢"

    embed = discord.Embed(
        title=f"Alt-check: {mc_username}",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    embed.add_field(
        name="Discord user",
        value=f"{discord_user.mention} (`{discord_user.name}`)",
        inline=True,
    )
    embed.add_field(
        name="Minecraft username",
        value=f"`{wynn_data.get('username', mc_username)}`",
        inline=True,
    )
    embed.add_field(
        name="Verdict",
        value=f"{verdict_emoji} **{verdict}** (score: `{score}`)",
        inline=False,
    )

    # Key stats summary
    global_data: dict = wynn_data.get("globalData", {}) or {}
    first_join = wynn_data.get("firstJoin", "unknown")
    account_age = f"{_days_since(first_join):.0f} days" if first_join != "unknown" else "unknown"
    playtime = wynn_data.get("playtime", 0) or 0

    stats_lines = [
        f"**Playtime:** {playtime:.1f} h",
        f"**Account age:** {account_age}",
        f"**Total levels:** {global_data.get('totalLevel', 0)}",
        f"**Quests done:** {global_data.get('completedQuests', 0)}",
        f"**Raids:** {(global_data.get('raids') or {}).get('total', 0)}",
        f"**Dungeons:** {(global_data.get('dungeons') or {}).get('total', 0)}",
        f"**Characters:** {len(wynn_data.get('characters', {}) or {})}",
    ]
    embed.add_field(name="Player stats", value="\n".join(stats_lines), inline=True)

    # Minecraft metadata summary
    combined_textures = _get_textures(mc_data)

    mc_age_str = "unknown"
    created_at = _get_created_at(mc_data)
    if created_at:
        mc_age_str = f"{_days_since(created_at):.0f} days"
    else:
        first_seen = _get_first_seen_estimate(mc_data)
        if first_seen:
            mc_age_str = f"≥ {_days_since(first_seen):.0f} days (Laby.net estimate)"

    username_history = _get_username_history(mc_data)
    history_count = len(username_history) if isinstance(username_history, list) else "unknown"

    last_change_str = "unknown"
    if isinstance(username_history, list):
        for entry in reversed(username_history):
            changed_at = entry.get("changed_at") if isinstance(entry, dict) else None
            if changed_at:
                last_change_str = f"{_days_since(changed_at):.0f} days ago"
                break
        if last_change_str == "unknown" and history_count == 1:
            last_change_str = "never (original name)"

    skin_info = combined_textures.get("skin") or {}
    skin_url = skin_info.get("url") if isinstance(skin_info, dict) else None
    skin_is_default = (
        skin_info.get("is_default") is True
        if isinstance(skin_info, dict) and "is_default" in skin_info
        else _is_default_skin(skin_url)
    )
    has_custom_skin = not skin_is_default
    has_cape = bool(combined_textures.get("cape")) or _get_cape_count(mc_data) > 0
    skin_count = _get_skin_count(mc_data)
    cape_count = _get_cape_count(mc_data)

    geyser = mc_data.get("geyser")
    vrc = mc_data.get("vrc") or {}
    java_is_linked = isinstance(geyser, dict) and (
        geyser.get("linked") or geyser.get("xuid")
    )
    bedrock_data = vrc.get("bedrock") if isinstance(vrc, dict) else None
    vrc_is_linked = isinstance(bedrock_data, dict) and (
        bedrock_data.get("xuid") or bedrock_data.get("xuid_decimal")
    )
    bedrock_linked = java_is_linked or vrc_is_linked

    cape_line = f"**Capes:** {cape_count}" if cape_count else f"**Cape:** {'yes' if has_cape else 'no'}"

    mc_lines = [
        f"**UUID:** `{mc_data.get('uuid') or 'unknown'}`",
        f"**MC account age:** {mc_age_str}",
        f"**Name changes:** {history_count}",
        f"**Last name change:** {last_change_str}",
        f"**Custom skin:** {'yes' if has_custom_skin else 'no (default)'}",
        f"**Skins:** {skin_count}" if skin_count else "**Skins:** unknown",
        cape_line,
        f"**Bedrock link:** {'yes' if bedrock_linked else 'no'}",
    ]
    embed.add_field(name="Minecraft info", value="\n".join(mc_lines), inline=True)

    discord_age_days = (
        datetime.now(timezone.utc) - discord_user.created_at
    ).total_seconds() / 86400
    embed.add_field(
        name="Discord info",
        value=f"**Account age:** {discord_age_days:.0f} days\n**Created:** {discord_user.created_at.strftime('%Y-%m-%d')}",
        inline=True,
    )

    # Flags
    if red_flags:
        embed.add_field(
            name="Red flags",
            value="\n".join(f"- {f}" for f in red_flags),
            inline=False,
        )
    if green_flags:
        embed.add_field(
            name="Green flags",
            value="\n".join(f"- {f}" for f in green_flags),
            inline=False,
        )

    embed.set_footer(text="Alt detection is heuristic-based and not definitive.")
    return embed


async def _fetch_wynncraft_data(mc_username: str) -> dict | None:
    """
    Fetch full player stats from the Wynncraft v3 API.
    Returns the parsed JSON dict, or None on failure.
    """
    url = f"https://api.wynncraft.com/v3/player/{mc_username}?fullResult"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                return await resp.json()
            return None


def setup(bot, has_required_role, config):
    """Setup function for bot integration."""

    # Shared handler
    async def run_alt_check(
        interaction: discord.Interaction,
        user: discord.User,
        mc_username: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        wynn_data, mc_data = await asyncio.gather(
            _fetch_wynncraft_data(mc_username),
            _gather_minecraft_data(mc_username),
        )

        if wynn_data is None:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Player not found",
                    description=f"Could not retrieve Wynncraft data for `{mc_username}`. Make sure the username is correct.",
                    color=0xFF0000,
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
            return

        # Multi-selector edge case: API returns a dict of UUIDs instead of player data
        if "username" not in wynn_data:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Ambiguous username",
                    description=f"Multiple accounts match `{mc_username}`. Please provide an exact, case-sensitive username.",
                    color=0xFF0000,
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
            return

        score, red_flags, green_flags = _score_player(wynn_data, user, mc_data)
        embed = _build_verdict_embed(score, red_flags, green_flags, wynn_data, user, mc_username, mc_data)
        await interaction.followup.send(embed=embed, ephemeral=True)
        print(
            f"[alt_check] {mc_username} linked to {user.name} -> score {score}"
        )

    @bot.tree.context_menu(name="Alt Check")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, private_channels=True)
    async def altcheck_context(interaction: discord.Interaction, user: discord.User):
        """Context menu entry: asks for MC username then runs the alt check."""

        class UsernameModal(discord.ui.Modal, title="Alt Check"):
            mc_username = discord.ui.TextInput(
                label="Minecraft username",
                placeholder="e.g. 190Q",
                min_length=1,
                max_length=16,
            )

            async def on_submit(self, modal_interaction: discord.Interaction):
                await run_alt_check(modal_interaction, user, str(self.mc_username))

        await interaction.response.send_modal(UsernameModal())

    print("[OK] Loaded altcheck command")