import sqlite3
import uuid as uuid_mod
from datetime import datetime
from utils.paths import DB_DIR

POINTS_DB = str(DB_DIR / "esi_points.db")


def _player_points_table(player_uuid):
    """Return a safe table name for a player UUID."""
    return "player_" + player_uuid.replace("-", "_")


def init_points_database():
    """Create the esi_points table."""
    conn = sqlite3.connect(POINTS_DB)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS esi_points (
            uuid TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            points INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def save_points(resolved_players, points, reason: str = "Unknown"):
    """Add points for each resolved player and log a record in their individual table."""
    conn = sqlite3.connect(POINTS_DB)
    c = conn.cursor()
    for player in resolved_players:
        uuid = player.get("uuid")
        if not uuid:
            continue

        # Upsert into the global leaderboard table
        c.execute("""
            INSERT INTO esi_points (uuid, username, points)
            VALUES (?, ?, ?)
            ON CONFLICT(uuid) DO UPDATE SET
                username = excluded.username,
                points = esi_points.points + excluded.points
        """, (uuid, player["username"], points))

        # Per-player history table
        table = _player_points_table(uuid)
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS "{table}" (
                record_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                points_gained INTEGER NOT NULL,
                reason TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)
        c.execute(f"""
            INSERT INTO "{table}" (record_id, username, points_gained, reason, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """, (
            str(uuid_mod.uuid4()),
            player["username"],
            points,
            reason,
            datetime.utcnow().isoformat()
        ))

    conn.commit()
    conn.close()