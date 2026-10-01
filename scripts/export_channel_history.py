#!/usr/bin/env python3
"""
Export the full message history of a list of channels in a Discord server.

Messages are pulled from a start date up to now (or an explicit end date) and
saved to disk, one file per channel plus an optional combined file.

Examples:
  # List every channel in the guild with its ID (handy for building the list)
  python scripts/export_channel_history.py --list-channels

  # Export two channels by ID from 2025-01-01 until today
  python scripts/export_channel_history.py --since 2025-01-01 --channel-id 111 --channel-id 222

  # Export every channel named in a file (one ID per line, # comments allowed)
  python scripts/export_channel_history.py --since 2025-06-01 --channels-file scripts/channels.txt

  # Export by channel name, as CSV, into a custom folder
  python scripts/export_channel_history.py --since 2025-01-01 --channel-name general --format csv --output-dir exports/general

  # Include threads under the exported channels and skip bot messages
  python scripts/export_channel_history.py --since 2025-01-01 --channel-id 111 --include-threads --exclude-bots

Notes:
  - Requires DISCORD_TOKEN in the project .env (same token as the bot).
  - The "Message Content Intent" must be enabled for the bot in the Discord
    Developer Portal, otherwise message text comes back empty.
  - Files are written incrementally, so a cancelled or failed run still keeps
    whatever was already fetched.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO

import discord
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_GUILD_ID = 802999599060221992
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "exports" / "channel_history"

PROGRESS_EVERY = 250
MAX_RETRIES = 5
RETRY_BACKOFF_SECONDS = 5.0

_DATE_ONLY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UNSAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")

TEXT_CHANNEL_TYPES = (discord.TextChannel, discord.ForumChannel)

CSV_FIELDS = [
    "message_id",
    "channel_id",
    "channel_name",
    "author_id",
    "author_name",
    "author_display_name",
    "author_is_bot",
    "created_at",
    "edited_at",
    "content",
    "attachment_count",
    "attachment_urls",
    "embed_count",
    "sticker_count",
    "mention_user_ids",
    "mention_role_ids",
    "mentions_everyone",
    "reply_to_message_id",
    "pinned",
    "message_type",
    "jump_url",
]


def _parse_datetime(raw: str, *, arg_name: str, end_of_day: bool) -> datetime:
    value = str(raw).strip()
    if not value:
        raise argparse.ArgumentTypeError(f"{arg_name} must not be empty.")

    if value[-1] in {"Z", "z"}:
        value = value[:-1] + "+00:00"

    if _DATE_ONLY_PATTERN.fullmatch(value):
        value += "T23:59:59.999999+00:00" if end_of_day else "T00:00:00+00:00"

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid datetime for {arg_name}: {raw!r}. "
            "Use YYYY-MM-DD or an ISO 8601 datetime (e.g. 2025-01-01T12:00:00Z)."
        ) from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_since(raw: str) -> datetime:
    return _parse_datetime(raw, arg_name="--since", end_of_day=False)


def _parse_until(raw: str) -> datetime:
    # A date-only --until covers the whole day, so the end is inclusive.
    return _parse_datetime(raw, arg_name="--until", end_of_day=True)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _load_token() -> str:
    load_dotenv(PROJECT_ROOT / ".env")
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN not found in .env")
    return token


def _load_channel_ids_from_file(path: Path) -> List[int]:
    if not path.exists():
        raise SystemExit(f"Channels file not found: {path}")

    channel_ids: List[int] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            channel_ids.append(int(line))
        except ValueError as exc:
            raise SystemExit(
                f"Invalid channel ID on line {line_number} of {path}: {raw_line.strip()!r}"
            ) from exc
    return channel_ids


def _safe_filename(name: str, *, fallback: str) -> str:
    cleaned = _UNSAFE_FILENAME_PATTERN.sub("-", name).strip("-._")
    if not cleaned:
        cleaned = fallback
    return cleaned[:80]


class _BaseWriter:
    """Writes message records to a single output file."""

    extension = "txt"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.count = 0

    def write_channel_header(self, channel_name: str, channel_id: int) -> None:
        """Called once before the first record of each channel."""

    def write(self, record: Dict[str, Any]) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class _JsonlWriter(_BaseWriter):
    extension = "jsonl"

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._handle: TextIO = path.open("w", encoding="utf-8", newline="\n")

    def write(self, record: Dict[str, Any]) -> None:
        self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.count += 1

    def close(self) -> None:
        self._handle.close()


class _JsonWriter(_BaseWriter):
    """Buffers everything in memory; prefer jsonl for very large exports."""

    extension = "json"

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._records: List[Dict[str, Any]] = []

    def write(self, record: Dict[str, Any]) -> None:
        self._records.append(record)
        self.count += 1

    def close(self) -> None:
        with self.path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(self._records, handle, ensure_ascii=False, indent=2)
            handle.write("\n")


class _CsvWriter(_BaseWriter):
    extension = "csv"

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._handle: TextIO = path.open("w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        self._writer.writeheader()

    def write(self, record: Dict[str, Any]) -> None:
        row = {
            "message_id": record["message_id"],
            "channel_id": record["channel_id"],
            "channel_name": record["channel_name"],
            "author_id": record["author_id"],
            "author_name": record["author_name"],
            "author_display_name": record["author_display_name"],
            "author_is_bot": int(bool(record["author_is_bot"])),
            "created_at": record["created_at"],
            "edited_at": record["edited_at"] or "",
            "content": record["content"],
            "attachment_count": record["attachment_count"],
            "attachment_urls": " | ".join(item["url"] for item in record["attachments"]),
            "embed_count": record["embed_count"],
            "sticker_count": record["sticker_count"],
            "mention_user_ids": " ".join(str(value) for value in record["mention_user_ids"]),
            "mention_role_ids": " ".join(str(value) for value in record["mention_role_ids"]),
            "mentions_everyone": int(bool(record["mentions_everyone"])),
            "reply_to_message_id": record["reply_to_message_id"] or "",
            "pinned": int(bool(record["pinned"])),
            "message_type": record["message_type"],
            "jump_url": record["jump_url"],
        }
        self._writer.writerow(row)
        self.count += 1

    def close(self) -> None:
        self._handle.close()


class _TextWriter(_BaseWriter):
    extension = "txt"

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._handle: TextIO = path.open("w", encoding="utf-8", newline="\n")
        self._wrote_header = False

    def write_channel_header(self, channel_name: str, channel_id: int) -> None:
        if self._wrote_header:
            self._handle.write("\n")
        self._wrote_header = True
        self._handle.write(f"{'=' * 70}\n# {channel_name} ({channel_id})\n{'=' * 70}\n\n")

    def write(self, record: Dict[str, Any]) -> None:
        label = record["author_display_name"]
        if record["author_is_bot"]:
            label += " [bot]"
        self._handle.write(f"[{record['created_at']}] {label} ({record['author_id']}):\n")

        content = record["content"] or ""
        if content:
            for line in content.splitlines():
                self._handle.write(f"  {line}\n")
        else:
            self._handle.write("  (no text content)\n")

        if record["reply_to_message_id"]:
            self._handle.write(f"  [reply to] {record['reply_to_message_id']}\n")

        for attachment in record["attachments"]:
            self._handle.write(f"  [attachment] {attachment['filename']} -> {attachment['url']}\n")

        if record["embed_count"]:
            self._handle.write(f"  [embeds] {record['embed_count']}\n")

        self._handle.write("\n")
        self.count += 1

    def close(self) -> None:
        self._handle.close()


_WRITER_TYPES = {
    "jsonl": _JsonlWriter,
    "json": _JsonWriter,
    "csv": _CsvWriter,
    "txt": _TextWriter,
}


def _make_writer(path: Path, fmt: str) -> _BaseWriter:
    return _WRITER_TYPES[fmt](path)


def _message_to_record(message: discord.Message, channel: discord.abc.GuildChannel) -> Dict[str, Any]:
    author = message.author
    reference = message.reference
    message_type = getattr(message.type, "name", str(message.type))

    return {
        "message_id": message.id,
        "channel_id": channel.id,
        "channel_name": getattr(channel, "name", str(channel.id)),
        "guild_id": getattr(message.guild, "id", None),
        "author_id": author.id,
        "author_name": author.name,
        "author_display_name": getattr(author, "display_name", author.name),
        "author_is_bot": bool(author.bot),
        "created_at": _iso(message.created_at),
        "edited_at": _iso(message.edited_at),
        "content": message.content or "",
        "attachments": [
            {
                "filename": attachment.filename,
                "url": attachment.url,
                "size": attachment.size,
                "content_type": attachment.content_type,
            }
            for attachment in message.attachments
        ],
        "attachment_count": len(message.attachments),
        "embeds": [embed.to_dict() for embed in message.embeds],
        "embed_count": len(message.embeds),
        "sticker_count": len(message.stickers),
        "mention_user_ids": [user.id for user in message.mentions],
        "mention_role_ids": [role.id for role in message.role_mentions],
        "mentions_everyone": bool(message.mention_everyone),
        "reply_to_message_id": reference.message_id if reference else None,
        "pinned": bool(message.pinned),
        "message_type": message_type,
        "jump_url": message.jump_url,
    }


@dataclass
class _ChannelResult:
    channel_id: int
    channel_name: str
    channel_type: str
    messages: int = 0
    first_message_at: Optional[str] = None
    last_message_at: Optional[str] = None
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "channel_type": self.channel_type,
            "messages": self.messages,
            "first_message_at": self.first_message_at,
            "last_message_at": self.last_message_at,
            "error": self.error,
        }


async def _resolve_channels(
    client: discord.Client,
    guild: discord.Guild,
    channel_ids: List[int],
    channel_names: List[str],
    *,
    include_threads: bool,
) -> tuple[List[discord.abc.GuildChannel], List[str]]:
    resolved: List[discord.abc.GuildChannel] = []
    seen: set[int] = set()
    problems: List[str] = []

    def _add(channel: Optional[discord.abc.GuildChannel]) -> None:
        if channel is not None and channel.id not in seen:
            seen.add(channel.id)
            resolved.append(channel)

    for channel_id in channel_ids:
        channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
        if channel is None:
            try:
                channel = await client.fetch_channel(channel_id)
            except discord.NotFound:
                problems.append(f"Channel ID {channel_id} not found (or bot has no access).")
                continue
            except discord.Forbidden:
                problems.append(f"Channel ID {channel_id}: missing access.")
                continue
        _add(channel)

    for name in channel_names:
        matches = [
            channel
            for channel in guild.channels
            if isinstance(channel, TEXT_CHANNEL_TYPES) and channel.name.casefold() == name.casefold()
        ]
        if not matches:
            problems.append(f"No text channel named {name!r} found in {guild.name}.")
            continue
        for match in matches:
            _add(match)

    if include_threads:
        thread_candidates = list(resolved)
        for parent in thread_candidates:
            if not isinstance(parent, TEXT_CHANNEL_TYPES):
                continue
            for thread in list(guild.threads):
                if thread.parent_id == parent.id:
                    _add(thread)
            try:
                async for thread in parent.archived_threads(limit=None):
                    _add(thread)
            except (discord.Forbidden, discord.HTTPException) as exc:
                problems.append(f"Could not list archived threads for #{parent.name}: {exc}")

    return resolved, problems


def _list_guild_channels(guild: discord.Guild) -> None:
    print(f"Channels in {guild.name} ({guild.id}):\n")
    for channel in sorted(guild.channels, key=lambda item: (str(item.type), item.position)):
        kind = str(channel.type)
        print(f"  {channel.id}  [{kind}]  #{channel.name}")

    threads = list(guild.threads)
    if threads:
        print(f"\nActive threads ({len(threads)}):\n")
        for thread in sorted(threads, key=lambda item: item.name):
            print(f"  {thread.id}  [thread]  {thread.name}")


async def _iter_history(
    channel: discord.abc.GuildChannel,
    since: datetime,
    *,
    limit: Optional[int],
) -> Any:
    """Yield messages oldest-first, resuming after transient HTTP failures."""
    cursor: Optional[int] = None
    yielded = 0
    attempt = 0

    while True:
        try:
            if cursor is None:
                # Back up one second so messages in the same millisecond as
                # `since` are not skipped by Discord's exclusive `after` filter.
                iterator = channel.history(
                    limit=None,
                    after=since - timedelta(seconds=1),
                    oldest_first=True,
                )
            else:
                iterator = channel.history(
                    limit=None,
                    after=discord.Object(id=cursor),
                    oldest_first=True,
                )

            async for message in iterator:
                cursor = message.id
                yielded += 1
                yield message
                if limit is not None and yielded >= limit:
                    return
            return
        except discord.HTTPException as exc:
            attempt += 1
            if attempt > MAX_RETRIES:
                raise
            delay = RETRY_BACKOFF_SECONDS * attempt
            print(
                f"  [WARN] history fetch failed for #{getattr(channel, 'name', channel.id)} "
                f"({exc}); retrying in {delay:.0f}s (attempt {attempt}/{MAX_RETRIES})"
            )
            await asyncio.sleep(delay)


async def _export_channel(
    channel: discord.abc.GuildChannel,
    writers: List[_BaseWriter],
    *,
    since: datetime,
    until: Optional[datetime],
    exclude_bots: bool,
    limit: Optional[int],
    index: int,
    total: int,
) -> _ChannelResult:
    name = getattr(channel, "name", str(channel.id))
    result = _ChannelResult(channel_id=channel.id, channel_name=name, channel_type=str(channel.type))

    print(f"[{index}/{total}] Exporting #{name} ({channel.id})...")

    for writer in writers:
        writer.write_channel_header(name, channel.id)

    try:
        async for message in _iter_history(channel, since, limit=limit):
            if message.created_at < since:
                continue
            if until is not None and message.created_at >= until:
                break
            if exclude_bots and message.author.bot:
                continue

            record = _message_to_record(message, channel)
            for writer in writers:
                writer.write(record)

            result.messages += 1
            if result.first_message_at is None:
                result.first_message_at = record["created_at"]
            result.last_message_at = record["created_at"]

            if result.messages % PROGRESS_EVERY == 0:
                print(f"    ... {result.messages:,} messages (up to {record['created_at']})")
    except discord.Forbidden:
        result.error = "Missing permission to read message history."
    except discord.HTTPException as exc:
        result.error = f"HTTP error: {exc}"

    if result.error:
        print(f"    [ERROR] {result.error}")
    else:
        span = ""
        if result.first_message_at and result.last_message_at:
            span = f" ({result.first_message_at} -> {result.last_message_at})"
        print(f"    [OK] {result.messages:,} messages{span}")

    return result


async def _process(client: discord.Client, args: argparse.Namespace) -> int:
    guild = client.get_guild(args.guild_id)
    if guild is None:
        try:
            guild = await client.fetch_guild(args.guild_id)
        except discord.NotFound:
            print(f"Guild {args.guild_id} not found, or the bot is not a member.", file=sys.stderr)
            return 1
        except discord.Forbidden:
            print(f"Missing access to guild {args.guild_id}.", file=sys.stderr)
            return 1

    print(f"Connected as {client.user} | Guild: {guild.name} ({guild.id})")

    if args.list_channels:
        _list_guild_channels(guild)
        return 0

    if args.since is None:
        print("--since is required (e.g. --since 2025-01-01).", file=sys.stderr)
        return 2

    channel_ids = list(args.channel_id)
    if args.channels_file:
        channel_ids.extend(_load_channel_ids_from_file(Path(args.channels_file).expanduser()))

    if not channel_ids and not args.channel_name:
        print(
            "No channels selected. Pass --channel-id, --channel-name, or --channels-file "
            "(use --list-channels to see available IDs).",
            file=sys.stderr,
        )
        return 2

    channels, problems = await _resolve_channels(
        client,
        guild,
        channel_ids,
        list(args.channel_name),
        include_threads=args.include_threads,
    )
    for problem in problems:
        print(f"[WARN] {problem}", file=sys.stderr)

    if not channels:
        print("No channels to export.", file=sys.stderr)
        return 1

    since = args.since
    until = args.until
    limit = args.limit if args.limit and args.limit > 0 else None

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Guild:    {guild.name} ({guild.id})")
    print(f"Channels: {len(channels)}")
    print(f"Window:   {since.isoformat()} -> {(until.isoformat() if until else 'now')}")
    print(f"Format:   {args.format}")
    print(f"Output:   {output_dir}")
    print("-" * 70)

    combined_path = output_dir / _safe_filename(f"_all-{guild.name}", fallback="all")
    combined_path = combined_path.with_suffix(f".{_WRITER_TYPES[args.format].extension}")
    combined_writer = None if args.no_combined else _make_writer(combined_path, args.format)

    results: List[_ChannelResult] = []
    try:
        for index, channel in enumerate(channels, 1):
            stem = _safe_filename(
                f"{getattr(channel, 'name', channel.id)}-{channel.id}",
                fallback=str(channel.id),
            )
            channel_path = output_dir / f"{stem}.{_WRITER_TYPES[args.format].extension}"

            writers: List[_BaseWriter] = [_make_writer(channel_path, args.format)]
            if combined_writer is not None:
                writers.append(combined_writer)

            try:
                result = await _export_channel(
                    channel,
                    writers,
                    since=since,
                    until=until,
                    exclude_bots=args.exclude_bots,
                    limit=limit,
                    index=index,
                    total=len(channels),
                )
            finally:
                writers[0].close()

            results.append(result)
    finally:
        if combined_writer is not None:
            combined_writer.close()

    total_messages = sum(result.messages for result in results)
    failed = [result for result in results if result.error]

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "guild_id": guild.id,
        "guild_name": guild.name,
        "since": since.isoformat(),
        "until": until.isoformat() if until else None,
        "format": args.format,
        "exclude_bots": bool(args.exclude_bots),
        "include_threads": bool(args.include_threads),
        "per_channel_limit": limit,
        "total_messages": total_messages,
        "channels": [result.as_dict() for result in results],
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("-" * 70)
    print(f"Exported {total_messages:,} message(s) from {len(results)} channel(s).")
    if combined_writer is not None:
        print(f"Combined file: {combined_path}")
    print(f"Manifest:      {manifest_path}")
    if failed:
        print(f"[WARN] {len(failed)} channel(s) finished with errors:")
        for result in failed:
            print(f"  - #{result.channel_name} ({result.channel_id}): {result.error}")

    return 0 if not failed else 1


async def _run(args: argparse.Namespace) -> int:
    intents = discord.Intents.none()
    intents.guilds = True
    intents.message_content = True

    client = discord.Client(intents=intents)
    exit_code = 0
    failure: Optional[BaseException] = None

    @client.event
    async def on_ready() -> None:
        nonlocal exit_code, failure
        try:
            exit_code = await _process(client, args)
        except BaseException as exc:  # noqa: BLE001 - re-raised after the client closes
            failure = exc
        finally:
            await client.close()

    try:
        await client.start(_load_token())
    except discord.PrivilegedIntentsRequired:
        print(
            "The bot needs the 'Message Content Intent' enabled in the Discord "
            "Developer Portal (Bot -> Privileged Gateway Intents).",
            file=sys.stderr,
        )
        return 1
    except discord.LoginFailure:
        print("Failed to log in — check DISCORD_TOKEN in .env.", file=sys.stderr)
        return 1

    if failure is not None:
        raise failure
    return exit_code


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export every message from a list of Discord channels between two dates, "
            "saving them to disk."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python scripts/export_channel_history.py --list-channels\n"
            "  python scripts/export_channel_history.py --since 2025-01-01 --channel-id 111 --channel-id 222\n"
            "  python scripts/export_channel_history.py --since 2025-06-01 --channels-file scripts/channels.txt --format csv\n"
            "  python scripts/export_channel_history.py --since 2025-01-01 --channel-name general --include-threads\n"
        ),
    )
    parser.add_argument(
        "--guild-id",
        type=int,
        default=DEFAULT_GUILD_ID,
        help=f"Discord server (guild) ID (default: {DEFAULT_GUILD_ID}).",
    )
    parser.add_argument(
        "--channel-id",
        type=int,
        action="append",
        default=[],
        help="Channel (or thread) ID to export. Repeatable.",
    )
    parser.add_argument(
        "--channel-name",
        type=str,
        action="append",
        default=[],
        help="Channel name to export, matched case-insensitively. Repeatable.",
    )
    parser.add_argument(
        "--channels-file",
        type=str,
        default=None,
        help="Text file with one channel ID per line ('#' starts a comment).",
    )
    parser.add_argument(
        "--include-threads",
        action="store_true",
        help="Also export active and archived threads under the selected channels.",
    )
    parser.add_argument(
        "--list-channels",
        action="store_true",
        help="Print the guild's channels and thread IDs, then exit without exporting.",
    )
    parser.add_argument(
        "--since",
        type=_parse_since,
        default=None,
        help="Start of the window (inclusive). YYYY-MM-DD or ISO 8601, UTC assumed.",
    )
    parser.add_argument(
        "--until",
        type=_parse_until,
        default=None,
        help="End of the window (inclusive for dates). Defaults to now.",
    )
    parser.add_argument(
        "--format",
        choices=sorted(_WRITER_TYPES),
        default="jsonl",
        help="Output format (default: jsonl). Use json for a single pretty-printed array.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"Directory to write exports into (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--exclude-bots",
        action="store_true",
        help="Skip messages sent by bots (including this bot).",
    )
    parser.add_argument(
        "--no-combined",
        action="store_true",
        help="Only write per-channel files, without the combined '_all' file.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum messages per channel (0 = no limit). Useful for a quick test run.",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nCancelled by user.", file=sys.stderr)
        return 130
    except discord.Forbidden as exc:
        print(f"Missing access: {exc}", file=sys.stderr)
        return 1
    except discord.HTTPException as exc:
        print(f"Discord API error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
