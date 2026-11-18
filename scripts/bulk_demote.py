#!/usr/bin/env python3
"""Preview and apply Discord demotions for linked Minecraft usernames."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import discord
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from commands.members.demote import (
    TARGET_RANKS,
    apply_demotion,
    calculate_demotion,
    get_current_rank,
    get_demotion_error,
    get_demotion_targets,
)
from utils.inactivity import _load_username_index

DEFAULT_GUILD_ID = 554418045397762048


@dataclass
class DemotionPreview:
    username: str
    member: discord.Member
    current_rank: str
    target_rank: str
    roles_to_remove: list[int]
    roles_to_add: list[int]
    new_nickname: str | None


def _split_usernames(text: str) -> list[str]:
    return text.replace(",", " ").split()


def _collect_usernames(args: argparse.Namespace) -> list[str]:
    usernames = []
    for value in args.usernames:
        usernames.extend(_split_usernames(value))
    if args.usernames_file:
        for line in args.usernames_file.read_text(encoding="utf-8-sig").splitlines():
            usernames.extend(_split_usernames(line.split("#", 1)[0]))
    if not usernames:
        print("Enter Minecraft usernames, one or more per line. Submit an empty line to finish.")
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                break
            if not line:
                break
            usernames.extend(_split_usernames(line))
    return list(dict.fromkeys(username.lower() for username in usernames))


def _username_links() -> dict[str, list[int]]:
    links: dict[str, list[int]] = {}
    for discord_id, (username, _) in _load_username_index().items():
        if not isinstance(username, str) or not username.strip():
            continue
        try:
            user_id = int(discord_id)
        except (TypeError, ValueError):
            continue
        links.setdefault(username.strip().lower(), []).append(user_id)
    return links


async def _choose_rank(username: str, current_rank: str, targets: list[str]) -> str | None:
    print(f"\n{username}: current rank {current_rank}")
    for index, rank in enumerate(targets, 1):
        print(f"  {index}. {rank.title()}")
    while True:
        choice = (await asyncio.to_thread(input, "Choose a rank (or 'skip'): ")).strip().lower()
        if choice == "skip":
            return None
        if choice.isdecimal() and 1 <= int(choice) <= len(targets):
            return targets[int(choice) - 1]
        if choice in targets:
            return choice
        print("Choose one of the listed ranks or enter 'skip'.")


def _print_preview(preview: DemotionPreview, guild: discord.Guild) -> None:
    def role_names(role_ids: list[int]) -> str:
        names = []
        for role_id in role_ids:
            role = guild.get_role(role_id)
            names.append(f"{role.name} ({role_id})" if role else f"missing role ({role_id})")
        return ", ".join(names) if names else "none"

    member = preview.member
    print(f"\n{preview.username} -> {member} | {member.display_name} | Discord ID {member.id}")
    print(f"  Rank: {preview.current_rank} -> {preview.target_rank.title()}")
    print(f"  Remove: {role_names(preview.roles_to_remove)}")
    print(f"  Add: {role_names(preview.roles_to_add)}")
    if preview.new_nickname:
        print(f"  Nickname: {member.display_name} -> {preview.new_nickname}")


async def _process(client: discord.Client, args: argparse.Namespace, usernames: list[str]) -> int:
    guild = client.get_guild(args.guild_id)
    if guild is None:
        print(f"Guild {args.guild_id} was not found in the bot's servers.", file=sys.stderr)
        return 1

    links = _username_links()
    if not links:
        print("No valid username links found in data/username_matches.json.", file=sys.stderr)
        return 1

    print(f"Connected as {client.user}; server: {guild.name} ({guild.id}).")
    print("The bot account will be recorded as the executor for these demotions.")
    previews: list[DemotionPreview] = []
    seen_ids: set[int] = set()
    failures = 0

    for username in usernames:
        matches = links.get(username, [])
        if not matches:
            print(f"[SKIP] {username}: no linked Discord user.")
            failures += 1
            continue
        if len(matches) != 1:
            print(f"[SKIP] {username}: multiple linked Discord users ({', '.join(map(str, matches))}).")
            failures += 1
            continue
        user_id = matches[0]
        if user_id in seen_ids:
            print(f"[SKIP] {username}: Discord user {user_id} was already included.")
            failures += 1
            continue
        seen_ids.add(user_id)
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            print(f"[SKIP] {username}: linked Discord user {user_id} is not in this server.")
            failures += 1
            continue
        except discord.HTTPException as exc:
            print(f"[ERROR] {username}: could not fetch Discord member: {exc}")
            failures += 1
            continue

        error = get_demotion_error(member)
        if error:
            print(f"[SKIP] {username}: {error.replace(chr(10), ' ').replace('**', '')}")
            failures += 1
            continue

        current_rank = get_current_rank(member)
        targets = get_demotion_targets(current_rank)
        if not targets:
            print(f"[SKIP] {username}: no valid demotion targets for {current_rank}.")
            failures += 1
            continue
        target_rank = args.rank
        if target_rank is None:
            target_rank = await _choose_rank(username, current_rank, targets)
            if target_rank is None:
                print(f"[SKIP] {username}: skipped by operator.")
                continue
        if target_rank not in targets:
            print(f"[SKIP] {username}: {target_rank} is not a valid demotion from {current_rank}.")
            failures += 1
            continue

        roles_to_remove, roles_to_add, new_nickname = calculate_demotion(member, current_rank, target_rank)
        missing_roles = [role_id for role_id in roles_to_add if guild.get_role(role_id) is None]
        if missing_roles:
            print(f"[SKIP] {username}: target roles missing from server: {missing_roles}.")
            failures += 1
            continue
        preview = DemotionPreview(
            username, member, current_rank, target_rank, roles_to_remove, roles_to_add, new_nickname
        )
        previews.append(preview)
        _print_preview(preview, guild)

    if not previews:
        print("\nNo demotions to apply.")
        return 1 if failures else 0
    if args.dry_run:
        print(f"\nDry run: {len(previews)} demotion(s) previewed; no changes made.")
        return 1 if failures else 0
    if not args.yes:
        answer = await asyncio.to_thread(input, f"\nApply {len(previews)} demotion(s)? [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            print("Cancelled; no changes made.")
            return 1 if failures else 0

    succeeded = 0
    for preview in previews:
        try:
            if _username_links().get(preview.username) != [preview.member.id]:
                print(f"[SKIP] {preview.username}: username link changed since preview; run again.")
                failures += 1
                continue
            member = await guild.fetch_member(preview.member.id)
            before_roles = {role.id for role in preview.member.roles}
            after_roles = {role.id for role in member.roles}
            if before_roles != after_roles or preview.member.display_name != member.display_name:
                print(f"[SKIP] {preview.username}: roles or nickname changed since preview; run again.")
                failures += 1
                continue
            if any(guild.get_role(role_id) is None for role_id in preview.roles_to_add):
                print(f"[SKIP] {preview.username}: target role disappeared since preview; run again.")
                failures += 1
                continue
            await apply_demotion(
                member, guild, preview.roles_to_remove, preview.roles_to_add,
                preview.new_nickname, client.user,
            )
            succeeded += 1
            print(f"[OK] {preview.username}: demoted to {preview.target_rank.title()}.")
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] {preview.username}: {exc}. Check the member for partial changes.")
            failures += 1

    print(f"\nCompleted {succeeded} demotion(s); {failures} failure(s).")
    return 1 if failures else 0


async def _run(args: argparse.Namespace, usernames: list[str]) -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("DISCORD_TOKEN not found in the environment or project .env.", file=sys.stderr)
        return 1

    intents = discord.Intents.none()
    intents.guilds = True
    client = discord.Client(intents=intents)
    result = 1
    failure = None
    started = False

    @client.event
    async def on_ready() -> None:
        nonlocal result, failure, started
        if started:
            return
        started = True
        try:
            result = await _process(client, args, usernames)
        except Exception as exc:  # noqa: BLE001
            failure = exc
        finally:
            await client.close()

    try:
        await client.start(token)
    except discord.LoginFailure:
        print("Failed to log in; check DISCORD_TOKEN.", file=sys.stderr)
        return 1
    if failure:
        raise failure
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Demote linked Minecraft users with the same rules as /demote.",
    )
    parser.add_argument("usernames", nargs="*", help="Minecraft usernames, separated by spaces or commas.")
    parser.add_argument("--usernames-file", type=Path, help="Text file with Minecraft usernames.")
    parser.add_argument("--rank", type=str.lower, choices=TARGET_RANKS, help="Target rank for all users.")
    parser.add_argument("--guild-id", type=int, default=DEFAULT_GUILD_ID, help="Discord server ID.")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without applying them.")
    parser.add_argument("--yes", action="store_true", help="Apply the preview without prompting for confirmation.")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        usernames = _collect_usernames(args)
        if not usernames:
            print("No usernames provided.", file=sys.stderr)
            return 1
        return asyncio.run(_run(args, usernames))
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        return 1
    except (OSError, discord.DiscordException) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
