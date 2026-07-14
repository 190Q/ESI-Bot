import json
import os
import shutil
import sqlite3
import statistics
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Dict, Optional, Union
from utils.coverage_utils import record_daily_coverage


PathLike = Union[str, Path]

# Sanity caps: a player realistically cannot gain more than this many per cycle
MAX_NEW_WARS_PER_CYCLE = 20
MAX_NEW_GRAIDS_PER_CYCLE = 5
STALE_OFFSET_REBASE_THRESHOLD = 50

# If the tracker has not run for longer than this, the next award cycle is
# treated as a cold start after downtime: dropped counters are silently
# re-baselined to the current values instead of emitting "counter dropped"
# warnings and holding a stale-high baseline (which would otherwise repeat
# every cycle until each player climbed back over the old value).
DOWNTIME_REBASE_THRESHOLD_SECONDS = 30 * 60  # 30 minutes

# Guild-wide graid fault detection thresholds
GRAID_FAULT_MIN_AFFECTED_PCT = 0.5
GRAID_FAULT_MIN_MEMBERS = 10
GRAID_FAULT_MIN_MEDIAN_DELTA = 50
GRAID_FAULT_GUILD_WIDE_AVG = 15

# Storage constants
SIZE_LIMIT_BYTES = 20 * 1024 * 1024 * 1024  # 20GB
CLEANUP_INTERVAL_MINUTES = 30

BADGE_ROLES = {
    "War Badges": {
        "10k": 1426633275635404981,
        "6k": 1426633206857465888,
        "3k": 1426633036736368861,
        "1.5k": 1426632920528846880,
        "750": 1426633144093638778,
        "300": 1426632862207049778,
        "100": 1426632780615385098,
    },
    "Quest Badges": {
        "350": 1426636141242617906,
        "225": 1426636108321525891,
        "150": 1426636066856898593,
        "90": 1426636018664341675,
        "50": 1426635982614040676,
        "25": 1426635948992761988,
        "10": 1426635880462024937,
    },
    "Recruitment Badges": {
        "250": 1426637291706912788,
        "150": 1426637244109946920,
        "80": 1426637209301160039,
        "50": 1426637168071282808,
        "25": 1426637134378303619,
        "10": 1426637094339608586,
        "5": 1426636993630175447,
    },
    "Raid Badges": {
        "6k": 1426634664025526405,
        "3.5k": 1426634622791323938,
        "2k": 1426634579644514347,
        "1k": 1426634531284324353,
        "500": 1426634469401432194,
        "100": 1426634408370114773,
        "50": 1426634317970542613,
    },
    "Event Badges": {
        "100": 1440682465717915779,
        "75": 1440682471086751815,
        "55": 1440682473641083011,
        "35": 1440682477055115304,
        "20": 1440682480846897232,
        "10": 1440682485548711997,
        "3": 1440682762133569730,
    },
}


def _as_path(path_value: PathLike) -> Path:
    return path_value if isinstance(path_value, Path) else Path(path_value)


def get_current_day_string() -> str:
    return datetime.now(timezone.utc).strftime("%d-%m-%Y")


def get_day_folder_path(api_tracking_folder: PathLike, day_string: Optional[str] = None) -> Path:
    if day_string is None:
        day_string = get_current_day_string()
    return _as_path(api_tracking_folder) / f"api_{day_string}"


def cleanup_daily_folder(
    day_folder: PathLike,
    cleanup_interval_minutes: int = CLEANUP_INTERVAL_MINUTES,
    log_prefix: str = "[API]",
) -> None:
    day_folder_path = _as_path(day_folder)
    if not day_folder_path.exists():
        return

    db_files = sorted(day_folder_path.glob("*.db"), key=lambda f: f.stat().st_mtime)
    if len(db_files) <= 1:
        return

    files_to_keep = set()
    last_kept_time = None
    margin_seconds = 3 * 60  # 3 minute margin

    for db_file in db_files:
        file_mtime = db_file.stat().st_mtime
        if last_kept_time is None:
            files_to_keep.add(db_file)
            last_kept_time = file_mtime
            continue

        time_diff = file_mtime - last_kept_time
        if time_diff >= (cleanup_interval_minutes * 60 - margin_seconds):
            files_to_keep.add(db_file)
            last_kept_time = file_mtime

    files_to_keep.add(db_files[-1])

    deleted_count = 0
    for db_file in db_files:
        if db_file in files_to_keep:
            continue
        try:
            db_file.unlink()
            deleted_count += 1
        except Exception as e:
            print(f"{log_prefix} Failed to delete {db_file}: {e}")

    if deleted_count > 0:
        print(f"{log_prefix} Cleaned up {deleted_count} files from {day_folder_path.name}")


def cleanup_old_day_folders(
    api_tracking_folder: PathLike,
    min_days_old: int = 7,
    max_days_old: Optional[int] = None,
    log_prefix: str = "[API]",
) -> None:
    api_tracking = _as_path(api_tracking_folder)
    if not api_tracking.exists():
        return

    today = datetime.now(timezone.utc).date()

    for folder in api_tracking.iterdir():
        if not folder.is_dir() or not folder.name.startswith("api_"):
            continue

        try:
            date_str = folder.name.replace("api_", "")
            folder_date = datetime.strptime(date_str, "%d-%m-%Y").date()
            days_old = (today - folder_date).days
        except ValueError:
            continue
        db_files = sorted(folder.glob("*.db"), key=lambda f: f.stat().st_mtime)
        should_record_coverage = 1 <= days_old < min_days_old
        if should_record_coverage:
            record_daily_coverage(
                api_tracking / "coverage.db",
                date_str,
                folder.name,
                db_files,
                log_prefix=f"{log_prefix}[COVERAGE]",
            )

        if days_old < min_days_old:
            continue
        if max_days_old is not None and days_old > max_days_old:
            continue
        if len(db_files) <= 1:
            continue

        deleted_count = 0
        for db_file in db_files[:-1]:
            try:
                db_file.unlink()
                deleted_count += 1
            except Exception as e:
                print(f"{log_prefix} Failed to delete {db_file}: {e}")

        if deleted_count > 0:
            print(
                f"{log_prefix} Cleaned {deleted_count} files from {folder.name} "
                f"({days_old} days old, kept latest only)"
            )


def cleanup_old_databases(
    api_tracking_folder: PathLike,
    size_limit_bytes: int = SIZE_LIMIT_BYTES,
    log_prefix: str = "[API]",
) -> None:
    api_tracking = _as_path(api_tracking_folder)
    if not api_tracking.exists():
        return

    total_size = 0
    for root, _, files in os.walk(api_tracking):
        for filename in files:
            total_size += os.path.getsize(os.path.join(root, filename))

    if total_size <= size_limit_bytes:
        return

    print(f"{log_prefix} Storage exceeds 20GB ({total_size / (1024**3):.2f} GB), cleaning up...")

    day_folders = []
    for folder in api_tracking.iterdir():
        if not folder.is_dir() or not folder.name.startswith("api_"):
            continue
        try:
            folder_date = datetime.strptime(folder.name.replace("api_", ""), "%d-%m-%Y")
            day_folders.append((folder, folder_date))
        except ValueError:
            continue
    day_folders.sort(key=lambda item: item[1])

    for folder, _ in day_folders:
        if total_size <= size_limit_bytes:
            break

        try:
            folder_size = sum(
                os.path.getsize(os.path.join(root, f))
                for root, _, files in os.walk(folder)
                for f in files
            )
            shutil.rmtree(folder)
            total_size -= folder_size
            print(f"{log_prefix} Deleted old folder: {folder.name}")
        except Exception as e:
            print(f"{log_prefix} Failed to delete {folder}: {e}")


def get_latest_fault_offsets(api_tracking_folder: PathLike, log_prefix: str = "[POINTS]") -> Dict[str, int]:
    offsets: Dict[str, int] = {}
    tracking_folder = _as_path(api_tracking_folder)

    try:
        if not tracking_folder.exists():
            return offsets

        db_files = []
        for day_folder in tracking_folder.iterdir():
            if day_folder.is_dir() and day_folder.name.startswith("api_"):
                db_files.extend(day_folder.glob("ESI_*.db"))
        if not db_files:
            return offsets

        db_files.sort(key=lambda file_path: file_path.stat().st_mtime, reverse=True)
        for db_file in db_files:
            try:
                conn = sqlite3.connect(str(db_file))
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='graid_fault_offsets'"
                )
                if not cursor.fetchone():
                    conn.close()
                    continue

                cursor.execute("SELECT username, offset FROM graid_fault_offsets")
                for row in cursor.fetchall():
                    if row[0] and row[1]:
                        offsets[row[0].lower()] = row[1]
                conn.close()
                break
            except Exception:
                continue
    except Exception as e:
        print(f"{log_prefix} Error loading fault offsets: {e}")

    return offsets


def apply_graid_fault_offset(
    total_graids: int,
    offset: int,
    username: str,
    log_warning: bool = False,
    log_prefix: str = "[POINTS]",
):
    raw_total = int(total_graids or 0)
    fault_offset = int(offset or 0)
    stale_offset = False

    if raw_total > 0 and fault_offset > 0 and fault_offset >= raw_total:
        stale_offset = True
        fault_offset = 0
        if log_warning:
            print(
                f"{log_prefix}[WARN] {username}: stale graid fault offset detected "
                f"(offset >= total: {offset} >= {raw_total}); ignoring offset."
            )

    return max(0, raw_total - fault_offset), stale_offset


def extract_guild_raid_payload(member: dict) -> dict:
    if not isinstance(member, dict):
        return {"total": 0, "list": {}}

    candidates = [
        member.get("guildRaids"),
        (member.get("globalData") or {}).get("guildRaids"),
    ]

    for candidate in candidates:
        if isinstance(candidate, dict):
            total = int(candidate.get("total", 0) or 0)
            per_raid = candidate.get("list", {})
            if not isinstance(per_raid, dict):
                per_raid = {}
            return {"total": total, "list": per_raid}
        if isinstance(candidate, (int, float)):
            return {"total": int(candidate), "list": {}}

    return {"total": 0, "list": {}}


def update_aspects_from_guild_data(
    guild_members,
    aspects_file: PathLike,
    api_tracking_folder: PathLike,
    log_prefix: str = "[ASPECTS]",
) -> None:
    aspects_path = _as_path(aspects_file)
    try:
        if aspects_path.exists():
            with open(aspects_path, "r", encoding="utf-8") as f:
                aspects_data = json.load(f)
        else:
            aspects_data = {"total_aspects": 22, "members": {}}

        fault_offsets = get_latest_fault_offsets(api_tracking_folder)
        changed = False

        for member in guild_members or []:
            uuid = member.get("uuid")
            if not uuid:
                continue

            username = member.get("username", "")
            graids_data = extract_guild_raid_payload(member)
            total_graids = graids_data.get("total", 0)
            offset = fault_offsets.get(username.lower(), 0)
            total_graids, _ = apply_graid_fault_offset(
                total_graids,
                offset,
                username,
                log_warning=False,
            )

            if uuid not in aspects_data["members"]:
                aspects_data["members"][uuid] = {
                    "name": username,
                    "baseline_graids": total_graids,
                    "owed": 0,
                }
                changed = True
            else:
                stored = aspects_data["members"][uuid]
                if stored.get("name") != username:
                    stored["name"] = username
                    changed = True

                baseline = stored.get("baseline_graids", total_graids)
                new_graids = total_graids - baseline
                if new_graids >= 2:
                    new_aspects = new_graids // 2
                    stored["owed"] = stored.get("owed", 0) + new_aspects
                    aspects_data["total_aspects"] += new_aspects
                    stored["baseline_graids"] = baseline + (new_aspects * 2)
                    changed = True
                    print(f"{log_prefix} {username}: +{new_aspects} aspects ({new_graids} new graids)")

        if changed:
            with open(aspects_path, "w", encoding="utf-8") as f:
                json.dump(aspects_data, f, indent=2)
            print(f"{log_prefix} Updated aspects data (total: {aspects_data['total_aspects']})")
    except Exception as e:
        print(f"{log_prefix} Error updating aspects data: {e}")


def init_points_baseline(points_baseline_db: PathLike) -> None:
    conn = sqlite3.connect(str(_as_path(points_baseline_db)))
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS baseline (
            uuid TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            wars INTEGER NOT NULL DEFAULT 0,
            total_graids INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS tracker_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS recovered_graid_allocations (
            uuid TEXT,
            username TEXT NOT NULL,
            start_day TEXT NOT NULL,
            end_day TEXT NOT NULL,
            span_days INTEGER NOT NULL,
            total_graids INTEGER NOT NULL,
            source TEXT,
            snapshot_path TEXT,
            snapshot_time_utc TEXT,
            generated_at_utc TEXT
        )
        """
    )
    recovered_cols = {
        row[1]
        for row in c.execute("PRAGMA table_info(recovered_graid_allocations)").fetchall()
        if len(row) > 1
    }
    if "source" not in recovered_cols:
        c.execute("ALTER TABLE recovered_graid_allocations ADD COLUMN source TEXT")
    if "snapshot_path" not in recovered_cols:
        c.execute("ALTER TABLE recovered_graid_allocations ADD COLUMN snapshot_path TEXT")
    if "snapshot_time_utc" not in recovered_cols:
        c.execute("ALTER TABLE recovered_graid_allocations ADD COLUMN snapshot_time_utc TEXT")
    if "generated_at_utc" not in recovered_cols:
        c.execute("ALTER TABLE recovered_graid_allocations ADD COLUMN generated_at_utc TEXT")
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_recovered_graid_allocations_username "
        "ON recovered_graid_allocations(username)"
    )
    conn.commit()
    conn.close()


def _read_last_award_run(conn) -> Optional[datetime]:
    """Return the UTC time of the last award cycle, or None if unknown."""
    try:
        row = conn.execute(
            "SELECT value FROM tracker_meta WHERE key = 'last_award_run_utc'"
        ).fetchone()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except ValueError:
        return None


def _write_last_award_run(conn, when: datetime) -> None:
    """Persist the timestamp of the current award cycle."""
    conn.execute(
        "INSERT OR REPLACE INTO tracker_meta (key, value) VALUES ('last_award_run_utc', ?)",
        (when.isoformat(),),
    )


def award_points_from_diff(
    member_stats: list,
    guild_members: list,
    points_baseline_db: PathLike,
    api_tracking_folder: PathLike,
    save_points_func: Callable,
    max_new_wars_per_cycle: int = MAX_NEW_WARS_PER_CYCLE,
    max_new_graids_per_cycle: int = MAX_NEW_GRAIDS_PER_CYCLE,
    stale_offset_rebase_threshold: int = STALE_OFFSET_REBASE_THRESHOLD,
    downtime_rebase_threshold_seconds: int = DOWNTIME_REBASE_THRESHOLD_SECONDS,
    log_prefix: str = "[POINTS]",
) -> None:
    init_points_baseline(points_baseline_db)
    fault_offsets = get_latest_fault_offsets(api_tracking_folder)

    graids_by_uuid = {}
    graid_stale_offset_by_uuid = {}
    rank_by_uuid = {}
    for member in (guild_members or []):
        uuid = member.get("uuid")
        if not uuid:
            continue
        graids_data = extract_guild_raid_payload(member)
        total_graids = graids_data.get("total", 0)
        username = member.get("username", "")
        offset = fault_offsets.get(username.lower(), 0)
        corrected_graids, stale_offset = apply_graid_fault_offset(
            total_graids,
            offset,
            username,
            log_warning=True,
        )
        graids_by_uuid[uuid] = corrected_graids
        graid_stale_offset_by_uuid[uuid] = stale_offset
        rank_by_uuid[uuid] = (member.get("rank") or "").lower()

    if not graids_by_uuid:
        print(
            f"{log_prefix}[WARN] No guild-raid totals were extracted from the guild payload; "
            "guild-raid EP awards will be skipped."
        )

    conn = sqlite3.connect(str(_as_path(points_baseline_db)))
    c = conn.cursor()

    # Detect a cold start after the tracker was stopped for a while. On the
    # first cycle back, counters legitimately look "dropped" (a graid season
    # reset, fault offsets carried forward, etc.), so we silently re-baseline
    # instead of warning and holding a stale-high baseline. A missing record
    # (first run ever / pre-existing baseline) is also treated as a cold start.
    now = datetime.now(timezone.utc)
    last_run = _read_last_award_run(conn)
    if last_run is None:
        cold_start_after_downtime = True
        print(
            f"{log_prefix} No previous run recorded; re-baselining any dropped "
            f"counters silently for this cycle."
        )
    elif (now - last_run).total_seconds() > downtime_rebase_threshold_seconds:
        cold_start_after_downtime = True
        gap_minutes = (now - last_run).total_seconds() / 60
        print(
            f"{log_prefix} Detected tracker downtime (~{gap_minutes:.0f} min since last run); "
            f"re-baselining dropped counters silently for this cycle."
        )
    else:
        cold_start_after_downtime = False
    recovery_start_day = now.date()
    recovery_end_day = now.date()
    recovery_span_days = 1
    if cold_start_after_downtime and isinstance(last_run, datetime):
        recovery_start_day = last_run.astimezone(timezone.utc).date() + timedelta(days=1)
        if recovery_start_day > recovery_end_day:
            recovery_start_day = recovery_end_day
        recovery_span_days = max(1, (recovery_end_day - recovery_start_day).days + 1)
    recovered_graid_markers = {}

    awarded_war_ep = 0
    awarded_graid_ep = 0
    awarded_players = set()

    for stats in member_stats:
        uuid = stats.get("uuid")
        username = stats.get("username")
        if not uuid or not username:
            continue

        current_wars = stats.get("wars", 0) or 0
        current_graids = graids_by_uuid.get(uuid, 0)

        c.execute("SELECT wars, total_graids FROM baseline WHERE uuid = ?", (uuid,))
        row = c.fetchone()

        if row is None:
            c.execute(
                "INSERT INTO baseline (uuid, username, wars, total_graids) VALUES (?, ?, ?, ?)",
                (uuid, username, current_wars, current_graids),
            )
            continue

        prev_wars, prev_graids = row
        stale_offset = graid_stale_offset_by_uuid.get(uuid, False)
        baseline_wars = prev_wars
        baseline_graids = prev_graids

        if current_wars < prev_wars:
            if cold_start_after_downtime:
                # Restart after downtime: accept the current value as the new
                # baseline without warning or awarding retroactively.
                new_wars = 0
                baseline_wars = current_wars
            else:
                print(
                    f"{log_prefix}[WARN] {username}: wars counter dropped "
                    f"({prev_wars} -> {current_wars}); keeping previous baseline."
                )
                new_wars = 0
        else:
            new_wars = current_wars - prev_wars
            baseline_wars = current_wars

        if current_graids < prev_graids:
            if cold_start_after_downtime:
                # Restart after downtime: accept the current value as the new
                # baseline without warning or awarding retroactively.
                new_graids = 0
                baseline_graids = current_graids
            else:
                print(
                    f"{log_prefix}[WARN] {username}: guild-raid counter dropped "
                    f"({prev_graids} -> {current_graids}); keeping previous baseline."
                )
                new_graids = 0
        else:
            new_graids = current_graids - prev_graids
            baseline_graids = current_graids

        if new_wars > max_new_wars_per_cycle:
            print(
                f"{log_prefix}[WARN] {username}: +{new_wars} new wars exceeds "
                f"cap of {max_new_wars_per_cycle}; rebasing without retroactive award."
            )
            new_wars = 0
            baseline_wars = max(baseline_wars, current_wars)

        if stale_offset and prev_graids == 0 and current_graids >= stale_offset_rebase_threshold:
            print(
                f"{log_prefix}[WARN] {username}: stale-offset recovery rebasing guild-raid baseline "
                f"to {current_graids} without retroactive award."
            )
            new_graids = 0
            baseline_graids = max(baseline_graids, current_graids)

        player = [{"uuid": uuid, "username": username}]
        player_rank = rank_by_uuid.get(uuid, "")

        if new_wars > 0:
            save_points_func(player, new_wars * 1, reason="War", rank_at_cycle_start=player_rank)
            print(f"{log_prefix} {username}: +{new_wars} war point(s)")
            awarded_war_ep += new_wars
            awarded_players.add(uuid)

        if new_graids > max_new_graids_per_cycle:
            print(
                f"{log_prefix}[WARN] {username}: +{new_graids} new raids exceeds "
                f"cap of {max_new_graids_per_cycle}; awarding full delta."
            )

        if new_graids > 0:
            graid_ep = new_graids * 10
            save_points_func(player, graid_ep, reason="Guild Raid", rank_at_cycle_start=player_rank)
            print(f"{log_prefix} {username}: +{graid_ep} guild raid point(s) ({new_graids} new raid(s))")
            awarded_graid_ep += graid_ep
            awarded_players.add(uuid)
        if cold_start_after_downtime and new_graids > max_new_graids_per_cycle:
            marker_key = username.lower().strip()
            if marker_key:
                marker = recovered_graid_markers.get(marker_key)
                if marker is None:
                    recovered_graid_markers[marker_key] = {
                        "uuid": uuid,
                        "username": username,
                        "total_graids": int(new_graids),
                    }
                else:
                    marker["total_graids"] += int(new_graids)

        c.execute(
            "UPDATE baseline SET username = ?, wars = ?, total_graids = ? WHERE uuid = ?",
            (username, baseline_wars, baseline_graids, uuid),
        )
    if cold_start_after_downtime:
        start_day_iso = recovery_start_day.isoformat()
        end_day_iso = recovery_end_day.isoformat()
        c.execute(
            "DELETE FROM recovered_graid_allocations WHERE start_day = ? AND end_day = ?",
            (start_day_iso, end_day_iso),
        )
        inserted_markers = 0
        for marker in recovered_graid_markers.values():
            total_graids = int(marker.get("total_graids", 0) or 0)
            if total_graids <= 0:
                continue
            c.execute(
                """
                INSERT INTO recovered_graid_allocations
                    (uuid, username, start_day, end_day, span_days, total_graids, generated_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    marker.get("uuid"),
                    marker.get("username"),
                    start_day_iso,
                    end_day_iso,
                    recovery_span_days,
                    total_graids,
                    now.isoformat(),
                ),
            )
            inserted_markers += 1
        if inserted_markers > 0:
            print(
                f"{log_prefix} Recorded {inserted_markers} recovered guild-raid marker(s) "
                f"for {start_day_iso} -> {end_day_iso}."
            )

    _write_last_award_run(conn, now)
    conn.commit()
    conn.close()
    print(
        f"{log_prefix} Award summary: total={awarded_war_ep + awarded_graid_ep} EP "
        f"(war={awarded_war_ep}, guild_raid={awarded_graid_ep}) across {len(awarded_players)} player(s)."
    )


def save_player_and_raid_stats(
    cursor,
    member_stats: list,
    guild_members: Optional[list] = None,
    include_shortened_rank: bool = False,
):
    if include_shortened_rank:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS player_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                uuid TEXT,
                timestamp TEXT,
                shortened_rank TEXT,
                guild_uuid TEXT,
                guild_name TEXT,
                guild_prefix TEXT,
                guild_rank TEXT,
                playtime INTEGER,
                wars INTEGER,
                total_level INTEGER,
                mobs_killed INTEGER,
                chests_found INTEGER,
                dungeons_total INTEGER,
                dungeons_list TEXT,
                raids_total INTEGER,
                raids_list TEXT,
                world_events INTEGER,
                loot_runs INTEGER,
                caves INTEGER,
                completed_quests INTEGER,
                pvp_kills INTEGER,
                pvp_deaths INTEGER
            )
            """
        )
    else:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS player_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                uuid TEXT,
                timestamp TEXT,
                guild_uuid TEXT,
                guild_name TEXT,
                guild_prefix TEXT,
                guild_rank TEXT,
                playtime INTEGER,
                wars INTEGER,
                total_level INTEGER,
                mobs_killed INTEGER,
                chests_found INTEGER,
                dungeons_total INTEGER,
                dungeons_list TEXT,
                raids_total INTEGER,
                raids_list TEXT,
                world_events INTEGER,
                loot_runs INTEGER,
                caves INTEGER,
                completed_quests INTEGER,
                pvp_kills INTEGER,
                pvp_deaths INTEGER
            )
            """
        )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS uuid_username_map (
            uuid TEXT PRIMARY KEY,
            username TEXT NOT NULL
        )
        """
    )

    uuid_username_pairs = []
    for stats in member_stats or []:
        if stats.get("uuid") and stats.get("username"):
            uuid_username_pairs.append((stats["uuid"], stats["username"]))

        guild_data = stats.get("guild", {})
        dungeons_data = stats.get("dungeons", {})
        raids_data = stats.get("raids", {})
        pvp_data = stats.get("pvp", {})

        if include_shortened_rank:
            cursor.execute(
                """
                INSERT INTO player_stats (
                    username, uuid, timestamp, shortened_rank,
                    guild_uuid, guild_name, guild_prefix, guild_rank,
                    playtime, wars, total_level, mobs_killed, chests_found,
                    dungeons_total, dungeons_list, raids_total, raids_list,
                    world_events, loot_runs, caves, completed_quests,
                    pvp_kills, pvp_deaths
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stats.get("username"),
                    stats.get("uuid"),
                    stats.get("timestamp"),
                    stats.get("shortenedRank"),
                    guild_data.get("uuid") if isinstance(guild_data, dict) else None,
                    guild_data.get("name") if isinstance(guild_data, dict) else None,
                    guild_data.get("prefix") if isinstance(guild_data, dict) else None,
                    guild_data.get("rank") if isinstance(guild_data, dict) else None,
                    stats.get("playtime", 0),
                    stats.get("wars", 0),
                    stats.get("totalLevel", 0),
                    stats.get("mobsKilled", 0),
                    stats.get("chestsFound", 0),
                    dungeons_data.get("total", 0) if isinstance(dungeons_data, dict) else 0,
                    json.dumps(dungeons_data.get("list", {})) if isinstance(dungeons_data, dict) else "{}",
                    raids_data.get("total", 0) if isinstance(raids_data, dict) else 0,
                    json.dumps(raids_data.get("list", {})) if isinstance(raids_data, dict) else "{}",
                    stats.get("worldEvents", 0),
                    stats.get("lootRuns", 0),
                    stats.get("caves", 0),
                    stats.get("completedQuests", 0),
                    pvp_data.get("kills", 0) if isinstance(pvp_data, dict) else 0,
                    pvp_data.get("deaths", 0) if isinstance(pvp_data, dict) else 0,
                ),
            )
        else:
            cursor.execute(
                """
                INSERT INTO player_stats (
                    username, uuid, timestamp,
                    guild_uuid, guild_name, guild_prefix, guild_rank,
                    playtime, wars, total_level, mobs_killed, chests_found,
                    dungeons_total, dungeons_list, raids_total, raids_list,
                    world_events, loot_runs, caves, completed_quests,
                    pvp_kills, pvp_deaths
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stats.get("username"),
                    stats.get("uuid"),
                    stats.get("timestamp"),
                    guild_data.get("uuid") if isinstance(guild_data, dict) else None,
                    guild_data.get("name") if isinstance(guild_data, dict) else None,
                    guild_data.get("prefix") if isinstance(guild_data, dict) else None,
                    guild_data.get("rank") if isinstance(guild_data, dict) else None,
                    stats.get("playtime", 0),
                    stats.get("wars", 0),
                    stats.get("totalLevel", 0),
                    stats.get("mobsKilled", 0),
                    stats.get("chestsFound", 0),
                    dungeons_data.get("total", 0) if isinstance(dungeons_data, dict) else 0,
                    json.dumps(dungeons_data.get("list", {})) if isinstance(dungeons_data, dict) else "{}",
                    raids_data.get("total", 0) if isinstance(raids_data, dict) else 0,
                    json.dumps(raids_data.get("list", {})) if isinstance(raids_data, dict) else "{}",
                    stats.get("worldEvents", 0),
                    stats.get("lootRuns", 0),
                    stats.get("caves", 0),
                    stats.get("completedQuests", 0),
                    pvp_data.get("kills", 0) if isinstance(pvp_data, dict) else 0,
                    pvp_data.get("deaths", 0) if isinstance(pvp_data, dict) else 0,
                ),
            )

    if uuid_username_pairs:
        cursor.executemany(
            "INSERT OR REPLACE INTO uuid_username_map (uuid, username) VALUES (?, ?)",
            uuid_username_pairs,
        )

    guild_raid_count = 0
    if guild_members:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_raid_stats (
                username TEXT NOT NULL,
                uuid TEXT,
                total_graids INTEGER DEFAULT 0,
                canyon_colossus INTEGER DEFAULT 0,
                orphions_nexus INTEGER DEFAULT 0,
                grootslangs INTEGER DEFAULT 0,
                nameless_anomaly INTEGER DEFAULT 0
            )
            """
        )
        for member in guild_members:
            graids = extract_guild_raid_payload(member)
            graid_list = graids.get("list", {})
            cursor.execute(
                """
                INSERT INTO guild_raid_stats (username, uuid, total_graids, canyon_colossus, orphions_nexus, grootslangs, nameless_anomaly)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    member.get("username"),
                    member.get("uuid"),
                    graids.get("total", 0) if isinstance(graids, dict) else 0,
                    graid_list.get("The Canyon Colossus", 0),
                    graid_list.get("Orphion's Nexus of Light", 0),
                    graid_list.get("Nest of the Grootslangs", 0),
                    graid_list.get("The Nameless Anomaly", 0),
                ),
            )
        guild_raid_count = len(guild_members)

    return {
        "uuid_username_count": len(uuid_username_pairs),
        "guild_raid_count": guild_raid_count,
    }


def get_previous_api_db(api_tracking_folder: PathLike, current_db_path: PathLike):
    db_files = []
    folder = str(_as_path(api_tracking_folder))
    if os.path.isdir(folder):
        for day_name in os.listdir(folder):
            if not day_name.startswith("api_"):
                continue
            day_path = os.path.join(folder, day_name)
            if not os.path.isdir(day_path):
                continue
            for filename in os.listdir(day_path):
                if filename.endswith(".db"):
                    db_files.append(os.path.join(day_path, filename))
    db_files.sort(key=os.path.getmtime)

    current_abs = os.path.abspath(str(current_db_path))
    for db_path in reversed(db_files):
        if os.path.abspath(db_path) != current_abs:
            return db_path
    return None


def get_previous_day_latest_db(api_tracking_folder: PathLike, current_db_path: PathLike):
    current_folder = os.path.basename(os.path.dirname(os.path.abspath(str(current_db_path))))
    if not current_folder.startswith("api_"):
        return None

    try:
        current_date = datetime.strptime(current_folder[4:], "%d-%m-%Y").date()
    except ValueError:
        return None

    best_date = None
    best_path = None
    folder = str(_as_path(api_tracking_folder))

    for name in os.listdir(folder):
        if not name.startswith("api_"):
            continue
        day_path = os.path.join(folder, name)
        if not os.path.isdir(day_path):
            continue
        try:
            folder_date = datetime.strptime(name[4:], "%d-%m-%Y").date()
        except ValueError:
            continue
        if folder_date >= current_date:
            continue
        if best_date is not None and folder_date < best_date:
            continue

        db_files = sorted([f for f in os.listdir(day_path) if f.endswith(".db")])
        if db_files:
            best_date = folder_date
            best_path = os.path.join(day_path, db_files[-1])

    return best_path


def read_graid_totals_from_db(db_path):
    if not db_path:
        return {}
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='guild_raid_stats'")
        if not cur.fetchone():
            conn.close()
            return {}
        result = {}
        for row in cur.execute("SELECT LOWER(username), total_graids FROM guild_raid_stats").fetchall():
            result[row[0]] = row[1] or 0
        conn.close()
        return result
    except Exception:
        return {}


def read_prev_offsets_from_db(db_path):
    if not db_path:
        return {}
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='graid_fault_offsets'")
        if not cur.fetchone():
            conn.close()
            return {}
        result = {}
        for row in cur.execute(
            "SELECT LOWER(username), username, uuid, offset FROM graid_fault_offsets"
        ).fetchall():
            result[row[0]] = {"username": row[1], "uuid": row[2], "offset": row[3] or 0}
        conn.close()
        return result
    except Exception:
        return {}


def detect_graid_fault_deltas(
    prev_totals,
    curr_totals,
    min_affected_pct: float = GRAID_FAULT_MIN_AFFECTED_PCT,
    min_members: int = GRAID_FAULT_MIN_MEMBERS,
    min_median_delta: int = GRAID_FAULT_MIN_MEDIAN_DELTA,
    guild_wide_avg: int = GRAID_FAULT_GUILD_WIDE_AVG,
    log_prefix: str = "[GRAID_FAULT]",
):
    common = set(prev_totals.keys()) & set(curr_totals.keys())
    if len(common) < min_members:
        return {}

    positive_deltas = []
    for username_lower in common:
        prev_val = prev_totals[username_lower]
        delta = curr_totals[username_lower] - prev_val
        if delta > 0 and prev_val > 0:
            positive_deltas.append((username_lower, delta))

    if len(positive_deltas) < min_members:
        return {}

    affected_pct = len(positive_deltas) / len(common)
    median_delta = statistics.median([delta for _, delta in positive_deltas])
    total_delta = sum(delta for _, delta in positive_deltas)
    avg_delta = total_delta / len(common)

    individual_spike = (
        affected_pct >= min_affected_pct and median_delta >= min_median_delta
    )
    guild_wide_spike = affected_pct >= 0.6 and avg_delta >= guild_wide_avg
    extreme_jumps = sum(1 for _, delta in positive_deltas if delta >= 200) >= 3

    if individual_spike or guild_wide_spike or extreme_jumps:
        print(
            f"{log_prefix} Detected: {len(positive_deltas)}/{len(common)} affected "
            f"({affected_pct:.0%}), median={median_delta:.0f}, avg={avg_delta:.0f}"
        )
        return {username_lower: delta for username_lower, delta in positive_deltas}

    return {}


def detect_and_store_graid_fault_offsets(
    conn,
    current_db_path: PathLike,
    api_tracking_folder: PathLike,
    log_prefix: str = "[GRAID_FAULT]",
) -> None:
    cursor = conn.cursor()
    prev_db_path = get_previous_api_db(api_tracking_folder, current_db_path)
    prev_day_db_path = get_previous_day_latest_db(api_tracking_folder, current_db_path)

    prev_offsets = read_prev_offsets_from_db(prev_db_path)
    if not prev_offsets and prev_day_db_path:
        prev_offsets = read_prev_offsets_from_db(prev_day_db_path)

    curr_graids = {}
    try:
        for row in cursor.execute(
            "SELECT LOWER(username), username, uuid, total_graids FROM guild_raid_stats"
        ).fetchall():
            curr_graids[row[0]] = {"username": row[1], "uuid": row[2], "total": row[3] or 0}
    except Exception:
        return

    curr_totals = {username_lower: info["total"] for username_lower, info in curr_graids.items()}
    new_fault_deltas = {}

    if prev_db_path:
        prev_totals = read_graid_totals_from_db(prev_db_path)
        if prev_totals:
            new_fault_deltas = detect_graid_fault_deltas(prev_totals, curr_totals)

    if not new_fault_deltas and prev_day_db_path and prev_db_path:
        prev_folder = os.path.basename(os.path.dirname(os.path.abspath(str(prev_db_path))))
        curr_folder = os.path.basename(os.path.dirname(os.path.abspath(str(current_db_path))))
        if prev_folder != curr_folder:
            prev_day_totals = read_graid_totals_from_db(prev_day_db_path)
            if prev_day_totals:
                new_fault_deltas = detect_graid_fault_deltas(prev_day_totals, curr_totals)

    merged = {}
    for username_lower, data in prev_offsets.items():
        merged[username_lower] = dict(data)
    for username_lower, delta in new_fault_deltas.items():
        if username_lower in merged:
            merged[username_lower]["offset"] += delta
        else:
            info = curr_graids.get(username_lower, {})
            merged[username_lower] = {
                "username": info.get("username", username_lower),
                "uuid": info.get("uuid"),
                "offset": delta,
            }

    stale_removed = 0
    cleaned = {}
    for username_lower, data in merged.items():
        current_total = int((curr_graids.get(username_lower) or {}).get("total", 0) or 0)
        offset = int(data.get("offset", 0) or 0)
        if offset <= 0:
            continue
        if current_total > 0 and offset >= current_total:
            stale_removed += 1
            continue
        cleaned[username_lower] = {
            "username": data.get("username") or (curr_graids.get(username_lower) or {}).get("username", username_lower),
            "uuid": data.get("uuid") or (curr_graids.get(username_lower) or {}).get("uuid"),
            "offset": offset,
        }
    merged = cleaned

    if stale_removed:
        print(f"{log_prefix} Removed {stale_removed} stale carried offsets.")

    if merged:
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS graid_fault_offsets (username TEXT NOT NULL, uuid TEXT, offset INTEGER DEFAULT 0)"
        )
        for data in merged.values():
            cursor.execute(
                "INSERT INTO graid_fault_offsets (username, uuid, offset) VALUES (?, ?, ?)",
                (data["username"], data["uuid"], data["offset"]),
            )
        if new_fault_deltas:
            print(f"{log_prefix} Stored offsets for {len(merged)} players (new faults: {len(new_fault_deltas)})")
        else:
            print(f"{log_prefix} Carried forward offsets for {len(merged)} players")


def save_additional_data(
    conn,
    recruited_db_path: PathLike,
    badge_roles: Dict = BADGE_ROLES,
    log_prefix: str = "[API]",
) -> None:
    recruited_db = _as_path(recruited_db_path)

    def _parse_threshold(label: str) -> int:
        try:
            value = label.strip().lower()
            multiplier = 1
            if value.endswith("k"):
                multiplier = 1000
                value = value[:-1]
            return int(float(value) * multiplier)
        except Exception:
            return 0

    def _get_badge_for_value(category: str, value: int):
        thresholds = badge_roles.get(category, {})
        best_label = None
        best_role = None
        best_threshold = -1
        for label, role_id in thresholds.items():
            threshold_value = _parse_threshold(label)
            if value >= threshold_value and threshold_value > best_threshold:
                best_threshold = threshold_value
                best_label = label
                best_role = role_id
        return best_label, best_role

    try:
        cursor = conn.cursor()
        current_timestamp = datetime.now(timezone.utc).isoformat()

        if recruited_db.exists():
            recruited_conn = sqlite3.connect(str(recruited_db))
            recruited_cursor = recruited_conn.cursor()

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS recruited (
                    recruiter TEXT NOT NULL,
                    recruited TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )
                """
            )
            recruited_cursor.execute("SELECT recruiter, recruited, timestamp FROM recruited")
            recruited_data = recruited_cursor.fetchall()
            cursor.executemany(
                "INSERT INTO recruited (recruiter, recruited, timestamp) VALUES (?, ?, ?)",
                recruited_data,
            )

            recruited_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='quest_progress'")
            if recruited_cursor.fetchone():
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS quest_progress (
                        player TEXT NOT NULL,
                        points INTEGER NOT NULL,
                        last_updated TEXT,
                        snapshot_timestamp TEXT NOT NULL
                    )
                    """
                )
                recruited_cursor.execute("SELECT player, points, last_updated FROM quest_progress")
                quest_data = recruited_cursor.fetchall()
                quest_data_with_timestamp = [
                    (player, points, last_updated, current_timestamp)
                    for player, points, last_updated in quest_data
                ]
                cursor.executemany(
                    "INSERT INTO quest_progress (player, points, last_updated, snapshot_timestamp) VALUES (?, ?, ?, ?)",
                    quest_data_with_timestamp,
                )

            recruited_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='event_progress'")
            if recruited_cursor.fetchone():
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS event_progress (
                        player TEXT NOT NULL,
                        points INTEGER NOT NULL,
                        last_updated TEXT,
                        snapshot_timestamp TEXT NOT NULL
                    )
                    """
                )
                recruited_cursor.execute("SELECT player, points, last_updated FROM event_progress")
                event_data = recruited_cursor.fetchall()
                event_data_with_timestamp = [
                    (player, points, last_updated, current_timestamp)
                    for player, points, last_updated in event_data
                ]
                cursor.executemany(
                    "INSERT INTO event_progress (player, points, last_updated, snapshot_timestamp) VALUES (?, ?, ?, ?)",
                    event_data_with_timestamp,
                )

            recruited_conn.close()
        else:
            print(f"{log_prefix} recruited_data.db not found, skipping...")

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS badges (
                player TEXT NOT NULL,
                category TEXT NOT NULL,
                tier TEXT NOT NULL,
                role_id INTEGER,
                value INTEGER NOT NULL,
                snapshot_timestamp TEXT NOT NULL
            )
            """
        )

        badge_rows = []

        try:
            cursor.execute("SELECT username, wars FROM player_stats")
            for username, wars in cursor.fetchall():
                wars = wars or 0
                tier, role_id = _get_badge_for_value("War Badges", wars)
                if tier is not None:
                    badge_rows.append((username, "War Badges", tier, role_id, wars, current_timestamp))
        except Exception as e:
            print(f"{log_prefix} Failed to compute war badges: {e}")

        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='quest_progress'")
            if cursor.fetchone():
                cursor.execute("SELECT player, points FROM quest_progress")
                for player, points in cursor.fetchall():
                    points = points or 0
                    tier, role_id = _get_badge_for_value("Quest Badges", points)
                    if tier is not None:
                        badge_rows.append((player, "Quest Badges", tier, role_id, points, current_timestamp))
        except Exception as e:
            print(f"{log_prefix} Failed to compute quest badges: {e}")

        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='recruited'")
            if cursor.fetchone():
                cursor.execute("SELECT recruiter, COUNT(*) FROM recruited GROUP BY recruiter")
                for recruiter, count in cursor.fetchall():
                    count = count or 0
                    tier, role_id = _get_badge_for_value("Recruitment Badges", count)
                    if tier is not None:
                        badge_rows.append((recruiter, "Recruitment Badges", tier, role_id, count, current_timestamp))
        except Exception as e:
            print(f"{log_prefix} Failed to compute recruitment badges: {e}")

        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='guild_raid_stats'")
            if cursor.fetchone():
                cursor.execute("SELECT username, total_graids FROM guild_raid_stats")
                for username, total_graids in cursor.fetchall():
                    total_graids = total_graids or 0
                    tier, role_id = _get_badge_for_value("Raid Badges", total_graids)
                    if tier is not None:
                        badge_rows.append((username, "Raid Badges", tier, role_id, total_graids, current_timestamp))
        except Exception as e:
            print(f"{log_prefix} Failed to compute raid badges: {e}")

        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='event_progress'")
            if cursor.fetchone():
                cursor.execute("SELECT player, points FROM event_progress")
                for player, points in cursor.fetchall():
                    points = points or 0
                    tier, role_id = _get_badge_for_value("Event Badges", points)
                    if tier is not None:
                        badge_rows.append((player, "Event Badges", tier, role_id, points, current_timestamp))
        except Exception as e:
            print(f"{log_prefix} Failed to compute event badges: {e}")

        if badge_rows:
            cursor.executemany(
                "INSERT INTO badges (player, category, tier, role_id, value, snapshot_timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                badge_rows,
            )
        else:
            print(f"{log_prefix} No badge records computed for this snapshot")

        conn.commit()
    except Exception as e:
        print(f"{log_prefix} Error saving additional data: {e}")
        import traceback
        traceback.print_exc()
