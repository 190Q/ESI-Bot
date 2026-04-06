import json
import os
import discord
from discord import app_commands
from datetime import datetime
from utils.permissions import has_roles

REQUIRED_ROLES = []


def load_username_matches():
    """Load the username_matches.json file"""
    path = os.path.join(os.path.dirname(__file__), "..", "..", "data/username_matches.json")
    with open(path, "r") as f:
        return json.load(f)


def resolve_player(user: discord.Member, matches: dict) -> dict | None:
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


ROLE_EMOJIS = {
    "DPS": "⚔️",
    "Tank": "🛡️",
    "Healer": "❤️",
    "Solo": "🔱",
}


def calculate_points(base_damage: float, base_speed: float) -> float:
    """Calculate points from damage and speed."""
    raw = base_damage * base_speed / 4.7 / 2000
    if raw >= 50:
        return base_damage * base_speed / 4.7 / 5000
    return raw


def build_claim_snipe_embed(players, base_damage, base_speed, requester):
    """Build the claim snipe embed from the current player list.

    `players` is a list of dicts: {"member": discord.Member, "role": str}
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
            f"**{i}.** {role_emoji} **[{player['role']}]** {player['mention']} — {player['username']} ({uuid_display})"
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
        self.selected_member: discord.Member | None = None

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
            content=f"Selected {member.mention} — now pick a role:", view=self
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
    Step 2: Pick the replacement user (UserSelect, row 1) — starts hidden/disabled.
    Step 3: Pick the role (buttons, row 2) — starts disabled.
    """

    def __init__(self, parent_view: "ClaimSnipeView"):
        super().__init__(timeout=60)
        self.parent_view = parent_view
        self.old_player_id: int | None = None
        self.new_member: discord.Member | None = None

        # Row 0 — pick which player to replace
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
            content=f"Selected {self.new_member.mention} — now pick a role:", view=self
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
        )
        await self.parent_view.message.edit(embed=embed, view=self.parent_view)
        await interaction.response.edit_message(
            content=f"Replaced with {self.new_member.mention} as **{role}**.", view=None
        )
        self.stop()


class ClaimSnipeView(discord.ui.View):
    """Main persistent view with Add / Remove / Edit buttons."""

    def __init__(self, base_damage: float, base_speed: float, requester: discord.Member):
        super().__init__(timeout=None)
        self.players: list[dict] = []  # [{"member": discord.Member, "role": str}]
        self.base_damage = base_damage
        self.base_speed = base_speed
        self.requester = requester
        self.message: discord.Message | None = None

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
            self.players, self.base_damage, self.base_speed, self.requester
        )
        embed.title = "✅ Claim Snipe — Confirmed"
        embed.color = 0x57F287

        # Disable all buttons on the original ephemeral message
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)

        # Send the final team publicly
        await interaction.channel.send(embed=embed)
        self.stop()

        print(
            f"[claim_snipe] {interaction.user} confirmed — "
            f"players={[p['member'].display_name for p in self.players]}"
        )


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""

    @bot.tree.command(
        name="claim_snipe",
        description="Plan a claim snipe — manage players interactively via buttons",
    )
    @app_commands.describe(
        base_damage="Base damage value for the team (highest damage from the given range)",
        base_speed="Base speed value for the team (attack speed)",
    )
    async def claim_snipe(
        interaction: discord.Interaction,
        base_damage: float,
        base_speed: float,
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

        view = ClaimSnipeView(base_damage, base_speed, interaction.user)
        embed = build_claim_snipe_embed([], base_damage, base_speed, interaction.user)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

        print(
            f"[claim_snipe] {interaction.user} — damage={base_damage}, speed={base_speed} "
            f"(interactive session started)"
        )

    print("[OK] Loaded claim_snipe command")
