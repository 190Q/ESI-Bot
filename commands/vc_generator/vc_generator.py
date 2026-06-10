import importlib
import sys
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))
if "_vc_core" in sys.modules:
    _vc_core = importlib.reload(sys.modules["_vc_core"])
else:
    _vc_core = importlib.import_module("_vc_core")

if "_vc_views" in sys.modules:
    _vc_views = importlib.reload(sys.modules["_vc_views"])
else:
    _vc_views = importlib.import_module("_vc_views")

TempVCSystem = _vc_core.TempVCSystem
send_ephemeral = _vc_core.send_ephemeral
DEFAULT_GENERATOR_CHANNEL_NAME = _vc_core.DEFAULT_GENERATOR_CHANNEL_NAME
clamp = _vc_core.clamp
VCPanelView = _vc_views.VCPanelView
ChannelPickerView = _vc_views.ChannelPickerView
KnockChannelPickerView = _vc_views.KnockChannelPickerView
PresetBuilderView = _vc_views.PresetBuilderView
LISTENER_ATTR = "_vc_generator_listeners"
LISTENER_EVENTS = ("on_ready", "on_guild_channel_delete", "on_voice_state_update", "on_message")
LISTENER_QUALNAMES = {
    "setup.<locals>.on_ready",
    "setup.<locals>.on_guild_channel_delete",
    "setup.<locals>.on_voice_state_update",
    "setup.<locals>.on_message",
}


def _remove_registered_listeners(bot):
    tracked = getattr(bot, LISTENER_ATTR, [])
    for event_name, listener in tracked:
        try:
            bot.remove_listener(listener, event_name)
        except Exception:
            pass
    setattr(bot, LISTENER_ATTR, [])

    extra_events = getattr(bot, "extra_events", {})
    for event_name in LISTENER_EVENTS:
        listeners = list(extra_events.get(event_name, []))
        for listener in listeners:
            module_name = getattr(listener, "__module__", "")
            qualname = getattr(listener, "__qualname__", "")
            if "vc_generator" in module_name and qualname in LISTENER_QUALNAMES:
                try:
                    bot.remove_listener(listener, event_name)
                except Exception:
                    pass


def _register_listener(bot, event_name: str, listener):
    bot.add_listener(listener, event_name)
    tracked = getattr(bot, LISTENER_ATTR, [])
    tracked.append((event_name, listener))
    setattr(bot, LISTENER_ATTR, tracked)


def teardown(bot):
    _remove_registered_listeners(bot)
    print("[VC_GENERATOR] Teardown complete")


def setup(bot, has_required_role, config):
    _remove_registered_listeners(bot)
    system = TempVCSystem(bot)

    async def on_ready():
        if getattr(bot, "_temp_vc_reconciled_once", False):
            return
        bot._temp_vc_reconciled_once = True
        try:
            await system.reconcile_state()
            print("[VC_GENERATOR] Reconciled temporary VC state on startup")
        except Exception as exc:
            print(f"[VC_GENERATOR] Failed to reconcile temp VC state: {exc}")

    async def on_guild_channel_delete(channel):
        if isinstance(channel, discord.VoiceChannel):
            await system.cleanup_deleted_channel(channel.id)

    async def on_voice_state_update(member, before, after):
        if member.bot:
            return
        if before.channel == after.channel:
            return

        if before.channel and isinstance(before.channel, discord.VoiceChannel):
            if await system.get_entry(before.channel.id):
                await system.handle_member_left(member, before.channel)

        guild_cfg = await system.get_guild_config(member.guild.id)
        generator_id = guild_cfg.get("generator_channel_id")

        if (
            generator_id
            and after.channel
            and isinstance(after.channel, discord.VoiceChannel)
            and after.channel.id == int(generator_id)
            and (before.channel is None or before.channel.id != int(generator_id))
        ):
            try:
                await system.create_temp_channel_for_member(member, after.channel)
            except Exception as exc:
                print(f"[VC_GENERATOR] Failed to create temp VC for {member}: {exc}")
            return

        if after.channel and isinstance(after.channel, discord.VoiceChannel):
            if await system.get_entry(after.channel.id):
                await system.mark_member_join(member, after.channel)

    async def on_message(message: discord.Message):
        if not isinstance(message, discord.Message):
            return
        if getattr(message.author, "bot", False):
            return
        if not message.guild:
            return
        if not isinstance(message.channel, discord.VoiceChannel):
            return
        entry = await system.get_entry(message.channel.id)
        if not entry:
            return
        try:
            await system.save_temp_vc_message(message)
        except Exception as exc:
            print(f"[VC_GENERATOR] Failed to save temp VC message {getattr(message, 'id', 'unknown')}: {exc}")

    _register_listener(bot, "on_ready", on_ready)
    _register_listener(bot, "on_guild_channel_delete", on_guild_channel_delete)
    _register_listener(bot, "on_voice_state_update", on_voice_state_update)
    _register_listener(bot, "on_message", on_message)

    @bot.tree.command(name="vc_setup", description="Configure the temporary VC generator")
    @app_commands.describe(
        generator_channel="Users join this channel to auto-create a temporary VC",
        temp_category="Category where temporary VCs are created",
        log_channel="Optional log channel for VC events",
        logging_enabled="Enable or disable VC event logging without clearing the selected log channel",
        default_limit="Default user limit for new temporary VCs (0 = unlimited)",
        create_generator_channel="Create a fresh generator voice channel automatically",
    )
    async def vc_setup(
        interaction: discord.Interaction,
        generator_channel: Optional[discord.VoiceChannel] = None,
        temp_category: Optional[discord.CategoryChannel] = None,
        log_channel: Optional[discord.TextChannel] = None,
        logging_enabled: Optional[bool] = None,
        default_limit: Optional[int] = None,
        create_generator_channel: bool = False,
    ):
        if not system.is_admin_member(interaction.user):
            await send_ephemeral(interaction, "Only Parliament/admin can use `/vc_setup`.")
            return

        guild_cfg = await system.get_guild_config(interaction.guild.id)
        changes = {}
        notes = []

        if create_generator_channel and generator_channel is None:
            try:
                created_channel = await interaction.guild.create_voice_channel(
                    DEFAULT_GENERATOR_CHANNEL_NAME,
                    category=temp_category if temp_category else None,
                    reason=f"Temporary VC generator setup by {interaction.user}",
                )
                generator_channel = created_channel
                notes.append(f"Created generator channel: {created_channel.mention}")
            except Exception as exc:
                await send_ephemeral(interaction, f"Failed to create generator channel: {exc}")
                return

        if generator_channel is not None:
            changes["generator_channel_id"] = generator_channel.id
            notes.append(f"Generator: {generator_channel.name}")

        if temp_category is not None:
            changes["temp_category_id"] = temp_category.id
            notes.append(f"Temp category: {temp_category.name}")

        if log_channel is not None:
            changes["log_channel_id"] = log_channel.id
            notes.append(f"Log channel: {log_channel.name}")
            if logging_enabled is None:
                changes["log_channel_enabled"] = True
                notes.append("Logging: enabled")

        if logging_enabled is not None:
            changes["log_channel_enabled"] = bool(logging_enabled)
            notes.append(f"Logging: {'enabled' if logging_enabled else 'disabled'}")

        if default_limit is not None:
            if default_limit < 0 or default_limit > 99:
                await send_ephemeral(interaction, "Default user limit must be between 0 and 99.")
                return
            changes["default_user_limit"] = int(default_limit)
            notes.append(f"Default limit: {default_limit}")

        if changes:
            guild_cfg = await system.update_guild_config(interaction.guild.id, changes)

        generator_id = guild_cfg.get("generator_channel_id")
        category_id = guild_cfg.get("temp_category_id")
        logs_id = guild_cfg.get("log_channel_id")
        log_channel_enabled = bool(guild_cfg.get("log_channel_enabled", bool(logs_id)))

        generator_obj = interaction.guild.get_channel(generator_id) if generator_id else None
        category_obj = interaction.guild.get_channel(category_id) if category_id else None
        logs_obj = interaction.guild.get_channel(logs_id) if logs_id else None

        embed = discord.Embed(
            title="⚙️ VC Generator Setup",
            color=0x57F287,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Generator Channel",
            value=generator_obj.mention if isinstance(generator_obj, discord.VoiceChannel) else "*Not configured*",
            inline=False,
        )
        embed.add_field(
            name="Temp VC Category",
            value=category_obj.name if isinstance(category_obj, discord.CategoryChannel) else "*Use generator's category*",
            inline=False,
        )
        embed.add_field(
            name="Log Channel",
            value=logs_obj.mention if isinstance(logs_obj, discord.TextChannel) else "*Not configured*",
            inline=False,
        )
        embed.add_field(
            name="VC Event Logging",
            value="Enabled" if log_channel_enabled else "Disabled",
            inline=True,
        )
        embed.add_field(name="Default Limit", value=str(guild_cfg.get("default_user_limit", 0)), inline=True)
        embed.add_field(name="Default Bitrate", value=f"{int(guild_cfg.get('default_bitrate', 64000)) // 1000} kbps", inline=True)
        embed.add_field(name="Default Template", value=guild_cfg.get("default_template", "default"), inline=True)
        if notes:
            embed.set_footer(text=" | ".join(notes)[:2048])
        else:
            embed.set_footer(text="No changes were provided. Showing current setup.")

        await send_ephemeral(interaction, embed=embed)
    @bot.tree.command(name="vc_presets", description="Manage your saved temp VC presets")
    @app_commands.describe(
        action="Choose how to manage your presets",
        preset_name="Preset name (required for edit/delete/set_default, optional for create)",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Create preset", value="create"),
            app_commands.Choice(name="Edit preset", value="edit"),
            app_commands.Choice(name="List presets", value="list"),
            app_commands.Choice(name="Delete preset", value="delete"),
            app_commands.Choice(name="Set default preset", value="set_default"),
            app_commands.Choice(name="Clear default preset", value="clear_default"),
        ]
    )
    async def vc_presets(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        preset_name: Optional[str] = None,
    ):
        if interaction.guild is None:
            await send_ephemeral(interaction, "This command can only be used in a server.")
            return
        chosen_action = action.value
        bucket = await system.get_user_preset_bucket(interaction.guild.id, interaction.user.id)
        presets = bucket.get("presets", {})
        default_preset = bucket.get("default_preset")

        if chosen_action == "edit":
            if not preset_name:
                if not presets:
                    await send_ephemeral(
                        interaction,
                        "You have no saved presets yet. Use **Create preset** first.",
                    )
                else:
                    await send_ephemeral(
                        interaction,
                        "Provide `preset_name` to edit an existing preset.",
                    )
                return

            matched_name, matched_settings = await system.get_user_preset(
                interaction.guild.id,
                interaction.user.id,
                preset_name,
            )
            if not matched_name or not matched_settings:
                await send_ephemeral(interaction, f"Preset **{preset_name}** was not found.")
                return

            view = PresetBuilderView(
                system=system,
                guild_id=interaction.guild.id,
                requester_id=interaction.user.id,
                draft_settings=matched_settings,
                initial_preset_name=matched_name,
            )
            embed = view.build_panel_embed(
                interaction.guild,
                status=f"Editing preset '{matched_name}'. Use Save Preset to apply changes.",
            )
            await send_ephemeral(interaction, embed=embed, view=view)
            return
        if chosen_action == "create":
            config = await system.get_guild_config(interaction.guild.id)
            requested_name = system.normalize_user_preset_name(preset_name or "")
            loaded_name: Optional[str] = None
            draft_settings: Optional[dict] = None

            if requested_name:
                matched_name, matched_settings = await system.get_user_preset(
                    interaction.guild.id,
                    interaction.user.id,
                    requested_name,
                )
                if matched_name and matched_settings:
                    loaded_name = matched_name
                    requested_name = matched_name
                    draft_settings = matched_settings

            if draft_settings is None:
                base_entry = system._default_channel_entry(interaction.guild.id)
                base_entry["owner_id"] = interaction.user.id
                base_entry["bitrate"] = clamp(
                    int(config.get("default_bitrate", 64000)),
                    8000,
                    int(interaction.guild.bitrate_limit),
                )
                base_entry["user_limit"] = clamp(int(config.get("default_user_limit", 0)), 0, 99)
                template_name = str(config.get("default_template", "default") or "default")
                system.apply_template_to_entry(base_entry, template_name, config)
                draft_settings = system.build_user_preset_from_entry(base_entry)

            view = PresetBuilderView(
                system=system,
                guild_id=interaction.guild.id,
                requester_id=interaction.user.id,
                draft_settings=draft_settings,
                initial_preset_name=requested_name or None,
            )
            status = (
                f"Loaded existing preset '{loaded_name}' into the builder."
                if loaded_name
                else "Adjust settings, then use Save Preset."
            )
            embed = view.build_panel_embed(interaction.guild, status=status)
            await send_ephemeral(interaction, embed=embed, view=view)
            return

        if chosen_action == "list":
            if not presets:
                await send_ephemeral(interaction, "You have no saved presets yet. Use **Save Preset** in `/vc_manage`.")
                return

            lines = []
            for name in sorted(
                presets.keys(),
                key=lambda current: (
                    0 if isinstance(default_preset, str) and current.casefold() == default_preset.casefold() else 1,
                    current.casefold(),
                ),
            ):
                cfg = presets.get(name, {})
                default_tag = " (default)" if isinstance(default_preset, str) and name.casefold() == default_preset.casefold() else ""
                lines.append(
                    f"• **{name}**{default_tag} — limit {cfg.get('user_limit', 0)}, "
                    f"{'locked' if cfg.get('locked') else 'open'}, "
                    f"{'hidden' if cfg.get('hidden') else 'visible'}"
                )

            embed = discord.Embed(
                title="Your Temp VC Presets",
                description="\n".join(lines),
                color=0x5865F2,
                timestamp=discord.utils.utcnow(),
            )
            await send_ephemeral(interaction, embed=embed)
            return

        if chosen_action == "delete":
            if not preset_name:
                await send_ephemeral(interaction, "Provide `preset_name` to delete a preset.")
                return
            deleted_name = await system.delete_user_preset(interaction.guild.id, interaction.user.id, preset_name)
            if not deleted_name:
                await send_ephemeral(interaction, f"Preset **{preset_name}** was not found.")
                return
            await send_ephemeral(interaction, f"Deleted preset **{deleted_name}**.")
            return

        if chosen_action == "set_default":
            if not preset_name:
                await send_ephemeral(interaction, "Provide `preset_name` to set a default preset.")
                return
            try:
                default_name = await system.set_user_default_preset(
                    interaction.guild.id,
                    interaction.user.id,
                    preset_name,
                )
            except ValueError:
                await send_ephemeral(interaction, f"Preset **{preset_name}** was not found.")
                return
            await send_ephemeral(interaction, f"⭐ Default preset set to **{default_name}**.")
            return

        if chosen_action == "clear_default":
            await system.set_user_default_preset(interaction.guild.id, interaction.user.id, None)
            await send_ephemeral(interaction, "Default preset cleared. New temp VCs will use normal defaults/templates.")
            return

        await send_ephemeral(interaction, "Unknown preset action.")

    @bot.tree.command(name="vc_manage", description="Open the temporary VC management panel")
    @app_commands.describe(channel="Optional target temporary VC you own/manage")
    async def vc_manage(interaction: discord.Interaction, channel: Optional[discord.VoiceChannel] = None):
        requested_channel = channel
        if requested_channel is None and interaction.user.voice and interaction.user.voice.channel:
            if isinstance(interaction.user.voice.channel, discord.VoiceChannel):
                requested_channel = interaction.user.voice.channel

        if requested_channel:
            entry = await system.get_entry(requested_channel.id)
            if not entry:
                await send_ephemeral(interaction, "That channel is not a managed temporary VC.")
                return
            if not system.can_manage(interaction.user, entry):
                await send_ephemeral(
                    interaction,
                    "You can only use `/vc_manage` on channels you own (or if you are Parliament/admin). Use `/vc_knock` to request access.",
                )
                return
            panel = VCPanelView(
                system,
                requested_channel.id,
                interaction.user.id,
                show_claim_button=system.is_admin_member(interaction.user)
                and int(entry.get("owner_id") or 0) != interaction.user.id,
            )
            embed = system.build_panel_embed(requested_channel, entry, interaction.user)
            await send_ephemeral(interaction, embed=embed, view=panel)
            return

        entries = await system.list_guild_entries(interaction.guild.id)
        if not entries:
            await send_ephemeral(
                interaction,
                "No active temporary VCs found.",
            )
            return

        manageable_channels = []
        for channel_id, entry in entries.items():
            found = interaction.guild.get_channel(channel_id)
            if isinstance(found, discord.VoiceChannel) and system.can_manage(interaction.user, entry):
                manageable_channels.append(found)

        if not manageable_channels:
            await send_ephemeral(
                interaction,
                "You do not currently own a temporary VC. Use `/vc_knock` to request entry to locked/hidden channels.",
            )
            return

        picker = ChannelPickerView(system, interaction.user.id, manageable_channels)
        embed = discord.Embed(
            title="Select Temporary VC to Manage",
            description="Pick one of your manageable temporary voice channels.",
            color=0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        await send_ephemeral(interaction, embed=embed, view=picker)

    @bot.tree.command(name="vc_knock", description="Request access to a locked/hidden temporary VC")
    @app_commands.describe(channel="Optional target temporary VC to knock on")
    async def vc_knock(interaction: discord.Interaction, channel: Optional[discord.VoiceChannel] = None):
        if system.is_admin_member(interaction.user):
            await send_ephemeral(interaction, "Parliament/admin users should use `/vc_manage`.")
            return

        async def knock_on_channel(target_channel: discord.VoiceChannel) -> None:
            entry = await system.get_entry(target_channel.id)
            if not entry:
                await send_ephemeral(interaction, "That channel is not a managed temporary VC.")
                return
            if system.can_manage(interaction.user, entry):
                await send_ephemeral(interaction, "Use `/vc_manage` for channels you can manage.")
                return
            if interaction.user.voice and interaction.user.voice.channel and interaction.user.voice.channel.id == target_channel.id:
                await send_ephemeral(interaction, "You're already in this VC.")
                return
            if not entry.get("locked") and not entry.get("hidden"):
                await send_ephemeral(interaction, "This VC is open right now; you can join directly.")
                return
            result = await system.send_knock_notification(interaction.user, target_channel, entry)
            await send_ephemeral(interaction, f"{result}")

        if channel is not None:
            if not isinstance(channel, discord.VoiceChannel):
                await send_ephemeral(interaction, "Please choose a valid voice channel.")
                return
            await knock_on_channel(channel)
            return

        entries = await system.list_guild_entries(interaction.guild.id)
        knockable_channels = []
        for channel_id, entry in entries.items():
            found = interaction.guild.get_channel(channel_id)
            if not isinstance(found, discord.VoiceChannel):
                continue
            if system.can_manage(interaction.user, entry):
                continue
            if not entry.get("locked") and not entry.get("hidden"):
                continue
            knockable_channels.append(found)

        if not knockable_channels:
            await send_ephemeral(interaction, "No locked/hidden temporary VCs are currently available to knock on.")
            return

        if len(knockable_channels) == 1:
            await knock_on_channel(knockable_channels[0])
            return

        picker = KnockChannelPickerView(system, interaction.user.id, knockable_channels)
        embed = discord.Embed(
            title="Select Temporary VC to Knock",
            description="Pick a locked/hidden temporary voice channel and send a knock request.",
            color=0xFAA61A,
            timestamp=discord.utils.utcnow(),
        )
        await send_ephemeral(interaction, embed=embed, view=picker)

    print("[OK] Loaded vc_generator system")