import sqlite3
import uuid as uuid_mod
from datetime import datetime, timezone, timedelta
from utils.paths import DB_DIR

POINTS_DB = str(DB_DIR / "esi_points.db")

# Anchor: start of cycle 1
CYCLE_ANCHOR = datetime(2026, 4, 21, 16, 0, 0, tzinfo=timezone.utc)
CYCLE_DURATION = timedelta(weeks=2)

# Reasons that produce "dirty" EP for HR players
_DIRTY_EXACT = {"guild raid", "war"}


def is_dirty_reason(reason: str) -> bool:
    """Return True if the EP reason is classified as dirty for HR players."""
    r = reason.strip().lower()
    return r in _DIRTY_EXACT or r.startswith("quest")


def _player_points_table(player_uuid):
    """Return a safe table name for a player UUID."""
    return "player_" + player_uuid.replace("-", "_")


def get_cycle_id(dt: datetime = None) -> int:
    """Return the cycle number (1-based) for a given datetime (defaults to now)."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    return int((dt - CYCLE_ANCHOR) / CYCLE_DURATION) + 1


def get_cycle_bounds(cycle_id: int) -> tuple[datetime, datetime]:
    """Return the (start, end) UTC datetimes for a given cycle."""
    start = CYCLE_ANCHOR + CYCLE_DURATION * (cycle_id - 1)
    end = start + CYCLE_DURATION
    return start, end


def init_points_database():
    """Create the cycles and per-player tables."""
    conn = sqlite3.connect(POINTS_DB)
    c = conn.cursor()

    # One row per player per cycle
    c.execute("""
        CREATE TABLE IF NOT EXISTS esi_points (
            uuid TEXT NOT NULL,
            username TEXT NOT NULL,
            cycle_id INTEGER NOT NULL,
            points INTEGER NOT NULL DEFAULT 0,
            clean_ep INTEGER NOT NULL DEFAULT 0,
            dirty_ep INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (uuid, cycle_id)
        )
    """)

    # EP reservations
    c.execute("""
        CREATE TABLE IF NOT EXISTS ep_reservations (
            reservation_id TEXT PRIMARY KEY,
            uuid TEXT NOT NULL,
            username TEXT NOT NULL,
            reserved_amount INTEGER NOT NULL,
            ep_type TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            released_at TEXT
        )
    """)

    conn.commit()
    conn.close()


# HR guild ranks whose dirty reasons are filtered out of LE
HR_RANKS = {"strategist", "chief", "owner"}


def save_points(resolved_players, points, reason: str = "Unknown",
                rank_at_cycle_start: str | None = None):
    """
    Add points for each resolved player under the current cycle,
    and log a record in their individual history table.

    *rank_at_cycle_start* should be the lowered guild rank of the player at
    the start of the current cycle (e.g. "strategist").  When provided and
    the player is HR, the dirty-reason logic is applied; otherwise the
    record defaults to clean.
    """
    current_cycle = get_cycle_id()
    now = datetime.now(timezone.utc).isoformat()

    # Determine dirty flag
    dirty = 0
    if rank_at_cycle_start and rank_at_cycle_start.lower() in HR_RANKS:
        if is_dirty_reason(reason):
            dirty = 1

    conn = sqlite3.connect(POINTS_DB)
    c = conn.cursor()

    for player in resolved_players:
        uuid = player.get("uuid")
        if not uuid:
            continue

        clean_delta = 0 if dirty else points
        dirty_delta = points if dirty else 0

        # Upsert into the cycle leaderboard table
        c.execute("""
            INSERT INTO esi_points (uuid, username, cycle_id, points, clean_ep, dirty_ep)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(uuid, cycle_id) DO UPDATE SET
                username = excluded.username,
                points = esi_points.points + excluded.points,
                clean_ep = esi_points.clean_ep + excluded.clean_ep,
                dirty_ep = esi_points.dirty_ep + excluded.dirty_ep
        """, (uuid, player["username"], current_cycle, points, clean_delta, dirty_delta))

        # Per-player history table
        table = _player_points_table(uuid)
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS "{table}" (
                record_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                points_gained INTEGER NOT NULL,
                cycle_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                is_dirty INTEGER NOT NULL DEFAULT 0
            )
        """)
        c.execute(f"""
            INSERT INTO "{table}" (record_id, username, points_gained, cycle_id, reason, timestamp, is_dirty)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            str(uuid_mod.uuid4()),
            player["username"],
            points,
            current_cycle,
            reason,
            now,
            dirty,
        ))

    conn.commit()
    conn.close()


def get_claimable_points(uuid: str) -> dict:
    """
    Return claimable points for a player.

    - current_cycle_points: points accumulated this cycle (not yet claimable)
    - previous_cycle_points: points from the previous cycle (claimable now, cleared after this cycle ends)
    """
    current_cycle = get_cycle_id()
    previous_cycle = current_cycle - 1

    conn = sqlite3.connect(POINTS_DB)
    c = conn.cursor()

    c.execute("""
        SELECT cycle_id, points FROM esi_points WHERE uuid = ? AND cycle_id IN (?, ?)
    """, (uuid, current_cycle, previous_cycle))

    rows = {row[0]: row[1] for row in c.fetchall()}
    conn.close()

    return {
        "current_cycle": current_cycle,
        "current_cycle_points": rows.get(current_cycle, 0),
        "previous_cycle": previous_cycle,
        "previous_cycle_points": rows.get(previous_cycle, 0),
    }


def clear_expired_points():
    """
    Delete point records older than the previous cycle.
    Call this on bot startup or on a scheduled task.
    """
    current_cycle = get_cycle_id()
    cutoff_cycle = current_cycle - 1  # Keep current and previous only

    conn = sqlite3.connect(POINTS_DB)
    c = conn.cursor()
    c.execute("DELETE FROM esi_points WHERE cycle_id < ?", (cutoff_cycle,))
    conn.commit()
    conn.close()