import sqlite3
import uuid as uuid_mod
from datetime import datetime, timezone, timedelta
from utils.paths import DB_DIR

POINTS_DB = str(DB_DIR / "esi_points.db")

# Anchor: start of cycle 1
CYCLE_ANCHOR = datetime(2026, 4, 21, 16, 0, 0, tzinfo=timezone.utc)
CYCLE_DURATION = timedelta(weeks=2)


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
            PRIMARY KEY (uuid, cycle_id)
        )
    """)

    conn.commit()
    conn.close()


def save_points(resolved_players, points, reason: str = "Unknown"):
    """
    Add points for each resolved player under the current cycle,
    and log a record in their individual history table.
    """
    current_cycle = get_cycle_id()
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(POINTS_DB)
    c = conn.cursor()

    for player in resolved_players:
        uuid = player.get("uuid")
        if not uuid:
            continue

        # Upsert into the cycle leaderboard table
        c.execute("""
            INSERT INTO esi_points (uuid, username, cycle_id, points)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(uuid, cycle_id) DO UPDATE SET
                username = excluded.username,
                points = esi_points.points + excluded.points
        """, (uuid, player["username"], current_cycle, points))

        # Per-player history table
        table = _player_points_table(uuid)
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS "{table}" (
                record_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                points_gained INTEGER NOT NULL,
                cycle_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)
        c.execute(f"""
            INSERT INTO "{table}" (record_id, username, points_gained, cycle_id, reason, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            str(uuid_mod.uuid4()),
            player["username"],
            points,
            current_cycle,
            reason,
            now
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