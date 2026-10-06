"""
Shared configuration and helpers for Nitro boost colour roles.

Used by two command modules:

* ``commands/members/nitro_boost.py`` — creates the role when a member boosts and
  reclaims it when the boost ends.
* ``commands/members/nitro_colour.py`` — lets the member recolour that role.

Keeping the config and the storage in one place means the listener and the
command can never disagree about where a role lives or what it is called.
"""

from __future__ import annotations

import colorsys
import json
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import discord
from discord.http import Route

from utils.colour_names import normalise_simple_colour, simple_colour_of
from utils.paths import CONFIG_DIR, DATA_DIR

BOOST_ANNOUNCE_CHANNEL_ID = 554418045397762050
BOT_ROLE_ID = 1429795745107808358
COLOUR_COMMAND_NAME = "nitro_colour"
ROLE_NAME_SUFFIX = "Nitro"
RETENTION_DAYS = 30
HOLOGRAPHIC_COLOURS = (11127295, 16759788, 16761760)
ENHANCED_ROLE_COLORS_FEATURE = "ENHANCED_ROLE_COLORS"

COLOUR_ALERT_CHANNEL_ID = 1447167603951927347
COLOUR_ALERT_CONFIG_FILE = CONFIG_DIR / "nitro_colour_alerts.json"
_DEFAULT_ALERT_SIMPLE_COLOURS: Tuple[str, ...] = ("purple",)

STORE_FILE = DATA_DIR / "nitro_roles.json"

LINK_PERMISSION_ROLE_IDS = [
    600185623474601995,
]

_watched_simple_colours: Tuple[str, ...] = _DEFAULT_ALERT_SIMPLE_COLOURS
_alert_config_loaded = False

_HEX_PATTERN = re.compile(r"^(?:#|0[xX])?([0-9a-fA-F]{6})$")
_WORD_SPLIT = re.compile(r"[^a-z0-9]+")


def random_colour() -> int:
    """A vivid random colour, kept away from muddy near-black and near-white."""
    hue = random.random()
    saturation = random.uniform(0.65, 0.95)
    lightness = random.uniform(0.45, 0.65)
    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return (round(red * 255) << 16) | (round(green * 255) << 8) | round(blue * 255)


def parse_hex_colour(raw: str) -> int:
    """Parse ``#RRGGBB``, ``RRGGBB`` or ``0xRRGGBB`` into an int.

    Raises :exc:`ValueError` carrying the original string for anything else.
    """
    match = _HEX_PATTERN.match((raw or "").strip())
    if match is None:
        raise ValueError(raw)
    return int(match.group(1), 16)


def format_colour(value: int) -> str:
    """Render a colour int as ``#RRGGBB``."""
    return f"#{value:06X}"


def guild_has_enhanced_colours(guild: discord.Guild) -> bool:
    """Whether *guild* can render gradient and holographic role colours."""
    return ENHANCED_ROLE_COLORS_FEATURE in guild.features


async def apply_colours(
    client: discord.Client,
    guild: discord.Guild,
    role: discord.Role,
    primary: int,
    secondary: Optional[int] = None,
    tertiary: Optional[int] = None,
    *,
    reason: Optional[str] = None,
) -> None:
    """Write *role*'s colours, where ``None`` clears that slot.

    This always goes through the REST route rather than ``Role.edit``. discord.py
    computes ``secondary_colour or secondary_color``, so passing ``None`` is
    treated as "not supplied" and a gradient can never be cleared back to a
    solid colour. Sending the ``colors`` object directly avoids that, and works
    on every discord.py 2.x build instead of only 2.6+.
    """
    colours: Dict[str, Optional[int]] = {
        "primary_color": primary,
        "secondary_color": secondary,
        "tertiary_color": tertiary,
    }

    route = Route(
        "PATCH",
        "/guilds/{guild_id}/roles/{role_id}",
        guild_id=guild.id,
        role_id=role.id,
    )
    await client.http.request(route, json={"colors": colours}, reason=reason)


async def fetch_role_colours(
    client: discord.Client,
    guild: discord.Guild,
    role: discord.Role,
) -> Tuple[int, Optional[int]]:
    """The role's current ``(primary, secondary)``, read from the raw payload.

    The cached :class:`~discord.Role` is not enough here: builds before discord.py
    2.6 never parse the ``colors`` object, so a gradient's second colour would be
    invisible and the modal would keep offering a random one.

    Never raises. If the request fails the cached primary is used and the
    secondary is reported as unknown.
    """
    try:
        data = await client.http.get_role(guild.id, role.id)
    except Exception as exc:
        print(f"[Nitro Roles] Could not read colours for role {role.id}: {exc}")
        return role.colour.value, None

    colours = data.get("colors") or {}
    primary = colours.get("primary_color", data.get("color"))
    secondary = colours.get("secondary_color")

    return (
        int(primary) if primary is not None else role.colour.value,
        int(secondary) if secondary is not None else None,
    )


def role_name_for(member: discord.Member) -> str:
    """The colour role name for *member*."""
    return f"{member.name} {ROLE_NAME_SUFFIX}"


def _squash(text: str) -> str:
    """Lowercase and drop everything that is not a letter or digit."""
    return _WORD_SPLIT.sub("", (text or "").casefold())


def _words(text: str) -> set:
    """Lowercased alphanumeric words in *text*."""
    return {word for word in _WORD_SPLIT.split((text or "").casefold()) if word}


def _member_names(member: discord.Member) -> set:
    """Every name *member* might have been named after in a role."""
    return {
        name
        for name in (member.name, member.display_name, getattr(member, "global_name", None))
        if name
    }


def find_colour_role(
    guild: discord.Guild,
    member: discord.Member,
    store: Dict,
) -> Optional[discord.Role]:
    """Locate *member*'s colour role.

    The stored role id wins because it survives a username change. Failing that
    the role is matched by name, which picks up roles handed out before the store
    existed and roles a staff member linked by hand.
    """
    entry = get_entry(store, member.id)
    if entry is not None:
        role = guild.get_role(int(entry.get("role_id") or 0))
        if role is not None:
            return role

    wanted = {_squash(f"{name} {ROLE_NAME_SUFFIX}") for name in _member_names(member)}
    for role in guild.roles:
        if _squash(role.name) in wanted:
            return role

    suffix = _squash(ROLE_NAME_SUFFIX)
    names = {name.casefold() for name in _member_names(member)}
    loose = [
        role
        for role in guild.roles
        if suffix in _words(role.name) and names & _words(role.name)
    ]
    if len(loose) == 1:
        return loose[0]

    return None


def build_placement_payload(
    raw_roles: Sequence[Dict],
    role_id: int,
    anchor_role_id: int,
    ceiling_role_id: Optional[int] = None,
) -> List[Dict[str, int]]:
    """Positions payload that moves ``role_id`` directly above ``anchor_role_id``.

    ``raw_roles`` must come straight from the API. discord.py's role cache is not
    updated by ``create_role``, and Discord renumbers positions when a role is
    added, so cached positions cannot be trusted for this arithmetic.

    The role is never placed at or above ``ceiling_role_id``; if the anchor sits
    too high it lands directly below the ceiling instead. Only roles whose final
    position actually differs are included, which keeps the ceiling role and
    everything above it out of the request so the bot never tries to move a role
    it has no authority over.
    """
    ordered = sorted(raw_roles, key=lambda role: int(role["position"]))
    current = {int(role["id"]): int(role["position"]) for role in ordered}
    others = [int(role["id"]) for role in ordered if int(role["id"]) != role_id]

    if anchor_role_id not in others:
        raise ValueError(f"anchor role {anchor_role_id} is not in the role list")

    index = others.index(anchor_role_id) + 1
    if ceiling_role_id is not None and ceiling_role_id in others:
        index = min(index, others.index(ceiling_role_id))
    index = max(1, index)

    final = others[:index] + [role_id] + others[index:]
    return [
        {"id": rid, "position": position}
        for position, rid in enumerate(final)
        if current.get(rid) != position
    ]


def load_store() -> Dict:
    """Read ``data/nitro_roles.json``, falling back to an empty store."""
    try:
        if STORE_FILE.exists():
            with open(STORE_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("users"), dict):
                return data
    except Exception as exc:
        print(f"[Nitro Roles] Failed to read {STORE_FILE.name}: {exc}")
    return {"users": {}}


def save_store(store: Dict) -> None:
    """Persist *store* to ``data/nitro_roles.json``."""
    try:
        STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STORE_FILE, "w", encoding="utf-8") as handle:
            json.dump(store, handle, indent=2)
    except Exception as exc:
        print(f"[Nitro Roles] Failed to write {STORE_FILE.name}: {exc}")


def get_entry(store: Dict, user_id: int) -> Optional[Dict]:
    """The store entry for *user_id*, or ``None``."""
    return store.get("users", {}).get(str(user_id))


def upsert_entry(store: Dict, user_id: int, role_id: int, username: str) -> Dict:
    """Record *role_id* as *user_id*'s colour role, clearing any expiry."""
    entry = {
        "role_id": int(role_id),
        "username": username,
        "boost_ended_at": None,
    }
    store.setdefault("users", {})[str(user_id)] = entry
    return entry


def link_entry(store: Dict, user_id: int, role_id: int, username: str) -> Dict:
    """Attach *role_id* to *user_id*, leaving any pending expiry alone.

    Used by the staff link tool, which is a repair action and should not quietly
    cancel a retention timer that is already running.
    """
    existing = get_entry(store, user_id)
    if existing is None:
        return upsert_entry(store, user_id, role_id, username)
    existing["role_id"] = int(role_id)
    existing["username"] = username
    return existing


def mark_boost_ended(store: Dict, user_id: int, when: Optional[datetime] = None) -> None:
    """Stamp the moment *user_id*'s boost ended, starting the retention window."""
    entry = get_entry(store, user_id)
    if entry is not None:
        entry["boost_ended_at"] = (when or datetime.now(timezone.utc)).isoformat()


def clear_boost_ended(store: Dict, user_id: int) -> None:
    """Cancel a pending expiry, e.g. because the member boosted again."""
    entry = get_entry(store, user_id)
    if entry is not None:
        entry["boost_ended_at"] = None


def drop_entry(store: Dict, user_id: int) -> None:
    """Forget *user_id*'s colour role."""
    store.get("users", {}).pop(str(user_id), None)


def expiry_due(entry: Dict, now: Optional[datetime] = None) -> bool:
    """Whether *entry* has been expired for longer than the retention window."""
    raw = entry.get("boost_ended_at")
    if not raw:
        return False
    try:
        ended = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - ended >= timedelta(days=RETENTION_DAYS)


def load_alert_config(*, force: bool = False) -> Tuple[str, ...]:
    """(Re)read the watched simple colours from ``config/nitro_colour_alerts.json``.

    Read once on first use. Pass ``force=True`` (a command's ``setup`` does) to
    pick up an edit without restarting the bot. Unknown names are skipped, and a
    missing or unreadable file falls back to the defaults.
    """
    global _watched_simple_colours, _alert_config_loaded
    if _alert_config_loaded and not force:
        return _watched_simple_colours

    colours: Tuple[str, ...] = _DEFAULT_ALERT_SIMPLE_COLOURS
    try:
        if COLOUR_ALERT_CONFIG_FILE.exists():
            with open(COLOUR_ALERT_CONFIG_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            raw = data.get("simple_colours") if isinstance(data, dict) else data
            if isinstance(raw, str):
                raw = [raw]
            if isinstance(raw, list):
                resolved: List[str] = []
                for item in raw:
                    simple = normalise_simple_colour(item)
                    if simple is None:
                        print(
                            f"[Nitro Colour] Ignoring unknown simple colour {item!r} "
                            f"in {COLOUR_ALERT_CONFIG_FILE.name}"
                        )
                        continue
                    if simple not in resolved:
                        resolved.append(simple)
                colours = tuple(resolved)
            elif raw is not None:
                print(
                    f"[Nitro Colour] 'simple_colours' must be a list in "
                    f"{COLOUR_ALERT_CONFIG_FILE.name}; using defaults"
                )
        else:
            print(
                f"[Nitro Colour] {COLOUR_ALERT_CONFIG_FILE.name} not found; "
                "using default watched colours"
            )
    except Exception as exc:
        print(f"[Nitro Colour] Failed to read {COLOUR_ALERT_CONFIG_FILE.name}: {exc}")

    _watched_simple_colours = colours
    _alert_config_loaded = True
    if colours:
        print(f"[Nitro Colour] Watching simple colour(s): {', '.join(colours)}")
    else:
        print("[Nitro Colour] No watched simple colours configured; colour alerts are off")
    return _watched_simple_colours


def _watched_matches(
    colours: Sequence[Tuple[str, Optional[int]]],
) -> Tuple[Dict[str, List[str]], Optional[int]]:
    """Split *colours* into ``(matches, highlight)``.

    *colours* is ``(slot label, value)`` pairs, values optional. *matches* maps
    each watched simple colour to the slots that used it; *highlight* is the first
    offending value, used as the embed's sidebar colour.
    """
    watched = load_alert_config()
    matches: Dict[str, List[str]] = {}
    highlight: Optional[int] = None
    if not watched:
        return matches, highlight

    for label, value in colours:
        if value is None:
            continue
        simple = simple_colour_of(value)
        if simple not in watched:
            continue
        matches.setdefault(simple, []).append(label)
        if highlight is None:
            highlight = value
    return matches, highlight


async def notify_watched_colours(
    client: discord.Client,
    member: discord.Member,
    role: discord.Role,
    colours: Sequence[Tuple[str, Optional[int]]],
) -> None:
    """Report to the alert channel when a member picks a watched simple colour.

    *colours* is ``(slot label, value)`` pairs, e.g.
    ``(('Primary', primary), ('Secondary', secondary))``. Slots left as ``None``
    are ignored. Does nothing when nothing matched, and never raises.
    """
    matches, highlight = _watched_matches(colours)
    if not matches:
        return

    channel = client.get_channel(COLOUR_ALERT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await client.fetch_channel(COLOUR_ALERT_CHANNEL_ID)
        except discord.HTTPException as exc:
            print(f"[Nitro Colour] Could not reach alert channel {COLOUR_ALERT_CHANNEL_ID}: {exc}")
            return

    slots = sorted({label for labels in matches.values() for label in labels})
    applied = [
        f"**{label}**: `{format_colour(value)}` ({simple_colour_of(value)})"
        for label, value in colours
        if value is not None
    ]
    embed = discord.Embed(
        title="⚠️ Watched Colour Used",
        description=f"{member.mention} set {role.mention} to a watched colour.",
        color=discord.Colour(highlight if highlight is not None else 0x800080),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Simple colour",
        value=", ".join(f"`{name}`" for name in sorted(matches)),
        inline=True,
    )
    embed.add_field(name="Slot(s)", value=", ".join(slots), inline=True)
    if applied:
        embed.add_field(name="New colours", value="\n".join(applied), inline=False)
    embed.set_footer(text=f"User ID: {member.id}")

    try:
        await channel.send(embed=embed)
    except discord.HTTPException as exc:
        print(f"[Nitro Colour] Failed to post colour alert: {exc}")
