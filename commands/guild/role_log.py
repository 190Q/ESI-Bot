import discord
from discord import app_commands
import os
import json
from datetime import datetime
from utils.permissions import has_roles

REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0
]

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    @bot.tree.command(
        name="exportroles",
        description="Export all server roles to a JSON file"
    )
    async def exportroles(interaction: discord.Interaction):
        """Export all guild roles with their complete details to JSON"""

        # Check permissions if required
        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=False)
        
        roles_data = []
        for role in interaction.guild.roles:
            role_info = {
                "id": role.id,
                "name": role.name,
                "color": {
                    "hex": str(role.color),
                    "rgb": role.color.to_rgb(),
                    "value": role.color.value
                },
                "hoist": role.hoist,
                "icon": role.icon.url if role.icon else None,
                "unicode_emoji": role.unicode_emoji,
                "position": role.position,
                "managed": role.managed,
                "mentionable": role.mentionable,
                "tags": {
                    "bot_id": role.tags.bot_id if role.tags and role.tags.bot_id else None,
                    "integration_id": role.tags.integration_id if role.tags and role.tags.integration_id else None,
                    "premium_subscriber": role.tags.is_premium_subscriber() if role.tags else False,
                    "available_for_purchase": role.tags.is_available_for_purchase() if role.tags else False
                },
                "permissions": {
                    "value": role.permissions.value,
                    "administrator": role.permissions.administrator,
                    "manage_guild": role.permissions.manage_guild,
                    "manage_roles": role.permissions.manage_roles,
                    "manage_channels": role.permissions.manage_channels,
                    "kick_members": role.permissions.kick_members,
                    "ban_members": role.permissions.ban_members,
                    "create_instant_invite": role.permissions.create_instant_invite,
                    "change_nickname": role.permissions.change_nickname,
                    "manage_nicknames": role.permissions.manage_nicknames,
                    "manage_emojis": role.permissions.manage_emojis,
                    "manage_webhooks": role.permissions.manage_webhooks,
                    "view_audit_log": role.permissions.view_audit_log,
                    "view_channel": role.permissions.view_channel,
                    "send_messages": role.permissions.send_messages,
                    "send_tts_messages": role.permissions.send_tts_messages,
                    "manage_messages": role.permissions.manage_messages,
                    "embed_links": role.permissions.embed_links,
                    "attach_files": role.permissions.attach_files,
                    "read_message_history": role.permissions.read_message_history,
                    "mention_everyone": role.permissions.mention_everyone,
                    "external_emojis": role.permissions.external_emojis,
                    "view_guild_insights": role.permissions.view_guild_insights,
                    "connect": role.permissions.connect,
                    "speak": role.permissions.speak,
                    "mute_members": role.permissions.mute_members,
                    "deafen_members": role.permissions.deafen_members,
                    "move_members": role.permissions.move_members,
                    "use_voice_activation": role.permissions.use_voice_activation,
                    "priority_speaker": role.permissions.priority_speaker,
                    "stream": role.permissions.stream,
                    "add_reactions": role.permissions.add_reactions,
                    "use_application_commands": role.permissions.use_application_commands,
                    "request_to_speak": role.permissions.request_to_speak,
                    "manage_events": role.permissions.manage_events,
                    "manage_threads": role.permissions.manage_threads,
                    "create_public_threads": role.permissions.create_public_threads,
                    "create_private_threads": role.permissions.create_private_threads,
                    "external_stickers": role.permissions.external_stickers,
                    "send_messages_in_threads": role.permissions.send_messages_in_threads,
                    "use_embedded_activities": role.permissions.use_embedded_activities,
                    "moderate_members": role.permissions.moderate_members
                },
                "created_at": role.created_at.isoformat()
            }
            roles_data.append(role_info)
        
        filename = f"roles_{interaction.guild.id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump({
                "guild_id": interaction.guild.id,
                "guild_name": interaction.guild.name,
                "exported_at": datetime.utcnow().isoformat(),
                "total_roles": len(roles_data),
                "roles": roles_data
            }, f, indent=4, ensure_ascii=False)
        
        success_embed = discord.Embed(
            title="Roles Exported Successfully",
            description=f"Exported {len(roles_data)} roles to `{filename}`",
            color=0x00FF00,
            timestamp=datetime.utcnow()
        )
        await interaction.followup.send(embed=success_embed)
        
        print(f"[OK] Exported {len(roles_data)} roles to {filename}")
    
    print("[OK] Loaded exportroles command")