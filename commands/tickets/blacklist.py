import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any, Tuple, Set

import discord
from discord import app_commands
from discord.ui import View, Modal, TextInput, Select, Button

from utils.permissions import has_roles
from utils.paths import DB_DIR, DATA_DIR
from utils import errors

BLACKLIST_DB = DB_DIR / "blacklist.db"
USERNAME_MATCH_DB = DATA_DIR / "username_matches.json"

OWNER_ID = int(os.getenv("OWNER_ID")) if os.getenv("OWNER_ID") else 0
PARLIAMENT_ROLE_ID = 1055473486157598741
JUROR_ROLE_ID = 954566591520063510

CHECK_ROLES = [OWNER_ID, PARLIAMENT_ROLE_ID, JUROR_ROLE_ID]
MANAGE_ROLES = [OWNER_ID, PARLIAMENT_ROLE_ID]

# Stored categories
CATEGORY_ONE_WARNING = "one_warning"
CATEGORY_TWO_WARNINGS = "two_warnings"
CATEGORY_GUILD_KICK = "guild_kick"
CATEGORY_GUILD_BAN = "guild_ban"
CATEGORY_DISCORD_BAN = "discord_ban"
CATEGORY_RESTRICTION = "restriction"
CATEGORY_PERM_DEMOTION = "permanent_demotion"
CATEGORY_OTHER = "other"

# UI-only key for auto-escalating warnings
CATEGORY_WARNING = "warning"

ACTIVE_CATEGORIES = {
    CATEGORY_ONE_WARNING: {
        "label": "One Warning",
        "emoji": "⚠️",
        "color": 0xF1C40F,
        "description": "Player has received one warning",
    },
    CATEGORY_TWO_WARNINGS: {
        "label": "Two Warnings",
        "emoji": "❗",
        "color": 0xE67E22,
        "description": "Player has received two warnings",
    },
    CATEGORY_GUILD_KICK: {
        "label": "Guild Kick",
        "emoji": "👢",
        "color": 0xE74C3C,
        "description": "Kicked from the in-game guild",
    },
    CATEGORY_GUILD_BAN: {
        "label": "Guild Ban",
        "emoji": "🔨",
        "color": 0xC0392B,
        "description": "Banned from the in-game guild",
    },
    CATEGORY_DISCORD_BAN: {
        "label": "Discord Ban",
        "emoji": "⛔",
        "color": 0x922B21,
        "description": "Banned from the Discord server",
    },
    CATEGORY_RESTRICTION: {
        "label": "Restriction",
        "emoji": "🔒",
        "color": 0x8E44AD,
        "description": "Restricted from certain actions/privileges",
    },
    CATEGORY_PERM_DEMOTION: {
        "label": "Permanent Demotion",
        "emoji": "📉",
        "color": 0x9B59B6,
        "description": "Permanently capped at an in-game guild rank",
    },
    CATEGORY_OTHER: {
        "label": "Other",
        "emoji": "📝",
        "color": 0x7F8C8D,
        "description": "Other blacklist note",
    },
}

# Categories shown in the Add menu (warning auto-escalates)
ADD_CATEGORIES = {
    CATEGORY_WARNING: {
        "label": "Warning",
        "emoji": "⚠️",
        "description": "Auto: 1st warning, then 2nd warning",
    },
    CATEGORY_GUILD_KICK: ACTIVE_CATEGORIES[CATEGORY_GUILD_KICK],
    CATEGORY_GUILD_BAN: ACTIVE_CATEGORIES[CATEGORY_GUILD_BAN],
    CATEGORY_DISCORD_BAN: ACTIVE_CATEGORIES[CATEGORY_DISCORD_BAN],
    CATEGORY_RESTRICTION: ACTIVE_CATEGORIES[CATEGORY_RESTRICTION],
    CATEGORY_PERM_DEMOTION: ACTIVE_CATEGORIES[CATEGORY_PERM_DEMOTION],
    CATEGORY_OTHER: ACTIVE_CATEGORIES[CATEGORY_OTHER],
}

GUILD_RANK_LADDER = [
    "recruit",
    "recruiter",
    "captain",
    "strategist",
    "chief",
    "owner",
]
VALID_DEMOTION_RANKS = (
    "recruit",
    "recruiter",
    "captain",
    "strategist",
    "chief",
)
GUILD_RANK_INDEX = {name: idx for idx, name in enumerate(GUILD_RANK_LADDER)}
# Back-compat alias used by older UI code paths
DEMOTION_RANK_ROLES = {name: None for name in VALID_DEMOTION_RANKS}

SEVERITY_ORDER = {
    CATEGORY_ONE_WARNING: 1,
    CATEGORY_TWO_WARNINGS: 2,
    CATEGORY_OTHER: 3,
    CATEGORY_RESTRICTION: 4,
    CATEGORY_PERM_DEMOTION: 5,
    CATEGORY_GUILD_KICK: 6,
    CATEGORY_GUILD_BAN: 7,
    CATEGORY_DISCORD_BAN: 8,
}


def _now() -> Tuple[str, int]:
    now = datetime.now(timezone.utc)
    return now.isoformat(), int(now.timestamp())


def _norm_username(username: str) -> str:
    return (username or "").strip()


def _norm_key(username: str) -> str:
    return _norm_username(username).lower()


def _norm_uuid(uuid: Optional[str]) -> Optional[str]:
    if not uuid:
        return None
    value = re.sub(r"[^0-9a-fA-F]", "", str(uuid)).lower()
    if len(value) != 32:
        return None
    return f"{value[0:8]}-{value[8:12]}-{value[12:16]}-{value[16:20]}-{value[20:32]}"


def _norm_rank(rank: Optional[str]) -> Optional[str]:
    """Normalize an in-game guild rank. Returns None if not a known guild rank."""
    if not rank:
        return None
    value = rank.strip().lower().replace("_", " ").replace("-", " ")
    value = " ".join(value.split())
    # Accept singular/plural-ish typos
    aliases = {
        "recruits": "recruit",
        "recruiters": "recruiter",
        "captains": "captain",
        "strategists": "strategist",
        "chiefs": "chief",
        "owners": "owner",
    }
    value = aliases.get(value, value)
    if value not in GUILD_RANK_INDEX:
        return None
    return value


def _norm_demotion_cap(rank: Optional[str]) -> Optional[str]:
    """Normalize a selectable permanent-demotion cap rank."""
    value = _norm_rank(rank)
    if value is None or value not in VALID_DEMOTION_RANKS:
        return None
    return value


def _rank_display(rank: Optional[str]) -> str:
    if not rank:
        return "—"
    return " ".join(part.capitalize() for part in str(rank).split())


def _category_label(key: str) -> str:
    meta = ACTIVE_CATEGORIES.get(key)
    if meta:
        return f"{meta['emoji']} {meta['label']}"
    if key == CATEGORY_WARNING:
        return "⚠️ Warning"
    return key


def _category_color(keys: List[str]) -> int:
    if not keys:
        return 0x2ECC71
    best = max(keys, key=lambda k: SEVERITY_ORDER.get(k, 0))
    return ACTIVE_CATEGORIES.get(best, {}).get("color", 0xE74C3C)


def _parse_date_bound(value: Optional[str], *, end_of_day: bool = False) -> Optional[int]:
    """Parse YYYY-MM-DD or unix timestamp into unix seconds."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    try:
        dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if end_of_day:
            dt = dt + timedelta(days=1) - timedelta(seconds=1)
        return int(dt.timestamp())
    except ValueError:
        raise ValueError("Dates must be YYYY-MM-DD or a unix timestamp")


def _load_username_matches() -> Dict[str, Any]:
    try:
        if USERNAME_MATCH_DB.exists():
            with open(USERNAME_MATCH_DB, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[Blacklist] Failed to load username matches: {e}")
    return {}


def resolve_identity(
    *,
    username: Optional[str] = None,
    discord_id: Optional[int] = None,
    uuid: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve username / discord_id / uuid using local match DBs."""
    matches = _load_username_matches()
    resolved_username = _norm_username(username) if username else None
    resolved_discord_id = int(discord_id) if discord_id else None
    resolved_uuid = _norm_uuid(uuid)

    # From discord id
    if resolved_discord_id is not None:
        entry = matches.get(str(resolved_discord_id))
        if isinstance(entry, dict):
            if not resolved_username and entry.get("username"):
                resolved_username = _norm_username(entry["username"])
            if not resolved_uuid and entry.get("uuid"):
                resolved_uuid = _norm_uuid(entry["uuid"])
        elif isinstance(entry, str) and not resolved_username:
            resolved_username = _norm_username(entry)

    # From username -> discord id / uuid
    if resolved_username:
        key = _norm_key(resolved_username)
        for did, entry in matches.items():
            if isinstance(entry, dict):
                uname = _norm_key(entry.get("username") or "")
                if uname == key:
                    if resolved_discord_id is None:
                        try:
                            resolved_discord_id = int(did)
                        except ValueError:
                            pass
                    if not resolved_uuid and entry.get("uuid"):
                        resolved_uuid = _norm_uuid(entry["uuid"])
                    if entry.get("username"):
                        resolved_username = _norm_username(entry["username"])
                    break
            elif isinstance(entry, str) and _norm_key(entry) == key:
                if resolved_discord_id is None:
                    try:
                        resolved_discord_id = int(did)
                    except ValueError:
                        pass
                resolved_username = _norm_username(entry)
                break

    # From uuid -> discord id / username
    if resolved_uuid:
        for did, entry in matches.items():
            if isinstance(entry, dict) and _norm_uuid(entry.get("uuid")) == resolved_uuid:
                if resolved_discord_id is None:
                    try:
                        resolved_discord_id = int(did)
                    except ValueError:
                        pass
                if not resolved_username and entry.get("username"):
                    resolved_username = _norm_username(entry["username"])
                break

    return {
        "username": resolved_username,
        "discord_id": resolved_discord_id,
        "uuid": resolved_uuid,
    }


def get_related_usernames(username: str) -> List[str]:
    """Return username + linked alts/mains from blacklist account links."""
    clean = _norm_username(username)
    if not clean:
        return []

    related: Dict[str, str] = {_norm_key(clean): clean}

    # Expand through blacklist-managed account links (transitive)
    try:
        init_database()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT main_username, alt_username, main_username_key, alt_username_key
                FROM blacklist_account_links
                WHERE active = 1
                """
            )
            links = [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        print(f"[Blacklist] Failed to load account links: {e}")
        links = []

    changed = True
    while changed:
        changed = False
        for link in links:
            main_u = _norm_username(link.get("main_username") or "")
            alt_u = _norm_username(link.get("alt_username") or "")
            main_k = link.get("main_username_key") or _norm_key(main_u)
            alt_k = link.get("alt_username_key") or _norm_key(alt_u)
            if main_k in related or alt_k in related:
                if main_u and main_k not in related:
                    related[main_k] = main_u
                    changed = True
                if alt_u and alt_k not in related:
                    related[alt_k] = alt_u
                    changed = True

    # Also include username tied to the same Discord ID via username_matches
    identity = resolve_identity(username=clean)
    if identity.get("discord_id") is not None:
        matches = _load_username_matches()
        entry = matches.get(str(identity["discord_id"]))
        if isinstance(entry, dict) and entry.get("username"):
            related[_norm_key(entry["username"])] = _norm_username(entry["username"])
        elif isinstance(entry, str):
            related[_norm_key(entry)] = _norm_username(entry)

    ordered = [related[_norm_key(clean)]]
    for key, name in related.items():
        if key != _norm_key(clean):
            ordered.append(name)
    return ordered


@contextmanager
def get_db_connection():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(BLACKLIST_DB))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_column(cursor, table: str, column: str, definition: str):
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    if column not in existing:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_database():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS blacklist_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                username_key TEXT NOT NULL,
                category TEXT NOT NULL,
                reason TEXT NOT NULL,
                demotion_rank TEXT,
                demotion_role_id INTEGER,
                discord_id INTEGER,
                player_uuid TEXT,
                added_by_id INTEGER NOT NULL,
                added_by_name TEXT NOT NULL,
                added_at TEXT NOT NULL,
                added_unix INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                notes TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS blacklist_retractions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                username_key TEXT NOT NULL,
                entry_ids TEXT NOT NULL,
                categories TEXT NOT NULL,
                original_reasons TEXT NOT NULL,
                retraction_reason TEXT NOT NULL,
                demotion_ranks TEXT,
                discord_id INTEGER,
                player_uuid TEXT,
                retracted_by_id INTEGER NOT NULL,
                retracted_by_name TEXT NOT NULL,
                retracted_at TEXT NOT NULL,
                retracted_unix INTEGER NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS blacklist_account_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                main_username TEXT NOT NULL,
                main_username_key TEXT NOT NULL,
                alt_username TEXT NOT NULL,
                alt_username_key TEXT NOT NULL,
                note TEXT,
                linked_by_id INTEGER NOT NULL,
                linked_by_name TEXT NOT NULL,
                linked_at TEXT NOT NULL,
                linked_unix INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        # Migrations for older DBs
        _ensure_column(cursor, "blacklist_entries", "discord_id", "INTEGER")
        _ensure_column(cursor, "blacklist_entries", "player_uuid", "TEXT")
        _ensure_column(cursor, "blacklist_entries", "demotion_role_id", "INTEGER")
        _ensure_column(cursor, "blacklist_retractions", "discord_id", "INTEGER")
        _ensure_column(cursor, "blacklist_retractions", "player_uuid", "TEXT")

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_bl_username_key ON blacklist_entries(username_key)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_bl_active ON blacklist_entries(active)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_bl_category ON blacklist_entries(category)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_bl_discord_id ON blacklist_entries(discord_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_bl_player_uuid ON blacklist_entries(player_uuid)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_bl_added_by ON blacklist_entries(added_by_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_bl_added_unix ON blacklist_entries(added_unix)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_bl_ret_username_key ON blacklist_retractions(username_key)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_bl_link_main ON blacklist_account_links(main_username_key)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_bl_link_alt ON blacklist_account_links(alt_username_key)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_bl_link_active ON blacklist_account_links(active)"
        )


def _next_warning_category(username: str) -> str:
    """First warning -> one_warning; if any one_warning active (and no two) -> two_warnings."""
    entries = get_active_entries(username, include_alts=True)
    cats = {e["category"] for e in entries}
    if CATEGORY_TWO_WARNINGS in cats:
        raise ValueError("This player already has two active warnings")
    if CATEGORY_ONE_WARNING in cats:
        return CATEGORY_TWO_WARNINGS
    return CATEGORY_ONE_WARNING


def normalize_warning_levels_for_username(username: str) -> List[int]:
    """If only second-warning entries remain (no first), demote them to first warning.

    Returns the entry IDs that were converted.
    """
    if not username:
        return []

    init_database()
    active = get_active_entries(username, include_alts=True)
    one = [e for e in active if e.get("category") == CATEGORY_ONE_WARNING]
    two = [e for e in active if e.get("category") == CATEGORY_TWO_WARNINGS]

    # If a true first warning still exists, leave second warnings alone.
    if one or not two:
        return []

    # Only second-warning left -> convert all remaining seconds to first.
    converted_ids = [int(e["id"]) for e in two if e.get("id") is not None]
    if not converted_ids:
        return []

    with get_db_connection() as conn:
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in converted_ids)
        cursor.execute(
            f"""
            UPDATE blacklist_entries
            SET category = ?
            WHERE id IN ({placeholders}) AND active = 1 AND category = ?
            """,
            (CATEGORY_ONE_WARNING, *converted_ids, CATEGORY_TWO_WARNINGS),
        )

    print(
        f"[Blacklist] Normalized warning levels for {username}: "
        f"converted entry IDs {converted_ids} from two_warnings -> one_warning"
    )
    return converted_ids


def add_blacklist_entry(
    *,
    username: str,
    category: str,
    reason: str,
    added_by_id: int,
    added_by_name: str,
    demotion_rank: Optional[str] = None,
    notes: Optional[str] = None,
    discord_id: Optional[int] = None,
    uuid: Optional[str] = None,
) -> Dict[str, Any]:
    init_database()

    # Auto-escalating warning
    requested = category
    if category == CATEGORY_WARNING:
        category = _next_warning_category(username)

    if category not in ACTIVE_CATEGORIES:
        raise ValueError(f"Invalid category: {requested}")

    clean_username = _norm_username(username)
    if not clean_username:
        raise ValueError("Username is required")
    clean_reason = (reason or "").strip()
    if not clean_reason:
        raise ValueError("Reason is required")

    identity = resolve_identity(
        username=clean_username, discord_id=discord_id, uuid=uuid
    )
    if identity.get("username"):
        clean_username = identity["username"]
    resolved_discord_id = identity.get("discord_id")
    resolved_uuid = identity.get("uuid")

    rank = None
    if category == CATEGORY_PERM_DEMOTION:
        rank = _norm_demotion_cap(demotion_rank)
        if not rank:
            raise ValueError(
                "Permanent demotion requires a valid in-game guild rank "
                f"({', '.join(VALID_DEMOTION_RANKS)})"
            )

    timestamp_iso, unix_ts = _now()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO blacklist_entries (
                username, username_key, category, reason, demotion_rank, demotion_role_id,
                discord_id, player_uuid,
                added_by_id, added_by_name, added_at, added_unix, active, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                clean_username,
                _norm_key(clean_username),
                category,
                clean_reason,
                rank,
                None,
                resolved_discord_id,
                resolved_uuid,
                added_by_id,
                added_by_name,
                timestamp_iso,
                unix_ts,
                notes,
            ),
        )
        entry_id = cursor.lastrowid
        cursor.execute("SELECT * FROM blacklist_entries WHERE id = ?", (entry_id,))
        row = dict(cursor.fetchone())
        row["_requested_category"] = requested
        return row


def _entry_matches_identity(entry: Dict[str, Any], usernames: Set[str], discord_ids: Set[int], uuids: Set[str]) -> bool:
    if entry.get("username_key") in usernames:
        return True
    if entry.get("discord_id") and int(entry["discord_id"]) in discord_ids:
        return True
    if entry.get("player_uuid") and _norm_uuid(entry["player_uuid"]) in uuids:
        return True
    return False


def _collect_identity_sets(username: str) -> Tuple[Set[str], Set[int], Set[str], List[str]]:
    related = get_related_usernames(username)
    unames = {_norm_key(u) for u in related}
    discord_ids: Set[int] = set()
    uuids: Set[str] = set()
    for u in related:
        ident = resolve_identity(username=u)
        if ident.get("discord_id") is not None:
            discord_ids.add(int(ident["discord_id"]))
        if ident.get("uuid"):
            uuids.add(ident["uuid"])
    return unames, discord_ids, uuids, related


def get_entry_by_id(entry_id: int) -> Optional[Dict[str, Any]]:
    init_database()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM blacklist_entries WHERE id = ?", (int(entry_id),))
        row = cursor.fetchone()
        return dict(row) if row else None


def update_blacklist_entry(
    entry_id: int,
    *,
    username: Optional[str] = None,
    category: Optional[str] = None,
    reason: Optional[str] = None,
    demotion_rank: Optional[str] = None,
    discord_id: Optional[int] = None,
    uuid: Optional[str] = None,
    notes: Optional[str] = None,
    clear_discord_id: bool = False,
    clear_uuid: bool = False,
    clear_notes: bool = False,
) -> Dict[str, Any]:
    init_database()
    existing = get_entry_by_id(entry_id)
    if not existing:
        raise ValueError(f"No blacklist entry with ID `{entry_id}`")

    new_username = _norm_username(username) if username is not None else existing["username"]
    if not new_username:
        raise ValueError("Username is required")

    new_category = category if category is not None else existing["category"]
    if new_category not in ACTIVE_CATEGORIES:
        raise ValueError(f"Invalid category: {new_category}")

    new_reason = (reason if reason is not None else existing["reason"] or "").strip()
    if not new_reason:
        raise ValueError("Reason is required")

    new_rank = existing.get("demotion_rank")
    new_role_id = None
    if new_category == CATEGORY_PERM_DEMOTION:
        rank_src = demotion_rank if demotion_rank is not None else existing.get("demotion_rank")
        new_rank = _norm_demotion_cap(rank_src)
        if not new_rank:
            raise ValueError(
                "Permanent demotion requires a valid in-game guild rank "
                f"({', '.join(VALID_DEMOTION_RANKS)})"
            )
    else:
        new_rank = None

    identity = resolve_identity(
        username=new_username,
        discord_id=None if clear_discord_id else (
            discord_id if discord_id is not None else existing.get("discord_id")
        ),
        uuid=None if clear_uuid else (uuid if uuid is not None else existing.get("player_uuid")),
    )

    if clear_discord_id:
        resolved_discord_id = None
    elif discord_id is not None:
        resolved_discord_id = int(discord_id)
    else:
        resolved_discord_id = identity.get("discord_id") or existing.get("discord_id")

    if clear_uuid:
        resolved_uuid = None
    elif uuid is not None:
        resolved_uuid = _norm_uuid(uuid)
        if uuid.strip() and not resolved_uuid:
            raise ValueError("Invalid UUID format")
    else:
        resolved_uuid = identity.get("uuid") or existing.get("player_uuid")

    if clear_notes:
        new_notes = None
    elif notes is not None:
        new_notes = notes.strip() or None
    else:
        new_notes = existing.get("notes")

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE blacklist_entries SET
                username = ?,
                username_key = ?,
                category = ?,
                reason = ?,
                demotion_rank = ?,
                demotion_role_id = ?,
                discord_id = ?,
                player_uuid = ?,
                notes = ?
            WHERE id = ?
            """,
            (
                new_username,
                _norm_key(new_username),
                new_category,
                new_reason,
                new_rank,
                new_role_id,
                resolved_discord_id,
                resolved_uuid,
                new_notes,
                int(entry_id),
            ),
        )
        cursor.execute("SELECT * FROM blacklist_entries WHERE id = ?", (int(entry_id),))
        updated = dict(cursor.fetchone())

    # If this edit removed/changed a warning, keep the ladder consistent.
    old_cat = existing.get("category")
    new_cat = updated.get("category")
    touched_warnings = (
        old_cat in (CATEGORY_ONE_WARNING, CATEGORY_TWO_WARNINGS)
        or new_cat in (CATEGORY_ONE_WARNING, CATEGORY_TWO_WARNINGS)
    )
    if touched_warnings:
        try:
            targets = {
                existing.get("username") or "",
                updated.get("username") or "",
            }
            converted_all: List[int] = []
            for uname in targets:
                converted_all.extend(normalize_warning_levels_for_username(uname))
            if converted_all:
                updated["_normalized_warning_ids"] = sorted(set(converted_all))
                # Reload in case this same entry was converted
                refreshed = get_entry_by_id(entry_id)
                if refreshed:
                    refreshed["_normalized_warning_ids"] = updated["_normalized_warning_ids"]
                    return refreshed
        except Exception as e:
            print(f"[Blacklist] Warning normalize after edit failed: {e}")

    return updated


def delete_blacklist_entry(entry_id: int) -> Dict[str, Any]:
    """Hard-delete a blacklist entry permanently."""
    init_database()
    existing = get_entry_by_id(entry_id)
    if not existing:
        raise ValueError(f"No blacklist entry with ID `{entry_id}`")
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM blacklist_entries WHERE id = ?", (int(entry_id),))

    # If a second warning is left without a first, convert it down.
    if existing.get("category") in (CATEGORY_ONE_WARNING, CATEGORY_TWO_WARNINGS):
        try:
            converted = normalize_warning_levels_for_username(existing.get("username") or "")
            if converted:
                existing["_normalized_warning_ids"] = converted
        except Exception as e:
            print(f"[Blacklist] Warning normalize after delete failed: {e}")

    return existing


def get_active_entries(username: str, *, include_alts: bool = True) -> List[Dict[str, Any]]:
    init_database()
    unames, discord_ids, uuids, _related = _collect_identity_sets(username)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM blacklist_entries
            WHERE active = 1
            ORDER BY added_unix DESC, id DESC
            """
        )
        rows = [dict(r) for r in cursor.fetchall()]
    if not include_alts:
        key = _norm_key(username)
        return [r for r in rows if r.get("username_key") == key]
    return [r for r in rows if _entry_matches_identity(r, unames, discord_ids, uuids)]


def get_all_entries_for_user(username: str, *, include_alts: bool = True) -> List[Dict[str, Any]]:
    init_database()
    unames, discord_ids, uuids, _related = _collect_identity_sets(username)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM blacklist_entries
            ORDER BY added_unix DESC, id DESC
            """
        )
        rows = [dict(r) for r in cursor.fetchall()]
    if not include_alts:
        key = _norm_key(username)
        return [r for r in rows if r.get("username_key") == key]
    return [r for r in rows if _entry_matches_identity(r, unames, discord_ids, uuids)]


def get_retractions(username: str, *, include_alts: bool = True) -> List[Dict[str, Any]]:
    init_database()
    unames, discord_ids, uuids, _related = _collect_identity_sets(username)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM blacklist_retractions
            ORDER BY retracted_unix DESC, id DESC
            """
        )
        rows = [dict(r) for r in cursor.fetchall()]
    if not include_alts:
        key = _norm_key(username)
        return [r for r in rows if r.get("username_key") == key]
    return [r for r in rows if _entry_matches_identity(r, unames, discord_ids, uuids)]


def list_active_entries(
    *,
    category: Optional[str] = None,
    added_by_id: Optional[int] = None,
    start_unix: Optional[int] = None,
    end_unix: Optional[int] = None,
    username: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    init_database()
    clauses = ["active = 1"]
    params: List[Any] = []

    if category:
        clauses.append("category = ?")
        params.append(category)
    if added_by_id is not None:
        clauses.append("added_by_id = ?")
        params.append(int(added_by_id))
    if start_unix is not None:
        clauses.append("added_unix >= ?")
        params.append(int(start_unix))
    if end_unix is not None:
        clauses.append("added_unix <= ?")
        params.append(int(end_unix))
    if username:
        unames, discord_ids, uuids, _ = _collect_identity_sets(username)
        or_parts = []
        if unames:
            or_parts.append(
                f"username_key IN ({','.join('?' for _ in unames)})"
            )
            params.extend(sorted(unames))
        if discord_ids:
            or_parts.append(
                f"discord_id IN ({','.join('?' for _ in discord_ids)})"
            )
            params.extend(sorted(discord_ids))
        if uuids:
            or_parts.append(
                f"player_uuid IN ({','.join('?' for _ in uuids)})"
            )
            params.extend(sorted(uuids))
        if or_parts:
            clauses.append("(" + " OR ".join(or_parts) + ")")
        else:
            clauses.append("username_key = ?")
            params.append(_norm_key(username))

    where = " AND ".join(clauses)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT * FROM blacklist_entries
            WHERE {where}
            ORDER BY added_unix DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        )
        return [dict(row) for row in cursor.fetchall()]


def count_active_entries(
    *,
    category: Optional[str] = None,
    added_by_id: Optional[int] = None,
    start_unix: Optional[int] = None,
    end_unix: Optional[int] = None,
    username: Optional[str] = None,
) -> int:
    init_database()
    clauses = ["active = 1"]
    params: List[Any] = []
    if category:
        clauses.append("category = ?")
        params.append(category)
    if added_by_id is not None:
        clauses.append("added_by_id = ?")
        params.append(int(added_by_id))
    if start_unix is not None:
        clauses.append("added_unix >= ?")
        params.append(int(start_unix))
    if end_unix is not None:
        clauses.append("added_unix <= ?")
        params.append(int(end_unix))
    if username:
        clauses.append("username_key = ?")
        params.append(_norm_key(username))

    where = " AND ".join(clauses)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM blacklist_entries WHERE {where}", params)
        return int(cursor.fetchone()[0])


def list_retracted(*, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    init_database()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM blacklist_retractions
            ORDER BY retracted_unix DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        return [dict(row) for row in cursor.fetchall()]


def count_retractions() -> int:
    init_database()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM blacklist_retractions")
        return int(cursor.fetchone()[0])


def get_category_counts() -> Dict[str, int]:
    init_database()
    counts = {key: 0 for key in ACTIVE_CATEGORIES}
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT category, COUNT(*) as cnt
            FROM blacklist_entries
            WHERE active = 1
            GROUP BY category
            """
        )
        for row in cursor.fetchall():
            counts[row["category"]] = int(row["cnt"])
    return counts


def retract_entries(
    *,
    entry_ids: List[int],
    retraction_reason: str,
    retracted_by_id: int,
    retracted_by_name: str,
) -> Dict[str, Any]:
    init_database()
    clean_reason = (retraction_reason or "").strip()
    if not clean_reason:
        raise ValueError("Retraction reason is required")
    if not entry_ids:
        raise ValueError("No entries selected")

    timestamp_iso, unix_ts = _now()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in entry_ids)
        cursor.execute(
            f"""
            SELECT * FROM blacklist_entries
            WHERE id IN ({placeholders}) AND active = 1
            ORDER BY id ASC
            """,
            tuple(entry_ids),
        )
        rows = [dict(r) for r in cursor.fetchall()]
        if not rows:
            raise ValueError("No active entries found for the selected IDs")

        keys = {_norm_key(r["username"]) for r in rows}
        if len(keys) != 1:
            raise ValueError("All selected entries must belong to the same username")

        username = rows[0]["username"]
        categories = [r["category"] for r in rows]
        original_reasons = [r["reason"] for r in rows]
        demotion_ranks = [r["demotion_rank"] for r in rows if r.get("demotion_rank")]
        ids = [str(r["id"]) for r in rows]
        discord_id = rows[0].get("discord_id")
        player_uuid = rows[0].get("player_uuid")

        cursor.execute(
            f"""
            UPDATE blacklist_entries
            SET active = 0
            WHERE id IN ({placeholders}) AND active = 1
            """,
            tuple(entry_ids),
        )

        cursor.execute(
            """
            INSERT INTO blacklist_retractions (
                username, username_key, entry_ids, categories, original_reasons,
                retraction_reason, demotion_ranks, discord_id, player_uuid,
                retracted_by_id, retracted_by_name, retracted_at, retracted_unix
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                username,
                _norm_key(username),
                ",".join(ids),
                "|".join(categories),
                " || ".join(original_reasons),
                clean_reason,
                "|".join(demotion_ranks) if demotion_ranks else None,
                discord_id,
                player_uuid,
                retracted_by_id,
                retracted_by_name,
                timestamp_iso,
                unix_ts,
            ),
        )
        retraction_id = cursor.lastrowid
        cursor.execute(
            "SELECT * FROM blacklist_retractions WHERE id = ?",
            (retraction_id,),
        )
        result = dict(cursor.fetchone())

    # Keep warning ladder consistent after retracting a warning entry.
    if any(c in (CATEGORY_ONE_WARNING, CATEGORY_TWO_WARNINGS) for c in categories):
        try:
            converted = normalize_warning_levels_for_username(username)
            if converted:
                result["_normalized_warning_ids"] = converted
        except Exception as e:
            print(f"[Blacklist] Warning normalize after retract failed: {e}")

    return result


def link_accounts(
    *,
    main_username: str,
    alt_username: str,
    linked_by_id: int,
    linked_by_name: str,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    init_database()
    main_u = _norm_username(main_username)
    alt_u = _norm_username(alt_username)
    if not main_u or not alt_u:
        raise ValueError("Both main and alt usernames are required")
    if _norm_key(main_u) == _norm_key(alt_u):
        raise ValueError("Main and alt usernames must be different")

    main_k = _norm_key(main_u)
    alt_k = _norm_key(alt_u)
    timestamp_iso, unix_ts = _now()

    with get_db_connection() as conn:
        cursor = conn.cursor()
        # Already linked either direction?
        cursor.execute(
            """
            SELECT * FROM blacklist_account_links
            WHERE active = 1 AND (
                (main_username_key = ? AND alt_username_key = ?)
                OR (main_username_key = ? AND alt_username_key = ?)
            )
            LIMIT 1
            """,
            (main_k, alt_k, alt_k, main_k),
        )
        existing = cursor.fetchone()
        if existing:
            raise ValueError(
                f"`{main_u}` and `{alt_u}` are already linked "
                f"(link #{existing['id']})"
            )

        cursor.execute(
            """
            INSERT INTO blacklist_account_links (
                main_username, main_username_key, alt_username, alt_username_key,
                note, linked_by_id, linked_by_name, linked_at, linked_unix, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                main_u,
                main_k,
                alt_u,
                alt_k,
                (note or "").strip() or None,
                linked_by_id,
                linked_by_name,
                timestamp_iso,
                unix_ts,
            ),
        )
        link_id = cursor.lastrowid
        cursor.execute(
            "SELECT * FROM blacklist_account_links WHERE id = ?", (link_id,)
        )
        return dict(cursor.fetchone())


def unlink_accounts(
    *,
    link_ids: List[int],
    unlinked_by_id: int,
    unlinked_by_name: str,
) -> int:
    init_database()
    if not link_ids:
        raise ValueError("No links selected")

    with get_db_connection() as conn:
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in link_ids)
        cursor.execute(
            f"""
            UPDATE blacklist_account_links
            SET active = 0
            WHERE id IN ({placeholders}) AND active = 1
            """,
            tuple(link_ids),
        )
        return int(cursor.rowcount)


def list_account_links(
    *,
    username: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    init_database()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        if username:
            key = _norm_key(username)
            cursor.execute(
                """
                SELECT * FROM blacklist_account_links
                WHERE active = 1 AND (main_username_key = ? OR alt_username_key = ?)
                ORDER BY linked_unix DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (key, key, limit, offset),
            )
        else:
            cursor.execute(
                """
                SELECT * FROM blacklist_account_links
                WHERE active = 1
                ORDER BY linked_unix DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
        return [dict(row) for row in cursor.fetchall()]


def count_account_links(username: Optional[str] = None) -> int:
    init_database()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        if username:
            key = _norm_key(username)
            cursor.execute(
                """
                SELECT COUNT(*) FROM blacklist_account_links
                WHERE active = 1 AND (main_username_key = ? OR alt_username_key = ?)
                """,
                (key, key),
            )
        else:
            cursor.execute(
                "SELECT COUNT(*) FROM blacklist_account_links WHERE active = 1"
            )
        return int(cursor.fetchone()[0])


def is_blacklisted(username: str) -> Tuple[bool, Optional[str], List[Dict[str, Any]]]:
    entries = get_active_entries(username, include_alts=True)
    if not entries:
        return False, None, []

    parts = []
    for entry in entries:
        label = ACTIVE_CATEGORIES.get(entry["category"], {}).get("label", entry["category"])
        extra = ""
        if entry["category"] == CATEGORY_PERM_DEMOTION and entry.get("demotion_rank"):
            extra = f" (cap: {_rank_display(entry['demotion_rank'])})"
        parts.append(f"{label}{extra}: {entry['reason']}")
    return True, " | ".join(parts), entries


# Application embed categories to surface (active)
APPLICATION_SURFACE_CATEGORIES = {
    CATEGORY_ONE_WARNING,
    CATEGORY_TWO_WARNINGS,
    CATEGORY_OTHER,
    CATEGORY_GUILD_KICK,
}

DEMOTION_ALERT_THREAD_ID = 1462881693865218150


def resolve_username_for_discord(discord_id: int) -> Optional[str]:
    """Best-effort username from username_matches.json."""
    identity = resolve_identity(discord_id=int(discord_id))
    return identity.get("username")


def has_active_guild_ban(*, username: Optional[str] = None, discord_id: Optional[int] = None) -> bool:
    """True if this identity (or linked alts) has an active guild ban."""
    names: List[str] = []
    if username:
        names.append(username)
    if discord_id is not None:
        resolved = resolve_username_for_discord(discord_id)
        if resolved:
            names.append(resolved)

    seen = set()
    for name in names:
        key = _norm_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        for entry in get_active_entries(name, include_alts=True):
            if entry.get("category") == CATEGORY_GUILD_BAN:
                return True

    # Also check entries stored directly against this discord id
    if discord_id is not None:
        init_database()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT 1 FROM blacklist_entries
                WHERE active = 1 AND category = ? AND discord_id = ?
                LIMIT 1
                """,
                (CATEGORY_GUILD_BAN, int(discord_id)),
            )
            if cursor.fetchone():
                return True
    return False


def is_guild_member_application(application_name: str) -> bool:
    name = (application_name or "").strip().lower()
    if not name:
        return False
    return (
        name == "guild member"
        or name == "guild members"
        or "guild member" in name
        or name.replace("-", " ").replace("_", " ") == "guild member"
    )


def get_application_blacklist_context(
    username: Optional[str] = None,
    discord_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Collect warnings/other/kicks + retractions for application embeds."""
    names: List[str] = []
    if username:
        names.append(_norm_username(username))
    if discord_id is not None:
        resolved = resolve_username_for_discord(discord_id)
        if resolved:
            names.append(resolved)

    active: List[Dict[str, Any]] = []
    retractions: List[Dict[str, Any]] = []
    seen_entry_ids = set()
    seen_ret_ids = set()

    for name in names:
        if not name:
            continue
        for entry in get_active_entries(name, include_alts=True):
            eid = entry.get("id")
            if eid in seen_entry_ids:
                continue
            if entry.get("category") in APPLICATION_SURFACE_CATEGORIES:
                seen_entry_ids.add(eid)
                active.append(entry)
        for ret in get_retractions(name, include_alts=True):
            rid = ret.get("id")
            if rid in seen_ret_ids:
                continue
            seen_ret_ids.add(rid)
            retractions.append(ret)

    # Direct discord_id lookup for active surface categories
    if discord_id is not None:
        init_database()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM blacklist_entries
                WHERE active = 1 AND discord_id = ?
                ORDER BY added_unix DESC, id DESC
                """,
                (int(discord_id),),
            )
            for row in cursor.fetchall():
                entry = dict(row)
                eid = entry.get("id")
                if eid in seen_entry_ids:
                    continue
                if entry.get("category") in APPLICATION_SURFACE_CATEGORIES:
                    seen_entry_ids.add(eid)
                    active.append(entry)

    active.sort(key=lambda e: int(e.get("added_unix") or 0), reverse=True)
    retractions.sort(key=lambda e: int(e.get("retracted_unix") or 0), reverse=True)
    return {"active": active, "retractions": retractions}


def format_application_blacklist_fields(
    username: Optional[str] = None,
    discord_id: Optional[int] = None,
) -> List[Tuple[str, str]]:
    """Return embed fields (name, value) for application answer embeds."""
    ctx = get_application_blacklist_context(username=username, discord_id=discord_id)
    fields: List[Tuple[str, str]] = []

    if ctx["active"]:
        lines = []
        for entry in ctx["active"][:12]:
            label = ACTIVE_CATEGORIES.get(entry["category"], {}).get(
                "label", entry["category"]
            )
            when = entry.get("added_unix")
            when_txt = f"<t:{when}:F> (<t:{when}:R>)" if when else "unknown time"
            uname = entry.get("username") or username or "?"
            lines.append(
                f"• **{label}** on `{uname}`\n"
                f"  Reason: {entry.get('reason') or '—'}\n"
                f"  When: {when_txt}"
            )
        if len(ctx["active"]) > 12:
            lines.append(f"…and {len(ctx['active']) - 12} more active entries")
        fields.append(("⚠️ Blacklist History (Active)", "\n".join(lines)[:1024]))

    if ctx["retractions"]:
        lines = []
        for ret in ctx["retractions"][:10]:
            cats = [
                _category_label(c)
                for c in (ret.get("categories") or "").split("|")
                if c
            ]
            when = ret.get("retracted_unix")
            when_txt = f"<t:{when}:F> (<t:{when}:R>)" if when else "unknown time"
            lines.append(
                f"• **Retracted** on `{ret.get('username') or '?'}`\n"
                f"  Categories: {', '.join(cats) if cats else '—'}\n"
                f"  Original: {(ret.get('original_reasons') or '—')[:180]}\n"
                f"  Removed because: {(ret.get('retraction_reason') or '—')[:180]}\n"
                f"  When: {when_txt}"
            )
        if len(ctx["retractions"]) > 10:
            lines.append(f"…and {len(ctx['retractions']) - 10} more retractions")
        fields.append(("♻️ Blacklist Retractions", "\n".join(lines)[:1024]))

    return fields


def _rank_index(rank_name: Optional[str]) -> Optional[int]:
    if not rank_name:
        return None
    normalized = _norm_rank(rank_name)
    if normalized is None:
        return None
    return GUILD_RANK_INDEX.get(normalized)


def get_permanent_demotion_caps_for_username(
    username: str,
    *,
    uuid: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Active permanent demotion entries for a Minecraft username / linked alts."""
    caps: List[Dict[str, Any]] = []
    seen = set()

    for entry in get_active_entries(username, include_alts=True):
        if entry.get("category") != CATEGORY_PERM_DEMOTION:
            continue
        eid = entry.get("id")
        if eid in seen:
            continue
        seen.add(eid)
        caps.append(entry)

    # Also match by player UUID when provided
    normalized_uuid = _norm_uuid(uuid)
    if normalized_uuid:
        init_database()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM blacklist_entries
                WHERE active = 1 AND category = ? AND player_uuid = ?
                """,
                (CATEGORY_PERM_DEMOTION, normalized_uuid),
            )
            for row in cursor.fetchall():
                entry = dict(row)
                eid = entry.get("id")
                if eid in seen:
                    continue
                seen.add(eid)
                caps.append(entry)
    return caps


def find_ingame_demotion_violations(
    *,
    username: str,
    new_rank: str,
    uuid: Optional[str] = None,
    old_rank: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """If in-game rank is at/above a permanent demotion cap, return violations."""
    current = _norm_rank(new_rank)
    if current is None:
        return []
    current_idx = GUILD_RANK_INDEX[current]

    violations = []
    for cap in get_permanent_demotion_caps_for_username(username, uuid=uuid):
        cap_rank = _norm_demotion_cap(cap.get("demotion_rank")) or _norm_rank(cap.get("demotion_rank"))
        if cap_rank is None or cap_rank not in VALID_DEMOTION_RANKS:
            # Allow stored historical values only if they are valid guild ranks
            continue
        cap_idx = GUILD_RANK_INDEX.get(cap_rank)
        if cap_idx is None:
            continue
        # "rank or above their perma demotion" => current >= cap
        if current_idx >= cap_idx:
            violations.append(
                {
                    "entry": cap,
                    "username": username,
                    "uuid": uuid,
                    "old_rank": _norm_rank(old_rank),
                    "current_rank": current,
                    "cap_rank": cap_rank,
                }
            )
    return violations


async def check_ingame_rank_for_demotion_violations(
    bot,
    *,
    username: str,
    new_rank: str,
    uuid: Optional[str] = None,
    old_rank: Optional[str] = None,
    guild=None,
):
    """Public hook for guild tracker rank-change events."""
    try:
        violations = find_ingame_demotion_violations(
            username=username,
            new_rank=new_rank,
            uuid=uuid,
            old_rank=old_rank,
        )
    except Exception as e:
        print(f"[Blacklist] In-game demotion check failed for {username}: {e}")
        return
    if violations:
        await _send_demotion_violation_alert(
            bot,
            violations,
            guild=guild,
        )


def _identity_lines(username: str) -> List[str]:
    identity = resolve_identity(username=username)
    related = get_related_usernames(username)
    lines = []
    if identity.get("discord_id"):
        lines.append(f"**Discord:** <@{identity['discord_id']}> (`{identity['discord_id']}`)")
    if identity.get("uuid"):
        lines.append(f"**UUID:** `{identity['uuid']}`")
    alts = [u for u in related if _norm_key(u) != _norm_key(username)]
    if alts:
        lines.append("**Linked accounts:** " + ", ".join(f"`{u}`" for u in alts[:12]))
        if len(alts) > 12:
            lines.append(f"_…and {len(alts) - 12} more linked accounts_")
    return lines


def build_check_embeds(username: str) -> List[discord.Embed]:
    related = get_related_usernames(username)
    active = get_active_entries(username, include_alts=True)
    history = get_all_entries_for_user(username, include_alts=True)
    retractions = get_retractions(username, include_alts=True)

    display_name = related[0] if related else username
    identity = resolve_identity(username=display_name)

    color = _category_color([e["category"] for e in active]) if active else 0x2ECC71
    desc_lines = [
        f"**Status:** {'🚫 ACTIVE on blacklist' if active else '✅ Not currently blacklisted'}",
        f"**Active entries:** `{len(active)}`",
        f"**Historical entries:** `{len(history)}`",
        f"**Retractions:** `{len(retractions)}`",
        f"**NameMC:** [Open profile](https://namemc.com/search?q={display_name})",
    ]
    desc_lines.extend(_identity_lines(display_name))

    main = discord.Embed(
        title=f"Blacklist Check: {display_name}",
        description="\n".join(desc_lines),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    if active:
        categories = sorted(
            {e["category"] for e in active},
            key=lambda k: -SEVERITY_ORDER.get(k, 0),
        )
        main.add_field(
            name="Active Categories",
            value="\n".join(f"• {_category_label(c)}" for c in categories),
            inline=False,
        )
    embeds = [main]

    if active:
        active_embed = discord.Embed(
            title="Active Entries (includes linked alts)",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        for entry in active[:20]:
            value_lines = [
                f"**Username:** `{entry['username']}`",
                f"**Reason:** {entry['reason']}",
                f"**Added by:** {entry['added_by_name']} (`{entry['added_by_id']}`)",
                f"**When:** <t:{entry['added_unix']}:F> (<t:{entry['added_unix']}:R>)",
                f"**Entry ID:** `{entry['id']}`",
            ]
            if entry.get("discord_id"):
                value_lines.append(f"**Discord:** <@{entry['discord_id']}> (`{entry['discord_id']}`)")
            if entry.get("player_uuid"):
                value_lines.append(f"**UUID:** `{entry['player_uuid']}`")
            if entry.get("demotion_rank"):
                rank_line = (
                    f"**In-game rank cap:** "
                    f"`{_rank_display(entry['demotion_rank']).upper()}`"
                )
                value_lines.insert(2, rank_line)
            if entry.get("notes"):
                value_lines.append(f"**Notes:** {entry['notes']}")
            active_embed.add_field(
                name=_category_label(entry["category"]),
                value="\n".join(value_lines)[:1024],
                inline=False,
            )
        if len(active) > 20:
            active_embed.set_footer(text=f"Showing 20 of {len(active)} active entries")
        embeds.append(active_embed)

    if retractions:
        ret_embed = discord.Embed(
            title="Retractions",
            color=0x3498DB,
            timestamp=datetime.now(timezone.utc),
        )
        for ret in retractions[:15]:
            cats = [
                _category_label(c)
                for c in (ret.get("categories") or "").split("|")
                if c
            ]
            ranks = [r for r in (ret.get("demotion_ranks") or "").split("|") if r]
            value_lines = [
                f"**Username:** `{ret['username']}`",
                f"**Retracted categories:** {', '.join(cats) if cats else '—'}",
                f"**Original reasons:** {ret.get('original_reasons') or '—'}",
                f"**Retraction reason:** {ret.get('retraction_reason') or '—'}",
                f"**By:** {ret['retracted_by_name']} (`{ret['retracted_by_id']}`)",
                f"**When:** <t:{ret['retracted_unix']}:F> (<t:{ret['retracted_unix']}:R>)",
                f"**Entry IDs:** `{ret.get('entry_ids') or '—'}`",
            ]
            if ranks:
                value_lines.insert(
                    2,
                    f"**Demotion ranks:** {', '.join(_rank_display(r) for r in ranks)}",
                )
            ret_embed.add_field(
                name=f"Retraction #{ret['id']}",
                value="\n".join(value_lines)[:1024],
                inline=False,
            )
        embeds.append(ret_embed)

    if len(embeds) == 1 and not active and not history and not retractions:
        main.description += "\n\nNo blacklist records found for this username or linked accounts."

    # silence unused identity lint
    _ = identity
    return embeds


def build_warning_field(username: str) -> Optional[Tuple[str, str, int]]:
    listed, _summary, entries = is_blacklisted(username)
    if not listed:
        return None

    lines = ["🚫 **BLACKLISTED USER DETECTED**"]
    for entry in entries[:8]:
        label = ACTIVE_CATEGORIES.get(entry["category"], {}).get(
            "label", entry["category"]
        )
        extra = ""
        if entry.get("demotion_rank"):
            extra = f" · in-game cap `{_rank_display(entry['demotion_rank']).upper()}`"
        uname_note = ""
        if _norm_key(entry["username"]) != _norm_key(username):
            uname_note = f" _(on `{entry['username']}`)_"
        lines.append(f"• **{label}**{extra}{uname_note}: {entry['reason']}")
        lines.append(
            f"  ↳ by {entry['added_by_name']} · <t:{entry['added_unix']}:d> · id `{entry['id']}`"
        )
    if len(entries) > 8:
        lines.append(f"…and {len(entries) - 8} more. Use `/blacklist_check`.")
    lines.append(f"**NameMC:** [View Profile](https://namemc.com/search?q={username})")
    color = _category_color([e["category"] for e in entries])
    return ("⚠️ Blacklist Status", "\n".join(lines)[:1024], color)


def build_manage_home_embed() -> discord.Embed:
    counts = get_category_counts()
    total = sum(counts.values())
    retracted = count_retractions()
    embed = discord.Embed(
        title="Blacklist Manager",
        description=(
            "Manage guild/Discord discipline records.\n\n"
            f"**Active records:** `{total}`\n"
            f"**Retractions on file:** `{retracted}`\n\n"
            "Warnings auto-escalate: 1st → One Warning, 2nd → Two Warnings.\n"
            "Use **Link Alts** to connect main/alt usernames for shared checks."
        ),
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    for key, meta in ACTIVE_CATEGORIES.items():
        embed.add_field(
            name=f"{meta['emoji']} {meta['label']}",
            value=f"`{counts.get(key, 0)}` active",
            inline=True,
        )
    embed.add_field(
        name="🔗 Linked Alts",
        value=f"`{count_account_links()}` active links",
        inline=True,
    )
    embed.set_footer(text="Add · Retract · List · Search · Link Alts · Inspect")
    return embed


def build_list_embed(
    *,
    entries: List[Dict[str, Any]],
    page: int,
    total: int,
    category: Optional[str] = None,
    retracted_mode: bool = False,
    title_suffix: str = "",
) -> discord.Embed:
    title = "Retracted Entries" if retracted_mode else "Active Blacklist Entries"
    if category and not retracted_mode:
        title = f"Active: {ACTIVE_CATEGORIES.get(category, {}).get('label', category)}"
    if title_suffix:
        title = f"{title} · {title_suffix}"

    embed = discord.Embed(
        title=title,
        description=f"Page **{page + 1}** · showing `{len(entries)}` of `{total}`",
        color=0x3498DB if retracted_mode else 0xE74C3C,
        timestamp=datetime.now(timezone.utc),
    )
    if not entries:
        embed.description += "\n\nNo entries found."
        return embed

    if retracted_mode:
        for ret in entries:
            cats = [
                _category_label(c)
                for c in (ret.get("categories") or "").split("|")
                if c
            ]
            embed.add_field(
                name=f"{ret['username']} · retraction #{ret['id']}",
                value=(
                    f"**Categories:** {', '.join(cats) or '—'}\n"
                    f"**Original:** {(ret.get('original_reasons') or '—')[:180]}\n"
                    f"**Removed because:** {(ret.get('retraction_reason') or '—')[:180]}\n"
                    f"**By:** {ret['retracted_by_name']} · <t:{ret['retracted_unix']}:R>"
                )[:1024],
                inline=False,
            )
    else:
        for entry in entries:
            extra = ""
            if entry.get("demotion_rank"):
                extra = (
                    f"\n**In-game rank cap:** "
                    f"`{_rank_display(entry['demotion_rank']).upper()}`"
                )
            ids = []
            if entry.get("discord_id"):
                ids.append(f"dc `{entry['discord_id']}`")
            if entry.get("player_uuid"):
                ids.append(f"uuid `{entry['player_uuid'][:8]}…`")
            id_line = ("\n" + " · ".join(ids)) if ids else ""
            embed.add_field(
                name=(
                    f"{entry['username']} · {_category_label(entry['category'])} "
                    f"(#{entry['id']})"
                ),
                value=(
                    f"**Reason:** {entry['reason'][:200]}{extra}{id_line}\n"
                    f"**By:** {entry['added_by_name']} (`{entry['added_by_id']}`) · "
                    f"<t:{entry['added_unix']}:R>"
                )[:1024],
                inline=False,
            )
    return embed


class BlacklistAddModal(Modal):
    def __init__(self, category: str, manager_view: "BlacklistManageView"):
        if category == CATEGORY_WARNING:
            title = "Add Warning"
        else:
            meta = ACTIVE_CATEGORIES[category]
            title = f"Add {meta['label']}"
        super().__init__(title=title[:45])
        self.category = category
        self.manager_view = manager_view

        self.username_input = TextInput(
            label="Username",
            placeholder="Minecraft username",
            required=True,
            max_length=32,
        )
        self.reason_input = TextInput(
            label="Reason",
            placeholder="Why is this user being added?",
            required=True,
            style=discord.TextStyle.paragraph,
            max_length=500,
        )
        self.add_item(self.username_input)
        self.add_item(self.reason_input)

        # Discord modals allow max 5 inputs. For demotions, rank takes a slot.
        if category == CATEGORY_PERM_DEMOTION:
            self.rank_input = TextInput(
                label="In-game Rank Cap",
                placeholder="recruit / recruiter / captain / strategist / chief",
                required=True,
                max_length=32,
            )
            self.add_item(self.rank_input)
        else:
            self.rank_input = None

        self.discord_id_input = TextInput(
            label="Discord User ID (optional)",
            placeholder="Links entry to a Discord account",
            required=False,
            max_length=25,
        )
        self.uuid_input = TextInput(
            label="Player UUID (optional)",
            placeholder="Survives username changes",
            required=False,
            max_length=36,
        )
        self.add_item(self.discord_id_input)
        self.add_item(self.uuid_input)

    async def on_submit(self, interaction: discord.Interaction):
        discord_id = None
        raw_did = (self.discord_id_input.value or "").strip()
        if raw_did:
            if not raw_did.isdigit():
                await errors.send_custom_error(
                    interaction, "Invalid Discord ID", "Discord ID must be numeric."
                )
                return
            discord_id = int(raw_did)

        try:
            entry = add_blacklist_entry(
                username=self.username_input.value,
                category=self.category,
                reason=self.reason_input.value,
                demotion_rank=self.rank_input.value if self.rank_input else None,
                added_by_id=interaction.user.id,
                added_by_name=str(interaction.user),
                discord_id=discord_id,
                uuid=(self.uuid_input.value or "").strip() or None,
            )
        except ValueError as e:
            await errors.send_custom_error(interaction, "Invalid Entry", str(e))
            return
        except Exception as e:
            print(f"[Blacklist] Failed to add entry: {e}")
            await errors.DATABASE_ERROR.send(interaction)
            return

        cat = entry["category"]
        embed = discord.Embed(
            title="✅ Blacklist Entry Added",
            color=ACTIVE_CATEGORIES[cat]["color"],
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Username", value=f"`{entry['username']}`", inline=True)
        embed.add_field(name="Category", value=_category_label(cat), inline=True)
        embed.add_field(name="Entry ID", value=f"`{entry['id']}`", inline=True)
        if entry.get("_requested_category") == CATEGORY_WARNING:
            if cat == CATEGORY_ONE_WARNING:
                embed.add_field(
                    name="Escalation",
                    value="First warning applied (One Warning).",
                    inline=False,
                )
            elif cat == CATEGORY_TWO_WARNINGS:
                embed.add_field(
                    name="Escalation",
                    value="Second warning applied (Two Warnings).",
                    inline=False,
                )
        embed.add_field(name="Reason", value=entry["reason"], inline=False)
        if entry.get("discord_id"):
            embed.add_field(
                name="Discord",
                value=f"<@{entry['discord_id']}> (`{entry['discord_id']}`)",
                inline=True,
            )
        if entry.get("player_uuid"):
            embed.add_field(name="UUID", value=f"`{entry['player_uuid']}`", inline=True)
        if entry.get("demotion_rank"):
            embed.add_field(
                name="In-game Rank Cap",
                value=f"`{_rank_display(entry['demotion_rank']).upper()}`",
                inline=True,
            )

        linked = get_related_usernames(entry["username"])
        alts = [u for u in linked if _norm_key(u) != _norm_key(entry["username"])]
        if alts:
            embed.add_field(
                name="Linked accounts",
                value=", ".join(f"`{u}`" for u in alts[:10]),
                inline=False,
            )

        embed.set_footer(text=f"Added by {interaction.user}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

        await self.manager_view.return_to_home()


class BlacklistRetractModal(Modal, title="Retract Blacklist Entries"):
    def __init__(
        self, entries: List[Dict[str, Any]], manager_view: "BlacklistManageView"
    ):
        super().__init__()
        self.entries = entries
        self.manager_view = manager_view
        self.reason_input = TextInput(
            label="Retraction Reason",
            placeholder="Why are these entries being removed?",
            required=True,
            style=discord.TextStyle.paragraph,
            max_length=500,
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            retraction = retract_entries(
                entry_ids=[e["id"] for e in self.entries],
                retraction_reason=self.reason_input.value,
                retracted_by_id=interaction.user.id,
                retracted_by_name=str(interaction.user),
            )
        except ValueError as e:
            await errors.send_custom_error(interaction, "Retraction Failed", str(e))
            return
        except Exception as e:
            print(f"[Blacklist] Failed to retract: {e}")
            await errors.DATABASE_ERROR.send(interaction)
            return

        cats = [
            _category_label(c)
            for c in (retraction.get("categories") or "").split("|")
            if c
        ]
        embed = discord.Embed(
            title="♻️ Entries Retracted",
            description=(
                f"**Username:** `{retraction['username']}`\n"
                f"**Categories removed:** {', '.join(cats)}\n"
                f"**Original reasons:** {retraction.get('original_reasons')}\n"
                f"**Retraction reason:** {retraction.get('retraction_reason')}\n"
                f"**Retraction ID:** `{retraction['id']}`"
            ),
            color=0x3498DB,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Retracted by {interaction.user}")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await self.manager_view.return_to_home()


class BlacklistSearchModal(Modal, title="Search Blacklist"):
    def __init__(self, manager_view: "BlacklistManageView"):
        super().__init__()
        self.manager_view = manager_view

        # Prefill from current search so refining is easy
        cur_user = manager_view.search_username or ""
        cur_by = str(manager_view.search_added_by_id) if manager_view.search_added_by_id else ""
        cur_start = ""
        cur_end = ""
        if manager_view.search_start_unix:
            cur_start = datetime.fromtimestamp(
                manager_view.search_start_unix, tz=timezone.utc
            ).strftime("%Y-%m-%d")
        if manager_view.search_end_unix:
            cur_end = datetime.fromtimestamp(
                manager_view.search_end_unix, tz=timezone.utc
            ).strftime("%Y-%m-%d")

        self.username_input = TextInput(
            label="Username (optional)",
            placeholder="Search username / linked alts",
            required=False,
            max_length=32,
            default=cur_user[:32] if cur_user else None,
        )
        self.added_by_input = TextInput(
            label="Added by Discord ID (optional)",
            placeholder="e.g. 123456789012345678",
            required=False,
            max_length=25,
            default=cur_by[:25] if cur_by else None,
        )
        self.start_input = TextInput(
            label="Start date (optional)",
            placeholder="YYYY-MM-DD",
            required=False,
            max_length=32,
            default=cur_start or None,
        )
        self.end_input = TextInput(
            label="End date (optional)",
            placeholder="YYYY-MM-DD",
            required=False,
            max_length=32,
            default=cur_end or None,
        )
        self.add_item(self.username_input)
        self.add_item(self.added_by_input)
        self.add_item(self.start_input)
        self.add_item(self.end_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            added_by = None
            raw_by = (self.added_by_input.value or "").strip()
            if raw_by:
                if not raw_by.isdigit():
                    raise ValueError("Added-by must be a numeric Discord ID")
                added_by = int(raw_by)

            start_unix = _parse_date_bound(self.start_input.value, end_of_day=False)
            end_unix = _parse_date_bound(self.end_input.value, end_of_day=True)
            username = (self.username_input.value or "").strip() or None
        except ValueError as e:
            await errors.send_custom_error(interaction, "Invalid Search", str(e))
            return

        # Search sets the base query; keep the current category filter on top
        self.manager_view.search_username = username
        self.manager_view.search_added_by_id = added_by
        self.manager_view.search_start_unix = start_unix
        self.manager_view.search_end_unix = end_unix

        await self.manager_view.show_list(
            interaction,
            category=self.manager_view.filter_category,
            retracted_mode=self.manager_view.retracted_mode,
            page=0,
        )


class CategorySelect(Select):
    def __init__(self, manager_view: "BlacklistManageView", mode: str):
        self.manager_view = manager_view
        self.mode = mode
        if mode == "add":
            options = [
                discord.SelectOption(
                    label=meta["label"],
                    value=key,
                    description=meta.get("description", "")[:100],
                    emoji=meta.get("emoji"),
                )
                for key, meta in ADD_CATEGORIES.items()
            ]
        else:
            options = [
                discord.SelectOption(
                    label=meta["label"],
                    value=key,
                    description=meta["description"][:100],
                    emoji=meta["emoji"],
                )
                for key, meta in ACTIVE_CATEGORIES.items()
            ]
            options.insert(
                0,
                discord.SelectOption(
                    label="All Active",
                    value="__all__",
                    description="Show every active entry",
                    emoji="📋",
                ),
            )
            options.append(
                discord.SelectOption(
                    label="Retracted",
                    value="__retracted__",
                    description="Show retracted history",
                    emoji="♻️",
                )
            )
        super().__init__(
            placeholder="Choose a category...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        if self.mode == "add":
            await interaction.response.send_modal(
                BlacklistAddModal(value, self.manager_view)
            )
            return

        # Filter applies category on top of the current search
        if value == "__retracted__":
            self.manager_view.filter_category = None
            await self.manager_view.show_list(
                interaction, category=None, retracted_mode=True, page=0
            )
        elif value == "__all__":
            self.manager_view.filter_category = None
            await self.manager_view.show_list(
                interaction, category=None, retracted_mode=False, page=0
            )
        else:
            self.manager_view.filter_category = value
            await self.manager_view.show_list(
                interaction, category=value, retracted_mode=False, page=0
            )


class RetractEntrySelect(Select):
    def __init__(
        self,
        manager_view: "BlacklistManageView",
        entries: List[Dict[str, Any]],
        *,
        page: int = 0,
        total: int = 0,
        per_page: int = 25,
    ):
        self.manager_view = manager_view
        self.entry_map = {str(e["id"]): e for e in entries}
        total_pages = max(1, (max(total, 1) + per_page - 1) // per_page)
        options = []
        for entry in entries[:per_page]:
            label = f"#{entry['id']} {entry['username']}"[:100]
            desc = (
                f"{ACTIVE_CATEGORIES.get(entry['category'], {}).get('label', entry['category'])}: "
                f"{entry['reason']}"
            )
            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(entry["id"]),
                    description=desc[:100],
                )
            )
        placeholder = (
            f"Select entries to retract (page {page + 1}/{total_pages})..."
            if total > 0
            else "No active entries"
        )
        super().__init__(
            placeholder=placeholder[:150],
            min_values=1,
            max_values=min(per_page, len(options)) if options else 1,
            options=options or [discord.SelectOption(label="None", value="0")],
            disabled=not bool(entries),
        )

    async def callback(self, interaction: discord.Interaction):
        selected = [self.entry_map[v] for v in self.values if v in self.entry_map]
        if not selected:
            await errors.send_custom_error(
                interaction, "Nothing Selected", "No valid entries were selected."
            )
            return
        keys = {_norm_key(e["username"]) for e in selected}
        if len(keys) > 1:
            await errors.send_custom_error(
                interaction,
                "Mixed Usernames",
                "Please retract entries for only one username at a time.",
            )
            return
        await interaction.response.send_modal(
            BlacklistRetractModal(selected, self.manager_view)
        )


def build_entry_detail_embed(entry: Dict[str, Any]) -> discord.Embed:
    cat = entry.get("category") or ""
    color = ACTIVE_CATEGORIES.get(cat, {}).get("color", 0x5865F2)
    status = "Active" if int(entry.get("active") or 0) == 1 else "Inactive"
    embed = discord.Embed(
        title=f"Inspect Entry #{entry.get('id')}",
        description=f"**Status:** {status}\n**Category:** {_category_label(cat)}",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Username", value=f"`{entry.get('username')}`", inline=True)
    embed.add_field(name="Username Key", value=f"`{entry.get('username_key')}`", inline=True)
    embed.add_field(name="Entry ID", value=f"`{entry.get('id')}`", inline=True)
    embed.add_field(name="Reason", value=entry.get("reason") or "—", inline=False)

    if entry.get("demotion_rank"):
        embed.add_field(
            name="In-game Rank Cap",
            value=f"`{_rank_display(entry['demotion_rank']).upper()}`",
            inline=False,
        )

    if entry.get("discord_id"):
        embed.add_field(
            name="Discord",
            value=f"<@{entry['discord_id']}> (`{entry['discord_id']}`)",
            inline=True,
        )
    else:
        embed.add_field(name="Discord", value="—", inline=True)

    if entry.get("player_uuid"):
        embed.add_field(name="UUID", value=f"`{entry['player_uuid']}`", inline=True)
    else:
        embed.add_field(name="UUID", value="—", inline=True)

    embed.add_field(
        name="Added By",
        value=f"{entry.get('added_by_name')} (`{entry.get('added_by_id')}`)",
        inline=False,
    )
    added_unix = entry.get("added_unix")
    if added_unix:
        embed.add_field(
            name="Added When",
            value=f"<t:{added_unix}:F> (<t:{added_unix}:R>)\n`{entry.get('added_at') or '—'}`",
            inline=False,
        )
    if entry.get("notes"):
        embed.add_field(name="Notes", value=str(entry["notes"])[:1024], inline=False)

    linked = get_related_usernames(entry.get("username") or "")
    alts = [u for u in linked if _norm_key(u) != _norm_key(entry.get("username") or "")]
    if alts:
        embed.add_field(
            name="Linked Accounts",
            value=", ".join(f"`{u}`" for u in alts[:15]),
            inline=False,
        )
    embed.set_footer(text="Use Edit / Delete below · Delete is permanent")
    return embed


class BlacklistInspectModal(Modal, title="Inspect Blacklist Entry"):
    def __init__(self, manager_view: "BlacklistManageView"):
        super().__init__()
        self.manager_view = manager_view
        self.entry_id_input = TextInput(
            label="Entry ID",
            placeholder="Numeric blacklist entry ID (e.g. 42)",
            required=True,
            max_length=20,
        )
        self.add_item(self.entry_id_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = (self.entry_id_input.value or "").strip()
        if not raw.isdigit():
            await errors.send_custom_error(
                interaction, "Invalid ID", "Entry ID must be a positive number."
            )
            return
        entry_id = int(raw)
        entry = get_entry_by_id(entry_id)
        if not entry:
            await errors.send_custom_error(
                interaction,
                "Not Found",
                f"No blacklist entry with ID `{entry_id}`.",
            )
            return

        view = BlacklistInspectView(self.manager_view, entry_id)
        embed = build_entry_detail_embed(entry)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class BlacklistEditEntryModal(Modal, title="Edit Blacklist Entry"):
    def __init__(self, inspect_view: "BlacklistInspectView", entry: Dict[str, Any]):
        super().__init__()
        self.inspect_view = inspect_view
        self.entry_id = int(entry["id"])

        self.username_input = TextInput(
            label="Username",
            required=True,
            max_length=32,
            default=str(entry.get("username") or "")[:32],
        )
        self.category_input = TextInput(
            label="Category key",
            placeholder="one_warning / guild_kick / permanent_demotion / ...",
            required=True,
            max_length=40,
            default=str(entry.get("category") or "")[:40],
        )
        self.reason_input = TextInput(
            label="Reason",
            required=True,
            style=discord.TextStyle.paragraph,
            max_length=500,
            default=str(entry.get("reason") or "")[:500],
        )
        self.rank_input = TextInput(
            label="In-game rank cap (if permanent_demotion)",
            placeholder="recruit / recruiter / captain / strategist / chief",
            required=False,
            max_length=32,
            default=(str(entry.get("demotion_rank") or "")[:32] or None),
        )
        self.ids_input = TextInput(
            label="Discord ID | UUID | Notes",
            placeholder="discord_id | uuid | notes   (use - to clear a part)",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=400,
            default=(
                f"{entry.get('discord_id') or ''}|"
                f"{entry.get('player_uuid') or ''}|"
                f"{entry.get('notes') or ''}"
            )[:400],
        )
        self.add_item(self.username_input)
        self.add_item(self.category_input)
        self.add_item(self.reason_input)
        self.add_item(self.rank_input)
        self.add_item(self.ids_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            category = (self.category_input.value or "").strip().lower()
            if category not in ACTIVE_CATEGORIES:
                raise ValueError(
                    "Unknown category. Use: " + ", ".join(ACTIVE_CATEGORIES.keys())
                )

            raw_ids = (self.ids_input.value or "").split("|")
            while len(raw_ids) < 3:
                raw_ids.append("")
            discord_part = raw_ids[0].strip()
            uuid_part = raw_ids[1].strip()
            notes_part = "|".join(raw_ids[2:]).strip()  # allow | inside notes

            kwargs: Dict[str, Any] = {
                "username": self.username_input.value,
                "category": category,
                "reason": self.reason_input.value,
                "demotion_rank": (self.rank_input.value or "").strip() or None,
            }

            if discord_part == "-":
                kwargs["clear_discord_id"] = True
            elif discord_part:
                if not discord_part.isdigit():
                    raise ValueError("Discord ID must be numeric (or - to clear)")
                kwargs["discord_id"] = int(discord_part)

            if uuid_part == "-":
                kwargs["clear_uuid"] = True
            elif uuid_part:
                kwargs["uuid"] = uuid_part

            if notes_part == "-":
                kwargs["clear_notes"] = True
            else:
                kwargs["notes"] = notes_part

            entry = update_blacklist_entry(self.entry_id, **kwargs)
        except ValueError as e:
            await errors.send_custom_error(interaction, "Edit Failed", str(e))
            return
        except Exception as e:
            print(f"[Blacklist] Failed to edit entry: {e}")
            await errors.DATABASE_ERROR.send(interaction)
            return

        self.inspect_view.entry_id = int(entry["id"])
        embed = build_entry_detail_embed(entry)
        embed.title = f"✅ Updated Entry #{entry['id']}"
        await interaction.response.edit_message(embed=embed, view=self.inspect_view)


class BlacklistInspectView(View):
    def __init__(self, manager_view: "BlacklistManageView", entry_id: int):
        super().__init__(timeout=300)
        self.manager_view = manager_view
        self.entry_id = int(entry_id)
        self.owner_id = manager_view.owner_id
        self.confirm_delete = False

        edit_btn = Button(label="Edit", style=discord.ButtonStyle.primary, emoji="✏️")
        edit_btn.callback = self.edit_callback
        self.add_item(edit_btn)

        delete_btn = Button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️")
        delete_btn.callback = self.delete_callback
        self.add_item(delete_btn)

        refresh_btn = Button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄")
        refresh_btn.callback = self.refresh_callback
        self.add_item(refresh_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await errors.send_custom_error(
                interaction,
                "Permission Denied",
                "Only the user who opened this inspect view can use it.",
            )
            return False
        return True

    async def edit_callback(self, interaction: discord.Interaction):
        entry = get_entry_by_id(self.entry_id)
        if not entry:
            await errors.send_custom_error(
                interaction,
                "Not Found",
                f"Entry `{self.entry_id}` no longer exists.",
            )
            return
        self.confirm_delete = False
        await interaction.response.send_modal(BlacklistEditEntryModal(self, entry))

    async def delete_callback(self, interaction: discord.Interaction):
        entry = get_entry_by_id(self.entry_id)
        if not entry:
            await errors.send_custom_error(
                interaction,
                "Not Found",
                f"Entry `{self.entry_id}` no longer exists.",
            )
            return

        if not self.confirm_delete:
            self.confirm_delete = True
            embed = build_entry_detail_embed(entry)
            embed.color = 0xE74C3C
            embed.add_field(
                name="⚠️ Confirm Delete",
                value=(
                    "This **permanently** deletes the entry (not a retraction).\n"
                    "Click **Delete** again to confirm."
                ),
                inline=False,
            )
            await interaction.response.edit_message(embed=embed, view=self)
            return

        try:
            deleted = delete_blacklist_entry(self.entry_id)
        except ValueError as e:
            await errors.send_custom_error(interaction, "Delete Failed", str(e))
            return
        except Exception as e:
            print(f"[Blacklist] Failed to delete entry: {e}")
            await errors.DATABASE_ERROR.send(interaction)
            return

        self.clear_items()
        desc = (
            f"Permanently deleted entry **#{deleted.get('id')}**\n"
            f"**Username:** `{deleted.get('username')}`\n"
            f"**Category:** {_category_label(deleted.get('category') or '')}\n"
            f"**Reason:** {deleted.get('reason') or '—'}"
        )
        converted = deleted.get("_normalized_warning_ids") or []
        if converted:
            desc += (
                "\n\n**Warning ladder adjusted:** remaining second warning(s) "
                f"converted to first warning (IDs: {', '.join(f'`{i}`' for i in converted)})."
            )
        embed = discord.Embed(
            title="🗑️ Entry Deleted",
            description=desc,
            color=0xE74C3C,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Deleted by {interaction.user}")
        await interaction.response.edit_message(embed=embed, view=self)
        await self.manager_view.return_to_home()

    async def refresh_callback(self, interaction: discord.Interaction):
        entry = get_entry_by_id(self.entry_id)
        if not entry:
            self.clear_items()
            embed = discord.Embed(
                title="Not Found",
                description=f"Entry `{self.entry_id}` no longer exists.",
                color=0x95A5A6,
            )
            await interaction.response.edit_message(embed=embed, view=self)
            return
        self.confirm_delete = False
        await interaction.response.edit_message(
            embed=build_entry_detail_embed(entry), view=self
        )


class BlacklistLinkModal(Modal, title="Link Alt Account"):
    def __init__(self, manager_view: "BlacklistManageView"):
        super().__init__()
        self.manager_view = manager_view
        self.main_input = TextInput(
            label="Main Username",
            placeholder="Primary Minecraft username",
            required=True,
            max_length=32,
        )
        self.alt_input = TextInput(
            label="Alt Username",
            placeholder="Alt Minecraft username to link",
            required=True,
            max_length=32,
        )
        self.note_input = TextInput(
            label="Note (optional)",
            placeholder="Why these accounts are linked",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=300,
        )
        self.add_item(self.main_input)
        self.add_item(self.alt_input)
        self.add_item(self.note_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            link = link_accounts(
                main_username=self.main_input.value,
                alt_username=self.alt_input.value,
                linked_by_id=interaction.user.id,
                linked_by_name=str(interaction.user),
                note=self.note_input.value,
            )
        except ValueError as e:
            await errors.send_custom_error(interaction, "Link Failed", str(e))
            return
        except Exception as e:
            print(f"[Blacklist] Failed to link accounts: {e}")
            await errors.DATABASE_ERROR.send(interaction)
            return

        related = get_related_usernames(link["main_username"])
        embed = discord.Embed(
            title="🔗 Accounts Linked",
            description=(
                f"**Main:** `{link['main_username']}`\n"
                f"**Alt:** `{link['alt_username']}`\n"
                f"**Link ID:** `{link['id']}`\n"
                + (f"**Note:** {link['note']}\n" if link.get("note") else "")
                + f"\nBlacklist checks for either name now include:\n"
                + ", ".join(f"`{u}`" for u in related)
            ),
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Linked by {interaction.user}")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await self.manager_view.return_to_home()


class UnlinkSelect(Select):
    def __init__(
        self, manager_view: "BlacklistManageView", links: List[Dict[str, Any]]
    ):
        self.manager_view = manager_view
        self.link_map = {str(link["id"]): link for link in links}
        options = []
        for link in links[:25]:
            label = f"#{link['id']} {link['main_username']} ← {link['alt_username']}"[:100]
            desc = (link.get("note") or f"linked by {link.get('linked_by_name')}")[:100]
            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(link["id"]),
                    description=desc,
                )
            )
        super().__init__(
            placeholder="Select link(s) to remove...",
            min_values=1,
            max_values=min(25, len(options)) if options else 1,
            options=options or [discord.SelectOption(label="None", value="0")],
            disabled=not bool(links),
        )

    async def callback(self, interaction: discord.Interaction):
        ids = [int(v) for v in self.values if v in self.link_map]
        if not ids:
            await errors.send_custom_error(
                interaction, "Nothing Selected", "No valid links were selected."
            )
            return
        try:
            removed = unlink_accounts(
                link_ids=ids,
                unlinked_by_id=interaction.user.id,
                unlinked_by_name=str(interaction.user),
            )
        except ValueError as e:
            await errors.send_custom_error(interaction, "Unlink Failed", str(e))
            return
        except Exception as e:
            print(f"[Blacklist] Failed to unlink accounts: {e}")
            await errors.DATABASE_ERROR.send(interaction)
            return

        embed = discord.Embed(
            title="🔓 Account Links Removed",
            description=f"Removed **{removed}** active link(s).",
            color=0xE67E22,
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

        # Refresh the manager's Link Alts panel (not the ephemeral follow-up)
        try:
            links = list_account_links(limit=25, offset=0)
            total = count_account_links()
            self.manager_view.mode = "links"
            self.manager_view._build_links_items(links)
            panel = discord.Embed(
                title="🔗 Linked Alt Accounts",
                description=(
                    "Link a main account to an alt so blacklist checks and warnings "
                    "apply across both.\n\n"
                    f"**Active links:** `{total}`"
                ),
                color=0x5865F2,
                timestamp=datetime.now(timezone.utc),
            )
            if not links:
                panel.add_field(
                    name="No links yet",
                    value="Use **Link Accounts** to connect a main and alt username.",
                    inline=False,
                )
            else:
                for link in links[:15]:
                    note = f"\n**Note:** {link['note']}" if link.get("note") else ""
                    panel.add_field(
                        name=f"#{link['id']} `{link['main_username']}` ← `{link['alt_username']}`",
                        value=(
                            f"**By:** {link['linked_by_name']} · "
                            f"<t:{link['linked_unix']}:R>{note}"
                        )[:1024],
                        inline=False,
                    )
            if self.manager_view.message:
                await self.manager_view.message.edit(
                    embed=panel, view=self.manager_view
                )
        except Exception as e:
            print(f"[Blacklist] Failed to refresh links panel: {e}")


class BlacklistManageView(View):
    def __init__(self, owner_id: int, *, readonly: bool = False, hide_home: bool = False):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.readonly = bool(readonly)
        self.hide_home = bool(hide_home)
        self.mode = "home"
        self.list_page = 0
        self.retracted_mode = False
        self.retract_page = 0

        # Search = base query
        self.search_username = None
        self.search_added_by_id = None
        self.search_start_unix = None
        self.search_end_unix = None

        # Filter = category applied on top of search
        self.filter_category = None

        self.message = None
        self._build_home_items()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await errors.send_custom_error(
                interaction,
                "Permission Denied",
                "Only the user who opened this manager can use it.",
            )
            return False
        return True

    async def _deny_readonly(self, interaction: discord.Interaction) -> bool:
        """Return True if action was blocked because this session is read-only."""
        if not self.readonly:
            return False
        await errors.send_custom_error(
            interaction,
            "Permission Denied",
            "This browse session is read-only. Use `/blacklist_manage` to make changes.",
        )
        return True

    def _clear_items(self):
        self.clear_items()

    def _build_home_items(self):
        self._clear_items()
        if not self.readonly:
            add_btn = Button(label="Add Entry", style=discord.ButtonStyle.success, emoji="➕")
            add_btn.callback = self.add_callback
            self.add_item(add_btn)

            retract_btn = Button(label="Retract", style=discord.ButtonStyle.danger)
            retract_btn.callback = self.retract_callback
            self.add_item(retract_btn)

        list_btn = Button(label="Browse All", style=discord.ButtonStyle.primary, emoji="📋")
        list_btn.callback = self.list_callback
        self.add_item(list_btn)

        search_btn = Button(label="Search", style=discord.ButtonStyle.primary, emoji="🔎")
        search_btn.callback = self.search_callback
        self.add_item(search_btn)

        if not self.readonly:
            links_btn = Button(label="Link Alts", style=discord.ButtonStyle.secondary, emoji="🔗")
            links_btn.callback = self.links_callback
            self.add_item(links_btn)

        inspect_btn = Button(label="Inspect", style=discord.ButtonStyle.secondary, emoji="🔍")
        inspect_btn.callback = self.inspect_callback
        self.add_item(inspect_btn)

    def _build_add_items(self):
        self._clear_items()
        self.add_item(CategorySelect(self, mode="add"))
        back = Button(label="Back", style=discord.ButtonStyle.secondary, emoji="◀️")
        back.callback = self.home_callback
        self.add_item(back)

    def _has_active_search(self) -> bool:
        return any(
            [
                self.search_username,
                self.search_added_by_id is not None,
                self.search_start_unix is not None,
                self.search_end_unix is not None,
            ]
        )

    def _search_summary(self) -> str:
        parts = []
        if self.search_username:
            parts.append(f"user `{self.search_username}`")
        if self.search_added_by_id is not None:
            parts.append(f"by `{self.search_added_by_id}`")
        if self.search_start_unix is not None:
            parts.append(f"from <t:{self.search_start_unix}:d>")
        if self.search_end_unix is not None:
            parts.append(f"to <t:{self.search_end_unix}:d>")
        return ", ".join(parts) if parts else "none"

    def _build_list_filter_items(self):
        self._clear_items()
        self.add_item(CategorySelect(self, mode="list"))

        if self._has_active_search():
            clear_search = Button(
                label="Clear Search", style=discord.ButtonStyle.secondary
            )
            clear_search.callback = self.clear_search_callback
            self.add_item(clear_search)

        back = Button(label="Back", style=discord.ButtonStyle.secondary, emoji="◀️")
        back.callback = self.back_to_results_callback
        self.add_item(back)

        if not self.hide_home:
            home = Button(label="Home", style=discord.ButtonStyle.secondary, emoji="🏠")
            home.callback = self.home_callback
            self.add_item(home)

    def _build_list_items(self, total: int, per_page: int = 8):
        self._clear_items()
        prev_btn = Button(
            label="Prev",
            style=discord.ButtonStyle.secondary,
            emoji="⬅️",
            disabled=self.list_page <= 0,
        )
        prev_btn.callback = self.prev_page_callback
        self.add_item(prev_btn)

        next_btn = Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            emoji="➡️",
            disabled=(self.list_page + 1) * per_page >= total,
        )
        next_btn.callback = self.next_page_callback
        self.add_item(next_btn)

        # Search first, then filter that search
        search_btn = Button(label="Search", style=discord.ButtonStyle.primary, emoji="🔎")
        search_btn.callback = self.search_callback
        self.add_item(search_btn)

        filter_btn = Button(label="Filter", style=discord.ButtonStyle.primary)
        filter_btn.callback = self.filter_callback
        self.add_item(filter_btn)

        if self._has_active_search() or self.filter_category or self.retracted_mode:
            clear_btn = Button(label="Clear", style=discord.ButtonStyle.secondary)
            clear_btn.callback = self.clear_all_callback
            self.add_item(clear_btn)

        if not self.hide_home:
            back = Button(label="Home", style=discord.ButtonStyle.secondary)
            back.callback = self.home_callback
            self.add_item(back)

    def _build_retract_items(
        self,
        entries: List[Dict[str, Any]],
        *,
        page: int = 0,
        total: int = 0,
        per_page: int = 25,
    ):
        self._clear_items()
        self.add_item(
            RetractEntrySelect(
                self,
                entries,
                page=page,
                total=total,
                per_page=per_page,
            )
        )

        prev_btn = Button(
            label="Prev",
            style=discord.ButtonStyle.secondary,
            emoji="⬅️",
            disabled=page <= 0,
        )
        prev_btn.callback = self.retract_prev_callback
        self.add_item(prev_btn)

        next_btn = Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            emoji="➡️",
            disabled=(page + 1) * per_page >= total,
        )
        next_btn.callback = self.retract_next_callback
        self.add_item(next_btn)

        refresh = Button(label="Refresh", style=discord.ButtonStyle.primary, emoji="🔄")
        refresh.callback = self.retract_callback
        self.add_item(refresh)

        back = Button(label="Home", style=discord.ButtonStyle.secondary)
        back.callback = self.home_callback
        self.add_item(back)

    async def return_to_home(self):
        """Reset manager state and edit the original panel back to home."""
        self.mode = "home"
        self.search_username = None
        self.search_added_by_id = None
        self.search_start_unix = None
        self.search_end_unix = None
        self.filter_category = None
        self.retracted_mode = False
        self.list_page = 0
        self.retract_page = 0
        self._build_home_items()
        if not self.message:
            print("[Blacklist] return_to_home: manager message reference missing")
            return
        try:
            await self.message.edit(embed=build_manage_home_embed(), view=self)
        except Exception as e:
            print(f"[Blacklist] return_to_home failed: {e}")

    async def refresh_home(self, interaction: discord.Interaction = None):
        await self.return_to_home()

    async def home_callback(self, interaction: discord.Interaction):
        self.mode = "home"
        self.search_username = None
        self.search_added_by_id = None
        self.search_start_unix = None
        self.search_end_unix = None
        self.filter_category = None
        self.retracted_mode = False
        self.list_page = 0
        self.retract_page = 0
        self._build_home_items()
        await interaction.response.edit_message(
            embed=build_manage_home_embed(), view=self
        )

    async def add_callback(self, interaction: discord.Interaction):
        if await self._deny_readonly(interaction):
            return
        self.mode = "add"
        self._build_add_items()
        embed = discord.Embed(
            title="Add Blacklist Entry",
            description=(
                "Select a category, then fill in **Username** and **Reason**.\n"
                "Optional: Discord ID + UUID (auto-filled from matches when possible).\n\n"
                "**Warning** auto-escalates:\n"
                "• 1st time → One Warning\n"
                "• 2nd time → Two Warnings\n\n"
                "Permanent demotions require an **in-game rank cap** "
                "(recruit / recruiter / captain / strategist / chief)."
            ),
            color=0x2ECC71,
        )
        await interaction.response.edit_message(embed=embed, view=self)

    async def list_callback(self, interaction: discord.Interaction):
        """Browse all entries (clears search), then allow filter/search."""
        self.search_username = None
        self.search_added_by_id = None
        self.search_start_unix = None
        self.search_end_unix = None
        self.filter_category = None
        self.retracted_mode = False
        await self.show_list(interaction, category=None, retracted_mode=False, page=0)

    async def filter_callback(self, interaction: discord.Interaction):
        """Category filter applied on top of the current search."""
        self.mode = "list_filter"
        self._build_list_filter_items()
        search_line = self._search_summary()
        current = (
            ACTIVE_CATEGORIES.get(self.filter_category, {}).get("label", "All active")
            if not self.retracted_mode
            else "Retracted"
        )
        if self.filter_category is None and not self.retracted_mode:
            current = "All active"
        embed = discord.Embed(
            title="Filter Results",
            description=(
                "Pick a **category** to narrow the current search.\n\n"
                f"**Current search:** {search_line}\n"
                f"**Current filter:** {current}\n\n"
                "Search defines who/when. Filter defines which category."
            ),
            color=0x3498DB,
        )
        await interaction.response.edit_message(embed=embed, view=self)

    async def search_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BlacklistSearchModal(self))

    async def clear_search_callback(self, interaction: discord.Interaction):
        self.search_username = None
        self.search_added_by_id = None
        self.search_start_unix = None
        self.search_end_unix = None
        await self.show_list(
            interaction,
            category=self.filter_category,
            retracted_mode=self.retracted_mode,
            page=0,
        )

    async def clear_all_callback(self, interaction: discord.Interaction):
        self.search_username = None
        self.search_added_by_id = None
        self.search_start_unix = None
        self.search_end_unix = None
        self.filter_category = None
        self.retracted_mode = False
        await self.show_list(interaction, category=None, retracted_mode=False, page=0)

    async def back_to_results_callback(self, interaction: discord.Interaction):
        await self.show_list(
            interaction,
            category=self.filter_category,
            retracted_mode=self.retracted_mode,
            page=self.list_page,
        )

    async def inspect_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BlacklistInspectModal(self))

    def _build_links_items(self, links: List[Dict[str, Any]]):
        self._clear_items()
        add_link = Button(label="Link Accounts", style=discord.ButtonStyle.success, emoji="🔗")
        add_link.callback = self.link_add_callback
        self.add_item(add_link)

        if links:
            self.add_item(UnlinkSelect(self, links))

        refresh = Button(label="Refresh", style=discord.ButtonStyle.primary, emoji="🔄")
        refresh.callback = self.links_callback
        self.add_item(refresh)

        back = Button(label="Home", style=discord.ButtonStyle.secondary)
        back.callback = self.home_callback
        self.add_item(back)

    async def links_callback(self, interaction: discord.Interaction):
        if await self._deny_readonly(interaction):
            return
        self.mode = "links"
        links = list_account_links(limit=25, offset=0)
        total = count_account_links()
        self._build_links_items(links)

        embed = discord.Embed(
            title="🔗 Linked Alt Accounts",
            description=(
                "Link a main account to an alt so blacklist checks and warnings "
                "apply across both.\n\n"
                f"**Active links:** `{total}`\n\n"
                "• **Link Accounts**: create a main ↔ alt link\n"
                "• Select existing links below to **unlink** them"
            ),
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        if not links:
            embed.add_field(
                name="No links yet",
                value="Use **Link Accounts** to connect a main and alt username.",
                inline=False,
            )
        else:
            for link in links[:15]:
                note = f"\n**Note:** {link['note']}" if link.get("note") else ""
                embed.add_field(
                    name=f"#{link['id']} `{link['main_username']}` ← `{link['alt_username']}`",
                    value=(
                        f"**By:** {link['linked_by_name']} · "
                        f"<t:{link['linked_unix']}:R>{note}"
                    )[:1024],
                    inline=False,
                )

        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    async def link_add_callback(self, interaction: discord.Interaction):
        if await self._deny_readonly(interaction):
            return
        await interaction.response.send_modal(BlacklistLinkModal(self))

    async def retract_callback(self, interaction: discord.Interaction):
        if await self._deny_readonly(interaction):
            return
        # Opening retract from home/refresh starts at page 0
        self.retract_page = 0
        await self.show_retract_page(interaction, page=0)

    async def show_retract_page(self, interaction: discord.Interaction, *, page: int = 0):
        """Paginated active-entry picker for retraction (25 options per Discord select)."""
        per_page = 25
        total = count_active_entries()
        max_page = max(0, (total - 1) // per_page) if total else 0
        page = max(0, min(int(page), max_page))
        self.retract_page = page
        self.mode = "retract"

        entries = list_active_entries(limit=per_page, offset=page * per_page)
        self._build_retract_items(
            entries, page=page, total=total, per_page=per_page
        )

        total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
        start_n = (page * per_page + 1) if total else 0
        end_n = min((page + 1) * per_page, total)

        embed = discord.Embed(
            title="Retract Blacklist Entries",
            description=(
                "Select one or more **active** entries for the **same username**, "
                "then provide a retraction reason.\n\n"
                f"**Page:** `{page + 1}` / `{total_pages}`\n"
                f"**Showing:** `{start_n}`–`{end_n}` of `{total}` active entries\n\n"
                "Use **Prev/Next** to browse more entries.\n\n"
                "Retracted entries move into the retraction history with:\n"
                "• original categories\n• original reasons\n• retraction reason"
            ),
            color=0xE67E22,
            timestamp=datetime.now(timezone.utc),
        )
        if not entries:
            embed.description += "\n\n_No active entries available to retract._"

        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    async def retract_prev_callback(self, interaction: discord.Interaction):
        await self.show_retract_page(interaction, page=self.retract_page - 1)

    async def retract_next_callback(self, interaction: discord.Interaction):
        await self.show_retract_page(interaction, page=self.retract_page + 1)

    async def show_list(
        self,
        interaction: discord.Interaction,
        *,
        category: Optional[str] = None,
        retracted_mode: bool = False,
        page: int = 0,
    ):
        self.mode = "list"
        self.filter_category = category
        self.retracted_mode = retracted_mode
        self.list_page = max(0, page)
        per_page = 8
        offset = self.list_page * per_page

        # Always combine: Search (base) + Filter (category)
        username = self.search_username
        added_by_id = self.search_added_by_id
        start_unix = self.search_start_unix
        end_unix = self.search_end_unix

        if retracted_mode:
            # Retractions are a special filter mode; still honor username search if set
            all_rets = list_retracted(limit=500, offset=0)
            if username:
                unames, discord_ids, uuids, _ = _collect_identity_sets(username)
                all_rets = [
                    r for r in all_rets
                    if _entry_matches_identity(r, unames, discord_ids, uuids)
                ]
            if added_by_id is not None:
                all_rets = [
                    r for r in all_rets
                    if int(r.get("retracted_by_id") or 0) == int(added_by_id)
                ]
            if start_unix is not None:
                all_rets = [
                    r for r in all_rets
                    if int(r.get("retracted_unix") or 0) >= int(start_unix)
                ]
            if end_unix is not None:
                all_rets = [
                    r for r in all_rets
                    if int(r.get("retracted_unix") or 0) <= int(end_unix)
                ]
            total = len(all_rets)
            entries = all_rets[offset : offset + per_page]
        else:
            if username:
                entries_all = list_active_entries(
                    category=category,
                    added_by_id=added_by_id,
                    start_unix=start_unix,
                    end_unix=end_unix,
                    username=username,
                    limit=500,
                    offset=0,
                )
                total = len(entries_all)
                entries = entries_all[offset : offset + per_page]
            else:
                total = count_active_entries(
                    category=category,
                    added_by_id=added_by_id,
                    start_unix=start_unix,
                    end_unix=end_unix,
                )
                entries = list_active_entries(
                    category=category,
                    added_by_id=added_by_id,
                    start_unix=start_unix,
                    end_unix=end_unix,
                    limit=per_page,
                    offset=offset,
                )

        # Title suffix reflects both layers
        suffix_parts = []
        if self._has_active_search():
            suffix_parts.append("Search")
        if category:
            suffix_parts.append(ACTIVE_CATEGORIES.get(category, {}).get("label", category))
        elif retracted_mode:
            suffix_parts.append("Retracted")
        title_suffix = " + ".join(suffix_parts)

        self._build_list_items(total, per_page=per_page)
        embed = build_list_embed(
            entries=entries,
            page=self.list_page,
            total=total,
            category=category,
            retracted_mode=retracted_mode,
            title_suffix=title_suffix,
        )
        # Show active search/filter state on the embed
        if retracted_mode:
            filter_label = "Retracted"
        elif category:
            filter_label = ACTIVE_CATEGORIES.get(category, {}).get("label", category)
        else:
            filter_label = "All active"
        state_lines = [
            f"**Search:** {self._search_summary()}",
            f"**Filter:** {filter_label}",
        ]
        embed.description = (embed.description or "") + "\n" + "\n".join(state_lines)

        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    async def prev_page_callback(self, interaction: discord.Interaction):
        await self.show_list(
            interaction,
            category=self.filter_category,
            retracted_mode=self.retracted_mode,
            page=self.list_page - 1,
        )

    async def next_page_callback(self, interaction: discord.Interaction):
        await self.show_list(
            interaction,
            category=self.filter_category,
            retracted_mode=self.retracted_mode,
            page=self.list_page + 1,
        )


_demotion_listener = None
_recent_demotion_alerts = {}


async def _resolve_alert_thread(bot, guild, thread_id: int):
    channel = bot.get_channel(thread_id)
    if channel is not None:
        return channel
    thread = guild.get_thread(thread_id) if guild else None
    if thread is not None:
        return thread
    try:
        return await bot.fetch_channel(thread_id)
    except Exception as e:
        print(f"[Blacklist] Could not fetch demotion alert thread {thread_id}: {e}")
        return None


async def _send_demotion_violation_alert(
    bot,
    violations: List[Dict[str, Any]],
    *,
    guild=None,
):
    if not violations:
        return

    v0 = violations[0]
    username = v0.get("username") or "Unknown"
    current_rank = v0.get("current_rank") or "unknown"

    # Debounce duplicate alerts for the same player+rank within 10 minutes
    key = (_norm_key(username), current_rank)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    last = _recent_demotion_alerts.get(key)
    if last and now_ts - last < 600:
        return
    _recent_demotion_alerts[key] = now_ts

    alert_guild = guild
    if alert_guild is None and getattr(bot, "guilds", None):
        # Prefer the guild that owns the alert thread when possible
        for g in bot.guilds:
            if g.get_thread(DEMOTION_ALERT_THREAD_ID) or g.get_channel(DEMOTION_ALERT_THREAD_ID):
                alert_guild = g
                break
        if alert_guild is None and bot.guilds:
            alert_guild = bot.guilds[0]

    thread = await _resolve_alert_thread(bot, alert_guild, DEMOTION_ALERT_THREAD_ID)
    if thread is None:
        print(f"[Blacklist] Demotion alert thread not found: {DEMOTION_ALERT_THREAD_ID}")
        return

    identity = resolve_identity(username=username, uuid=v0.get("uuid"))
    discord_id = identity.get("discord_id")

    old_rank = v0.get("old_rank")
    rank_line = _rank_display(current_rank).upper()
    if old_rank:
        rank_line = f"{_rank_display(old_rank).upper()} → {rank_line}"

    embed = discord.Embed(
        title="Permanent Demotion Rank Violation",
        description=(
            f"**`{username}`** received an in-game guild rank at or above their "
            f"permanent demotion cap."
        ),
        color=0x9B59B6,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Player", value=f"`{username}`", inline=True)
    embed.add_field(name="In-game Rank", value=f"`{rank_line}`", inline=True)
    if discord_id:
        embed.add_field(
            name="Discord",
            value=f"<@{discord_id}> (`{discord_id}`)",
            inline=False,
        )
    if v0.get("uuid"):
        embed.add_field(name="UUID", value=f"`{v0['uuid']}`", inline=False)

    cap_lines = []
    for v in violations[:8]:
        entry = v["entry"]
        cap_txt = _rank_display(v.get("cap_rank")).upper()
        cap_lines.append(
            f"• Cap **`{cap_txt}`** on `{entry.get('username')}`\n"
            f"  Reason: {entry.get('reason') or '—'}\n"
            f"  Entry ID: `{entry.get('id')}` · "
            f"<t:{entry.get('added_unix')}:R>"
        )
    embed.add_field(
        name="Permanent Demotion Caps",
        value="\n".join(cap_lines)[:1024],
        inline=False,
    )
    embed.set_footer(text="Blacklist permanent demotion enforcement (in-game ranks)")

    try:
        await thread.send(embed=embed)
        print(
            f"[Blacklist] In-game demotion violation for {username} "
            f"({current_rank} >= {[v['cap_rank'] for v in violations]})"
        )
    except Exception as e:
        print(f"[Blacklist] Failed to send demotion violation alert: {e}")


def teardown(bot):
    # No Discord role listener anymore; kept for reload compatibility.
    return


def setup(bot, has_required_role, config):
    init_database()

    @bot.tree.command(
        name="blacklist_check",
        description="Check a username, or use %all% to browse/search the full blacklist",
    )
    @app_commands.describe(
        username="Minecraft username, or %all% to open browse/search/filter UI"
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def blacklist_check(interaction: discord.Interaction, username: str):
        if not has_roles(interaction.user, CHECK_ROLES) and CHECK_ROLES:
            await errors.NO_PERMISSION.send(interaction)
            return

        raw = (username or "").strip()
        if raw.lower() in {"%all%", "all", "*"}:
            can_manage = has_roles(interaction.user, MANAGE_ROLES)
            view = BlacklistManageView(
                owner_id=interaction.user.id,
                readonly=not can_manage,
                hide_home=True,
            )
            boot = discord.Embed(
                title="Blacklist Browser",
                description=(
                    "Loading all blacklist entries…\n"
                    "Use **Search** and **Filter** on the next screen."
                    + ("\n\n_Read-only mode._" if not can_manage else "")
                ),
                color=0x5865F2,
            )
            await interaction.response.send_message(
                embed=boot, view=view, ephemeral=True
            )
            try:
                view.message = await interaction.original_response()
            except Exception as e:
                print(f"[Blacklist] Failed to capture check-all message: {e}")
                view.message = None
            await view.show_list(
                interaction, category=None, retracted_mode=False, page=0
            )
            return

        embeds = build_check_embeds(raw)
        await interaction.response.send_message(embeds=embeds[:10], ephemeral=True)

    @bot.tree.command(
        name="blacklist_manage",
        description="Open the blacklist manager (add, retract, list, search)",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def blacklist_manage(interaction: discord.Interaction):
        if not has_roles(interaction.user, MANAGE_ROLES) and MANAGE_ROLES:
            await errors.NO_PERMISSION.send(interaction)
            return

        view = BlacklistManageView(owner_id=interaction.user.id, readonly=False)
        embed = build_manage_home_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        try:
            view.message = await interaction.original_response()
        except Exception as e:
            print(f"[Blacklist] Failed to capture manager message: {e}")
            try:
                view.message = interaction.response._response_message
            except Exception:
                view.message = None

    print("[OK] Loaded blacklist commands (check + manage)")
