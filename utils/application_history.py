"""
Durable history of forwarded applications and their votes.

forwarded_applications.json only holds tickets that are still open - the close
flow deletes entries - so everything the panel wants to report over time (who
voted, how an application ended, how many arrived in a week) disappeared the
moment a ticket closed.

sync_applications() mirrors the JSON into application_history.db after every
save. Applications are never deleted: one that drops out of the JSON is marked
closed and keeps its last known state. Each vote is one row keyed on
(application, voter) holding the time the vote was first seen, so withdrawing a
vote removes that row without disturbing the others.

Admin votes carry a negative placeholder id instead of a real user, so they are
not written to the votes table - they still count towards the application's own
approve_count / deny_count.
"""

import sqlite3
from datetime import datetime, timezone

from utils.paths import DB_DIR

HISTORY_DB = DB_DIR / "application_history.db"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS applications (
        message_id TEXT PRIMARY KEY,
        app_type TEXT,
        user_id INTEGER,
        ticket_channel_id INTEGER,
        threshold INTEGER,
        status TEXT,
        approve_count INTEGER NOT NULL DEFAULT 0,
        deny_count INTEGER NOT NULL DEFAULT 0,
        submitted_at REAL,
        first_seen REAL NOT NULL,
        last_seen REAL NOT NULL,
        closed_at REAL,
        queue_position INTEGER,
        queue_type TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS votes (
        message_id TEXT NOT NULL,
        voter_id INTEGER NOT NULL,
        vote TEXT NOT NULL,
        voted_at REAL NOT NULL,
        PRIMARY KEY (message_id, voter_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_app_history_voter ON votes(voter_id)",
    "CREATE INDEX IF NOT EXISTS idx_app_history_voted_at ON votes(voted_at)",
    "CREATE INDEX IF NOT EXISTS idx_app_history_submitted ON applications(submitted_at)",
)


def _connect():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(HISTORY_DB), timeout=10)
    for statement in _SCHEMA:
        conn.execute(statement)
    return conn


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sync_votes(cursor, message_id, app, now):
    """Reconcile one application's vote rows with the voters it now holds."""
    wanted = {}
    for vote_type, key in (("approve", "approve_voters"), ("deny", "deny_voters")):
        for voter in app.get(key) or []:
            voter_id = _as_int(voter)
            # Negative ids are the bot's admin-vote placeholders, not people.
            if voter_id is None or voter_id <= 0:
                continue
            wanted[voter_id] = vote_type

    existing = {
        row[0]: row[1]
        for row in cursor.execute(
            "SELECT voter_id, vote FROM votes WHERE message_id = ?", (message_id,)
        )
    }
    for voter_id, vote_type in wanted.items():
        if voter_id not in existing:
            cursor.execute(
                "INSERT INTO votes (message_id, voter_id, vote, voted_at)"
                " VALUES (?, ?, ?, ?)",
                (message_id, voter_id, vote_type, now),
            )
        elif existing[voter_id] != vote_type:
            cursor.execute(
                "UPDATE votes SET vote = ? WHERE message_id = ? AND voter_id = ?",
                (vote_type, message_id, voter_id),
            )
    for voter_id in set(existing) - set(wanted):
        cursor.execute(
            "DELETE FROM votes WHERE message_id = ? AND voter_id = ?",
            (message_id, voter_id),
        )


def sync_applications(apps):
    """Mirror the forwarded-applications JSON into the history database.

    Safe to call on every save: it is a handful of upserts over the open
    applications, and it is the only writer of this database.
    """
    if not isinstance(apps, dict):
        return
    now = datetime.now(timezone.utc).timestamp()
    try:
        conn = _connect()
    except sqlite3.Error as exc:
        print(f"[HISTORY] Could not open {HISTORY_DB}: {exc}")
        return

    try:
        cursor = conn.cursor()
        seen = []
        for key, app in apps.items():
            if not isinstance(app, dict):
                continue
            message_id = str(app.get("message_id") or key)
            seen.append(message_id)

            approve_count = _as_int(app.get("approve_count"))
            if approve_count is None:
                approve_count = len(app.get("approve_voters") or [])
            deny_count = _as_int(app.get("deny_count"))
            if deny_count is None:
                deny_count = len(app.get("deny_voters") or [])

            cursor.execute(
                "INSERT INTO applications (message_id, app_type, user_id,"
                " ticket_channel_id, threshold, status, approve_count, deny_count,"
                " submitted_at, first_seen, last_seen, closed_at, queue_position,"
                " queue_type)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)"
                " ON CONFLICT(message_id) DO UPDATE SET"
                " app_type = excluded.app_type,"
                " user_id = excluded.user_id,"
                " ticket_channel_id = excluded.ticket_channel_id,"
                " threshold = excluded.threshold,"
                " status = excluded.status,"
                " approve_count = excluded.approve_count,"
                " deny_count = excluded.deny_count,"
                " submitted_at = COALESCE(excluded.submitted_at,"
                "                          applications.submitted_at),"
                " last_seen = excluded.last_seen,"
                " closed_at = NULL,"
                " queue_position = excluded.queue_position,"
                " queue_type = excluded.queue_type",
                (
                    message_id,
                    str(app.get("app_type") or "") or None,
                    _as_int(app.get("user_id")),
                    _as_int(app.get("ticket_channel_id")),
                    _as_int(app.get("threshold")),
                    str(app.get("status") or "pending"),
                    approve_count,
                    deny_count,
                    app.get("timestamp"),
                    now,
                    now,
                    _as_int(app.get("queue_position")),
                    str(app.get("queue_type") or "") or None,
                ),
            )
            _sync_votes(cursor, message_id, app, now)

        # Anything the JSON no longer holds has been closed or dropped.
        if seen:
            marks = ",".join("?" * len(seen))
            cursor.execute(
                "UPDATE applications SET closed_at = ? WHERE closed_at IS NULL"
                " AND message_id NOT IN (" + marks + ")",
                tuple([now] + seen),
            )
        else:
            cursor.execute(
                "UPDATE applications SET closed_at = ? WHERE closed_at IS NULL",
                (now,),
            )
        conn.commit()
    except sqlite3.Error as exc:
        print(f"[HISTORY] Failed to record applications: {exc}")
    finally:
        conn.close()
