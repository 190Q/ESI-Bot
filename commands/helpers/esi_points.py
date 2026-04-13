import sqlite3
from datetime import datetime
from utils.paths import DB_DIR

POINTS_DB = str(DB_DIR / "esi_points.db")

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

def save_points(resolved_players, points):
    """Add points for each resolved player in the esi_points database."""
    conn = sqlite3.connect(POINTS_DB)
    c = conn.cursor()
    for player in resolved_players:
        uuid = player.get("uuid")
        if not uuid:
            continue
        c.execute("""
            INSERT INTO esi_points (uuid, username, points)
            VALUES (?, ?, ?)
            ON CONFLICT(uuid) DO UPDATE SET
                username = excluded.username,
                points = esi_points.points + excluded.points
        """, (uuid, player["username"], points))
    conn.commit()
    conn.close()
