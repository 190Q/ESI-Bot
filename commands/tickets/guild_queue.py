"""
Guild Queue - Checks guild capacity and manages a waiting queue for new members.
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from utils.paths import PROJECT_ROOT, DATA_DIR, DB_DIR

# Paths relative to ESI-Bot root
GUILD_SLOTS_FILE = DATA_DIR / "guild_member_slots.json"
TRACKED_GUILD_FILE = DATA_DIR / "tracked_guild.json"
QUEUE_FILE = DATA_DIR / "guild_member_queue.json"
CAPACITY_OVERRIDE_FILE = DATA_DIR / "guild_capacity_override.json"
PENDING_INVITES_FILE = DATA_DIR / "pending_invites.json"
VETERAN_ROLE_ID = 914422269802070057

# A player stays on the pending-invite list for this many days after being accepted
PENDING_INVITE_TTL_DAYS = 3


# ---------------------------------------------------------------------------
# Debug override for guild capacity
#
# When present, ``get_guild_capacity()`` will synthesize a capacity result that
# reports ``open_slots`` open slots (``player_count = max_slots - open_slots``).
# This lets the bot owner simulate slots opening or the guild being full for
# end‑to‑end queue testing without having to mutate ``tracked_guild.json``.
# ---------------------------------------------------------------------------

def get_capacity_override() -> dict | None:
    """Return the capacity override dict, or ``None`` if no override is set."""
    if not CAPACITY_OVERRIDE_FILE.exists():
        return None
    try:
        with open(CAPACITY_OVERRIDE_FILE, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def set_capacity_override(open_slots: int) -> dict:
    """Persist a capacity override that reports ``open_slots`` open slots.

    Negative values are clamped to ``0`` (== guild full). Returns the stored
    override dict.
    """
    payload = {"open_slots": max(0, int(open_slots))}
    CAPACITY_OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CAPACITY_OVERRIDE_FILE, "w") as f:
        json.dump(payload, f)
    return payload


def clear_capacity_override() -> bool:
    """Delete the capacity override file. Returns ``True`` if something was removed."""
    if CAPACITY_OVERRIDE_FILE.exists():
        try:
            CAPACITY_OVERRIDE_FILE.unlink()
            return True
        except OSError:
            return False
    return False


def _load_guild_slots():
    """Load the guild level -> max slots mapping."""
    with open(GUILD_SLOTS_FILE, "r") as f:
        return json.load(f)


def get_max_slots_for_level(guild_level: int) -> int:
    """Return the max member slots for a given guild level.

    The JSON keys are minimum-level thresholds. We pick the highest
    threshold that does not exceed the current level.
    """
    slots_map = _load_guild_slots()
    best_slots = 4  # fallback for level 0
    for level_str, slots in slots_map.items():
        if guild_level >= int(level_str):
            best_slots = max(best_slots, slots)
    return best_slots


def _apply_capacity_override(result: dict) -> dict:
    """If a capacity override is active, rewrite ``result`` to report the
    overridden number of open slots.

    The override sets ``player_count = max_slots - open_slots`` (clamped to
    ``[0, max_slots]``) and recomputes ``is_full``. A ``capacity_overridden``
    marker plus the raw ``override`` dict is added so callers/UIs can flag
    that they're seeing simulated data.
    """
    override = get_capacity_override()
    if override is None or "open_slots" not in override:
        return result

    max_slots = result.get("max_slots")
    if max_slots is None:
        # No real max_slots known; use open_slots directly as the inferred capacity.
        max_slots = max(1, int(override["open_slots"]))

    open_slots = max(0, min(int(override["open_slots"]), max_slots))
    player_count = max(0, max_slots - open_slots)

    return {
        **result,
        "max_slots": max_slots,
        "player_count": player_count,
        "effective_player_count": player_count,
        "pending_count": 0,
        "is_full": player_count >= max_slots,
        "capacity_overridden": True,
        "override": {"open_slots": open_slots},
    }


def get_guild_capacity() -> dict:
    """Read tracked_guild.json and return capacity info.

    Returns a dict with keys:
        guild_level, player_count, pending_count, effective_player_count,
        max_slots, is_full
    and, when a debug override is active, ``capacity_overridden`` and ``override``.

    ``effective_player_count`` = ``player_count`` + ``pending_count``. Players
    that were accepted and handed a ``/gu invite`` command but have not yet
    joined the guild in-game are tracked as pending invites and reserve a slot
    for up to ``PENDING_INVITE_TTL_DAYS`` days. ``is_full`` is based on the
    effective count so we don't over-invite.
    """
    pending_count = get_pending_invites_count()

    if not TRACKED_GUILD_FILE.exists():
        return _apply_capacity_override({
            "guild_level": None,
            "player_count": None,
            "pending_count": pending_count,
            "effective_player_count": None,
            "max_slots": None,
            "is_full": False,
        })

    try:
        with open(TRACKED_GUILD_FILE, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _apply_capacity_override({
            "guild_level": None,
            "player_count": None,
            "pending_count": pending_count,
            "effective_player_count": None,
            "max_slots": None,
            "is_full": False,
        })

    previous = data.get("previous_data", {})
    guild_level = previous.get("level")
    members = previous.get("members", {})
    player_count = sum(len(rank_list) for rank_list in members.values())
    effective_player_count = player_count + pending_count

    if guild_level is None:
        return _apply_capacity_override({
            "guild_level": None,
            "player_count": player_count,
            "pending_count": pending_count,
            "effective_player_count": effective_player_count,
            "max_slots": None,
            "is_full": False,
        })

    max_slots = get_max_slots_for_level(guild_level)
    return _apply_capacity_override({
        "guild_level": guild_level,
        "player_count": player_count,
        "pending_count": pending_count,
        "effective_player_count": effective_player_count,
        "max_slots": max_slots,
        "is_full": effective_player_count >= max_slots,
    })


def is_guild_full() -> bool:
    """Return True if the guild is at or over max capacity."""
    return get_guild_capacity()["is_full"]


# ---------------------------------------------------------------------------
# Pending invites
#
# When a ticket is accepted and the recruiter is handed a ``/gu invite`` command,
# the accepted player is added here. They reserve a slot toward the effective
# player count until either:
#   * the guild tracker sees them join the guild in-game (cleared by
#     ``remove_pending_invite_by_uuid`` / ``remove_pending_invite_by_username``),
#   * a staff member removes them manually (``remove_pending_invite``), or
#   * ``PENDING_INVITE_TTL_DAYS`` days elapse (auto-pruned).
# ---------------------------------------------------------------------------

def load_pending_invites() -> dict:
    """Load the pending-invite map from disk.

    Returns ``{}`` if the file is missing or corrupt. Keys are stringified
    Discord IDs.
    """
    if not PENDING_INVITES_FILE.exists():
        return {}
    try:
        with open(PENDING_INVITES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_pending_invites(data: dict) -> None:
    """Persist the pending-invite map to disk."""
    PENDING_INVITES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PENDING_INVITES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


def prune_expired_pending_invites() -> dict:
    """Remove pending invites older than ``PENDING_INVITE_TTL_DAYS`` days.

    Returns the (pruned) dict. Persists the change when something was removed.
    Entries with missing or unparseable ``invited_at`` are treated as expired
    and pruned, to avoid permanently stuck reservations.
    """
    data = load_pending_invites()
    if not data:
        return data

    now = datetime.now(timezone.utc)
    ttl = timedelta(days=PENDING_INVITE_TTL_DAYS)
    pruned = {}
    changed = False

    for key, entry in data.items():
        if not isinstance(entry, dict):
            changed = True
            continue
        invited_at_raw = entry.get("invited_at")
        if not invited_at_raw:
            changed = True
            continue
        try:
            invited_at = datetime.fromisoformat(invited_at_raw)
            if invited_at.tzinfo is None:
                invited_at = invited_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            changed = True
            continue
        if now - invited_at >= ttl:
            changed = True
            continue
        pruned[key] = entry

    if changed:
        save_pending_invites(pruned)
    return pruned


def get_pending_invites() -> dict:
    """Return the current (pruned) pending-invite map."""
    return prune_expired_pending_invites()


def get_pending_invites_count() -> int:
    """Return how many pending invites are currently reserving a slot."""
    return len(prune_expired_pending_invites())


def add_pending_invite(username: str, uuid: str | None, discord_id: int) -> dict:
    """Record ``discord_id`` as having been handed an in-game invite.

    Idempotent: re-accepting the same user just refreshes ``invited_at``.
    Returns the stored entry.
    """
    data = prune_expired_pending_invites()
    entry = {
        "username": username,
        "uuid": uuid,
        "invited_at": datetime.now(timezone.utc).isoformat(),
    }
    data[str(discord_id)] = entry
    save_pending_invites(data)
    return entry


def remove_pending_invite(discord_id: int) -> bool:
    """Remove the pending invite entry for ``discord_id``. Returns True if removed."""
    data = load_pending_invites()
    if str(discord_id) not in data:
        return False
    del data[str(discord_id)]
    save_pending_invites(data)
    return True


def remove_pending_invite_by_uuid(uuid: str) -> bool:
    """Remove the pending invite whose stored UUID matches ``uuid``.

    Returns True if an entry was removed.
    """
    if not uuid:
        return False
    data = load_pending_invites()
    target = None
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        if (entry.get("uuid") or "").lower() == uuid.lower():
            target = key
            break
    if target is None:
        return False
    del data[target]
    save_pending_invites(data)
    return True


def remove_pending_invite_by_username(username: str) -> bool:
    """Remove the pending invite whose stored username matches ``username``
    (case-insensitive). Returns True if an entry was removed.
    """
    if not username:
        return False
    data = load_pending_invites()
    target = None
    lowered = username.lower()
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        if (entry.get("username") or "").lower() == lowered:
            target = key
            break
    if target is None:
        return False
    del data[target]
    save_pending_invites(data)
    return True


# ---------------------------------------------------------------------------
# Queue management  (veteran = priority, normal = standard)
# ---------------------------------------------------------------------------

def load_queue() -> dict:
    """Load the member queue from disk.

    Returns ``{"veteran": [...], "normal": [...]}``.
    Automatically migrates the old flat-list format.
    """
    if QUEUE_FILE.exists():
        with open(QUEUE_FILE, "r") as f:
            data = json.load(f)
        # Migrate from old flat list format
        if isinstance(data, list):
            return {"veteran": [], "normal": data}
        return data
    return {"veteran": [], "normal": []}


def save_queue(queue: dict) -> None:
    """Persist the member queue to disk."""
    with open(QUEUE_FILE, "w") as f:
        json.dump(queue, f, indent=4)


def _effective_position(queue: dict, queue_type: str, position_in_queue: int) -> int:
    """Return the position within the player's own queue.

    Each queue (veteran / normal) is numbered independently starting from 1.
    Veterans are still treated as the priority queue when slots open.
    """
    return position_in_queue


def add_to_queue(username: str, uuid: str | None, discord_id: int, is_veteran: bool = False) -> tuple[int, str]:
    """Add a player to the appropriate queue.

    If the player is already queued, returns their existing effective position.
    Returns ``(effective_position, queue_type)`` where *queue_type* is
    ``"veteran"`` or ``"normal"``.
    """
    queue = load_queue()

    # Idempotent: skip if already in either queue
    for qt in ("veteran", "normal"):
        for entry in queue[qt]:
            if entry["discord_id"] == discord_id:
                return _effective_position(queue, qt, entry["position"]), qt

    queue_type = "veteran" if is_veteran else "normal"
    target = queue[queue_type]
    position = len(target) + 1
    target.append({
        "position": position,
        "username": username,
        "uuid": uuid,
        "discord_id": discord_id,
        "queued_at": datetime.now(timezone.utc).isoformat(),
    })
    save_queue(queue)
    return _effective_position(queue, queue_type, position), queue_type


def remove_from_queue(discord_id: int) -> bool:
    """Remove a player from the queue by Discord ID.

    Reorders remaining positions. Returns True if removed.
    """
    queue = load_queue()
    found = False
    for qt in ("veteran", "normal"):
        new_list = [e for e in queue[qt] if e["discord_id"] != discord_id]
        if len(new_list) != len(queue[qt]):
            found = True
            for i, entry in enumerate(new_list):
                entry["position"] = i + 1
            queue[qt] = new_list
    if found:
        save_queue(queue)
    return found


def get_queue_position(discord_id: int) -> tuple[int, str] | None:
    """Return ``(effective_position, queue_type)`` for a Discord user, or *None*."""
    queue = load_queue()
    for qt in ("veteran", "normal"):
        for entry in queue[qt]:
            if entry["discord_id"] == discord_id:
                return _effective_position(queue, qt, entry["position"]), qt
    return None


def move_in_queue(discord_id: int, new_position: int) -> tuple[int, str] | None:
    """Move a player to a new position within their current queue.

    *new_position* is 1-based within the player's queue type.
    Returns ``(effective_position, queue_type)`` or *None* if not found.
    """
    queue = load_queue()
    for qt in ("veteran", "normal"):
        for i, entry in enumerate(queue[qt]):
            if entry["discord_id"] == discord_id:
                queue[qt].pop(i)
                clamped = max(1, min(new_position, len(queue[qt]) + 1))
                queue[qt].insert(clamped - 1, entry)
                for j, e in enumerate(queue[qt]):
                    e["position"] = j + 1
                save_queue(queue)
                return _effective_position(queue, qt, clamped), qt
    return None


def switch_queue_type(discord_id: int) -> tuple[int, str, str] | None:
    """Switch a player between the veteran and normal queues.

    The player is appended to the end of the target queue.
    Returns ``(effective_position, new_queue_type, old_queue_type)`` or *None*.
    """
    queue = load_queue()
    for qt in ("veteran", "normal"):
        for i, entry in enumerate(queue[qt]):
            if entry["discord_id"] == discord_id:
                queue[qt].pop(i)
                for j, e in enumerate(queue[qt]):
                    e["position"] = j + 1
                new_qt = "normal" if qt == "veteran" else "veteran"
                entry["position"] = len(queue[new_qt]) + 1
                queue[new_qt].append(entry)
                save_queue(queue)
                return _effective_position(queue, new_qt, entry["position"]), new_qt, qt
    return None


def extract_username_from_embeds(embeds) -> str | None:
    """Extract the in-game username from forwarded application embeds."""
    for embed in embeds:
        for field in embed.fields:
            field_name_lower = field.name.lower()
            if any(kw in field_name_lower for kw in ['username', 'ign', 'in game name', 'in-game name', 'nickname']):
                username = field.value.replace('`', '').strip()
                if username and username != "*No answer provided*":
                    return username
    return None
