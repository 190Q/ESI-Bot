"""
Inactivity exemption data layer.

A week key is either the literal ``"permanent"`` or
``"YYYY-MM-DD_YYYY-MM-DD"``, the Monday and Tuesday of the week being checked.
Week exemptions expire on the Thursday 23:59 UTC of that week.

The JSON is the source of truth. ``databases/inactivity_exemptions.db`` is a
best-effort mirror that the website's analytics panel reads, refreshed on every
save.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from utils.paths import DATA_DIR, DB_DIR

USERNAME_MATCHES_PATH = DATA_DIR / "username_matches.json"
INACTIVITY_EXEMPTIONS_PATH = DATA_DIR / "inactivity_exemptions.json"

EXEMPTIONS_DB_PATH = DB_DIR / "inactivity_exemptions.db"
PERMANENT_EXEMPT_UNTIL = "2125-01-01T00:00:00+00:00"

# Roles that have restricted access (can view but not manage exemptions)
RESTRICTED_ROLES = [
    954566591520063510  # Jurors
]


def is_restricted_user(user):
    """Check if user has restricted access (can view but not manage)"""
    user_role_ids = [role.id for role in user.roles]
    has_parliament = 600185623474601995 in user_role_ids
    has_owner = user.id == int(os.getenv('OWNER_ID', '0'))
    has_restricted = any(role_id in user_role_ids for role_id in RESTRICTED_ROLES)
    return has_restricted and not has_parliament and not has_owner


def load_exemptions():
    """Load inactivity exemptions from JSON file.

    Returns: dict mapping discord_user_id (str) -> list of week keys ("YYYY-MM-DD_YYYY-MM-DD")
    """
    if not INACTIVITY_EXEMPTIONS_PATH.exists():
        return {}

    try:
        with open(INACTIVITY_EXEMPTIONS_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"[INAC_CHECK] Error loading exemptions: {e}")
        return {}


def save_exemptions(exemptions):
    """Save inactivity exemptions to JSON file.

    The JSON is the source of truth; the website's analytics panel instead
    reads a small ``exemptions`` table, so the mirror is refreshed here too.
    """
    try:
        INACTIVITY_EXEMPTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(INACTIVITY_EXEMPTIONS_PATH, 'w', encoding='utf-8') as f:
            json.dump(exemptions, f, indent=2)
        sync_exemptions_database(exemptions)
        return True
    except Exception as e:
        print(f"[INAC_CHECK] Error saving exemptions: {e}")
        return False


def _load_username_index():
    """discord id -> (minecraft username, uuid) from username_matches.json."""
    index = {}
    try:
        with open(USERNAME_MATCHES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return index
    if not isinstance(data, dict):
        return index
    for key, entry in data.items():
        if isinstance(entry, dict):
            index[str(key)] = (entry.get("username"), entry.get("uuid"))
        elif isinstance(entry, str):
            index[str(key)] = (entry, None)
    return index


def _exemption_rows(exemptions):
    """Flatten the exemptions JSON into one DB row per user with a live exemption.

    A user counts as exempt when they have at least one week entry, so
    reason-only records are skipped. ``exempt_until`` is the latest expiry
    across the user's weeks, or the permanent sentinel.
    """
    index = _load_username_index()
    by_username = {}
    for username, uuid in index.values():
        if isinstance(username, str):
            by_username.setdefault(username.lower(), uuid)

    rows = []
    for user_key, data in (exemptions or {}).items():
        username = None
        uuid = None
        if isinstance(data, list):
            weeks, reason = data, None
        elif isinstance(data, dict):
            weeks = data.get("weeks") or []
            reason = data.get("reason")
            username = data.get("username")
        else:
            continue

        key = str(user_key)
        if key.startswith("mc_"):
            username = username or key[3:]
            uuid = by_username.get(username.lower())
        else:
            match = index.get(key)
            if match:
                username = username or match[0]
                uuid = uuid or match[1]

        if "permanent" in weeks:
            exempt_until = PERMANENT_EXEMPT_UNTIL
        else:
            expiries = [e for e in (_week_expiry(w) for w in weeks) if e is not None]
            if not expiries:
                continue
            exempt_until = max(expiries).isoformat()

        rows.append((uuid, username or key, exempt_until, reason))
    return rows


def sync_exemptions_database(exemptions=None):
    """Mirror data/inactivity_exemptions.json into databases/inactivity_exemptions.db.

    The website's bot-analytics panel reads the ``exemptions`` table (one row
    per user, ``exempt_until`` as an ISO-8601 UTC timestamp) to count active and
    soon-to-expire exemptions, so this keeps that table in step with the JSON.

    Best-effort: any failure is logged and swallowed, because a broken mirror
    must never cost someone their exemption. Returns True when the table was
    rewritten.
    """
    if exemptions is None:
        exemptions = load_exemptions()

    try:
        rows = _exemption_rows(exemptions)
    except Exception as e:
        print(f"[INAC_CHECK] Could not build exemption rows: {e}")
        return False

    try:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(EXEMPTIONS_DB_PATH), timeout=10)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS exemptions (
                uuid TEXT,
                username TEXT,
                exempt_until TEXT,
                reason TEXT,
                added_by TEXT,
                added_at TEXT
            )
        """)

        previous = {}
        try:
            for uname, added_by, added_at in c.execute(
                "SELECT username, added_by, added_at FROM exemptions"
            ):
                if uname:
                    previous[str(uname).lower()] = (added_by, added_at)
        except sqlite3.Error:
            previous = {}

        now = datetime.now(timezone.utc).isoformat()
        c.execute("DELETE FROM exemptions")
        for uuid, username, exempt_until, reason in rows:
            added_by, added_at = previous.get(str(username).lower(), (None, None))
            c.execute(
                "INSERT INTO exemptions"
                " (uuid, username, exempt_until, reason, added_by, added_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (uuid, username, exempt_until, reason, added_by, added_at or now),
            )
        conn.commit()
        conn.close()
        return True
    except (sqlite3.Error, OSError) as e:
        print(f"[INAC_CHECK] Could not mirror exemptions to the database: {e}")
        return False


def cleanup_expired_exemptions():
    """Remove expired week exemptions from all users."""
    exemptions = load_exemptions()
    # Use full datetime for Thursday 23:59 expiry check
    now = datetime.now(timezone.utc)
    modified = False

    users_to_remove = []

    for user_key, data in exemptions.items():
        # Handle old format (list)
        if isinstance(data, list):
            original_len = len(data)
            data = [w for w in data if w == "permanent" or is_week_valid(w, now)]
            if len(data) != original_len:
                modified = True
                if data:
                    exemptions[user_key] = data
                else:
                    users_to_remove.append(user_key)

        # Handle new format (dict)
        elif isinstance(data, dict):
            weeks = data.get("weeks", [])
            original_len = len(weeks)
            weeks = [w for w in weeks if w == "permanent" or is_week_valid(w, now)]
            if len(weeks) != original_len:
                modified = True
                data["weeks"] = weeks
                if not weeks and not data.get("reason"):
                    users_to_remove.append(user_key)

    for user_key in users_to_remove:
        del exemptions[user_key]
        modified = True

    if modified:
        save_exemptions(exemptions)
        print(f"[INAC_CHECK] Cleaned up expired exemptions for {len(users_to_remove)} users")


def _week_expiry(week_key: str):
    """The UTC datetime a week exemption expires, or None if unparseable.

    Exemptions expire on Thursday at 23:59 UTC of the week their end date falls in.
    This ensures they are cleaned up before the second check runs.
    """
    try:
        _, end_str = week_key.split("_")
        end_date = datetime.fromisoformat(end_str).date()

        # Find the Thursday of the week containing end_date (weekday 3 = Thursday)
        days_to_thursday = (3 - end_date.weekday()) % 7
        expiry_date = end_date + timedelta(days=days_to_thursday)

        return datetime(
            expiry_date.year,
            expiry_date.month,
            expiry_date.day,
            23, 59, 0,
            tzinfo=timezone.utc
        )
    except Exception:
        return None


def is_week_valid(week_key: str, now: datetime) -> bool:
    """Check if a week exemption is still valid."""
    expiry_dt = _week_expiry(week_key)
    return expiry_dt is not None and now <= expiry_dt


def get_user_exemption_data(discord_id: int):
    """Get exemption data for a user, handling both old and new format.

    Returns: (weeks_list, reason_or_none)
    """
    exemptions = load_exemptions()
    user_key = str(discord_id)

    if user_key not in exemptions:
        return [], None

    data = exemptions[user_key]

    # Handle old format (just a list of weeks)
    if isinstance(data, list):
        return data, None

    # New format (dict with weeks and reason)
    if isinstance(data, dict):
        return data.get("weeks", []), data.get("reason")

    return [], None


def get_future_weeks(num_weeks: int = 12, second_check: bool = True) -> list:
    """Get future weeks for exemption purposes.

    Args:
        num_weeks: Number of future weeks to return
        second_check: If True, returns weeks from Monday to Tuesday

    Returns a list of tuples: (week_label, start_date, end_date)
    """
    today = datetime.now(timezone.utc).date()

    # Find the next Tuesday (or today if it's Tuesday)
    days_until_tuesday = (1 - today.weekday()) % 7
    if days_until_tuesday == 0 and today.weekday() != 1:
        days_until_tuesday = 7

    next_tuesday = today + timedelta(days=days_until_tuesday)
    next_monday = next_tuesday - timedelta(days=8)  # Monday of the week being checked

    weeks = []
    for i in range(num_weeks):
        monday = next_monday + timedelta(weeks=i)
        tuesday = next_tuesday + timedelta(weeks=i)

        # Format: "Jan 01 - Jan 09, 2024"
        if monday.month == tuesday.month:
            week_label = f"{monday.strftime('%b %d')} - {tuesday.strftime('%d')}, {tuesday.year}"
        elif monday.year == tuesday.year:
            week_label = f"{monday.strftime('%b %d')} - {tuesday.strftime('%b %d')}, {tuesday.year}"
        else:
            week_label = f"{monday.strftime('%b %d, %Y')} - {tuesday.strftime('%b %d, %Y')}"

        weeks.append((week_label, monday, tuesday))

    return weeks
