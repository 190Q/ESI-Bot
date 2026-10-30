import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Union


PathLike = Union[str, Path]

COVERAGE_DB_NAME = "coverage.db"
PARTIAL_SNAPSHOT_SUFFIX = ".partial"
STALE_PARTIAL_SECONDS = 60 * 60
SQLITE_TIMEOUT_SECONDS = 5
SECONDS_PER_DAY = 24 * 60 * 60
SLOT_SECONDS = 30 * 60
MATCH_TOLERANCE_SECONDS = 10 * 60
EXPECTED_SNAPSHOT_TIMES_SECONDS = [
    (index * SLOT_SECONDS) for index in range(48)
] + [((23 * 60) + 55) * 60]


def _as_path(path_value: PathLike) -> Path:
    return path_value if isinstance(path_value, Path) else Path(path_value)


def is_coverage_db(path_value: PathLike) -> bool:
    return _as_path(path_value).name.lower() == COVERAGE_DB_NAME


def list_snapshot_db_files(day_folder: PathLike) -> List[Path]:
    """Return snapshot .db files in a day folder, excluding coverage.db.

    Empty files are left out: a zero byte file is a valid SQLite database with
    no schema at all, so anything reading it fails with "no such table".
    """
    folder = _as_path(day_folder)
    if not folder.exists():
        return []
    return sorted(
        (
            path
            for path in folder.glob("*.db")
            if path.is_file()
            and not is_coverage_db(path)
            and path.stat().st_size > 0
        ),
        key=lambda path: path.stat().st_mtime,
    )


def delete_empty_snapshot_db_files(day_folder: PathLike) -> int:
    """Delete zero byte snapshot files in a day folder, returning how many went."""
    folder = _as_path(day_folder)
    if not folder.exists():
        return 0

    removed = 0
    for path in folder.glob("*.db"):
        if not path.is_file() or is_coverage_db(path):
            continue
        try:
            if path.stat().st_size == 0:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def is_empty_snapshot_db(path_value: PathLike) -> bool:
    """True for a zero byte file, which is what an interrupted write leaves."""
    try:
        return _as_path(path_value).stat().st_size == 0
    except OSError:
        return True


def is_usable_snapshot_db(path_value: PathLike) -> bool:
    """True when the file is a snapshot database holding at least one table.

    A write killed part way through can leave a zero byte file, or one with no
    tables at all, behind under a name the commands read. Both answer every
    query with "no such table", so neither may be picked as a snapshot.
    """
    path = _as_path(path_value)
    if is_empty_snapshot_db(path):
        return False

    try:
        conn = sqlite3.connect(str(path), timeout=SQLITE_TIMEOUT_SECONDS)
        try:
            found = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return False

    return found is not None


def newest_usable_snapshot_dbs(db_files: Iterable[PathLike], limit: Optional[int] = 1) -> List[Path]:
    """Return the newest usable snapshot files, newest first.

    Unusable files are skipped, so a snapshot broken by an interrupted write
    never wins over an older good one. Pass limit=None for every usable file.
    """
    candidates = sorted(
        (
            path
            for path in (_as_path(value) for value in db_files)
            if not is_empty_snapshot_db(path)
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    usable = []
    for path in candidates:
        if not is_usable_snapshot_db(path):
            continue
        usable.append(path)
        if limit is not None and len(usable) >= limit:
            break

    return usable


def delete_stale_partial_snapshot_files(
    day_folder: PathLike,
    max_age_seconds: int = STALE_PARTIAL_SECONDS,
) -> int:
    """Delete abandoned ``<snapshot>.db.partial`` files, returning how many went.

    Only files older than *max_age_seconds* are removed, so a write that is
    still in flight is never touched.
    """
    folder = _as_path(day_folder)
    if not folder.exists():
        return 0

    cutoff = time.time() - max_age_seconds
    removed = 0
    for path in folder.glob(f"*{PARTIAL_SNAPSHOT_SUFFIX}"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def coverage_db_path_for_day(day_folder: PathLike) -> Path:
    return _as_path(day_folder) / COVERAGE_DB_NAME

def _parse_file_seconds(db_file: Path) -> Optional[int]:
    try:
        timestamp_token = db_file.stem.rsplit("_", 1)[-1]
        if len(timestamp_token) != 6 or not timestamp_token.isdigit():
            return None
        hour = int(timestamp_token[0:2])
        minute = int(timestamp_token[2:4])
        second = int(timestamp_token[4:6])
        if hour > 23 or minute > 59 or second > 59:
            return None
        return hour * 3600 + minute * 60 + second
    except Exception:
        return None

def _format_expected_time(seconds_since_midnight: int) -> str:
    normalized = int(seconds_since_midnight) % SECONDS_PER_DAY
    hour = normalized // 3600
    minute = (normalized % 3600) // 60
    return f"{hour:02d}:{minute:02d}"

def _is_expected_time_present(expected_seconds: int, observed_seconds: List[int]) -> bool:
    for observed in observed_seconds:
        delta = abs(observed - expected_seconds)
        wrapped_delta = min(delta, SECONDS_PER_DAY - delta)
        if wrapped_delta <= MATCH_TOLERANCE_SECONDS:
            return True
    return False

def calculate_daily_coverage(db_files: Iterable[PathLike]) -> Dict[str, object]:
    file_paths = [
        path
        for path in (_as_path(value) for value in db_files)
        if not is_coverage_db(path)
    ]
    observed_seconds = [
        parsed
        for parsed in (_parse_file_seconds(path) for path in file_paths)
        if parsed is not None
    ]

    missing_times = [
        _format_expected_time(expected_seconds)
        for expected_seconds in EXPECTED_SNAPSHOT_TIMES_SECONDS
        if not _is_expected_time_present(expected_seconds, observed_seconds)
    ]
    expected_count = len(EXPECTED_SNAPSHOT_TIMES_SECONDS)
    covered_count = expected_count - len(missing_times)

    return {
        "observed_db_files": len(file_paths),
        "expected_db_files": expected_count,
        "covered_expected_files": covered_count,
        "missing_db_files": len(missing_times),
        "missing_times": missing_times,
        "coverage_ratio": (covered_count / expected_count) if expected_count else 1.0,
    }

def record_daily_coverage(
    coverage_db_path: PathLike,
    day_string: str,
    folder_name: str,
    db_files: Iterable[PathLike],
    log_prefix: str = "[COVERAGE]",
) -> Dict[str, object]:
    coverage_data = calculate_daily_coverage(db_files)
    coverage_data["recorded"] = False
    coverage_path = _as_path(coverage_db_path)
    coverage_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        conn = sqlite3.connect(str(coverage_path))
        cursor = conn.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_coverage (
                day TEXT PRIMARY KEY,
                folder_name TEXT NOT NULL,
                observed_db_files INTEGER NOT NULL,
                expected_db_files INTEGER NOT NULL,
                covered_expected_files INTEGER NOT NULL,
                missing_db_files INTEGER NOT NULL,
                coverage_ratio REAL NOT NULL,
                recorded_at_utc TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_coverage_missing_times (
                day TEXT NOT NULL,
                missing_time TEXT NOT NULL,
                PRIMARY KEY (day, missing_time),
                FOREIGN KEY(day) REFERENCES daily_coverage(day) ON DELETE CASCADE
            )
            """
        )

        cursor.execute(
            """
            INSERT OR IGNORE INTO daily_coverage (
                day,
                folder_name,
                observed_db_files,
                expected_db_files,
                covered_expected_files,
                missing_db_files,
                coverage_ratio,
                recorded_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                day_string,
                folder_name,
                int(coverage_data["observed_db_files"]),
                int(coverage_data["expected_db_files"]),
                int(coverage_data["covered_expected_files"]),
                int(coverage_data["missing_db_files"]),
                float(coverage_data["coverage_ratio"]),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

        if cursor.rowcount > 0:
            missing_times = coverage_data.get("missing_times", [])
            if missing_times:
                cursor.executemany(
                    """
                    INSERT OR IGNORE INTO daily_coverage_missing_times (day, missing_time)
                    VALUES (?, ?)
                    """,
                    [(day_string, missing_time) for missing_time in missing_times],
                )
            conn.commit()
            coverage_data["recorded"] = True
            print(
                f"{log_prefix} Recorded coverage for {folder_name}: "
                f"{coverage_data['observed_db_files']}/{coverage_data['expected_db_files']} "
                f"(missing {coverage_data['missing_db_files']})"
            )

        conn.close()
    except Exception as e:
        print(f"{log_prefix} Failed to record coverage for {folder_name}: {e}")

    return coverage_data
