#!/usr/bin/env python3
"""List every current guild member who has completed at least one quest.

This uses the bot's own quest system (the ``/quest_points`` command), whose
state lives in ``databases/recruited_data.db`` -> ``quest_progress``. A member
counts as having "completed at least one quest" when their quest points are
``>= 1`` (adjust with ``--min-points``).

"Currently in the guild" comes from the live Wynncraft guild endpoint
(https://api.wynncraft.com/v3/guild/prefix/<PREFIX>), which returns the
authoritative current roster. If the API is unreachable, the script falls back
to the newest local API snapshot under ``databases/api_tracking/``.

``quest_progress.player`` is stored as a player UUID for most rows, but some
legacy rows still hold a username, so rows are matched against members by
UUID *or* username (case-insensitive).

Usage:
  python scripts/list_guild_questers.py
  python scripts/list_guild_questers.py --detailed
  python scripts/list_guild_questers.py --format csv --output questers.csv
  python scripts/list_guild_questers.py --source db       # offline roster
  python scripts/list_guild_questers.py --min-points 10
"""

from __future__ import annotations

import argparse
import csv
import glob
import io
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_DIR = PROJECT_ROOT / "databases"
API_TRACKING_DIR = DB_DIR / "api_tracking"
QUEST_PROGRESS_DB = DB_DIR / "recruited_data.db"

DEFAULT_GUILD_PREFIX = "ESI"
DEFAULT_BASE_URL = "https://api.wynncraft.com/v3"

# Snapshot files are named <PREFIX>_<dd-mm-yyyy>_<HHMMSS>.db (e.g. ESI_04-06-2026_152421.db).
# The explicit pattern keeps stray databases (e.g. esi_points.db) from being picked up.
SNAPSHOT_NAME_RE = re.compile(r"^[A-Za-z0-9]+_\d{2}-\d{2}-\d{4}_\d{6}\.db$")

# Quest point thresholds -> badge name, mirroring commands/tracking/quest_points.py.
BADGE_TIERS = [
    (350, "[Name] Badge"),
    (225, "Onyx"),
    (150, "Diamond"),
    (90, "Platinum"),
    (50, "Gold"),
    (25, "Silver"),
    (10, "Bronze"),
    (0, "No badge"),
]


def badge_for_points(points: int) -> str:
    for threshold, badge in BADGE_TIERS:
        if points >= threshold:
            return badge
    return "No badge"


class Member:
    __slots__ = ("username", "uuid", "rank")

    def __init__(self, username: str, uuid: Optional[str], rank: Optional[str]) -> None:
        self.username = username
        self.uuid = uuid
        self.rank = rank

    def dedupe_key(self) -> str:
        return (self.uuid or self.username).lower()


class Quester:
    __slots__ = ("username", "uuid", "rank", "points")

    def __init__(self, member: Member, points: int) -> None:
        self.username = member.username
        self.uuid = member.uuid
        self.rank = member.rank
        self.points = points

    def dedupe_key(self) -> str:
        return (self.uuid or self.username).lower()


# --------------------------------------------------------------------------- #
# Guild roster (who is currently in the guild)
# --------------------------------------------------------------------------- #
def find_latest_snapshot() -> Optional[Path]:
    """Return the newest guild snapshot, or None if there is none."""
    candidates: List[Path] = []
    if API_TRACKING_DIR.is_dir():
        candidates.extend(Path(p) for p in glob.glob(str(API_TRACKING_DIR / "*" / "*.db")))
    # Backwards-compatible flat layout.
    candidates.extend(Path(p) for p in glob.glob(str(DB_DIR / "*.db")))
    candidates = [p for p in candidates if p.is_file() and SNAPSHOT_NAME_RE.match(p.name)]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def roster_from_snapshot(db_path: Path) -> List[Member]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT username, uuid, guild_rank FROM player_stats").fetchall()
    finally:
        conn.close()
    return [Member(username=u, uuid=uid, rank=rank) for u, uid, rank in rows]


def roster_from_api(base_url: str, guild_prefix: str, timeout: float) -> List[Member]:
    encoded = urllib.parse.quote(guild_prefix)
    url = f"{base_url}/guild/prefix/{encoded}"
    request = urllib.request.Request(url, headers={"User-Agent": "esi-bot-list-guild-questers"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    members_payload = payload.get("members") or {}
    members: List[Member] = []
    for rank, rank_members in members_payload.items():
        if rank == "total" or not isinstance(rank_members, dict):
            continue
        for username, info in rank_members.items():
            if not isinstance(info, dict):
                continue
            members.append(Member(username=username, uuid=info.get("uuid"), rank=rank))
    return members


def load_roster(
    source: str, guild_prefix: str, base_url: str, timeout: float
) -> Tuple[Optional[List[Member]], str]:
    """Return (members, source_label). members is None only if api was required and failed."""
    if source in ("auto", "api"):
        try:
            members = roster_from_api(base_url, guild_prefix, timeout)
            return members, f"live API ({guild_prefix})"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            if source == "api":
                print(f"Error: failed to reach the Wynncraft API: {exc}", file=sys.stderr)
                return None, ""
            print(f"[warn] Live API unavailable ({exc}); falling back to local snapshot.", file=sys.stderr)

    snapshot = find_latest_snapshot()
    if snapshot is None:
        print("Error: no local api_tracking snapshot found.", file=sys.stderr)
        return None, ""
    return roster_from_snapshot(snapshot), f"snapshot {snapshot.relative_to(PROJECT_ROOT)}"


# --------------------------------------------------------------------------- #
# Quest points (who has completed quests)
# --------------------------------------------------------------------------- #
def load_quest_points(db_path: Path) -> Dict[str, int]:
    """Return {player_key_lower: points} from quest_progress (max per key)."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT player, COALESCE(points, 0) FROM quest_progress").fetchall()
    finally:
        conn.close()

    points: Dict[str, int] = {}
    for player, value in rows:
        if not player:
            continue
        key = str(player).lower()
        points[key] = max(points.get(key, 0), int(value or 0))
    return points


def match_questers(members: List[Member], quest_points: Dict[str, int], min_points: int) -> List[Quester]:
    """Keep members that appear in quest_progress with at least ``min_points``."""
    index: Dict[str, Member] = {}
    for member in members:
        if member.uuid:
            index[member.uuid.lower()] = member
        index[member.username.lower()] = member

    best: Dict[str, Quester] = {}
    for key, points in quest_points.items():
        if points < min_points:
            continue
        member = index.get(key)
        if member is None:
            continue  # has quest points but is not in the guild right now
        existing = best.get(member.dedupe_key())
        if existing is None or points > existing.points:
            best[member.dedupe_key()] = Quester(member, points)

    return sorted(best.values(), key=lambda q: (-q.points, q.username.lower()))


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def render(questers: List[Quester], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(
            [
                {
                    "username": q.username,
                    "uuid": q.uuid,
                    "rank": q.rank,
                    "quest_points": q.points,
                    "badge": badge_for_points(q.points),
                }
                for q in questers
            ],
            indent=2,
            ensure_ascii=False,
        )

    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["username", "uuid", "rank", "quest_points", "badge"])
        for q in questers:
            writer.writerow([q.username, q.uuid or "", q.rank or "", q.points, badge_for_points(q.points)])
        return buffer.getvalue().rstrip("\n")

    if fmt == "detailed":
        width = max((len(q.username) for q in questers), default=8)
        width = max(width, len("username"))
        lines = [f"{'rank':<10} | {'username':<{width}} | {'points':>6} | {'badge':<11} | uuid"]
        lines.append("-" * len(lines[0]))
        for q in questers:
            lines.append(
                f"{q.rank or '':<10} | {q.username:<{width}} | {q.points:>6} | "
                f"{badge_for_points(q.points):<11} | {q.uuid or ''}"
            )
        return "\n".join(lines)

    # Default: bare usernames, one per line (easy to pipe / copy).
    return "\n".join(q.username for q in questers)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List current guild members who have completed at least one quest (via /quest_points).",
    )
    parser.add_argument(
        "--source",
        choices=["auto", "api", "db"],
        default="auto",
        help="Where to read the current roster from (default: auto = live API, fall back to snapshot).",
    )
    parser.add_argument(
        "--guild-prefix",
        default=DEFAULT_GUILD_PREFIX,
        help=f"Wynncraft guild prefix (default: {DEFAULT_GUILD_PREFIX}).",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Wynncraft API base URL (default: {DEFAULT_BASE_URL}).",
    )
    parser.add_argument(
        "--quests-db",
        default=str(QUEST_PROGRESS_DB),
        help=f"Path to recruited_data.db holding quest_progress (default: {QUEST_PROGRESS_DB.name}).",
    )
    parser.add_argument(
        "--min-points",
        type=int,
        default=1,
        help="Minimum quest points to include (default: 1 = at least one completed quest).",
    )
    parser.add_argument(
        "--format",
        choices=["names", "detailed", "csv", "json"],
        default="names",
        help="Output format (default: names).",
    )
    parser.add_argument("--output", default=None, help="Optional file to write the output to.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="API request timeout in seconds (default: 30).",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    quests_db = Path(args.quests_db).expanduser()
    if not quests_db.is_file():
        print(f"Error: quest database not found at {quests_db}", file=sys.stderr)
        return 1

    members, roster_source = load_roster(args.source, args.guild_prefix, args.base_url, args.timeout)
    if members is None:
        return 1

    quest_points = load_quest_points(quests_db)
    questers = match_questers(members, quest_points, args.min_points)

    body = render(questers, args.format)
    if body:
        print(body)

    if args.output:
        out_path = Path(args.output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(body + ("\n" if body else ""), encoding="utf-8")
        print(f"Wrote {len(questers)} member(s) to {out_path}", file=sys.stderr)

    print(
        f"{len(questers)} guild member(s) with >= {args.min_points} quest point(s) "
        f"out of {len(members)} in the guild [roster: {roster_source}]",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
