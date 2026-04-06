import discord
from discord import app_commands
from datetime import datetime
import os
import json
from typing import Optional, List, Tuple
import re
from utils.permissions import has_roles

REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0,
    954566591520063510, # Juror
    600185623474601995, # Parliament
]

# Path to the username ↔ user_id match database (shared with accept.py)
USERNAME_MATCH_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data/username_matches.json",
)


def _load_username_match_db() -> dict:
    """Load the username match DB from disk.

    Returns an empty dict if the file does not exist or is invalid.
    """
    try:
        with open(USERNAME_MATCH_DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        # Corrupt or empty file – start fresh rather than breaking the command
        return {}
    except Exception as e:
        print(f"[WARN] Failed to load username match DB: {e}")
    return {}


def _write_username_match_db(db: dict) -> None:
    """Persist the whole DB back to disk."""
    try:
        with open(USERNAME_MATCH_DB_PATH, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] Failed to write username match DB: {e}")
        raise


def set_username_match(user_id: int, username: str) -> None:
    """Set/overwrite the mapping of a Discord user ID to a username."""
    db = _load_username_match_db()
    db[str(user_id)] = username
    _write_username_match_db(db)


def delete_username_match(user_id: int) -> Optional[str]:
    """Delete the mapping for a given user ID.

    Returns the removed username, or None if no mapping existed.
    """
    db = _load_username_match_db()
    key = str(user_id)
    if key not in db:
        return None
    removed = db.pop(key)
    _write_username_match_db(db)
    return removed


class UsernameMatchesView(discord.ui.View):
    """Simple paginator for listing username matches."""

    def __init__(self, entries: List[Tuple[int, str]], per_page: int = 10, author_id: Optional[int] = None):
        super().__init__(timeout=None)
        self.entries = entries
        self.per_page = max(1, per_page)
        self.author_id = author_id
        self.page = 0
        self.total_pages = max(1, (len(entries) + self.per_page - 1) // self.per_page)

    def _build_embed(self) -> discord.Embed:
        start_index = self.page * self.per_page
        end_index = start_index + self.per_page
        page_entries = self.entries[start_index:end_index]

        if page_entries:
            lines = [
                f"<@{user_id}> (`{user_id}`) → `{username}`"
                for user_id, username in page_entries
            ]
            description = "\n".join(lines)
        else:
            description = "No matches on this page."

        embed = discord.Embed(
            title="Username Matches",
            description=description,
            color=0x00AAFF,
            timestamp=datetime.utcnow(),
        )
        embed.set_footer(
            text=f"Page {self.page + 1}/{self.total_pages} • Total matches: {len(self.entries)}"
        )
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only allow the original invoker to use the paginator controls."""
        if self.author_id is not None and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "You cannot control this paginator.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        if self.page < self.total_pages - 1:
            self.page += 1
        await interaction.response.edit_message(embed=self._build_embed(), view=self)


def setup(bot, has_required_role, config):
    """Setup function for bot integration"""

    @bot.tree.command(
        name="linked_users",
        description="List all stored username matches",
    )
    async def username_matches(interaction: discord.Interaction):
        """List all stored username matches with simple pagination."""

        # Permission check (reuse same gate as username_match)
        if isinstance(interaction.user, discord.Member):
            if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
                missing_roles_embed = discord.Embed(
                    title="Permission Denied",
                    description="You don't have permission to use this command!",
                    color=0xFF0000,
                    timestamp=datetime.utcnow(),
                )
                await interaction.response.send_message(
                    embed=missing_roles_embed, ephemeral=True
                )
                return

        db = _load_username_match_db()

        if not db:
            empty_embed = discord.Embed(
                title="No Matches",
                description="There are currently no stored username matches.",
                color=0xFFFF00,
                timestamp=datetime.utcnow(),
            )
            await interaction.response.send_message(
                embed=empty_embed, ephemeral=True
            )
            return

        # Convert to a sorted list of (user_id, username) for consistent paging
        entries: List[Tuple[int, str]] = []
        for key, username in db.items():
            try:
                user_id = int(key)
            except (TypeError, ValueError):
                # Skip invalid keys rather than breaking the command
                continue
            entries.append((user_id, str(username)))

        if not entries:
            empty_embed = discord.Embed(
                title="No Valid Matches",
                description="The match database exists but contains no valid entries.",
                color=0xFFFF00,
                timestamp=datetime.utcnow(),
            )
            await interaction.response.send_message(
                embed=empty_embed, ephemeral=True
            )
            return

        # Sort by username (case-insensitive) then by user_id for stability
        entries.sort(key=lambda pair: (pair[1].lower(), pair[0]))

        view = UsernameMatchesView(entries, per_page=10, author_id=interaction.user.id)
        first_embed = view._build_embed()

        await interaction.response.send_message(
            embed=first_embed,
            view=view,
            ephemeral=True,
        )

    print("[OK] Loaded username_match command")
