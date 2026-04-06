"""
Permission helpers for ESI-Bot commands.
"""

import discord


def has_roles(user, role_ids):
    """Check if *user* has any of the given role IDs or if their user ID is in the list."""
    if not isinstance(user, discord.Member):
        return False
    user_role_ids = [role.id for role in user.roles]
    return user.id in role_ids or any(rid in user_role_ids for rid in role_ids)
