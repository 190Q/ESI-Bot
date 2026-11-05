"""Hand new members the default roles as soon as they join."""
import discord

# Roles every new member starts with: the traveler role plus the miscellaneous separator
TRAVELER_ROLE_ID = 554884562862211082
MISC_SEPARATOR_ROLE_ID = 968121600488656906

_BOT_LISTENER_ATTR = "_join_roles_listeners"


def teardown(bot):
    """Remove the join listener so a reload does not stack handlers."""
    for event_name, listener in getattr(bot, _BOT_LISTENER_ATTR, []):
        try:
            bot.remove_listener(listener, event_name)
        except Exception as e:
            print(f"[Join Roles] Failed to remove the join listener: {e}")
    setattr(bot, _BOT_LISTENER_ATTR, [])


def setup(bot, has_required_role, config):
    """Give every new member the default roles."""

    async def on_member_join_roles(member):
        if member.bot:
            return

        # Role ids belong to one guild, so a server without these roles is skipped
        wanted = [
            member.guild.get_role(role_id)
            for role_id in (TRAVELER_ROLE_ID, MISC_SEPARATOR_ROLE_ID)
        ]
        missing = [role for role in wanted if role is not None and role not in member.roles]
        if not missing:
            return

        try:
            await member.add_roles(*missing, reason="Default roles for new members")
            print(f"[Join Roles] Gave {', '.join(role.name for role in missing)} to {member}")
        except discord.Forbidden:
            print(f"[Join Roles] Missing permissions to give roles to {member}")
        except discord.HTTPException as e:
            print(f"[Join Roles] Failed to give roles to {member}: {e}")

    teardown(bot)

    bot.add_listener(on_member_join_roles, "on_member_join")
    setattr(bot, _BOT_LISTENER_ATTR, [("on_member_join", on_member_join_roles)])

    print("[Join Roles] Loaded - new members get the traveler and miscellaneous separator roles")
