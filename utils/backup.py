"""
Backup utility for the bot's persistent state.

Creates timestamped snapshots of the SQLite databases under ``databases/`` and
the JSON state files under ``data/``.

SQLite databases are copied through SQLite's online backup API rather than a
plain file copy. ``shop.db`` runs in WAL mode, so copying just the ``.db`` file
can capture a torn state and silently drop everything still sitting in the
``-wal`` sidecar. The online backup API produces a consistent snapshot even
while the bot and the trackers are writing.

Each run is written to a temporary directory and only renamed into place once
every file has been copied, so an interrupted run can never leave a
half-written backup that looks valid. A lock file stops two runs overlapping.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
import zipfile
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

from utils.paths import DATA_DIR, DB_DIR, PROJECT_ROOT

BACKUP_ROOT = PROJECT_ROOT.parent / "backups"
EXPORTS_DIR = PROJECT_ROOT / "exports"

SNAPSHOT_SUBDIRS = ("api_tracking", "playtime_tracking")

DEFAULT_KEEP = 14
STAMP_FORMAT = "%Y-%m-%d_%H%M%S"

LOCK_FILENAME = ".lock"
LOCK_STALE_SECONDS = 6 * 3600
SQLITE_TIMEOUT_SECONDS = 30

LogFunc = Callable[[str], None]


class BackupError(RuntimeError):
    """Raised when a backup cannot be started or completed."""


class BackupInProgress(BackupError):
    """Raised when another backup run already holds the lock."""


@dataclass
class BackupResult:
    """Outcome of a backup run."""

    ok: bool
    path: Optional[Path] = None
    dry_run: bool = False
    databases: int = 0
    data_files: int = 0
    snapshot_files: int = 0
    export_files: int = 0
    bytes_written: int = 0
    pruned: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _parse_stamp(name: str) -> Optional[datetime]:
    """Parse a backup run name back into a UTC timestamp, or None."""
    try:
        return datetime.strptime(name, STAMP_FORMAT)
    except ValueError:
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quick_check(path: Path) -> str:
    """Run SQLite's quick integrity check on a copied database."""
    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=SQLITE_TIMEOUT_SECONDS)) as conn:
            row = conn.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as exc:
        return f"error: {exc}"
    if not row:
        return "unknown"
    return "ok" if row[0] == "ok" else str(row[0])


def _git_commit() -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _copy_sqlite(src: Path, dest: Path) -> str:
    """Copy one SQLite database consistently.

    Returns the method used: ``"backup-api"`` for a consistent online backup,
    ``"file-copy"`` for the raw-copy fallback used when the database cannot be
    opened at all.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(str(src), timeout=SQLITE_TIMEOUT_SECONDS)) as source:
            with closing(sqlite3.connect(str(dest))) as target:
                source.backup(target)
        return "backup-api"
    except sqlite3.Error:
        for sidecar in ("", "-wal", "-shm"):
            candidate = Path(str(src) + sidecar)
            if candidate.exists():
                shutil.copy2(candidate, Path(str(dest) + sidecar))
        return "file-copy"


def _iter_files(root: Path):
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def _dir_stats(root: Path) -> tuple:
    """Return (file_count, total_bytes) for a directory tree."""
    count = 0
    total = 0
    for path in _iter_files(root):
        count += 1
        total += path.stat().st_size
    return count, total


@contextmanager
def _backup_lock(root: Path):
    """Hold an exclusive lock for the duration of a backup run.

    Uses ``O_CREAT | O_EXCL`` so the check and the create are one atomic step.
    A lock left behind by a crashed run is reclaimed once it is older than
    ``LOCK_STALE_SECONDS``.
    """
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / LOCK_FILENAME
    fd = None

    for attempt in (1, 2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if attempt == 2:
                raise BackupInProgress(
                    f"Another backup is already running (lock: {lock_path})"
                )
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0
            if age < LOCK_STALE_SECONDS:
                raise BackupInProgress(
                    f"Another backup is already running (lock: {lock_path})"
                )
            try:
                lock_path.unlink()
            except OSError:
                raise BackupInProgress(
                    f"Could not reclaim stale lock: {lock_path}"
                )

    try:
        os.write(fd, f"pid={os.getpid()}\n".encode("utf-8"))
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            lock_path.unlink()
        except OSError:
            pass


def _list_runs(root: Path) -> List[Path]:
    """Return existing backup runs, oldest first.

    Only entries whose name is one of our own timestamps (optionally with a
    ``.zip`` suffix) are considered, so unrelated files in the backup root are
    never touched by pruning.
    """
    if not root.exists():
        return []

    runs = []
    for entry in root.iterdir():
        if entry.is_dir():
            stamp_name = entry.name
        elif entry.is_file() and entry.suffix == ".zip":
            stamp_name = entry.stem
        else:
            continue
        if _parse_stamp(stamp_name) is not None:
            runs.append(entry)

    return sorted(runs, key=lambda p: p.stem if p.is_file() else p.name)


def _prune(root: Path, keep: int, log: LogFunc) -> List[str]:
    if keep <= 0:
        return []

    runs = _list_runs(root)
    stale = runs[:-keep] if len(runs) > keep else []
    removed = []

    for path in stale:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append(path.name)
            log(f"Pruned old backup: {path.name}")
        except OSError as exc:
            log(f"Could not prune {path.name}: {exc}")

    return removed


def _resolve_dest(dest_root) -> Path:
    if dest_root is None:
        dest_root = os.getenv("BACKUP_DEST") or BACKUP_ROOT
    return Path(dest_root).expanduser()


def _collect_databases() -> List[Path]:
    if not DB_DIR.exists():
        return []
    return sorted(p for p in DB_DIR.glob("*.db") if p.is_file())


def _collect_data_files() -> List[Path]:
    return list(_iter_files(DATA_DIR))


def _summarise(result: BackupResult, log: LogFunc) -> None:
    log(
        f"{result.databases} database(s), {result.data_files} data file(s)"
        + (f", {result.snapshot_files} snapshot file(s)" if result.snapshot_files else "")
        + (f", {result.export_files} export file(s)" if result.export_files else "")
    )
    log(f"Total size: {result.bytes_written / (1024 * 1024):.2f} MB")


def create_backup(
    dest_root=None,
    keep: int = DEFAULT_KEEP,
    include_snapshots: bool = False,
    include_exports: bool = False,
    zip_output: bool = False,
    dry_run: bool = False,
    log: LogFunc = print,
) -> BackupResult:
    """Create one timestamped backup of the bot's databases and data files.

    Args:
        dest_root: Directory to hold backup runs. Defaults to ``BACKUP_DEST``
            or ``backups/`` at the repository root.
        keep: Number of runs to retain, newest first. ``0`` disables pruning.
        include_snapshots: Also copy the per-day snapshot folders under
            ``databases/`` (large; excluded by default).
        include_exports: Also copy the channel-history exports directory
            (large; excluded by default).
        zip_output: Write each run as a single ``.zip`` instead of a directory.
        dry_run: Report what would be copied without writing anything.
        log: Callable used for progress messages.

    Raises:
        BackupError: if the run could not be started or completed.
        BackupInProgress: if another run already holds the lock.
    """
    dest = _resolve_dest(dest_root)
    stamp = datetime.now(timezone.utc).strftime(STAMP_FORMAT)
    result = BackupResult(ok=True, dry_run=dry_run)

    databases = _collect_databases()
    data_files = _collect_data_files()
    snapshots = [DB_DIR / name for name in SNAPSHOT_SUBDIRS] if include_snapshots else []

    if dry_run:
        result.path = dest / f"{stamp}.zip" if zip_output else dest / stamp
        result.databases = len(databases)
        result.data_files = len(data_files)
        result.bytes_written = sum(p.stat().st_size for p in databases + data_files)
        for folder in snapshots:
            count, size = _dir_stats(folder)
            result.snapshot_files += count
            result.bytes_written += size
        if include_exports:
            count, size = _dir_stats(EXPORTS_DIR)
            result.export_files = count
            result.bytes_written += size

        log(f"[dry-run] Destination: {dest}")
        log(f"[dry-run] Would copy {len(databases)} database(s):")
        for db in databases:
            log(f"[dry-run]   {db.name} ({db.stat().st_size / 1024:.1f} KB)")
        _summarise(result, log)
        if include_snapshots:
            log("[dry-run] Snapshot folders included")
        if include_exports:
            log("[dry-run] Exports included")
        log("[dry-run] Nothing written.")
        return result

    with _backup_lock(dest):
        work_dir = dest / f".tmp-{stamp}"
        if work_dir.exists():
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True)

        manifest = {
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "stamp": stamp,
            "git_commit": _git_commit(),
            "include_snapshots": include_snapshots,
            "include_exports": include_exports,
            "databases": [],
            "data_files": [],
            "snapshots": {"included": include_snapshots, "files": 0, "bytes": 0},
            "exports": {"included": include_exports, "files": 0, "bytes": 0},
            "totals": {},
            "warnings": [],
            "ok": True,
        }

        try:
            db_dir = work_dir / "databases"
            for src in databases:
                try:
                    method = _copy_sqlite(src, db_dir / src.name)
                except OSError as exc:
                    warning = f"{src.name}: copy failed ({exc})"
                    result.warnings.append(warning)
                    manifest["warnings"].append(warning)
                    log(f"Warning: {warning}")
                    continue

                dest_file = db_dir / src.name
                integrity = _quick_check(dest_file)
                if method == "file-copy":
                    warning = f"{src.name}: could not be opened, raw file copy used"
                    result.warnings.append(warning)
                    manifest["warnings"].append(warning)
                    log(f"Warning: {warning}")
                if integrity != "ok":
                    warning = f"{src.name}: integrity check returned {integrity!r}"
                    result.warnings.append(warning)
                    manifest["warnings"].append(warning)
                    result.ok = False
                    log(f"Warning: {warning}")

                manifest["databases"].append(
                    {
                        "name": src.name,
                        "bytes": dest_file.stat().st_size,
                        "sha256": _sha256(dest_file),
                        "copy_method": method,
                        "integrity": integrity,
                    }
                )
                result.databases += 1
                result.bytes_written += dest_file.stat().st_size
                log(f"Backed up database: {src.name} ({method}, integrity={integrity})")

            for src in data_files:
                rel = src.relative_to(DATA_DIR)
                dest_file = work_dir / "data" / rel
                try:
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest_file)
                except OSError as exc:
                    warning = f"data/{rel.as_posix()}: copy failed ({exc})"
                    result.warnings.append(warning)
                    manifest["warnings"].append(warning)
                    log(f"Warning: {warning}")
                    continue

                manifest["data_files"].append(
                    {
                        "path": rel.as_posix(),
                        "bytes": dest_file.stat().st_size,
                        "sha256": _sha256(dest_file),
                    }
                )
                result.data_files += 1
                result.bytes_written += dest_file.stat().st_size

            log(f"Backed up {result.data_files} data file(s) from data/")

            for folder in snapshots:
                if not folder.exists():
                    continue
                try:
                    shutil.copytree(folder, db_dir / folder.name, dirs_exist_ok=True)
                except OSError as exc:
                    warning = f"{folder.name}/: copy failed ({exc})"
                    result.warnings.append(warning)
                    manifest["warnings"].append(warning)
                    log(f"Warning: {warning}")
                    continue
                count, size = _dir_stats(db_dir / folder.name)
                result.snapshot_files += count
                result.bytes_written += size
                manifest["snapshots"]["files"] += count
                manifest["snapshots"]["bytes"] += size
                log(f"Backed up snapshot folder: databases/{folder.name} ({count} files)")

            if include_exports and EXPORTS_DIR.exists():
                try:
                    shutil.copytree(EXPORTS_DIR, work_dir / "exports", dirs_exist_ok=True)
                    count, size = _dir_stats(work_dir / "exports")
                    result.export_files = count
                    result.bytes_written += size
                    manifest["exports"]["files"] = count
                    manifest["exports"]["bytes"] = size
                    log(f"Backed up exports/ ({count} files)")
                except OSError as exc:
                    warning = f"exports/: copy failed ({exc})"
                    result.warnings.append(warning)
                    manifest["warnings"].append(warning)
                    log(f"Warning: {warning}")

            manifest["totals"] = {
                "databases": result.databases,
                "data_files": result.data_files,
                "snapshot_files": result.snapshot_files,
                "export_files": result.export_files,
                "bytes": result.bytes_written,
            }
            manifest["ok"] = result.ok
            with open(work_dir / "manifest.json", "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, indent=2, ensure_ascii=False)

            if zip_output:
                tmp_zip = dest / f".tmp-{stamp}.zip"
                final_zip = dest / f"{stamp}.zip"
                with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as archive:
                    for path in sorted(work_dir.rglob("*")):
                        if path.is_file():
                            archive.write(path, path.relative_to(work_dir))
                shutil.rmtree(work_dir)
                if final_zip.exists():
                    final_zip.unlink()
                tmp_zip.rename(final_zip)
                result.path = final_zip
            else:
                final_dir = dest / stamp
                if final_dir.exists():
                    shutil.rmtree(final_dir)
                work_dir.rename(final_dir)
                result.path = final_dir

            log(f"Backup written: {result.path}")
            _summarise(result, log)

        except Exception:
            # Never leave a half-written run behind to be mistaken for a good one
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)
            raise

        result.pruned = _prune(dest, keep, log)

    return result
