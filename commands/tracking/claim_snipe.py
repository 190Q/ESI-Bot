import json
import os
import sqlite3
import uuid as uuid_mod
import discord
from discord import app_commands
from datetime import datetime
from typing import Optional
from utils.permissions import has_roles
from utils.paths import DB_DIR
from utils.esi_points import init_points_database, save_points
from utils.parsers import (
    parse_health,
    parse_defense,
    parse_duration,
    format_health,
    format_defense,
    format_duration,
)

REQUIRED_ROLES = [
    554514823191199747,   # Archduke
    1396112289832243282,  # Grand Duke
    591765870272053261    # Duke
]

SNIPES_DB = str(DB_DIR / "claim_snipes.db")


def init_snipes_database():
    """Create the snipes table."""
    conn = sqlite3.connect(SNIPES_DB)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS snipes (
            snipe_id TEXT PRIMARY KEY,
            base_damage REAL NOT NULL,
            base_speed REAL NOT NULL,
            health INTEGER,
            defense REAL,
            duration INTEGER,
            points INTEGER NOT NULL,
            player_uuids TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
    """)
    # Backfill columns on databases that pre-date these fields
    c.execute("PRAGMA table_info(snipes)")
    existing_cols = {row[1] for row in c.fetchall()}
    for col, decl in (
        ("health", "INTEGER"),
        ("defense", "REAL"),
        ("duration", "INTEGER"),
    ):
        if col not in existing_cols:
            c.execute(f'ALTER TABLE snipes ADD COLUMN {col} {decl}')
    conn.commit()
    conn.close()


def _player_table(player_uuid):
    """Return a safe table name for a player UUID."""
    return "player_" + player_uuid.replace("-", "_")


def save_snipe(resolved_players, base_damage, base_speed, points,
               health=None, defense=None, duration=None):
    """Record a snipe and update each player's individual table."""
    player_uuids = [p["uuid"] for p in resolved_players if p.get("uuid")]
    if not player_uuids:
        return

    snipe_id = str(uuid_mod.uuid4())
    conn = sqlite3.connect(SNIPES_DB)
    c = conn.cursor()

    c.execute("""
        INSERT INTO snipes (
            snipe_id, base_damage, base_speed, health, defense, duration,
            points, player_uuids, timestamp
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        snipe_id, base_damage, base_speed, health, defense, duration,
        points, json.dumps(player_uuids), datetime.utcnow().isoformat(),
    ))

    for player in resolved_players:
        uuid = player.get("uuid")
        if not uuid:
            continue
        table = _player_table(uuid)
        c.execute('CREATE TABLE IF NOT EXISTS "{}" ('
                  'snipe_id TEXT NOT NULL, '
                  'username TEXT NOT NULL, '
                  'role TEXT NOT NULL)'.format(table))
        c.execute('INSERT INTO "{}" (snipe_id, username, role) VALUES (?, ?, ?)'.format(table),
                  (snipe_id, player["username"], player.get("role", "Unknown")))

    conn.commit()
    conn.close()

def load_username_matches():
    """Load the username_matches.json file"""
    path = os.path.join(os.path.dirname(__file__), "..", "..", "data/username_matches.json")
    with open(path, "r") as f:
        return json.load(f)


def resolve_player(user: discord.Member, matches: dict) -> Optional[dict]:
    """
    Resolve a Discord member to their Minecraft username and UUID.
    Returns a dict with keys: mention, username, uuid
    or None if not found.
    """
    entry = matches.get(str(user.id))

    if entry is None:
        return None

    if isinstance(entry, str):
        return {
            "mention": user.mention,
            "username": entry,
            "uuid": None,
        }

    return {
        "mention": user.mention,
        "username": entry.get("username", "Unknown"),
        "uuid": entry.get("uuid", None),
    }


def calculate_points(base_damage: float, base_speed: float) -> float:
    """Calculate points from damage and speed."""
    raw = base_damage * base_speed / 4.7 / 2000
    if raw >= 50:
        return base_damage * base_speed / 4.7 / 5000
    return raw


ROLE_EMOJIS = {
    "DPS": "⚔️",
    "Tank": "🛡️",
    "Healer": "❤️",
    "Solo": "🔱",
}


def build_claim_snipe_embed(players, base_damage, base_speed, requester,
                            health=None, defense=None, duration=None):
    """Build the claim snipe embed from the current player list.

    `players` is a list of dicts: {"member": discord.Member, "role": str}

    `health`, `defense`, and `duration` are informational only and do not
    affect the points calculation.
    """
    embed = discord.Embed(
        title="Claim Snipe",
        color=0x5865F2,
        timestamp=datetime.utcnow(),
    )

    points = int(round(calculate_points(base_damage, base_speed)))

    embed.add_field(name="Base Damage", value=str(int(base_damage)), inline=True)
    embed.add_field(name="Base Speed", value=str(base_speed), inline=True)
    embed.add_field(name="Points", value=f"{points}", inline=True)

    if health is not None:
        embed.add_field(name="Health", value=format_health(health), inline=True)
    if defense is not None:
        embed.add_field(name="Defense", value=format_defense(defense), inline=True)
    if duration is not None:
        embed.add_field(name="Duration", value=format_duration(duration), inline=True)

    matches = load_username_matches()
    resolved = []
    unresolved = []

    for entry in players:
        member = entry["member"]
        role = entry["role"]
        data = resolve_player(member, matches)
        if data is None:
            unresolved.append(member.mention)
        else:
            data["role"] = role
            resolved.append(data)

    player_lines = []
    for i, player in enumerate(resolved, start=1):
        uuid_display = f"{player['uuid']}" if player["uuid"] else "*No UUID*"
        role_emoji = ROLE_EMOJIS.get(player["role"], "")
        player_lines.append(
            f"**{i}.** {role_emoji} **[{player['role']}]** {player['mention']} - {player['username']} ({uuid_display})"
        )

    embed.add_field(
        name=f"Players ({len(resolved)})",
        value="\n".join(player_lines) if player_lines else "*No players added yet*",
        inline=False,
    )

    if unresolved:
        embed.add_field(
            name="Not Found in username_matches.json",
            value="\n".join(unresolved),
            inline=False,
        )
        embed.color = 0xFFA500

    return embed


# ---------------------------------------------------------------------------
# Views & selects
# ---------------------------------------------------------------------------

class AddPlayerView(discord.ui.View):
    """Ephemeral view with user-select and role buttons in one message."""

    def __init__(self, parent_view: "ClaimSnipeView"):
        super().__init__(timeout=60)
        self.parent_view = parent_view
        self.selected_member: Optional[discord.Member] = None

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select a player to add…", min_values=1, max_values=1, row=0)
    async def user_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        member = select.values[0]
        if any(p["member"].id == member.id for p in self.parent_view.players):
            await interaction.response.send_message(
                f"{member.mention} is already in the player list.", ephemeral=True
            )
            return
        if len(self.parent_view.players) >= 5:
            await interaction.response.send_message(
                "Maximum of 5 players reached.", ephemeral=True
            )
            return

        self.selected_member = member
        # Enable the role buttons
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = False
        await interaction.response.edit_message(
            content=f"Selected {member.mention} - now pick a role:", view=self
        )

    @discord.ui.button(label="DPS", style=discord.ButtonStyle.danger, emoji="⚔️", row=1, disabled=True)
    async def dps(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._add_with_role(interaction, "DPS")

    @discord.ui.button(label="Tank", style=discord.ButtonStyle.primary, emoji="🛡️", row=1, disabled=True)
    async def tank(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._add_with_role(interaction, "Tank")

    @discord.ui.button(label="Healer", style=discord.ButtonStyle.success, emoji="❤️", row=1, disabled=True)
    async def healer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._add_with_role(interaction, "Healer")

    @discord.ui.button(label="Solo", style=discord.ButtonStyle.secondary, emoji="🔱", row=1, disabled=True)
    async def solo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._add_with_role(interaction, "Solo")

    async def _add_with_role(self, interaction: discord.Interaction, role: str):
        if self.selected_member is None:
            return
        # Double-check in case of race conditions
        if any(p["member"].id == self.selected_member.id for p in self.parent_view.players):
            await interaction.response.edit_message(
                content=f"{self.selected_member.mention} is already in the player list.", view=None
            )
            return
        if len(self.parent_view.players) >= 5:
            await interaction.response.edit_message(
                content="Maximum of 5 players reached.", view=None
            )
            return

        self.parent_view.players.append({"member": self.selected_member, "role": role})
        embed = build_claim_snipe_embed(
            self.parent_view.players,
            self.parent_view.base_damage,
            self.parent_view.base_speed,
            self.parent_view.requester,
            health=self.parent_view.health,
            defense=self.parent_view.defense,
            duration=self.parent_view.duration,
        )
        await self.parent_view.message.edit(embed=embed, view=self.parent_view)
        await interaction.response.edit_message(
            content=f"Added {self.selected_member.mention} as **{role}**.", view=None
        )
        self.stop()


class RemovePlayerSelect(discord.ui.Select):
    """String-select menu listing current players for removal."""

    def __init__(self, parent_view: "ClaimSnipeView"):
        options = [
            discord.SelectOption(
                label=f"{entry['member'].display_name} [{entry['role']}]",
                value=str(entry["member"].id),
            )
            for entry in parent_view.players
        ]
        super().__init__(placeholder="Select a player to remove…", options=options, min_values=1, max_values=1)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        user_id = int(self.values[0])
        self.parent_view.players = [
            p for p in self.parent_view.players if p["member"].id != user_id
        ]
        embed = build_claim_snipe_embed(
            self.parent_view.players,
            self.parent_view.base_damage,
            self.parent_view.base_speed,
            self.parent_view.requester,
            health=self.parent_view.health,
            defense=self.parent_view.defense,
            duration=self.parent_view.duration,
        )
        await self.parent_view.message.edit(embed=embed, view=self.parent_view)
        await interaction.response.send_message("Player removed.", ephemeral=True)


class RemovePlayerView(discord.ui.View):
    """Ephemeral view containing the string-select for removing a player."""

    def __init__(self, parent_view: "ClaimSnipeView"):
        super().__init__(timeout=60)
        self.add_item(RemovePlayerSelect(parent_view))


class EditPlayerView(discord.ui.View):
    """Ephemeral view that combines all edit steps in one message.

    Step 1: Pick the player to replace (StringSelect, row 0).
    Step 2: Pick the replacement user (UserSelect, row 1) - starts hidden/disabled.
    Step 3: Pick the role (buttons, row 2) - starts disabled.
    """

    def __init__(self, parent_view: "ClaimSnipeView"):
        super().__init__(timeout=60)
        self.parent_view = parent_view
        self.old_player_id: Optional[int] = None
        self.new_member: Optional[discord.Member] = None

        # Row 0 - pick which player to replace
        pick_select = discord.ui.Select(
            placeholder="Select the player to replace…",
            options=[
                discord.SelectOption(
                    label=f"{entry['member'].display_name} [{entry['role']}]",
                    value=str(entry["member"].id),
                )
                for entry in parent_view.players
            ],
            min_values=1,
            max_values=1,
            row=0,
        )
        pick_select.callback = self._on_pick_player
        self.pick_select = pick_select
        self.add_item(pick_select)

    async def _on_pick_player(self, interaction: discord.Interaction):
        self.old_player_id = int(self.pick_select.values[0])
        # Disable the pick select and add the user-select for replacement
        self.pick_select.disabled = True
        self.add_item(self._make_user_select())
        await interaction.response.edit_message(
            content="Now select the replacement player:", view=self
        )

    def _make_user_select(self) -> discord.ui.UserSelect:
        select = discord.ui.UserSelect(
            placeholder="Select replacement player…",
            min_values=1,
            max_values=1,
            row=1,
        )
        select.callback = self._on_pick_replacement
        return select

    async def _on_pick_replacement(self, interaction: discord.Interaction):
        new_member = interaction.data["resolved"]["users"]
        # interaction.data resolved users is a dict; get the first
        member_id = list(new_member.keys())[0]
        self.new_member = interaction.guild.get_member(int(member_id))
        if self.new_member is None:
            await interaction.response.edit_message(
                content="Could not resolve that member.", view=None
            )
            return

        if self.new_member.id != self.old_player_id and any(
            p["member"].id == self.new_member.id for p in self.parent_view.players
        ):
            await interaction.response.edit_message(
                content=f"{self.new_member.mention} is already in the player list.", view=None
            )
            return

        # Enable role buttons
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = False
        await interaction.response.edit_message(
            content=f"Selected {self.new_member.mention} - now pick a role:", view=self
        )

    @discord.ui.button(label="DPS", style=discord.ButtonStyle.danger, emoji="⚔️", row=2, disabled=True)
    async def dps(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._replace_with_role(interaction, "DPS")

    @discord.ui.button(label="Tank", style=discord.ButtonStyle.primary, emoji="🛡️", row=2, disabled=True)
    async def tank(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._replace_with_role(interaction, "Tank")

    @discord.ui.button(label="Healer", style=discord.ButtonStyle.success, emoji="❤️", row=2, disabled=True)
    async def healer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._replace_with_role(interaction, "Healer")

    @discord.ui.button(label="Solo", style=discord.ButtonStyle.secondary, emoji="🔱", row=2, disabled=True)
    async def solo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._replace_with_role(interaction, "Solo")

    async def _replace_with_role(self, interaction: discord.Interaction, role: str):
        if self.new_member is None or self.old_player_id is None:
            return

        for i, p in enumerate(self.parent_view.players):
            if p["member"].id == self.old_player_id:
                self.parent_view.players[i] = {"member": self.new_member, "role": role}
                break

        embed = build_claim_snipe_embed(
            self.parent_view.players,
            self.parent_view.base_damage,
            self.parent_view.base_speed,
            self.parent_view.requester,
            health=self.parent_view.health,
            defense=self.parent_view.defense,
            duration=self.parent_view.duration,
        )
        await self.parent_view.message.edit(embed=embed, view=self.parent_view)
        await interaction.response.edit_message(
            content=f"Replaced with {self.new_member.mention} as **{role}**.", view=None
        )
        self.stop()


class ClaimSnipeView(discord.ui.View):
    """Main persistent view with Add / Remove / Edit buttons."""

    def __init__(
        self,
        base_damage: float,
        base_speed: float,
        requester: discord.Member,
        health: Optional[int] = None,
        defense: Optional[float] = None,
        duration: Optional[int] = None,
    ):
        super().__init__(timeout=None)
        self.players: list[dict] = []  # [{"member": discord.Member, "role": str}]
        self.base_damage = base_damage
        self.base_speed = base_speed
        self.health = health
        self.defense = defense
        self.duration = duration
        self.requester = requester
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                "Only the command user can manage this claim snipe.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Add Player", style=discord.ButtonStyle.success, emoji="➕")
    async def add_player(self, interaction: discord.Interaction, button: discord.ui.Button):
        if len(self.players) >= 5:
            await interaction.response.send_message(
                "Maximum of 5 players reached.", ephemeral=True
            )
            return
        view = AddPlayerView(self)
        await interaction.response.send_message(
            "Select a player to add:", view=view, ephemeral=True
        )

    @discord.ui.button(label="Remove Player", style=discord.ButtonStyle.danger, emoji="➖")
    async def remove_player(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.players:
            await interaction.response.send_message(
                "No players to remove.", ephemeral=True
            )
            return
        view = RemovePlayerView(self)
        await interaction.response.send_message(
            "Select a player to remove:", view=view, ephemeral=True
        )

    @discord.ui.button(label="Edit Player", style=discord.ButtonStyle.primary, emoji="✏️")
    async def edit_player(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.players:
            await interaction.response.send_message(
                "No players to edit.", ephemeral=True
            )
            return
        view = EditPlayerView(self)
        await interaction.response.send_message(
            "Select the player to replace:", view=view, ephemeral=True
        )

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.players:
            await interaction.response.send_message(
                "Add at least one player before confirming.", ephemeral=True
            )
            return

        # Build the final embed
        embed = build_claim_snipe_embed(
            self.players,
            self.base_damage,
            self.base_speed,
            self.requester,
            health=self.health,
            defense=self.defense,
            duration=self.duration,
        )
        embed.title = "✅ Claim Snipe - Confirmed"
        embed.color = 0x57F287

        # Save to databases
        points = int(round(calculate_points(self.base_damage, self.base_speed)))
        matches = load_username_matches()
        resolved = []
        for entry in self.players:
            data = resolve_player(entry["member"], matches)
            if data:
                data["role"] = entry["role"]
                resolved.append(data)
        try:
            save_points(resolved, points, reason="Claim Snipe")
            save_snipe(
                resolved,
                self.base_damage,
                self.base_speed,
                points,
                health=self.health,
                defense=self.defense,
                duration=self.duration,
            )
        except Exception as e:
            print(f"[claim_snipe] Failed to save data: {e}")

        # Disable all buttons on the original ephemeral message
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)

        # Send the final team publicly
        await interaction.channel.send(embed=embed)
        self.stop()

        print(
            f"[claim_snipe] {interaction.user} confirmed - "
            f"players={[p['member'].display_name for p in self.players]}"
        )


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""

    init_points_database()
    init_snipes_database()

    @bot.tree.command(
        name="claim_snipe",
        description="Plan a claim snipe - manage players interactively via buttons",
    )
    @app_commands.describe(
        base_damage="Base damage value for the team (highest damage from the given range)",
        base_speed="Base speed value for the team (attack speed)",
        health="Target health (e.g. 20M, 100K, 1.5M, 5000) - informational only",
        defense="Target defense as a percentage (e.g. 50%, 37.5) - informational only",
        duration="Fight duration (e.g. 4m20s, 410s, 5 minutes, 1h30m) - informational only",
    )
    async def claim_snipe(
        interaction: discord.Interaction,
        base_damage: float,
        base_speed: float,
        health: Optional[str] = None,
        defense: Optional[str] = None,
        duration: Optional[str] = None,
    ):
        # Check permissions if required
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

        # Parse the optional informational fields
        try:
            health_val = parse_health(health) if health is not None else None
            defense_val = parse_defense(defense) if defense is not None else None
            duration_val = parse_duration(duration) if duration is not None else None
        except ValueError as e:
            error_embed = discord.Embed(
                title="Invalid input",
                description=str(e),
                color=0xFF0000,
                timestamp=datetime.utcnow(),
            )
            await interaction.response.send_message(
                embed=error_embed, ephemeral=True
            )
            return

        view = ClaimSnipeView(
            base_damage,
            base_speed,
            interaction.user,
            health=health_val,
            defense=defense_val,
            duration=duration_val,
        )
        embed = build_claim_snipe_embed(
            [],
            base_damage,
            base_speed,
            interaction.user,
            health=health_val,
            defense=defense_val,
            duration=duration_val,
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

        print(
            f"[claim_snipe] {interaction.user} - damage={base_damage}, speed={base_speed}, "
            f"health={health_val}, defense={defense_val}, duration={duration_val} "
            f"(interactive session started)"
        )

    print("[OK] Loaded claim_snipe command")