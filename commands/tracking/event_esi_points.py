import discord
from discord import app_commands
from datetime import datetime
import asyncio
import os
import json
from utils.permissions import has_roles
from utils.paths import PROJECT_ROOT
import utils.esi_points as esi

REQUIRED_ROLES = (
    os.getenv('OWNER_ID') if os.getenv('OWNER_ID') else 0,
    600185623474601995,  # Parliament
    683448131148447929,  # Sindrian Pride
)


def _load_username_match_db():
    """Load the username match DB from disk."""
    username_match_db_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data/username_matches.json",
    )
    try:
        with open(username_match_db_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        print(f"[WARN] Failed to load username match DB: {e}")
        return {}


def setup(bot, has_required_role, config):
    """Setup function for bot integration"""

    # Ensure the ESI points database/tables exist
    esi.init_points_database()

    @bot.tree.command(
        name="event_esi_points",
        description="Award ESI points to a player as an event reward."
    )
    @app_commands.describe(
        player="Discord user of the player to award ESI points to.",
        esi_points="Number of ESI points to award (must be 1 or higher).",
        reason="Optional reason/name of the event for the ESI points award.",
    )
    async def event_esi_points(
        interaction: discord.Interaction,
        player: discord.User,
        esi_points: int,
        reason: str = "",
    ):
        """Award ESI points to a player for an event."""

        await interaction.response.defer()

        # Permission check
        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000,
                timestamp=datetime.utcnow(),
            )
            await interaction.followup.send(embed=missing_roles_embed)
            return

        # Validate the points value
        if esi_points <= 0:
            invalid_points = discord.Embed(
                title="Invalid Input",
                description="ESI points has to be 1 or higher.",
                color=0xFF0000,
                timestamp=datetime.utcnow(),
            )
            await interaction.followup.send(embed=invalid_points)
            return

        # Resolve the player's linked Minecraft username + UUID
        username_db = _load_username_match_db()
        player_data = username_db.get(str(player.id))

        if not player_data:
            no_username_embed = discord.Embed(
                title="Username Not Found",
                description=(
                    f"No Minecraft username found for {player.mention}. Their discord "
                    f"user ID must be linked to a minecraft username using `/link_user` "
                    f"or `/accept`."
                ),
                color=0xFF0000,
                timestamp=datetime.utcnow(),
            )
            await interaction.followup.send(embed=no_username_embed)
            return

        player_uuid = player_data.get('uuid') if isinstance(player_data, dict) else None
        player_username = (
            player_data.get('username') if isinstance(player_data, dict) else player_data
        )

        if not player_uuid:
            missing_embed = discord.Embed(
                title="UUID Not Found",
                description=(
                    f"UUID not found for {player.mention}. Please ensure account is "
                    f"properly linked."
                ),
                color=0xFF0000,
                timestamp=datetime.utcnow(),
            )
            await interaction.followup.send(embed=missing_embed)
            return

        resolved = [{
            "uuid": player_uuid,
            "username": player_username,
        }]

        # Save ESI points in a thread so we don't block the event loop
        reason_text = f"Event: {reason}" if reason else "Event ESI points command"

        def db_operation():
            try:
                esi.save_points(resolved, esi_points, f"Event: {reason_text}")
                return {"success": True}
            except Exception as e:
                return {"success": False, "error": str(e)}

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, db_operation)

        if not result["success"]:
            error_embed = discord.Embed(
                title="Database Error",
                description=f"An error occurred: `{result['error']}`",
                color=0xFF0000,
                timestamp=datetime.utcnow(),
            )
            await interaction.followup.send(embed=error_embed)
            return

        # Success response
        description = (
            f"**{player.mention}** (`{player_username}`) has been awarded "
            f"**{esi_points}** ESI points."
        )
        if reason:
            description += f"\n\n**Reason:** {reason}"

        result_embed = discord.Embed(
            title=f"{esi_points} ESI Points Awarded",
            description=description,
            color=0x00FF00,
            timestamp=datetime.utcnow(),
        )
        await interaction.followup.send(embed=result_embed)

    print("[OK] Loaded event_esi_points command")
