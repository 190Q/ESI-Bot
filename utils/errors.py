"""
Centralized command error responses for ESI-Bot.

Every user-facing error returned by a command should come from this module so
that all errors share the same structure and are always delivered ephemerally
(only the user who ran the command can see them).

The shared structure mirrors the original "Player Not Found" error:
    * a red embed whose title is prefixed with ❌
    * a description
    * an optional "What to do:" field rendered as ` - step` lines, followed by a
      `-#` contact-support subtext line
    * an optional footer

Usage
-----
Prefer a named catalog entry::

    from utils import errors
    await errors.PLAYER_NOT_FOUND.send(interaction, username=name)

For a genuinely one-off error that does not belong in the catalog, use the
custom helper (it produces the identical structure and is still ephemeral)::

    await errors.send_custom_error(
        interaction,
        "Uniform Missing",
        f"No uniform file was found for `{rank}`.",
        steps=["Try a different rank."],
    )
"""

import discord

# Shared red used by every command error embed
ERROR_COLOR = 0xFF0000
# Every command error must remain visible only to the command invoker
ERROR_EPHEMERAL = True

# Contact-support subtext appended to the "What to do:" field
SUPPORT_HINT = (
    "-# if you think this is a mistake, you can contact support using "
    "`/contact_support`."
)


def _fmt(text: str, fmt: dict) -> str:
    """Format *text* with *fmt* placeholders, tolerating missing/bad keys.

    Static descriptions (no placeholders) are returned unchanged. When
    formatting values are supplied but a placeholder is missing or the template
    contains stray braces, the raw text is returned instead of raising.
    """
    if not fmt:
        return text
    try:
        return text.format(**fmt)
    except (KeyError, IndexError, ValueError):
        return text


class CommandError:
    """A reusable, standardized command error.

    Parameters
    ----------
    title:
        Short error title. Rendered as ``❌ {title}``.
    description:
        Embed description. May contain ``str.format`` placeholders (e.g.
        ``{username}``) that are filled from keyword args passed to
        :meth:`build_embed` / :meth:`send`.
    steps:
        Optional list of guidance lines shown under a "What to do:" field. Each
        line may also contain ``str.format`` placeholders.
    include_support:
        Whether to append the :data:`SUPPORT_HINT` contact-support subtext to
        the "What to do:" field. Defaults to ``True``.
    """

    def __init__(self, title, description, steps=None, *, include_support=True):
        self.title = title
        self.description = description
        self.steps = list(steps) if steps else []
        self.include_support = include_support

    def build_embed(self, *, footer=None, **fmt) -> discord.Embed:
        """Build the standardized error embed."""
        embed = discord.Embed(
            title=f"❌ {self.title}",
            description=_fmt(self.description, fmt),
            color=ERROR_COLOR,
        )

        # Assemble the "What to do:" field from the guidance steps
        value_parts = [f" - {_fmt(step, fmt)}" for step in self.steps]
        if self.include_support:
            # Blank line before the subtext, matching the original layout
            value_parts.append(f"\n{SUPPORT_HINT}" if value_parts else SUPPORT_HINT)

        if value_parts:
            embed.add_field(
                name="What to do:",
                value="\n".join(value_parts),
                inline=False,
            )

        if footer:
            embed.set_footer(text=footer)

        return embed

    async def send(self, interaction: discord.Interaction, *, footer=None, **fmt) -> None:
        """Send this error to *interaction*, always ephemerally.

        Uses ``followup.send`` when the interaction has already been responded
        to or deferred, otherwise ``response.send_message``.
        """
        embed = self.build_embed(footer=footer, **fmt)
        try:
            if interaction.response.is_done():
                message = await interaction.followup.send(
                    embed=embed,
                    ephemeral=ERROR_EPHEMERAL,
                    wait=True,
                )
            else:
                await interaction.response.send_message(
                    embed=embed,
                    ephemeral=ERROR_EPHEMERAL,
                )
        except Exception as e:  # pragma: no cover - best-effort delivery
            print(f"[ERRORS] Failed to send error '{self.title}': {e}")


def custom(title, description, *, steps=None, include_support=True) -> CommandError:
    """Create a one-off :class:`CommandError` without adding it to the catalog."""
    return CommandError(title, description, steps, include_support=include_support)


async def send_custom_error(
    interaction: discord.Interaction,
    title,
    description,
    *,
    steps=None,
    footer=None,
    include_support=True,
    **fmt,
) -> None:
    """Build and send a one-off error with the standard structure, ephemerally."""
    await custom(
        title, description, steps=steps, include_support=include_support
    ).send(interaction, footer=footer, **fmt)


# ---------------------------------------------------------------------------
# Error catalog
#
# Named, reusable errors shared across commands. Descriptions use named
# placeholders that callers fill via keyword args, e.g
# ``errors.PLAYER_NOT_FOUND.send(interaction, username=name)``
# ---------------------------------------------------------------------------

# Permissions / context
NO_PERMISSION = CommandError(
    "Permission Denied",
    "You don't have permission to use this command.",
    ["Make sure you have the required role to run this command."],
)

WRONG_CHANNEL = CommandError(
    "Wrong Channel",
    "This command can't be used in this channel.",
    ["Run this command in the correct channel and try again."],
)

# Input / generic failures
INVALID_INPUT = CommandError(
    "Invalid Input",
    "{reason}",
    ["Double-check the values you provided and try again."],
)

UNEXPECTED_ERROR = CommandError(
    "Something Went Wrong",
    "An unexpected error occurred while running this command.",
    ["Please try again in a little while."],
)

DATABASE_ERROR = CommandError(
    "Database Error",
    "A database error occurred while processing your request.",
    ["Please try again in a little while."],
)

# Wynncraft API
API_ERROR = CommandError(
    "API Error",
    "Something went wrong while contacting the Wynncraft API.",
    ["Please try again in a few moments."],
)

API_RATE_LIMITED = CommandError(
    "Rate Limited",
    "The Wynncraft API rate limit has been exceeded.",
    ["Please wait a moment and try again."],
)

# Players / linked accounts
PLAYER_NOT_FOUND = CommandError(
    "Player Not Found",
    "`{username}` is not a valid Wynncraft username.",
    [
        "Check if you spelled the username correctly.",
        "Make sure the player has logged into Wynncraft at least once.",
    ],
)

NO_LINKED_ACCOUNT = CommandError(
    "No Linked Account",
    "No Minecraft account is linked to your Discord account.",
    ["Provide a username, or link your account with `/link_user`."],
)

USERNAME_NOT_FOUND = CommandError(
    "Username Not Found",
    "No Minecraft username is linked to {user}.",
    ["Link the account with `/link_user` or `/accept` first."],
)

UUID_NOT_FOUND = CommandError(
    "UUID Not Found",
    "No UUID is linked to {user}.",
    ["Make sure the account is properly linked, then try again."],
)

USER_NOT_IN_SERVER = CommandError(
    "User Not In Server",
    "The Discord user linked to `{username}` is not in this server.",
    [
        "Make sure the linked Discord account is still in this server.",
        "Relink the account with `/link_user` if the link is outdated.",
    ],
)

PLAYER_NOT_IN_GUILD = CommandError(
    "Player Not In Guild Data",
    "Player `{username}` was not found in the guild tracking data.",
    [
        "Check the username spelling and capitalization.",
        "Make sure the player is currently in the tracked guild.",
        "Wait for the tracker to collect a fresh snapshot, then try again.",
    ],
)

# Records / data
NO_RECORDS_FOUND = CommandError(
    "No Records Found",
    "No records were found for `{username}`.",
    [
        "Check the username and try again.",
        "If this is a new player, wait for tracking data to be collected.",
    ],
)

NO_DATA_AVAILABLE = CommandError(
    "No Data Available",
    "{reason}",
    ["Try again after more tracking data has been collected."],
)

# Roles / assets
MISSING_ROLE = CommandError(
    "Missing Role",
    "{reason}",
    ["Make sure the required role exists and is assigned correctly."],
)

IMAGE_GENERATION_FAILED = CommandError(
    "Image Generation Failed",
    "{reason}",
    ["Please try again in a little while."],
)

# Generic fallback "not found"
NOT_FOUND = CommandError(
    "Not Found",
    "{reason}",
    ["Double-check your input and try again."],
)


__all__ = [
    "ERROR_COLOR",
    "ERROR_EPHEMERAL",
    "SUPPORT_HINT",
    "CommandError",
    "custom",
    "send_custom_error",
    # catalog
    "NO_PERMISSION",
    "WRONG_CHANNEL",
    "INVALID_INPUT",
    "UNEXPECTED_ERROR",
    "DATABASE_ERROR",
    "API_ERROR",
    "API_RATE_LIMITED",
    "PLAYER_NOT_FOUND",
    "NO_LINKED_ACCOUNT",
    "USERNAME_NOT_FOUND",
    "UUID_NOT_FOUND",
    "USER_NOT_IN_SERVER",
    "PLAYER_NOT_IN_GUILD",
    "NO_RECORDS_FOUND",
    "NO_DATA_AVAILABLE",
    "MISSING_ROLE",
    "IMAGE_GENERATION_FAILED",
    "NOT_FOUND",
]
