#!/usr/bin/env python3
"""
Search and summarize temp VC message metadata.

Examples:
  python scripts/search_temp_vc_messages.py list-vcs
  python scripts/search_temp_vc_messages.py list-vcs --page 2 --page-size 10
  python scripts/search_temp_vc_messages.py search --temp-vc-table temp_vc_123 --author-id 111
  python scripts/search_temp_vc_messages.py search --mention-user-id 111 --created-after 2026-06-01T00:00:00Z --json
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "databases" / "temp_vc_messages.db"
TABLE_NAME_PATTERN = re.compile(r"^temp_vc_\d+$")


def _parse_iso_datetime(raw: str) -> str:
    value = str(raw).strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid datetime: {raw}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _parse_bool(raw: str) -> int:
    normalized = str(raw).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return 1
    if normalized in {"0", "false", "no", "n", "off"}:
        return 0
    raise argparse.ArgumentTypeError(f"Invalid boolean value: {raw}")


def _load_json_list(raw: Any) -> List[Any]:
    if raw is None:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return data
    return []


def _validate_temp_vc_table_name(table_name: str) -> str:
    normalized = str(table_name).strip()
    if not TABLE_NAME_PATTERN.fullmatch(normalized):
        raise ValueError(f"Invalid temp VC table name: {table_name}")
    return normalized


def _get_connection(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def _get_temp_vc_tables(connection: sqlite3.Connection, requested_tables: List[str]) -> List[str]:
    rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name LIKE 'temp_vc_%'
        ORDER BY name
        """
    ).fetchall()
    available = [str(row["name"]) for row in rows if TABLE_NAME_PATTERN.fullmatch(str(row["name"]))]

    if not requested_tables:
        return available

    requested = [_validate_temp_vc_table_name(table_name) for table_name in requested_tables]
    requested_set = set(requested)
    missing = sorted(requested_set.difference(set(available)))
    if missing:
        raise ValueError(f"Requested temp VC table(s) not found: {', '.join(missing)}")
    return sorted(requested_set)


def _paginate_items(items: List[Any], page: int, page_size: int) -> tuple[List[Any], int, int, int, int]:
    if page < 1:
        raise ValueError("--page must be at least 1.")
    if page_size < 0:
        raise ValueError("--page-size must be 0 or greater.")

    total_items = len(items)
    if total_items == 0:
        return [], 1, page_size, 0, 1
    if page_size == 0:
        return list(items), 1, 0, total_items, 1

    total_pages = max(1, math.ceil(total_items / page_size))
    current_page = min(page, total_pages)
    start_idx = (current_page - 1) * page_size
    end_idx = start_idx + page_size
    return list(items[start_idx:end_idx]), current_page, page_size, total_items, total_pages


def _collect_table_summaries(connection: sqlite3.Connection, tables: List[str]) -> List[Dict[str, Any]]:
    summaries: List[Dict[str, Any]] = []
    for table_name in tables:
        row = connection.execute(
            f"""
            SELECT
                COUNT(*) AS message_count,
                COUNT(DISTINCT author_id) AS unique_authors,
                MIN(created_at) AS first_seen,
                MAX(created_at) AS last_seen
            FROM "{table_name}"
            """
        ).fetchone()
        summaries.append(
            {
                "temp_vc_table": table_name,
                "message_count": int(row["message_count"] or 0),
                "unique_authors": int(row["unique_authors"] or 0),
                "first_seen": row["first_seen"] or "-",
                "last_seen": row["last_seen"] or "-",
            }
        )
    return summaries


def _print_table_summaries(
    connection: sqlite3.Connection,
    tables: List[str],
    page: int,
    page_size: int,
    as_json: bool,
) -> None:
    summaries = _collect_table_summaries(connection, tables)
    page_rows, current_page, current_page_size, total_rows, total_pages = _paginate_items(
        summaries,
        page=page,
        page_size=page_size,
    )

    if as_json:
        print(
            json.dumps(
                {
                    "total_temp_vc_tables": total_rows,
                    "page": current_page,
                    "page_size": current_page_size,
                    "total_pages": total_pages,
                    "tables": page_rows,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    if not summaries:
        print("No temp VC tables found.")
        return

    print(
        f"Found {total_rows} temp VC table(s). "
        f"Showing page {current_page}/{total_pages} ({len(page_rows)} item(s))."
    )
    for row in page_rows:
        print(
            f"- {row['temp_vc_table']} | messages={row['message_count']} | "
            f"authors={row['unique_authors']} | first={row['first_seen']} | last={row['last_seen']}"
        )
    if total_pages > 1:
        print(
            f"Use --page <n> to view more pages"
            + (f" (current page size: {current_page_size})" if current_page_size > 0 else "")
            + "."
        )


def _build_sql_filters(args: argparse.Namespace) -> tuple[List[str], List[Any]]:
    clauses: List[str] = []
    params: List[Any] = []

    if args.message_id is not None:
        clauses.append("message_id = ?")
        params.append(args.message_id)
    if args.author_id is not None:
        clauses.append("author_id = ?")
        params.append(args.author_id)
    if args.author_is_bot is not None:
        clauses.append("author_is_bot = ?")
        params.append(args.author_is_bot)
    if args.message_type is not None:
        clauses.append("message_type = ?")
        params.append(args.message_type)
    if args.created_at is not None:
        clauses.append("created_at = ?")
        params.append(args.created_at)
    if args.created_after is not None:
        clauses.append("created_at >= ?")
        params.append(args.created_after)
    if args.created_before is not None:
        clauses.append("created_at <= ?")
        params.append(args.created_before)
    if args.mentions_everyone is not None:
        clauses.append("mentions_everyone = ?")
        params.append(args.mentions_everyone)
    if args.attachment_count is not None:
        clauses.append("attachment_count = ?")
        params.append(args.attachment_count)
    if args.attachment_count_min is not None:
        clauses.append("attachment_count >= ?")
        params.append(args.attachment_count_min)
    if args.attachment_count_max is not None:
        clauses.append("attachment_count <= ?")
        params.append(args.attachment_count_max)
    if args.embed_count is not None:
        clauses.append("embed_count = ?")
        params.append(args.embed_count)
    if args.embed_count_min is not None:
        clauses.append("embed_count >= ?")
        params.append(args.embed_count_min)
    if args.embed_count_max is not None:
        clauses.append("embed_count <= ?")
        params.append(args.embed_count_max)
    if args.sticker_count is not None:
        clauses.append("sticker_count = ?")
        params.append(args.sticker_count)
    if args.sticker_count_min is not None:
        clauses.append("sticker_count >= ?")
        params.append(args.sticker_count_min)
    if args.sticker_count_max is not None:
        clauses.append("sticker_count <= ?")
        params.append(args.sticker_count_max)
    if args.referenced_message_id is not None:
        clauses.append("referenced_message_id = ?")
        params.append(args.referenced_message_id)

    return clauses, params


def _passes_json_filters(row: Dict[str, Any], args: argparse.Namespace) -> bool:
    mention_user_ids = [int(x) for x in _load_json_list(row.get("mention_user_ids")) if str(x).isdigit()]
    mention_role_ids = [int(x) for x in _load_json_list(row.get("mention_role_ids")) if str(x).isdigit()]
    attachment_urls = [str(x) for x in _load_json_list(row.get("attachment_urls"))]

    if args.mention_user_id:
        required = {int(x) for x in args.mention_user_id}
        if not required.issubset(set(mention_user_ids)):
            return False

    if args.mention_role_id:
        required = {int(x) for x in args.mention_role_id}
        if not required.issubset(set(mention_role_ids)):
            return False

    if args.attachment_url_contains:
        needle = args.attachment_url_contains.casefold()
        if not any(needle in url.casefold() for url in attachment_urls):
            return False

    return True


def _search_rows(connection: sqlite3.Connection, tables: List[str], args: argparse.Namespace) -> List[Dict[str, Any]]:
    where_clauses, where_params = _build_sql_filters(args)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    matches: List[Dict[str, Any]] = []
    for table_name in tables:
        query = f"""
            SELECT
                message_id,
                author_id,
                author_is_bot,
                message_type,
                created_at,
                mentions_everyone,
                mention_user_ids,
                mention_role_ids,
                attachment_count,
                attachment_urls,
                embed_count,
                sticker_count,
                referenced_message_id
            FROM "{table_name}"
            {where_sql}
        """
        rows = connection.execute(query, where_params).fetchall()
        for row in rows:
            row_dict = dict(row)
            row_dict["temp_vc_table"] = table_name
            if _passes_json_filters(row_dict, args):
                matches.append(row_dict)

    reverse = args.order == "desc"
    matches.sort(
        key=lambda item: (str(item.get("created_at") or ""), int(item.get("message_id") or 0)),
        reverse=reverse,
    )
    if args.offset > 0:
        matches = matches[args.offset :]
    if args.limit >= 0:
        matches = matches[: args.limit]
    return matches


def _print_search_results(
    rows: List[Dict[str, Any]],
    total_rows: int,
    page: int,
    page_size: int,
    total_pages: int,
    as_json: bool,
) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "total_matches": total_rows,
                    "page": page,
                    "page_size": page_size,
                    "total_pages": total_pages,
                    "rows": rows,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    if total_rows == 0:
        print("No rows matched the provided filters.")
        return

    print(
        f"Matched {total_rows} row(s). "
        f"Showing page {page}/{total_pages} ({len(rows)} row(s) on this page)."
    )
    for row in rows:
        print(
            f"- {row['temp_vc_table']} | message_id={row['message_id']} | author_id={row['author_id']} | "
            f"created_at={row['created_at']} | type={row['message_type']} | bot={row['author_is_bot']} | "
            f"mentions_everyone={row['mentions_everyone']} | attachments={row['attachment_count']} | "
            f"embeds={row['embed_count']} | stickers={row['sticker_count']} | "
            f"referenced_message_id={row['referenced_message_id']}"
        )
    if total_pages > 1:
        print(
            f"Use --page <n> to view more pages"
            + (f" (current page size: {page_size})" if page_size > 0 else "")
            + "."
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search temp VC message metadata and list saved temp VC tables.",
    )
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help=f"Path to temp VC sqlite database (default: {DEFAULT_DB_PATH})",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list-vcs", help="List temp VC tables with a brief summary.")
    list_parser.add_argument(
        "--temp-vc-table",
        action="append",
        default=[],
        help="Filter to specific temp VC table name(s), e.g. temp_vc_123 (repeatable).",
    )
    list_parser.add_argument("--page", type=int, default=1, help="1-based page number.")
    list_parser.add_argument("--page-size", type=int, default=25, help="Rows per page; set to 0 for all.")
    list_parser.add_argument("--json", action="store_true", help="Output summaries as JSON.")

    search_parser = subparsers.add_parser("search", help="Search saved temp VC messages with filters.")
    search_parser.add_argument(
        "--temp-vc-table",
        action="append",
        default=[],
        help="Filter to specific temp VC table name(s), e.g. temp_vc_123 (repeatable).",
    )
    search_parser.add_argument("--message-id", type=int)
    search_parser.add_argument("--author-id", type=int)
    search_parser.add_argument("--author-is-bot", type=_parse_bool)
    search_parser.add_argument("--message-type", type=int)
    search_parser.add_argument("--created-at", type=_parse_iso_datetime, help="Exact UTC datetime in ISO format.")
    search_parser.add_argument("--created-after", type=_parse_iso_datetime, help="Inclusive UTC datetime filter.")
    search_parser.add_argument("--created-before", type=_parse_iso_datetime, help="Inclusive UTC datetime filter.")
    search_parser.add_argument("--mentions-everyone", type=_parse_bool)
    search_parser.add_argument(
        "--mention-user-id",
        type=int,
        action="append",
        default=[],
        help="Require mention_user_ids to contain this user id (repeatable; all must be present).",
    )
    search_parser.add_argument(
        "--mention-role-id",
        type=int,
        action="append",
        default=[],
        help="Require mention_role_ids to contain this role id (repeatable; all must be present).",
    )
    search_parser.add_argument("--attachment-count", type=int)
    search_parser.add_argument("--attachment-count-min", type=int)
    search_parser.add_argument("--attachment-count-max", type=int)
    search_parser.add_argument(
        "--attachment-url-contains",
        type=str,
        help="Case-insensitive substring match against saved attachment URLs JSON.",
    )
    search_parser.add_argument("--embed-count", type=int)
    search_parser.add_argument("--embed-count-min", type=int)
    search_parser.add_argument("--embed-count-max", type=int)
    search_parser.add_argument("--sticker-count", type=int)
    search_parser.add_argument("--sticker-count-min", type=int)
    search_parser.add_argument("--sticker-count-max", type=int)
    search_parser.add_argument("--referenced-message-id", type=int)
    search_parser.add_argument("--order", choices=["asc", "desc"], default="desc")
    search_parser.add_argument("--offset", type=int, default=0)
    search_parser.add_argument("--limit", type=int, default=-1, help="Set to -1 for no limit.")
    search_parser.add_argument("--page", type=int, default=1, help="1-based page number.")
    search_parser.add_argument("--page-size", type=int, default=50, help="Rows per page; set to 0 for all.")
    search_parser.add_argument("--json", action="store_true", help="Output search matches as JSON.")

    return parser

def _print_full_help(parser: argparse.ArgumentParser) -> None:
    parser.print_help()
    subparsers_action = next(
        (action for action in parser._actions if isinstance(action, argparse._SubParsersAction)),
        None,
    )
    if subparsers_action is None:
        return

    for command_name in ("list-vcs", "search"):
        subparser = subparsers_action.choices.get(command_name)
        if subparser is None:
            continue
        print(f"\n{command_name} options:")
        print(subparser.format_help().strip())


def main() -> int:
    parser = _build_parser()
    if len(sys.argv) == 2 and sys.argv[1] in {"-h", "--help"}:
        _print_full_help(parser)
        return 0
    args = parser.parse_args()

    db_path = Path(args.db_path).expanduser().resolve()
    if not db_path.exists():
        print(f"Database file not found: {db_path}")
        return 1

    try:
        with _get_connection(db_path) as connection:
            tables = _get_temp_vc_tables(connection, getattr(args, "temp_vc_table", []))
            if args.command == "list-vcs":
                _print_table_summaries(
                    connection,
                    tables,
                    page=int(args.page),
                    page_size=int(args.page_size),
                    as_json=bool(args.json),
                )
                return 0
            if args.command == "search":
                all_rows = _search_rows(connection, tables, args)
                page_rows, current_page, current_page_size, total_rows, total_pages = _paginate_items(
                    all_rows,
                    page=int(args.page),
                    page_size=int(args.page_size),
                )
                _print_search_results(
                    page_rows,
                    total_rows=total_rows,
                    page=current_page,
                    page_size=current_page_size,
                    total_pages=total_pages,
                    as_json=bool(args.json),
                )
                return 0
            print(f"Unknown command: {args.command}")
            return 1
    except ValueError as exc:
        print(str(exc))
        return 1
    except sqlite3.Error as exc:
        print(f"SQLite error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
