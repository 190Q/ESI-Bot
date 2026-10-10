#!/usr/bin/env python3
"""
Back up the bot's databases and data files.

Thin command-line wrapper around ``utils.backup.create_backup``. See
``README.md`` ("Backups") for what is covered, retention, and how to restore.

Examples:
    python scripts/backup.py
    python scripts/backup.py --dest /mnt/backups/esi-bot --keep 30
    python scripts/backup.py --dry-run
    python scripts/backup.py --include-snapshots --zip

Exit codes:
    0  backup completed and every database passed its integrity check
    1  the backup could not be run (for example, another run holds the lock)
    2  the backup completed but something was skipped or failed a check
"""

import argparse
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils.backup import (
    DEFAULT_KEEP,
    BackupInProgress,
    create_backup,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Back up the bot's SQLite databases and data/ state files.",
    )
    parser.add_argument(
        "--dest",
        default=None,
        help="Directory to hold backup runs (default: $BACKUP_DEST, then backups/).",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP,
        help=f"Number of runs to retain, newest first (default: {DEFAULT_KEEP}; 0 disables pruning).",
    )
    parser.add_argument(
        "--include-snapshots",
        action="store_true",
        help="Also copy databases/api_tracking/ and databases/playtime_tracking/ (large).",
    )
    parser.add_argument(
        "--include-exports",
        action="store_true",
        help="Also copy the exports/ channel-history directory (large).",
    )
    parser.add_argument(
        "--zip",
        dest="zip_output",
        action="store_true",
        help="Write the run as a single .zip instead of a directory.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be copied without writing anything.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only print warnings and the final status line.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    def log(message: str) -> None:
        if not args.quiet:
            print(message)

    try:
        result = create_backup(
            dest_root=args.dest,
            keep=args.keep,
            include_snapshots=args.include_snapshots,
            include_exports=args.include_exports,
            zip_output=args.zip_output,
            dry_run=args.dry_run,
            log=log,
        )
    except BackupInProgress as exc:
        print(f"[ERROR] {exc}")
        return 1
    except Exception as exc:
        print(f"[ERROR] Backup failed: {exc}")
        traceback.print_exc()
        return 1

    if result.warnings:
        print(f"[WARN] {len(result.warnings)} warning(s):")
        for warning in result.warnings:
            print(f"       - {warning}")

    if result.dry_run:
        print(f"[OK] Dry run complete, nothing written (would use {result.path}).")
        return 0

    if result.ok:
        print(f"[OK] Backup complete: {result.path}")
        return 0

    print(f"[WARN] Backup finished with problems: {result.path}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
