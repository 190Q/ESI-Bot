import json
import os
import platform
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from utils.paths import DATA_DIR

BOT_STATUS_PATH = DATA_DIR / "bot_status.json"
TRACKER_STATUS_PATH = DATA_DIR / "tracker_status.json"

# How often each process republishes its status file.
HEARTBEAT_INTERVAL_SECONDS = 30

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_git_commit = None
_git_commit_resolved = False


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def resolve_git_commit():
    """The checked-out commit sha, or None when it cannot be read.

    Resolved once per process by reading .git directly, so no git binary is
    needed and no subprocess is spawned.
    """
    global _git_commit, _git_commit_resolved
    if _git_commit_resolved:
        return _git_commit
    _git_commit_resolved = True

    git_dir = _PROJECT_ROOT / ".git"
    try:
        if git_dir.is_file():
            marker = git_dir.read_text(encoding="utf-8").strip()
            if marker.startswith("gitdir:"):
                target = Path(marker.split(":", 1)[1].strip())
                if not target.is_absolute():
                    target = (_PROJECT_ROOT / target).resolve()
                git_dir = target
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None

    if not head.startswith("ref:"):
        _git_commit = head[:40] or None
        return _git_commit

    ref = head.split(":", 1)[1].strip()
    try:
        _git_commit = (git_dir / ref).read_text(encoding="utf-8").strip()[:40] or None
        return _git_commit
    except OSError:
        pass
    try:
        for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                _git_commit = parts[0][:40]
                return _git_commit
    except OSError:
        pass
    return None


def atomic_write_json(path, payload) -> bool:
    """Write JSON via a temp file plus rename, so readers never see a partial file."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except (OSError, TypeError, ValueError) as exc:
        print(f"[STATUS] Could not write {path}: {exc}")
        return False


def write_status(path, service, started_at, **fields) -> dict:
    """Publish one heartbeat, returning the payload that was written."""
    now = datetime.now(timezone.utc)
    try:
        uptime = int((now - started_at).total_seconds())
    except (TypeError, AttributeError):
        uptime = None

    payload = {
        "service": service,
        "pid": os.getpid(),
        "started_at": started_at.isoformat() if hasattr(started_at, "isoformat") else started_at,
        "heartbeat_at": now.isoformat(),
        "uptime_seconds": uptime,
        "git_commit": resolve_git_commit(),
        "python_version": platform.python_version(),
    }
    payload.update(fields)
    atomic_write_json(path, payload)
    return payload


def staleness_window(interval: int) -> float:
    """How long a tracker may go without a run before it counts as stale."""
    return max(interval * 20, interval + 120)


def tracker_remaining_seconds(last_run_ts, interval: int, now_ts=None):
    """Seconds until this tracker's next run, or None once it has gone quiet."""
    if last_run_ts is None:
        return None
    now_ts = time.time() if now_ts is None else now_ts
    elapsed = max(0.0, now_ts - last_run_ts)
    if elapsed > staleness_window(interval):
        return None
    remaining = int(interval - (elapsed % interval))
    if remaining <= 0 or remaining > interval:
        remaining = interval
    return remaining
