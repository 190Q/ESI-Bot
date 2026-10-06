import discord

from utils import errors
from utils.nitro_roles import (
    COLOUR_COMMAND_NAME,
    HOLOGRAPHIC_COLOURS,
    LINK_PERMISSION_ROLE_IDS,
    apply_colours,
    fetch_role_colours,
    find_colour_role,
    format_colour,
    guild_has_enhanced_colours,
    link_entry,
    load_alert_config,
    load_store,
    notify_watched_colours,
    parse_hex_colour,
    random_colour,
    save_store,
)
from utils.permissions import has_roles


async def _owned_by(interaction: discord.Interaction, author_id: int) -> bool:
    """The message is ephemeral, but never act on someone else's controls."""
    if interaction.user.id != author_id:
        await errors.send_custom_error(
            interaction,
            "Not Your Menu",
            "Run the command yourself to use these controls.",
        )
        return False
    return True


def _chooser_embed(role, enhanced: bool, *, is_staff: bool, boosting: bool) -> discord.Embed:
    """The ephemeral style picker."""
    if role is None:
        embed = discord.Embed(
            title="Nitro Colour Role",
            description="No colour role is linked to your account yet.",
            color=discord.Colour.blurple(),
        )
        embed.add_field(
            name="What now?",
            value=(
                "Boost the server and one is created for you automatically."
                if not boosting
                else "You're boosting, so a staff member can link your role for you."
            ),
            inline=False,
        )
    else:
        embed = discord.Embed(
            title="Nitro Colour Role",
            description=(
                f"Choose how you want {role.mention} to look.\n\n"
                "**Normal**: one solid colour\n"
                "**Gradient**: a blend of two colours\n"
                "**Holographic**: Discord's shimmer preset"
            ),
            color=role.colour,
        )

    if not enhanced:
        embed.add_field(
            name="Gradient and Holographic are locked",
            value=(
                "This server hasn't reached **Boost Level 2** yet, so those styles "
                "aren't available. You can still pick **Normal**."
            ),
            inline=False,
        )

    if is_staff:
        embed.add_field(
            name="Staff tools",
            value=(
                "Use **Link a Nitro role** to attach an existing role to a member "
                "when it can't be matched to them automatically."
            ),
            inline=False,
        )

    embed.set_footer(text="Only you can see this message")
    return embed


def _success_embed(
    role: discord.Role,
    primary: int,
    secondary,
    tertiary,
) -> discord.Embed:
    embed = discord.Embed(
        title="Colour Role Updated",
        description=f"Your role {role.mention} has been recoloured.",
        color=discord.Colour(primary),
    )
    embed.add_field(name="Primary", value=f"`{format_colour(primary)}`", inline=True)
    if secondary is not None:
        embed.add_field(name="Secondary", value=f"`{format_colour(secondary)}`", inline=True)
    if tertiary is not None:
        embed.add_field(name="Tertiary", value=f"`{format_colour(tertiary)}`", inline=True)
    return embed


class _ColourModal(discord.ui.Modal):
    """Applies the submitted colours to the member's own colour role."""

    def __init__(self, role: discord.Role, *, title: str, previous=None):
        super().__init__(title=title)
        self.role = role
        self.previous = previous

    def _read_colours(self):
        """Return ``(primary, secondary, tertiary)``; raise ValueError on bad input."""
        raise NotImplementedError

    async def on_submit(self, interaction: discord.Interaction):
        try:
            primary, secondary, tertiary = self._read_colours()
        except ValueError as exc:
            await errors.send_custom_error(
                interaction,
                "Invalid Colour",
                f"`{exc.args[0]}` is not a valid hex colour.",
                steps=["Use six hex digits, for example `#FF8800` or `FF8800`."],
            )
            return

        try:
            await apply_colours(
                interaction.client,
                interaction.guild,
                self.role,
                primary,
                secondary,
                tertiary,
                reason=f"Colour role set by {interaction.user}",
            )
        except discord.HTTPException as exc:
            await errors.send_custom_error(
                interaction,
                "Could Not Update Role",
                f"Discord rejected the colour change: `{exc}`",
            )
            return

        await interaction.response.send_message(
            embed=_success_embed(self.role, primary, secondary, tertiary),
            ephemeral=True,
        )

        previous_primary, previous_secondary = self.previous or (None, None)
        if primary == previous_primary and secondary == previous_secondary:
            return
        try:
            await notify_watched_colours(
                interaction.client,
                interaction.user,
                self.role,
                (
                    ("Primary", primary),
                    ("Secondary", secondary),
                    ("Tertiary", tertiary),
                ),
            )
        except Exception as exc:
            print(f"[Nitro Colour] Watched-colour alert failed: {exc}")


class NormalColourModal(_ColourModal):
    colour = discord.ui.TextInput(
        label="Colour",
        placeholder="#FF8800",
        min_length=6,
        max_length=8,
        required=True,
    )

    def __init__(self, role: discord.Role, primary: int, secondary=None):
        super().__init__(role, title="Solid Role Colour", previous=(primary, secondary))
        self.colour.default = format_colour(primary)

    def _read_colours(self):
        # Secondary and tertiary are cleared so a previous gradient goes away.
        return parse_hex_colour(self.colour.value), None, None


class GradientColourModal(_ColourModal):
    primary = discord.ui.TextInput(
        label="Primary colour",
        placeholder="#FF8800",
        min_length=6,
        max_length=8,
        required=True,
    )
    secondary = discord.ui.TextInput(
        label="Secondary colour",
        placeholder="#0088FF",
        min_length=6,
        max_length=8,
        required=True,
    )

    def __init__(self, role: discord.Role, primary: int, secondary):
        super().__init__(role, title="Role Gradient", previous=(primary, secondary))
        self.primary.default = format_colour(primary)
        self.secondary.default = format_colour(
            secondary if secondary is not None else random_colour()
        )

    def _read_colours(self):
        return (
            parse_hex_colour(self.primary.value),
            parse_hex_colour(self.secondary.value),
            None,
        )


class ColourTypeView(discord.ui.View):
    """Ephemeral controls for the three colour styles, plus the staff link tool."""

    def __init__(self, bot, role, author_id: int, enhanced: bool, is_staff: bool):
        super().__init__(timeout=300)
        self.bot = bot
        self.role = role
        self.author_id = author_id

        if role is not None:
            normal = discord.ui.Button(
                label="Normal",
                style=discord.ButtonStyle.primary
            )
            normal.callback = self._normal
            self.add_item(normal)

            gradient = discord.ui.Button(
                label="Gradient",
                style=discord.ButtonStyle.primary,
                disabled=not enhanced,
            )
            gradient.callback = self._gradient
            self.add_item(gradient)

            holographic = discord.ui.Button(
                label="Holographic",
                style=discord.ButtonStyle.primary,
                disabled=not enhanced,
            )
            holographic.callback = self._holographic
            self.add_item(holographic)

        if is_staff:
            link = discord.ui.Button(
                label="Link a Nitro role",
                style=discord.ButtonStyle.secondary,
            )
            link.callback = self._link
            self.add_item(link)

    async def _normal(self, interaction: discord.Interaction):
        if not await _owned_by(interaction, self.author_id):
            return
        primary, secondary = await fetch_role_colours(self.bot, interaction.guild, self.role)
        await interaction.response.send_modal(
            NormalColourModal(self.role, primary, secondary)
        )

    async def _gradient(self, interaction: discord.Interaction):
        if not await _owned_by(interaction, self.author_id):
            return
        primary, secondary = await fetch_role_colours(self.bot, interaction.guild, self.role)
        await interaction.response.send_modal(
            GradientColourModal(self.role, primary, secondary)
        )

    async def _link(self, interaction: discord.Interaction):
        if not await _owned_by(interaction, self.author_id):
            return
        view = LinkRoleView(self.author_id)
        await interaction.response.edit_message(embed=view.embed_for(interaction.guild), view=view)

    async def _holographic(self, interaction: discord.Interaction):
        if not await _owned_by(interaction, self.author_id):
            return

        # Holographic is a fixed triple, so there is nothing to ask for.
        primary, secondary, tertiary = HOLOGRAPHIC_COLOURS
        await interaction.response.defer(ephemeral=True)

        try:
            await apply_colours(
                self.bot,
                interaction.guild,
                self.role,
                primary,
                secondary,
                tertiary,
                reason=f"Holographic colour set by {interaction.user}",
            )
        except discord.HTTPException as exc:
            await errors.send_custom_error(
                interaction,
                "Could Not Update Role",
                f"Discord rejected the colour change: `{exc}`",
            )
            return

        await interaction.followup.send(
            embed=_success_embed(self.role, primary, secondary, tertiary),
            ephemeral=True,
        )


class LinkRoleView(discord.ui.View):
    """Staff tool: attach an existing role to a member so the lookup finds it.

    This is the repair path for roles the automatic name match cannot resolve,
    for example roles named after something other than the Discord username.
    """

    def __init__(self, author_id: int):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.user_id = None
        self.role_id = None

        user_select = discord.ui.UserSelect(
            placeholder="Which member?", min_values=1, max_values=1, row=0
        )
        user_select.callback = self._pick_user
        self.add_item(user_select)

        role_select = discord.ui.RoleSelect(
            placeholder="Which role is theirs?", min_values=1, max_values=1, row=1
        )
        role_select.callback = self._pick_role
        self.add_item(role_select)

        self.confirm = discord.ui.Button(
            label="Link", style=discord.ButtonStyle.success, disabled=True, row=2
        )
        self.confirm.callback = self._confirm
        self.add_item(self.confirm)

    def embed_for(self, guild: discord.Guild) -> discord.Embed:
        member = guild.get_member(self.user_id) if self.user_id else None
        role = guild.get_role(self.role_id) if self.role_id else None

        embed = discord.Embed(
            title="Link a Nitro Role",
            description=(
                "Pick the member and the role that belongs to them. Only needed "
                "when the role can't be matched automatically."
            ),
            color=discord.Colour.blurple(),
        )
        embed.add_field(name="Member", value=member.mention if member else "—", inline=True)
        embed.add_field(name="Role", value=role.mention if role else "—", inline=True)
        embed.set_footer(text="Only you can see this message")
        return embed

    async def _refresh(self, interaction: discord.Interaction):
        self.confirm.disabled = self.user_id is None or self.role_id is None
        await interaction.response.edit_message(embed=self.embed_for(interaction.guild), view=self)

    async def _pick_user(self, interaction: discord.Interaction):
        if not await _owned_by(interaction, self.author_id):
            return
        self.user_id = int(interaction.data["values"][0])
        await self._refresh(interaction)

    async def _pick_role(self, interaction: discord.Interaction):
        if not await _owned_by(interaction, self.author_id):
            return
        self.role_id = int(interaction.data["values"][0])
        await self._refresh(interaction)

    async def _confirm(self, interaction: discord.Interaction):
        if not await _owned_by(interaction, self.author_id):
            return

        member = interaction.guild.get_member(self.user_id)
        role = interaction.guild.get_role(self.role_id)
        if member is None or role is None:
            await errors.send_custom_error(
                interaction,
                "Link Failed",
                "That member or role is no longer available.",
            )
            return

        store = load_store()
        link_entry(store, member.id, role.id, member.name)
        save_store(store)

        print(f"[Nitro Colour] Linked {role.name} ({role.id}) to {member} ({member.id})")
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Role Linked",
                description=f"{role.mention} is now {member.mention}'s colour role.",
                color=discord.Colour.green(),
            ),
            view=None,
        )


def setup(bot):
    load_alert_config(force=True)

    @bot.tree.command(
        name=COLOUR_COMMAND_NAME,
        description="Customise the colours of your Nitro boost role.",
    )
    async def nitro_colour(interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await errors.send_custom_error(
                interaction,
                "Server Only",
                "This command can only be used inside a server.",
            )
            return

        is_staff = has_roles(interaction.user, LINK_PERMISSION_ROLE_IDS)
        boosting = interaction.user.premium_since is not None
        role = find_colour_role(guild, interaction.user, load_store())

        if role is None and not is_staff:
            if not boosting:
                await errors.send_custom_error(
                    interaction,
                    "Not Boosting",
                    "You aren't currently boosting this server, so you don't have a colour role.",
                    steps=["Boost the server to get one, then run this command again."],
                )
            else:
                await errors.send_custom_error(
                    interaction,
                    "No Colour Role",
                    "You're boosting, but no colour role could be found for you.",
                    steps=["Let a staff member know so they can sort it out for you."],
                )
            return

        enhanced = guild_has_enhanced_colours(guild)
        await interaction.response.send_message(
            embed=_chooser_embed(role, enhanced, is_staff=is_staff, boosting=boosting),
            view=ColourTypeView(bot, role, interaction.user.id, enhanced, is_staff),
            ephemeral=True,
        )

    print(f"[OK] Loaded /{COLOUR_COMMAND_NAME} command")
