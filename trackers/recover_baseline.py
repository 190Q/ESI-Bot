"""
recover_baseline.py

Rebuild ``points_baseline.db``'s ``baseline`` table from a known-good API
snapshot (an ``ESI_*.db`` file under ``databases/api_tracking/api_*/``).

Use this after the tracker was offline for a while and the baseline / guild-raid
fault offsets went stale (e.g. a guild-raid reset happened during downtime, so
counters "dropped" and the offsets grew larger than the raw totals).

What it does
------------
For every player found in the snapshot it sets:
  * ``wars``         = the player's ``wars`` from ``player_stats``
  * ``total_graids`` = the player's raw ``total_graids`` from ``guild_raid_stats``
                       with the snapshot's ``graid_fault_offsets`` applied using
                       the exact same logic the tracker uses (stale offsets, where
                       ``offset >= raw total``, are ignored).
  * ``recovered_graid_allocations`` marker rows in ``points_baseline.db``
                       for positive graid deltas versus the previous baseline,
                       tagged with the inferred offline span.

Because the baseline is rebuilt to the *corrected* values the tracker itself
will compute, the next award cycle sees a delta of 0 and awards no spurious EP.
It intentionally does NOT write the ``tracker_meta`` last-run marker, so the
first live cycle is still treated as a cold start and will silently re-baseline
any residual drops (handled by award_points_from_diff).

It does NOT touch ``esi_points.db`` (awarded EP / history is left untouched) and
does NOT modify the snapshot. Stale offsets left in the snapshots self-heal on
the next live cycle and, since they are ignored anyway, do not change results.

Usage
-----
    python trackers/recover_baseline.py --snapshot "<path to ESI_*.db>"
    python trackers/recover_baseline.py --snapshot "<...>" --dry-run
    python trackers/recover_baseline.py --snapshot "<...>" --baseline-db "<path>"

Stop the tracker before running this, then start it again afterwards. Point
``--snapshot`` at your most recent snapshot so the offsets match what the
tracker will read on its next cycle.
"""

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Repo root = parent of the trackers/ directory that holds this script
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    # Preferred: reuse the tracker's own offset logic so results match exactly
    from utils.api_tracking_utils import apply_graid_fault_offset
except Exception:
    # Fallback replica
    def apply_graid_fault_offset(total_graids, offset, username,
                                 log_warning=False, log_prefix="[POINTS]"):
        raw_total = int(total_graids or 0)
        fault_offset = int(offset or 0)
        stale = False
        if raw_total > 0 and fault_offset > 0 and fault_offset >= raw_total:
            stale = True
            fault_offset = 0
        return max(0, raw_total - fault_offset), stale


DEFAULT_BASELINE_DB = ROOT / "databases" / "points_baseline.db"


def _table_exists(cur, name: str) -> bool:
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


def _read_existing_baseline_state(baseline_path: Path):
    """Return (by_uuid, by_username, last_award_run_utc) from current baseline DB."""
    by_uuid = {}
    by_username = {}
    last_award_run = None
    if not baseline_path.exists():
        return by_uuid, by_username, last_award_run

    conn = sqlite3.connect(str(baseline_path))
    cur = conn.cursor()
    try:
        if _table_exists(cur, "baseline"):
            for uuid, username, total_graids in cur.execute(
                "SELECT uuid, username, total_graids FROM baseline"
            ):
                total = int(total_graids or 0)
                if uuid:
                    by_uuid[uuid] = total
                ulow = (username or "").strip().lower()
                if ulow:
                    by_username[ulow] = total
        if _table_exists(cur, "tracker_meta"):
            row = cur.execute(
                "SELECT value FROM tracker_meta WHERE key = 'last_award_run_utc'"
            ).fetchone()
            if row and row[0]:
                try:
                    parsed = datetime.fromisoformat(str(row[0]))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    last_award_run = parsed.astimezone(timezone.utc)
                except ValueError:
                    last_award_run = None
    finally:
        conn.close()

    return by_uuid, by_username, last_award_run


def _infer_snapshot_day(snapshot_path: Path, snapshot_mtime_utc: datetime):
    parent_name = snapshot_path.parent.name
    if parent_name.startswith("api_"):
        try:
            return datetime.strptime(parent_name[4:], "%d-%m-%Y").date()
        except ValueError:
            pass
    return snapshot_mtime_utc.date()


def _infer_recovery_span(snapshot_path: Path, snapshot_mtime_utc: datetime, last_award_run_utc):
    end_day = _infer_snapshot_day(snapshot_path, snapshot_mtime_utc)
    if isinstance(last_award_run_utc, datetime):
        start_day = (last_award_run_utc.astimezone(timezone.utc).date() + timedelta(days=1))
        if start_day > end_day:
            start_day = end_day
    else:
        start_day = end_day
    return start_day, end_day


def read_snapshot(snapshot_path: Path):
    """Return (players, offsets).

    players: {uuid: {"username": str, "wars": int, "raw_graids": int}}
    offsets: {username_lower: int}
    """
    conn = sqlite3.connect(str(snapshot_path))
    cur = conn.cursor()

    if not _table_exists(cur, "player_stats"):
        conn.close()
        raise SystemExit(
            f"Snapshot has no 'player_stats' table (not a valid ESI snapshot?): {snapshot_path}"
        )

    players = {}
    for uuid, username, wars in cur.execute(
        "SELECT uuid, username, wars FROM player_stats"
    ):
        if not uuid or not username:
            continue
        players[uuid] = {
            "username": username,
            "wars": int(wars or 0),
            "raw_graids": 0,
        }

    if _table_exists(cur, "guild_raid_stats"):
        for uuid, username, total in cur.execute(
            "SELECT uuid, username, total_graids FROM guild_raid_stats"
        ):
            if not uuid:
                continue
            if uuid in players:
                players[uuid]["raw_graids"] = int(total or 0)
            else:
                players[uuid] = {
                    "username": username or "",
                    "wars": 0,
                    "raw_graids": int(total or 0),
                }

    offsets = {}
    if _table_exists(cur, "graid_fault_offsets"):
        for username, offset in cur.execute(
            "SELECT username, offset FROM graid_fault_offsets"
        ):
            if username:
                offsets[username.lower()] = int(offset or 0)

    conn.close()
    return players, offsets


def build_rows(players: dict, offsets: dict):
    """Return (rows, stale_count) where rows = [(uuid, username, wars, graids)]."""
    rows = []
    stale_count = 0
    for uuid, info in players.items():
        username = info["username"]
        raw = info["raw_graids"]
        offset = offsets.get((username or "").lower(), 0)
        corrected, stale = apply_graid_fault_offset(raw, offset, username)
        if stale:
            stale_count += 1
        rows.append((uuid, username, int(info["wars"]), int(corrected)))
    return rows, stale_count


def build_recovered_rows(rows, previous_by_uuid, previous_by_username, start_day, end_day):
    """Return marker rows: [(uuid, username, start_day, end_day, span_days, total_graids)]."""
    marker_rows = []
    span_days = max(1, (end_day - start_day).days + 1)
    start_iso = start_day.isoformat()
    end_iso = end_day.isoformat()

    for uuid, username, _wars, corrected_graids in rows:
        prev = None
        if uuid in previous_by_uuid:
            prev = int(previous_by_uuid.get(uuid, 0))
        else:
            ulow = (username or "").strip().lower()
            if ulow in previous_by_username:
                prev = int(previous_by_username.get(ulow, 0))
        if prev is None:
            continue

        delta = max(0, int(corrected_graids) - prev)
        if delta <= 0:
            continue
        marker_rows.append(
            (uuid, username, start_iso, end_iso, span_days, delta)
        )

    return marker_rows


def main():
    ap = argparse.ArgumentParser(
        description="Rebuild points_baseline.db from a known-good API snapshot."
    )
    ap.add_argument(
        "--snapshot", required=True,
        help="Path to the ESI_*.db snapshot to rebuild the baseline from.",
    )
    ap.add_argument(
        "--baseline-db", default=str(DEFAULT_BASELINE_DB),
        help=f"Path to points_baseline.db (default: {DEFAULT_BASELINE_DB}).",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Preview the rebuilt baseline without writing anything.",
    )
    ap.add_argument(
        "--no-backup", action="store_true",
        help="Skip writing a timestamped backup of the baseline DB.",
    )
    args = ap.parse_args()

    snapshot_path = Path(args.snapshot)
    baseline_path = Path(args.baseline_db)

    if not snapshot_path.exists():
        raise SystemExit(f"Snapshot not found: {snapshot_path}")

    snap_mtime_dt = datetime.fromtimestamp(
        os.path.getmtime(snapshot_path), tz=timezone.utc
    )
    snap_mtime = snap_mtime_dt.isoformat()
    previous_by_uuid, previous_by_username, last_award_run = _read_existing_baseline_state(
        baseline_path
    )

    players, offsets = read_snapshot(snapshot_path)
    rows, stale_count = build_rows(players, offsets)
    recovery_start_day, recovery_end_day = _infer_recovery_span(
        snapshot_path,
        snap_mtime_dt,
        last_award_run,
    )
    recovered_rows = build_recovered_rows(
        rows,
        previous_by_uuid,
        previous_by_username,
        recovery_start_day,
        recovery_end_day,
    )

    print(f"Snapshot:      {snapshot_path}")
    print(f"Snapshot time: {snap_mtime} (UTC)")
    print(f"Baseline DB:   {baseline_path}")
    print(
        f"Players:       {len(rows)}  "
        f"(offsets in snapshot: {len(offsets)}, stale ignored: {stale_count})"
    )
    if previous_by_uuid or previous_by_username:
        span_days = max(1, (recovery_end_day - recovery_start_day).days + 1)
        total_recovered = sum(int(r[5]) for r in recovered_rows)
        print(
            f"Recover span:  {recovery_start_day.isoformat()} -> {recovery_end_day.isoformat()} "
            f"({span_days} day(s))"
        )
        print(
            f"Recovered:     {len(recovered_rows)} players, {total_recovered} total graids "
            f"(marker rows)"
        )
    else:
        print("Recovered:     skipped marker generation (no previous baseline to diff)")
    print("Top 10 by graids (username / wars / corrected graids):")
    for uuid, username, wars, graids in sorted(
        rows, key=lambda r: r[3], reverse=True
    )[:10]:
        print(f"  {username:<20} wars={wars:<7} graids={graids}")

    if args.dry_run:
        print("\n[dry-run] No changes written.")
        return

    if not rows:
        raise SystemExit(
            "Refusing to wipe the baseline: the snapshot produced 0 players."
        )

    # Safety backup of the existing baseline DB
    if baseline_path.exists() and not args.no_backup:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup = baseline_path.with_name(
            f"{baseline_path.stem}.backup_{stamp}{baseline_path.suffix}"
        )
        shutil.copy2(baseline_path, backup)
        print(f"\nBackup written: {backup}")

    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(baseline_path))
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS baseline (
            uuid TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            wars INTEGER NOT NULL DEFAULT 0,
            total_graids INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS recovered_graid_allocations (
            uuid TEXT NOT NULL,
            username TEXT NOT NULL,
            start_day TEXT NOT NULL,
            end_day TEXT NOT NULL,
            span_days INTEGER NOT NULL,
            total_graids INTEGER NOT NULL,
            snapshot_path TEXT,
            snapshot_time_utc TEXT,
            generated_at_utc TEXT NOT NULL
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_recovered_graid_allocations_username "
        "ON recovered_graid_allocations(username)"
    )
    cur.execute("DELETE FROM baseline")
    cur.execute("DELETE FROM recovered_graid_allocations")
    cur.executemany(
        "INSERT OR REPLACE INTO baseline (uuid, username, wars, total_graids) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    if recovered_rows:
        generated_at = datetime.now(timezone.utc).isoformat()
        cur.executemany(
            "INSERT INTO recovered_graid_allocations "
            "(uuid, username, start_day, end_day, span_days, total_graids, "
            " snapshot_path, snapshot_time_utc, generated_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    uuid,
                    username,
                    start_day,
                    end_day,
                    int(span_days),
                    int(total_graids),
                    str(snapshot_path),
                    snap_mtime,
                    generated_at,
                )
                for uuid, username, start_day, end_day, span_days, total_graids in recovered_rows
            ],
        )
    conn.commit()
    conn.close()

    print(f"\nRebuilt baseline with {len(rows)} players.")
    if recovered_rows:
        print(f"Wrote {len(recovered_rows)} recovered marker rows to recovered_graid_allocations.")
    else:
        print("No recovered marker rows written.")
    print(
        "Done. Start the tracker again; the first cycle should award 0 EP "
        "(no spurious deltas), and any residual drops are re-baselined silently."
    )


if __name__ == "__main__":
    main()
