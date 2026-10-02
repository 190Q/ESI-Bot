"""
Usage counters for the analytics panel.

One row per slash-command invocation, and one per non-command feature event, so
the panel can show which commands and features the guild actually uses.

Every write here is best-effort. If the database cannot be written the failure
is logged and the caller carries on, because losing a counter must never cost
someone their command.
"""

import sqlite3
import time
from contextlib import closing

from utils.paths import DB_DIR

USAGE_DB = DB_DIR / "usage.db"

KIND_COMMAND = "command"
KIND_FEATURE = "feature"

RETENTION_DAYS = 400

_ready = False


def init_database():
    """Create the table and prune expired rows. Safe to call repeatedly."""
    global _ready
    if _ready:
        return
    try:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(USAGE_DB), timeout=5)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    user_id INTEGER,
                    guild_id INTEGER,
                    ok INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_kind_ts ON usage(kind, ts)"
            )
            conn.execute(
                "DELETE FROM usage WHERE ts < ?",
                (int(time.time()) - RETENTION_DAYS * 86400,),
            )
            conn.commit()
        _ready = True
    except (sqlite3.Error, OSError) as exc:
        print(f"[USAGE] Could not prepare the usage database: {exc}")


def _record(kind, name, user_id=None, guild_id=None, ok=1):
    if not name:
        return
    init_database()
    try:
        with closing(sqlite3.connect(str(USAGE_DB), timeout=5)) as conn:
            conn.execute(
                "INSERT INTO usage (ts, kind, name, user_id, guild_id, ok)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (int(time.time()), kind, str(name)[:80], user_id, guild_id, ok),
            )
            conn.commit()
    except (sqlite3.Error, OSError) as exc:
        print(f"[USAGE] Could not record {kind} {name!r}: {exc}")


def record_command(name, user_id=None, guild_id=None, ok=True):
    """Count one slash-command invocation."""
    _record(KIND_COMMAND, name, user_id, guild_id, 1 if ok else 0)


def record_feature(name, user_id=None, guild_id=None):
    """Count one non-command feature event."""
    _record(KIND_FEATURE, name, user_id, guild_id)
