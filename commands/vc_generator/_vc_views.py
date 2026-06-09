import discord
from discord.ui import View, Modal, TextInput
from typing import Optional, Dict, Any, List

from _vc_core import (
    TempVCSystem,
    send_ephemeral,
    clamp,
    PARLIAMENT_ROLE_ID,
    OWNER_ID,
    MAX_PERMIT_TARGETS_PER_ACTION,
    REGION_OPTIONS,
)


class UserBoundView(View):
    def __init__(self, requester_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await send_ephemeral(interaction, "This panel belongs to another user.")
            return False
        return True


class PermitSelectorView(UserBoundView):
    def __init__(
        self,
        system: TempVCSystem,
        channel_id: int,
        requester_id: int,
        owner_id: int,
        default_user_ids: Optional[List[int]] = None,
        default_role_ids: Optional[List[int]] = None,
        banned_user_ids: Optional[List[int]] = None,
        banned_role_ids: Optional[List[int]] = None,
    ):
        super().__init__(requester_id=requester_id, timeout=180)
        self.system = system
        self.channel_id = channel_id
        self.owner_id = int(owner_id or 0)
        self.blocked_user_ids = {uid for uid in (self.owner_id, int(OWNER_ID or 0)) if uid > 0}
        self.blocked_role_ids = {int(PARLIAMENT_ROLE_ID)} if int(PARLIAMENT_ROLE_ID or 0) > 0 else set()
        self.banned_user_ids = {int(uid) for uid in (banned_user_ids or []) if str(uid).isdigit()}
        self.banned_role_ids = {int(rid) for rid in (banned_role_ids or []) if str(rid).isdigit()}
        self.pending_user_ids = [
            uid for uid in list(dict.fromkeys(default_user_ids or []))
            if int(uid) not in self.blocked_user_ids and int(uid) not in self.banned_user_ids
        ]
        self.pending_role_ids = [
            rid for rid in list(dict.fromkeys(default_role_ids or []))
            if int(rid) not in self.blocked_role_ids and int(rid) not in self.banned_role_ids
        ]
        self.ignored_targets: List[str] = []

        default_values = [
            discord.SelectDefaultValue(id=user_id, type=discord.SelectDefaultValueType.user)
            for user_id in self.pending_user_ids
        ] + [
            discord.SelectDefaultValue(id=role_id, type=discord.SelectDefaultValueType.role)
            for role_id in self.pending_role_ids
        ]
        default_values = default_values[:25]
        max_values = min(25, max(MAX_PERMIT_TARGETS_PER_ACTION, len(default_values), 1))

        self.selector = discord.ui.MentionableSelect(
            placeholder="Select users/roles to permit",
            min_values=0,
            max_values=max_values,
            default_values=default_values,
        )
        self.selector.callback = self._permit_callback
        self.add_item(self.selector)
    def _collect_selected_ids(self, guild: discord.Guild) -> tuple[List[int], List[int], List[str]]:
        selected_user_ids: List[int] = []
        selected_role_ids: List[int] = []
        ignored: List[str] = []

        for target in list(self.selector.values):
            if isinstance(target, discord.Role):
                if target == guild.default_role:
                    ignored.append("@everyone")
                    continue
                if target.id in self.blocked_role_ids:
                    ignored.append(f"{target.mention} (privileged role)")
                    continue
                if target.id in self.banned_role_ids:
                    ignored.append(f"{target.mention} (currently banned)")
                    continue
                selected_role_ids.append(target.id)
                continue

            target_id = int(getattr(target, "id", 0) or 0)
            if target_id > 0:
                if target_id in self.blocked_user_ids:
                    if self.owner_id and target_id == self.owner_id:
                        ignored.append(f"{target.mention} (VC owner)")
                    else:
                        ignored.append(f"{target.mention} (OWNER_ID)")
                    continue
                if target_id in self.banned_user_ids:
                    ignored.append(f"{target.mention} (currently banned)")
                    continue
                if isinstance(target, discord.Member) and any(role.id in self.banned_role_ids for role in target.roles):
                    ignored.append(f"{target.mention} (has a banned role)")
                    continue
                selected_user_ids.append(target_id)
        return list(dict.fromkeys(selected_user_ids)), list(dict.fromkeys(selected_role_ids)), ignored

    async def _permit_callback(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(self.channel_id)
        entry = await self.system.get_entry(self.channel_id)
        if not isinstance(channel, discord.VoiceChannel) or not entry:
            await interaction.response.edit_message(
                content="This temporary VC is no longer available.",
                embed=None,
                view=None,
            )
            return
        if not self.system.can_manage(interaction.user, entry):
            await interaction.response.edit_message(
                content="You can only use Permit if you own the VC (or are Parliament/admin).",
                embed=None,
                view=None,
            )
            return

        self.pending_user_ids, self.pending_role_ids, self.ignored_targets = self._collect_selected_ids(interaction.guild)
        await interaction.response.defer()

    def _format_users(self, guild: discord.Guild, ids: set[int]) -> str:
        mentions = []
        for user_id in sorted(ids):
            member = guild.get_member(user_id)
            mentions.append(member.mention if member else f"<@{user_id}>")
        return ", ".join(mentions)

    def _format_roles(self, guild: discord.Guild, ids: set[int]) -> str:
        mentions = []
        for role_id in sorted(ids):
            role = guild.get_role(role_id)
            mentions.append(role.mention if role else f"<@&{role_id}>")
        return ", ".join(mentions)

    async def _send_updated_panel(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
        entry: Dict[str, Any],
        status: str,
    ):
        panel = VCPanelView(
            self.system,
            channel.id,
            self.requester_id,
            show_claim_button=self.system.is_admin_member(interaction.user)
            and int(entry.get("owner_id") or 0) != interaction.user.id,
        )
        embed = self.system.build_panel_embed(channel, entry, interaction.user, status=status)
        await send_ephemeral(interaction, embed=embed, view=panel)


    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, row=1)
    async def save_changes(self, interaction: discord.Interaction, _: discord.ui.Button):
        channel = interaction.guild.get_channel(self.channel_id)
        entry = await self.system.get_entry(self.channel_id)
        if not isinstance(channel, discord.VoiceChannel) or not entry:
            await interaction.response.edit_message(
                content="This temporary VC is no longer available.",
                embed=None,
                view=None,
            )
            return
        if not self.system.can_manage(interaction.user, entry):
            await interaction.response.edit_message(
                content="You can only use Permit if you own the VC (or are Parliament/admin).",
                embed=None,
                view=None,
            )
            return

        previous_user_ids = set(int(uid) for uid in entry.get("permitted_users", []) if str(uid).isdigit())
        previous_role_ids = set(
            int(rid)
            for rid in entry.get("permitted_roles", [])
            if str(rid).isdigit() and int(rid) != PARLIAMENT_ROLE_ID
        )
        safe_user_ids = [
            uid for uid in self.pending_user_ids
            if uid not in self.blocked_user_ids and uid not in self.banned_user_ids
        ]
        safe_role_ids = [
            rid for rid in self.pending_role_ids
            if rid not in self.blocked_role_ids and rid not in self.banned_role_ids
        ]
        blocked_by_banned_role_user_ids: set[int] = set()
        for user_id in safe_user_ids:
            member = interaction.guild.get_member(int(user_id))
            if member and any(role.id in self.banned_role_ids for role in member.roles):
                blocked_by_banned_role_user_ids.add(int(user_id))
        if blocked_by_banned_role_user_ids:
            safe_user_ids = [uid for uid in safe_user_ids if uid not in blocked_by_banned_role_user_ids]
        selected_user_set = set(safe_user_ids)
        selected_role_set = set(safe_role_ids)

        added_user_ids = selected_user_set - previous_user_ids
        removed_user_ids = previous_user_ids - selected_user_set
        added_role_ids = selected_role_set - previous_role_ids
        removed_role_ids = previous_role_ids - selected_role_set

        entry["permitted_users"] = list(dict.fromkeys(safe_user_ids))
        entry["permitted_roles"] = list(dict.fromkeys(safe_role_ids + [PARLIAMENT_ROLE_ID]))

        changed = bool(added_user_ids or removed_user_ids or added_role_ids or removed_role_ids)
        if changed:
            self.system.add_log(
                entry,
                interaction.user.id,
                "permit",
                (
                    f"Permit sync add_users={len(added_user_ids)} remove_users={len(removed_user_ids)} "
                    f"add_roles={len(added_role_ids)} remove_roles={len(removed_role_ids)}"
                ),
            )
            await self.system.sync_channel(channel, entry, reason=f"Permit update by {interaction.user}")
            await self.system.upsert_entry(channel.id, entry)

        lines = []
        if added_user_ids:
            lines.append(f"Users added: {self._format_users(interaction.guild, added_user_ids)}")
        if removed_user_ids:
            lines.append(f"Users removed: {self._format_users(interaction.guild, removed_user_ids)}")
        if added_role_ids:
            lines.append(f"Roles added: {self._format_roles(interaction.guild, added_role_ids)}")
        if removed_role_ids:
            lines.append(f"Roles removed: {self._format_roles(interaction.guild, removed_role_ids)}")
        if blocked_by_banned_role_user_ids:
            lines.append(f"Blocked (has banned role): {self._format_users(interaction.guild, blocked_by_banned_role_user_ids)}")
        if self.ignored_targets:
            lines.append(f"Ignored: {', '.join(self.ignored_targets)}")

        header = "✅ Permit list saved." if changed else "No permit changes were made."
        embed = discord.Embed(
            title="Permit Users/Roles",
            description=header if not lines else f"{header}\n" + "\n".join(lines),
            color=0x57F287 if changed else 0x5865F2,
        )
        await interaction.response.edit_message(content=None, embed=embed, view=None)
        await self._send_updated_panel(
            interaction,
            channel,
            entry,
            status="✅ Permit list updated." if changed else "No permit changes were made.",
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel_changes(self, interaction: discord.Interaction, _: discord.ui.Button):
        embed = discord.Embed(
            title="Permit Users/Roles",
            description="❎ Changes cancelled. No updates were applied.",
            color=0x747F8D,
        )
        await interaction.response.edit_message(content=None, embed=embed, view=None)
        self.stop()

class BanSelectorView(UserBoundView):
    def __init__(
        self,
        system: TempVCSystem,
        channel_id: int,
        requester_id: int,
        owner_id: int,
        default_user_ids: Optional[List[int]] = None,
        default_role_ids: Optional[List[int]] = None,
    ):
        super().__init__(requester_id=requester_id, timeout=180)
        self.system = system
        self.channel_id = channel_id
        self.owner_id = int(owner_id or 0)
        self.blocked_user_ids = {uid for uid in (self.owner_id, int(OWNER_ID or 0)) if uid > 0}
        self.blocked_role_ids = {int(PARLIAMENT_ROLE_ID)} if int(PARLIAMENT_ROLE_ID or 0) > 0 else set()
        self.pending_user_ids = [
            uid for uid in list(dict.fromkeys(default_user_ids or []))
            if int(uid) not in self.blocked_user_ids
        ]
        self.pending_role_ids = [
            rid for rid in list(dict.fromkeys(default_role_ids or []))
            if int(rid) not in self.blocked_role_ids
        ]
        self.ignored_targets: List[str] = []

        default_values = [
            discord.SelectDefaultValue(id=user_id, type=discord.SelectDefaultValueType.user)
            for user_id in self.pending_user_ids
        ] + [
            discord.SelectDefaultValue(id=role_id, type=discord.SelectDefaultValueType.role)
            for role_id in self.pending_role_ids
        ]
        default_values = default_values[:25]
        max_values = min(25, max(MAX_PERMIT_TARGETS_PER_ACTION, len(default_values), 1))

        self.selector = discord.ui.MentionableSelect(
            placeholder="Select users/roles to ban",
            min_values=0,
            max_values=max_values,
            default_values=default_values,
        )
        self.selector.callback = self._ban_callback
        self.add_item(self.selector)

    def _collect_selected_ids(self, guild: discord.Guild) -> tuple[List[int], List[int], List[str]]:
        selected_user_ids: List[int] = []
        selected_role_ids: List[int] = []
        ignored: List[str] = []

        for target in list(self.selector.values):
            if isinstance(target, discord.Role):
                if target == guild.default_role:
                    ignored.append("@everyone")
                    continue
                if target.id in self.blocked_role_ids:
                    ignored.append(f"{target.mention} (privileged role)")
                    continue
                selected_role_ids.append(target.id)
                continue

            target_id = int(getattr(target, "id", 0) or 0)
            if target_id > 0:
                if target_id in self.blocked_user_ids:
                    if self.owner_id and target_id == self.owner_id:
                        ignored.append(f"{target.mention} (VC owner)")
                    else:
                        ignored.append(f"{target.mention} (OWNER_ID)")
                    continue
                selected_user_ids.append(target_id)
        return list(dict.fromkeys(selected_user_ids)), list(dict.fromkeys(selected_role_ids)), ignored

    async def _ban_callback(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(self.channel_id)
        entry = await self.system.get_entry(self.channel_id)
        if not isinstance(channel, discord.VoiceChannel) or not entry:
            await interaction.response.edit_message(
                content="This temporary VC is no longer available.",
                embed=None,
                view=None,
            )
            return
        if not self.system.can_manage(interaction.user, entry):
            await interaction.response.edit_message(
                content="You can only use Ban if you own the VC (or are Parliament/admin).",
                embed=None,
                view=None,
            )
            return

        self.pending_user_ids, self.pending_role_ids, self.ignored_targets = self._collect_selected_ids(interaction.guild)
        await interaction.response.defer()

    def _format_users(self, guild: discord.Guild, ids: set[int]) -> str:
        mentions = []
        for user_id in sorted(ids):
            member = guild.get_member(user_id)
            mentions.append(member.mention if member else f"<@{user_id}>")
        return ", ".join(mentions)

    def _format_roles(self, guild: discord.Guild, ids: set[int]) -> str:
        mentions = []
        for role_id in sorted(ids):
            role = guild.get_role(role_id)
            mentions.append(role.mention if role else f"<@&{role_id}>")
        return ", ".join(mentions)

    async def _send_updated_panel(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
        entry: Dict[str, Any],
        status: str,
    ):
        panel = VCPanelView(
            self.system,
            channel.id,
            self.requester_id,
            show_claim_button=self.system.is_admin_member(interaction.user)
            and int(entry.get("owner_id") or 0) != interaction.user.id,
        )
        embed = self.system.build_panel_embed(channel, entry, interaction.user, status=status)
        await send_ephemeral(interaction, embed=embed, view=panel)

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, row=1)
    async def save_changes(self, interaction: discord.Interaction, _: discord.ui.Button):
        channel = interaction.guild.get_channel(self.channel_id)
        entry = await self.system.get_entry(self.channel_id)
        if not isinstance(channel, discord.VoiceChannel) or not entry:
            await interaction.response.edit_message(
                content="This temporary VC is no longer available.",
                embed=None,
                view=None,
            )
            return
        if not self.system.can_manage(interaction.user, entry):
            await interaction.response.edit_message(
                content="You can only use Ban if you own the VC (or are Parliament/admin).",
                embed=None,
                view=None,
            )
            return

        previous_user_ids = set(
            int(uid) for uid in entry.get("banned_users", [])
            if str(uid).isdigit() and int(uid) not in self.blocked_user_ids
        )
        previous_role_ids = set(
            int(rid) for rid in entry.get("banned_roles", [])
            if str(rid).isdigit() and int(rid) not in self.blocked_role_ids
        )

        safe_user_ids = [uid for uid in self.pending_user_ids if uid not in self.blocked_user_ids]
        safe_role_ids = [rid for rid in self.pending_role_ids if rid not in self.blocked_role_ids]
        selected_user_set = set(safe_user_ids)
        selected_role_set = set(safe_role_ids)

        added_user_ids = selected_user_set - previous_user_ids
        removed_user_ids = previous_user_ids - selected_user_set
        added_role_ids = selected_role_set - previous_role_ids
        removed_role_ids = previous_role_ids - selected_role_set

        current_permitted_users = [int(uid) for uid in entry.get("permitted_users", []) if str(uid).isdigit()]
        current_permitted_roles = [int(rid) for rid in entry.get("permitted_roles", []) if str(rid).isdigit()]
        removed_permit_user_ids = set(uid for uid in current_permitted_users if uid in selected_user_set)
        removed_permit_role_ids = set(
            rid for rid in current_permitted_roles if rid != PARLIAMENT_ROLE_ID and rid in selected_role_set
        )

        entry["banned_users"] = list(dict.fromkeys(safe_user_ids))
        entry["banned_roles"] = list(dict.fromkeys(safe_role_ids))
        entry["permitted_users"] = [uid for uid in current_permitted_users if uid not in selected_user_set]
        entry["permitted_roles"] = [
            rid for rid in current_permitted_roles
            if rid == PARLIAMENT_ROLE_ID or rid not in selected_role_set
        ]
        if PARLIAMENT_ROLE_ID not in entry["permitted_roles"]:
            entry["permitted_roles"].append(PARLIAMENT_ROLE_ID)

        changed = bool(
            added_user_ids
            or removed_user_ids
            or added_role_ids
            or removed_role_ids
            or removed_permit_user_ids
            or removed_permit_role_ids
        )
        if changed:
            self.system.add_log(
                entry,
                interaction.user.id,
                "ban_list",
                (
                    f"Ban sync add_users={len(added_user_ids)} remove_users={len(removed_user_ids)} "
                    f"add_roles={len(added_role_ids)} remove_roles={len(removed_role_ids)}"
                ),
            )
            await self.system.sync_channel(channel, entry, reason=f"Ban update by {interaction.user}")
            await self.system.upsert_entry(channel.id, entry)

        lines = []
        if added_user_ids:
            lines.append(f"Users banned: {self._format_users(interaction.guild, added_user_ids)}")
        if removed_user_ids:
            lines.append(f"Users unbanned: {self._format_users(interaction.guild, removed_user_ids)}")
        if added_role_ids:
            lines.append(f"Roles banned: {self._format_roles(interaction.guild, added_role_ids)}")
        if removed_role_ids:
            lines.append(f"Roles unbanned: {self._format_roles(interaction.guild, removed_role_ids)}")
        if removed_permit_user_ids:
            lines.append(f"Users removed from permit: {self._format_users(interaction.guild, removed_permit_user_ids)}")
        if removed_permit_role_ids:
            lines.append(f"Roles removed from permit: {self._format_roles(interaction.guild, removed_permit_role_ids)}")
        if self.ignored_targets:
            lines.append(f"Ignored: {', '.join(self.ignored_targets)}")

        header = "✅ Banned list saved." if changed else "No banned-list changes were made."
        embed = discord.Embed(
            title="Ban Users/Roles",
            description=header if not lines else f"{header}\n" + "\n".join(lines),
            color=0xED4245 if changed else 0x5865F2,
        )
        await interaction.response.edit_message(content=None, embed=embed, view=None)
        await self._send_updated_panel(
            interaction,
            channel,
            entry,
            status="✅ Banned list updated." if changed else "No banned-list changes were made.",
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel_changes(self, interaction: discord.Interaction, _: discord.ui.Button):
        embed = discord.Embed(
            title="Ban Users/Roles",
            description="❎ Changes cancelled. No updates were applied.",
            color=0x747F8D,
        )
        await interaction.response.edit_message(content=None, embed=embed, view=None)
        self.stop()


class RenameModal(Modal, title="Rename Temporary VC"):
    new_name = TextInput(
        label="New channel name",
        placeholder="Enter the new voice channel name",
        required=True,
        max_length=100,
    )

    def __init__(self, system: TempVCSystem, channel_id: int, requester_id: int, current_name: str = ""):
        super().__init__()
        self.system = system
        self.channel_id = channel_id
        self.requester_id = requester_id
        self.new_name.default = (current_name or "")[:100]

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester_id:
            await send_ephemeral(interaction, "This modal is not for you.")
            return

        channel = interaction.guild.get_channel(self.channel_id)
        entry = await self.system.get_entry(self.channel_id)
        if not isinstance(channel, discord.VoiceChannel) or not entry:
            await send_ephemeral(interaction, "This temporary VC is no longer available.")
            return
        if not self.system.can_manage(interaction.user, entry):
            await send_ephemeral(interaction, "You can only rename your own VC (or be Parliament/admin).")
            return

        from _vc_core import clean_channel_name

        name = clean_channel_name(self.new_name.value)
        await channel.edit(name=name, reason=f"Rename temporary VC by {interaction.user}")
        self.system.add_log(entry, interaction.user.id, "rename", f"Renamed to {name}")
        await self.system.upsert_entry(channel.id, entry)
        await send_ephemeral(interaction, f"✅ Channel renamed to **{name}**.")


class SetLimitModal(Modal, title="Set User Limit"):
    user_limit = TextInput(
        label="User limit (0-99, 0 = unlimited)",
        placeholder="e.g. 5",
        required=True,
        max_length=2,
    )

    def __init__(self, system: TempVCSystem, channel_id: int, requester_id: int, current_limit: int = 0):
        super().__init__()
        self.system = system
        self.channel_id = channel_id
        self.requester_id = requester_id
        try:
            limit_value = int(current_limit)
        except (TypeError, ValueError):
            limit_value = 0
        self.user_limit.default = str(clamp(limit_value, 0, 99))

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester_id:
            await send_ephemeral(interaction, "This modal is not for you.")
            return
        channel = interaction.guild.get_channel(self.channel_id)
        entry = await self.system.get_entry(self.channel_id)
        if not isinstance(channel, discord.VoiceChannel) or not entry:
            await send_ephemeral(interaction, "This temporary VC is no longer available.")
            return
        if not self.system.can_manage(interaction.user, entry):
            await send_ephemeral(interaction, "You can only set limit on your own VC (or be Parliament/admin).")
            return
        try:
            new_limit = int(self.user_limit.value.strip())
        except ValueError:
            await send_ephemeral(interaction, "Please provide a valid number between 0 and 99.")
            return
        if new_limit < 0 or new_limit > 99:
            await send_ephemeral(interaction, "Limit must be between 0 and 99.")
            return
        entry["user_limit"] = new_limit
        self.system.add_log(entry, interaction.user.id, "limit_set", f"Set user limit to {new_limit}")
        await self.system.sync_channel(channel, entry, reason=f"Set VC limit by {interaction.user}")
        await self.system.upsert_entry(channel.id, entry)
        await send_ephemeral(interaction, f"✅ User limit set to **{new_limit}**.")


class SetBitrateModal(Modal, title="Set Channel Bitrate"):
    bitrate_input = TextInput(
        label="Bitrate (kbps or bps)",
        placeholder="e.g. 96 (kbps) or 96000 (bps)",
        required=True,
        max_length=8,
    )

    def __init__(self, system: TempVCSystem, channel_id: int, requester_id: int, current_bitrate: int = 64000):
        super().__init__()
        self.system = system
        self.channel_id = channel_id
        self.requester_id = requester_id
        try:
            bitrate_value = int(current_bitrate)
        except (TypeError, ValueError):
            bitrate_value = 64000
        if bitrate_value < 0:
            bitrate_value = 64000
        self.bitrate_input.default = str(bitrate_value)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester_id:
            await send_ephemeral(interaction, "This modal is not for you.")
            return
        channel = interaction.guild.get_channel(self.channel_id)
        entry = await self.system.get_entry(self.channel_id)
        if not isinstance(channel, discord.VoiceChannel) or not entry:
            await send_ephemeral(interaction, "This temporary VC is no longer available.")
            return
        if not self.system.can_manage(interaction.user, entry):
            await send_ephemeral(interaction, "You can only set bitrate on your own VC (or be Parliament/admin).")
            return

        try:
            raw_value = int(self.bitrate_input.value.strip())
        except ValueError:
            await send_ephemeral(interaction, "Bitrate must be a number.")
            return

        bitrate = raw_value * 1000 if raw_value < 1000 else raw_value
        max_bitrate = int(channel.guild.bitrate_limit)
        if bitrate < 8000 or bitrate > max_bitrate:
            await send_ephemeral(
                interaction,
                f"Bitrate must be between **8 kbps** and **{max_bitrate // 1000} kbps** for this guild.",
            )
            return

        entry["bitrate"] = bitrate
        self.system.add_log(entry, interaction.user.id, "bitrate_set", f"Set bitrate to {bitrate} bps")
        await self.system.sync_channel(channel, entry, reason=f"Set VC bitrate by {interaction.user}")
        await self.system.upsert_entry(channel.id, entry)
        await send_ephemeral(interaction, f"✅ Bitrate set to **{bitrate // 1000} kbps**.")


class KickMemberView(UserBoundView):
    def __init__(self, system: TempVCSystem, channel_id: int, requester_id: int):
        super().__init__(requester_id=requester_id, timeout=120)
        self.system = system
        self.channel_id = channel_id
        self.member_select = None

    async def setup_options(self, guild: discord.Guild):
        channel = guild.get_channel(self.channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            return
        options = []
        for member in channel.members[:25]:
            options.append(
                discord.SelectOption(
                    label=member.display_name[:100],
                    value=str(member.id),
                    description=f"Kick {member.name}"[:100],
                )
            )
        select = discord.ui.Select(
            placeholder="Select a user to kick",
            min_values=1,
            max_values=1,
            options=options or [discord.SelectOption(label="No members available", value="0")],
        )
        select.callback = self._kick_callback
        self.member_select = select
        self.add_item(select)

    async def _kick_callback(self, interaction: discord.Interaction):
        if self.member_select.values[0] == "0":
            await send_ephemeral(interaction, "No members available to kick.")
            return
        target_id = int(self.member_select.values[0])
        channel = interaction.guild.get_channel(self.channel_id)
        entry = await self.system.get_entry(self.channel_id)
        if not isinstance(channel, discord.VoiceChannel) or not entry:
            await send_ephemeral(interaction, "This temporary VC is no longer available.")
            return
        if not self.system.can_manage(interaction.user, entry):
            await send_ephemeral(interaction, "You can only kick users from your own VC (or be Parliament/admin).")
            return

        target_member = interaction.guild.get_member(target_id)
        if not target_member or not target_member.voice or target_member.voice.channel.id != channel.id:
            await send_ephemeral(interaction, "That user is no longer in this voice channel.")
            return
        try:
            await target_member.move_to(None, reason=f"Kicked from temp VC by {interaction.user}")
        except Exception as exc:
            await send_ephemeral(interaction, f"Failed to kick user: {exc}")
            return

        self.system.add_log(entry, interaction.user.id, "kick", f"Kicked {target_member.id}")
        await self.system.upsert_entry(channel.id, entry)
        await send_ephemeral(interaction, f"✅ {target_member.mention} was kicked from {channel.mention}.")


class TransferOwnerView(UserBoundView):
    def __init__(self, system: TempVCSystem, channel_id: int, requester_id: int):
        super().__init__(requester_id=requester_id, timeout=120)
        self.system = system
        self.channel_id = channel_id
        self.member_select = None

    async def setup_options(self, guild: discord.Guild):
        channel = guild.get_channel(self.channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            return
        options = []
        for member in channel.members[:25]:
            options.append(
                discord.SelectOption(
                    label=member.display_name[:100],
                    value=str(member.id),
                    description=f"Transfer ownership to {member.name}"[:100],
                )
            )
        select = discord.ui.Select(
            placeholder="Select a new owner",
            min_values=1,
            max_values=1,
            options=options or [discord.SelectOption(label="No members available", value="0")],
        )
        select.callback = self._transfer_callback
        self.member_select = select
        self.add_item(select)

    async def _transfer_callback(self, interaction: discord.Interaction):
        if self.member_select.values[0] == "0":
            await send_ephemeral(interaction, "No members available for transfer.")
            return
        target_id = int(self.member_select.values[0])
        channel = interaction.guild.get_channel(self.channel_id)
        entry = await self.system.get_entry(self.channel_id)
        if not isinstance(channel, discord.VoiceChannel) or not entry:
            await send_ephemeral(interaction, "This temporary VC is no longer available.")
            return
        if not self.system.can_manage(interaction.user, entry):
            await send_ephemeral(interaction, "You can only transfer ownership of your own VC (or be Parliament/admin).")
            return
        target_member = interaction.guild.get_member(target_id)
        if not target_member:
            await send_ephemeral(interaction, "Target member not found.")
            return

        entry["owner_id"] = target_member.id
        self.system.add_log(entry, interaction.user.id, "transfer", f"Ownership transferred to {target_member.id}")
        await self.system.sync_channel(channel, entry, reason=f"Ownership transfer by {interaction.user}")
        await self.system.upsert_entry(channel.id, entry)
        await send_ephemeral(interaction, f"✅ Ownership transferred to {target_member.mention}.")


class RegionSelectView(UserBoundView):
    def __init__(self, system: TempVCSystem, channel_id: int, requester_id: int):
        super().__init__(requester_id=requester_id, timeout=120)
        self.system = system
        self.channel_id = channel_id

        options = [discord.SelectOption(label=label, value=value) for label, value in REGION_OPTIONS]
        select = discord.ui.Select(placeholder="Choose a voice region", options=options, min_values=1, max_values=1)
        select.callback = self._region_callback
        self.add_item(select)
        self.select = select

    async def _region_callback(self, interaction: discord.Interaction):
        chosen = self.select.values[0]
        channel = interaction.guild.get_channel(self.channel_id)
        entry = await self.system.get_entry(self.channel_id)
        if not isinstance(channel, discord.VoiceChannel) or not entry:
            await send_ephemeral(interaction, "This temporary VC is no longer available.")
            return
        if not self.system.can_manage(interaction.user, entry):
            await send_ephemeral(interaction, "You can only change region on your own VC (or be Parliament/admin).")
            return
        entry["region"] = chosen
        self.system.add_log(entry, interaction.user.id, "region", f"Region changed to {chosen}")
        await self.system.sync_channel(channel, entry, reason=f"Region change by {interaction.user}")
        await self.system.upsert_entry(channel.id, entry)
        await send_ephemeral(interaction, f"✅ Region set to **{chosen}**.")


class TemplateSelectView(UserBoundView):
    def __init__(self, system: TempVCSystem, channel_id: int, requester_id: int, templates: Dict[str, Dict[str, Any]]):
        super().__init__(requester_id=requester_id, timeout=120)
        self.system = system
        self.channel_id = channel_id
        self.templates = templates

        options = []
        for key, cfg in list(templates.items())[:25]:
            options.append(
                discord.SelectOption(
                    label=str(cfg.get("display_name", key))[:100],
                    value=key,
                    description=(
                        f"limit {cfg.get('user_limit', 0)} | "
                        f"{'locked' if cfg.get('locked') else 'open'} | "
                        f"{'hidden' if cfg.get('hidden') else 'visible'}"
                    )[:100],
                )
            )
        select = discord.ui.Select(placeholder="Choose a template", options=options, min_values=1, max_values=1)
        select.callback = self._template_callback
        self.add_item(select)
        self.select = select

    async def _template_callback(self, interaction: discord.Interaction):
        template_name = self.select.values[0]
        channel = interaction.guild.get_channel(self.channel_id)
        entry = await self.system.get_entry(self.channel_id)
        if not isinstance(channel, discord.VoiceChannel) or not entry:
            await send_ephemeral(interaction, "This temporary VC is no longer available.")
            return
        if not self.system.can_manage(interaction.user, entry):
            await send_ephemeral(interaction, "You can only apply templates on your own VC (or be Parliament/admin).")
            return

        config = await self.system.get_guild_config(interaction.guild.id)
        self.system.apply_template_to_entry(entry, template_name, config)

        template_cfg = config.get("templates", {}).get(template_name, {})
        owner_id = int(entry.get("owner_id") or 0)
        owner_member = interaction.guild.get_member(owner_id) if owner_id else None
        if owner_member and template_cfg.get("name_format"):
            new_name = self.system._compute_channel_name(owner_member, template_cfg)
            try:
                await channel.edit(name=new_name, reason=f"Template rename by {interaction.user}")
            except Exception:
                pass

        self.system.add_log(entry, interaction.user.id, "template", f"Applied template {template_name}")
        await self.system.sync_channel(channel, entry, reason=f"Template applied by {interaction.user}")
        await self.system.upsert_entry(channel.id, entry)
        await send_ephemeral(interaction, f"✅ Template **{template_name}** applied.")

class PresetSaveModal(Modal, title="Save Temp VC Preset"):
    preset_name = TextInput(
        label="Preset name",
        placeholder="e.g. Ranked Duo",
        required=True,
        max_length=40,
    )

    def __init__(self, system: TempVCSystem, channel_id: int, requester_id: int):
        super().__init__()
        self.system = system
        self.channel_id = channel_id
        self.requester_id = requester_id

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester_id:
            await send_ephemeral(interaction, "This modal is not for you.")
            return
        channel = interaction.guild.get_channel(self.channel_id)
        entry = await self.system.get_entry(self.channel_id)
        if not isinstance(channel, discord.VoiceChannel) or not entry:
            await send_ephemeral(interaction, "This temporary VC is no longer available.")
            return
        if not self.system.can_manage(interaction.user, entry):
            await send_ephemeral(interaction, "You can only save presets from your own VC (or be Parliament/admin).")
            return

        preset_name = self.system.normalize_user_preset_name(self.preset_name.value)
        if not preset_name:
            await send_ephemeral(interaction, "Preset name cannot be empty.")
            return

        preset_payload = self.system.build_user_preset_from_entry(entry)
        try:
            result = await self.system.save_user_preset(
                interaction.guild.id,
                interaction.user.id,
                preset_name,
                preset_payload,
            )
        except ValueError as exc:
            await send_ephemeral(interaction, str(exc))
            return

        verb = "Saved" if result.get("created") else "Updated"
        default_name = result.get("default_preset")
        default_note = ""
        if isinstance(default_name, str) and default_name.casefold() == str(result.get("name", "")).casefold():
            default_note = "\nThis preset is your default preset."
        await send_ephemeral(
            interaction,
            f"✅ {verb} preset **{result['name']}** ({result['count']} total).{default_note}",
        )


class PresetLoadSelectView(UserBoundView):
    def __init__(
        self,
        system: TempVCSystem,
        channel_id: int,
        requester_id: int,
        presets: Dict[str, Dict[str, Any]],
        default_preset: Optional[str] = None,
    ):
        super().__init__(requester_id=requester_id, timeout=180)
        self.system = system
        self.channel_id = channel_id
        self.default_preset = default_preset
        self.preset_names = sorted(
            presets.keys(),
            key=lambda name: (
                0 if isinstance(default_preset, str) and name.casefold() == default_preset.casefold() else 1,
                name.casefold(),
            ),
        )

        options = []
        for name in self.preset_names[:25]:
            cfg = presets.get(name, {})
            descriptor = (
                f"limit {cfg.get('user_limit', 0)} | "
                f"{'locked' if cfg.get('locked') else 'open'} | "
                f"{'hidden' if cfg.get('hidden') else 'visible'}"
            )
            if isinstance(default_preset, str) and name.casefold() == default_preset.casefold():
                descriptor = f"default | {descriptor}"
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=name,
                    description=descriptor[:100],
                )
            )

        self.selector = discord.ui.Select(
            placeholder="Choose a preset to load",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.selector.callback = self._load_callback
        self.add_item(self.selector)

    async def _send_updated_panel(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
        entry: Dict[str, Any],
        status: str,
    ):
        panel = VCPanelView(
            self.system,
            channel.id,
            self.requester_id,
            show_claim_button=self.system.is_admin_member(interaction.user)
            and int(entry.get("owner_id") or 0) != interaction.user.id,
        )
        embed = self.system.build_panel_embed(channel, entry, interaction.user, status=status)
        await send_ephemeral(interaction, embed=embed, view=panel)

    async def _load_callback(self, interaction: discord.Interaction):
        selected_name = self.selector.values[0]
        channel = interaction.guild.get_channel(self.channel_id)
        entry = await self.system.get_entry(self.channel_id)
        if not isinstance(channel, discord.VoiceChannel) or not entry:
            await interaction.response.edit_message(
                content="This temporary VC is no longer available.",
                embed=None,
                view=None,
            )
            return
        if not self.system.can_manage(interaction.user, entry):
            await interaction.response.edit_message(
                content="You can only load presets into your own VC (or be Parliament/admin).",
                embed=None,
                view=None,
            )
            return

        matched_name, preset_settings = await self.system.get_user_preset(
            interaction.guild.id,
            interaction.user.id,
            selected_name,
        )
        if not matched_name or not preset_settings:
            await interaction.response.edit_message(
                content="That preset no longer exists. Open Load Preset again.",
                embed=None,
                view=None,
            )
            return

        before_snapshot = self.system.build_user_preset_from_entry(entry)
        self.system.apply_user_preset_to_entry(entry, preset_settings)
        after_snapshot = self.system.build_user_preset_from_entry(entry)
        changed = before_snapshot != after_snapshot

        if changed:
            self.system.add_log(entry, interaction.user.id, "preset_load", f"Loaded preset {matched_name}")
            await self.system.sync_channel(channel, entry, reason=f"Preset load by {interaction.user}")
            await self.system.upsert_entry(channel.id, entry)

        header = f"✅ Loaded preset **{matched_name}**." if changed else f"Preset **{matched_name}** already matches this VC."
        embed = discord.Embed(
            title="Load Temp VC Preset",
            description=header,
            color=0x57F287 if changed else 0x5865F2,
        )
        await interaction.response.edit_message(content=None, embed=embed, view=None)
        await self._send_updated_panel(
            interaction,
            channel,
            entry,
            status=f"✅ Loaded preset {matched_name}." if changed else f"Preset {matched_name} already active.",
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel_load(self, interaction: discord.Interaction, _: discord.ui.Button):
        embed = discord.Embed(
            title="Load Temp VC Preset",
            description="❎ Preset load cancelled.",
            color=0x747F8D,
        )
        await interaction.response.edit_message(content=None, embed=embed, view=None)
        self.stop()


class ChannelPickerView(UserBoundView):
    def __init__(self, system: TempVCSystem, requester_id: int, channels: List[discord.VoiceChannel]):
        super().__init__(requester_id=requester_id, timeout=120)
        self.system = system
        self.channels = channels[:25]
        options = [
            discord.SelectOption(
                label=channel.name[:100],
                value=str(channel.id),
                description=f"Members: {len(channel.members)}",
            )
            for channel in self.channels
        ]
        select = discord.ui.Select(
            placeholder="Select a temporary VC",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._pick_callback
        self.add_item(select)
        self.select = select

    async def _pick_callback(self, interaction: discord.Interaction):
        channel_id = int(self.select.values[0])
        channel = interaction.guild.get_channel(channel_id)
        entry = await self.system.get_entry(channel_id)
        if not isinstance(channel, discord.VoiceChannel) or not entry:
            await send_ephemeral(interaction, "Selected VC is no longer active.")
            return
        if not self.system.can_manage(interaction.user, entry):
            await interaction.response.edit_message(
                content="You can only manage your own temporary VC.",
                embed=None,
                view=None,
            )
            return
        panel = VCPanelView(
            self.system,
            channel_id,
            self.requester_id,
            show_claim_button=self.system.is_admin_member(interaction.user)
            and int(entry.get("owner_id") or 0) != interaction.user.id,
        )
        embed = self.system.build_panel_embed(channel, entry, interaction.user)
        await interaction.response.edit_message(embed=embed, view=panel)

class KnockChannelPickerView(UserBoundView):
    def __init__(self, system: TempVCSystem, requester_id: int, channels: List[discord.VoiceChannel]):
        super().__init__(requester_id=requester_id, timeout=120)
        self.system = system
        self.channels = channels[:25]
        options = [
            discord.SelectOption(
                label=channel.name[:100],
                value=str(channel.id),
                description=f"Members: {len(channel.members)}",
            )
            for channel in self.channels
        ]
        select = discord.ui.Select(
            placeholder="Select a temporary VC to knock on",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._pick_callback
        self.add_item(select)
        self.select = select

    async def _pick_callback(self, interaction: discord.Interaction):
        channel_id = int(self.select.values[0])
        channel = interaction.guild.get_channel(channel_id)
        entry = await self.system.get_entry(channel_id)
        if not isinstance(channel, discord.VoiceChannel) or not entry:
            await interaction.response.edit_message(content="Selected VC is no longer active.", embed=None, view=None)
            return
        if self.system.can_manage(interaction.user, entry):
            await interaction.response.edit_message(content="Use `/vc_manage` for channels you can manage.", embed=None, view=None)
            return
        if interaction.user.voice and interaction.user.voice.channel and interaction.user.voice.channel.id == channel.id:
            await interaction.response.edit_message(content="You're already in this VC.", embed=None, view=None)
            return
        if not entry.get("locked") and not entry.get("hidden"):
            await interaction.response.edit_message(content="This VC is open right now; you can join directly.", embed=None, view=None)
            return
        result = await self.system.send_knock_notification(interaction.user, channel, entry)
        await interaction.response.edit_message(content=f"🚪 {result}", embed=None, view=None)


class VCPanelView(UserBoundView):
    def __init__(self, system: TempVCSystem, channel_id: int, requester_id: int, show_claim_button: bool = False):
        super().__init__(requester_id=requester_id, timeout=900)
        self.system = system
        self.channel_id = channel_id
        self.show_claim_button = show_claim_button
        self._claim_button: Optional[discord.ui.Button] = None
        self._build_buttons()
        self._set_claim_button_visibility(show_claim_button)

    def _add_action_button(self, label: str, style: discord.ButtonStyle, row: int, callback, emoji: Optional[str] = None):
        button = discord.ui.Button(label=label, style=style, row=row, emoji=emoji)

        async def wrapper(interaction: discord.Interaction):
            await callback(interaction)

        button.callback = wrapper
        self.add_item(button)
        return button

    def _build_buttons(self):
        self._add_action_button("Lock", discord.ButtonStyle.secondary, 0, self.lock_channel)
        self._add_action_button("Unlock", discord.ButtonStyle.secondary, 0, self.unlock_channel)
        self._add_action_button("Hide", discord.ButtonStyle.secondary, 0, self.hide_channel)
        self._add_action_button("Show", discord.ButtonStyle.secondary, 0, self.show_channel)
        self._add_action_button("Push-to-talk", discord.ButtonStyle.secondary, 0, self.toggle_ptt)

        self._add_action_button("Permit", discord.ButtonStyle.primary, 1, self.permit_targets)
        self._add_action_button("Kick", discord.ButtonStyle.danger, 1, self.kick_member)
        self._add_action_button("Ban", discord.ButtonStyle.danger, 1, self.ban_targets)
        self._claim_button = self._add_action_button("Claim", discord.ButtonStyle.success, 1, self.claim_channel)
        self._add_action_button("Transfer", discord.ButtonStyle.primary, 1, self.transfer_owner)

        self._add_action_button("Limit", discord.ButtonStyle.primary, 2, self.set_limit)
        self._add_action_button("+Limit", discord.ButtonStyle.success, 2, self.increase_limit)
        self._add_action_button("-Limit", discord.ButtonStyle.danger, 2, self.decrease_limit)
        self._add_action_button("Rename", discord.ButtonStyle.primary, 2, self.rename_channel)
        self._add_action_button("Bitrate", discord.ButtonStyle.primary, 2, self.set_bitrate)

        self._add_action_button("Region", discord.ButtonStyle.primary, 3, self.change_region)
        self._add_action_button("Template", discord.ButtonStyle.primary, 3, self.apply_template)
        self._add_action_button("VC Invite", discord.ButtonStyle.success, 3, self.create_invite)
        self._add_action_button("VC Info", discord.ButtonStyle.secondary, 3, self.show_info)
        self._add_action_button("Logs", discord.ButtonStyle.secondary, 3, self.show_logs)
        self._add_action_button("Save Preset", discord.ButtonStyle.primary, 4, self.save_preset)
        self._add_action_button("Load Preset", discord.ButtonStyle.secondary, 4, self.load_preset)

        self._add_action_button("Refresh", discord.ButtonStyle.secondary, 4, self.refresh_panel)
        self._add_action_button("Delete VC", discord.ButtonStyle.danger, 4, self.delete_channel)

    def _set_claim_button_visibility(self, visible: bool):
        if self._claim_button is None:
            return
        if visible and self._claim_button not in self.children:
            self.add_item(self._claim_button)
            return
        if not visible and self._claim_button in self.children:
            self.remove_item(self._claim_button)

    async def _get_context(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(self.channel_id)
        entry = await self.system.get_entry(self.channel_id)
        if not isinstance(channel, discord.VoiceChannel) or not entry:
            await send_ephemeral(interaction, "This temporary VC is no longer active.")
            return None, None
        return channel, entry

    async def _require_manage(self, interaction: discord.Interaction, entry: Dict[str, Any]) -> bool:
        if self.system.can_manage(interaction.user, entry):
            return True
        await send_ephemeral(interaction, "You can only manage your own temporary VC (or be Parliament/admin).")
        return False

    async def _refresh_with_status(self, interaction: discord.Interaction, status: str = ""):
        channel, entry = await self._get_context(interaction)
        if not channel or not entry:
            return
        self._set_claim_button_visibility(
            self.system.is_admin_member(interaction.user)
            and int(entry.get("owner_id") or 0) != interaction.user.id
        )
        embed = self.system.build_panel_embed(channel, entry, interaction.user, status=status)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    async def _update_state(self, interaction: discord.Interaction, entry: Dict[str, Any], channel: discord.VoiceChannel, action: str, details: str):
        self.system.add_log(entry, interaction.user.id, action, details)
        await self.system.sync_channel(channel, entry, reason=f"{action} by {interaction.user}")
        await self.system.upsert_entry(channel.id, entry)

    async def lock_channel(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if not await self._require_manage(interaction, entry):
            return
        entry["locked"] = True
        await self._update_state(interaction, entry, channel, "lock", "Channel locked")
        await self._refresh_with_status(interaction, "🔒 Channel locked.")

    async def unlock_channel(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if not await self._require_manage(interaction, entry):
            return
        entry["locked"] = False
        await self._update_state(interaction, entry, channel, "unlock", "Channel unlocked")
        await self._refresh_with_status(interaction, "🔓 Channel unlocked.")

    async def hide_channel(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if not await self._require_manage(interaction, entry):
            return
        entry["hidden"] = True
        await self._update_state(interaction, entry, channel, "hide", "Channel hidden")
        await self._refresh_with_status(interaction, "🙈 Channel hidden.")

    async def show_channel(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if not await self._require_manage(interaction, entry):
            return
        entry["hidden"] = False
        await self._update_state(interaction, entry, channel, "show", "Channel visible")
        await self._refresh_with_status(interaction, "Channel shown.")

    async def toggle_ptt(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if not await self._require_manage(interaction, entry):
            return
        entry["push_to_talk"] = not bool(entry.get("push_to_talk"))
        await self._update_state(
            interaction,
            entry,
            channel,
            "push_to_talk",
            f"Push-to-talk {'enabled' if entry['push_to_talk'] else 'disabled'}",
        )
        status = "🎙️ Push-to-talk enabled." if entry["push_to_talk"] else "🎙️ Push-to-talk disabled."
        await self._refresh_with_status(interaction, status)

    async def permit_targets(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if not await self._require_manage(interaction, entry):
            return
        owner_id = int(entry.get("owner_id") or 0)
        blocked_user_ids = {uid for uid in (owner_id, int(OWNER_ID or 0)) if uid > 0}
        banned_user_ids = {int(uid) for uid in entry.get("banned_users", []) if str(uid).isdigit()}
        banned_role_ids = {int(rid) for rid in entry.get("banned_roles", []) if str(rid).isdigit()}

        default_user_ids = []
        for user_id in entry.get("permitted_users", []):
            try:
                normalized = int(user_id)
            except (TypeError, ValueError):
                continue
            if normalized in blocked_user_ids:
                continue
            if normalized in banned_user_ids:
                continue
            if interaction.guild.get_member(normalized):
                default_user_ids.append(normalized)

        default_role_ids = []
        for role_id in entry.get("permitted_roles", []):
            try:
                normalized = int(role_id)
            except (TypeError, ValueError):
                continue
            if normalized == PARLIAMENT_ROLE_ID:
                continue
            if normalized in banned_role_ids:
                continue
            if interaction.guild.get_role(normalized):
                default_role_ids.append(normalized)

        editable_count = min(25, max(MAX_PERMIT_TARGETS_PER_ACTION, len(default_user_ids) + len(default_role_ids)))
        await send_ephemeral(
            interaction,
            embed=discord.Embed(
                title="Permit Users/Roles",
                description=(
                    "Edit the access list below. Keep selected entries to retain access, unselect to remove.\n"
                    "Banned users/roles cannot be permitted until they are unbanned.\n"
                    "Changes are only applied when you click Save; Cancel discards them.\n"
                    f"You can choose up to {editable_count} entries."
                ),
                color=0x5865F2,
            ),
            view=PermitSelectorView(
                self.system,
                channel.id,
                self.requester_id,
                owner_id=owner_id,
                default_user_ids=default_user_ids,
                default_role_ids=default_role_ids,
                banned_user_ids=list(banned_user_ids),
                banned_role_ids=list(banned_role_ids),
            ),
        )

    async def ban_targets(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if not await self._require_manage(interaction, entry):
            return
        owner_id = int(entry.get("owner_id") or 0)
        blocked_user_ids = {uid for uid in (owner_id, int(OWNER_ID or 0)) if uid > 0}

        default_user_ids = []
        for user_id in entry.get("banned_users", []):
            try:
                normalized = int(user_id)
            except (TypeError, ValueError):
                continue
            if normalized in blocked_user_ids:
                continue
            if interaction.guild.get_member(normalized):
                default_user_ids.append(normalized)

        default_role_ids = []
        for role_id in entry.get("banned_roles", []):
            try:
                normalized = int(role_id)
            except (TypeError, ValueError):
                continue
            if normalized == PARLIAMENT_ROLE_ID:
                continue
            if interaction.guild.get_role(normalized):
                default_role_ids.append(normalized)

        editable_count = min(25, max(MAX_PERMIT_TARGETS_PER_ACTION, len(default_user_ids) + len(default_role_ids)))
        await send_ephemeral(
            interaction,
            embed=discord.Embed(
                title="Ban Users/Roles",
                description=(
                    "Select users/roles that should be completely blocked from this VC.\n"
                    "Banned targets cannot join and cannot knock.\n"
                    "Changes are only applied when you click Save; Cancel discards them.\n"
                    f"You can choose up to {editable_count} entries."
                ),
                color=0xED4245,
            ),
            view=BanSelectorView(
                self.system,
                channel.id,
                self.requester_id,
                owner_id=owner_id,
                default_user_ids=default_user_ids,
                default_role_ids=default_role_ids,
            ),
        )

    async def kick_member(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if not await self._require_manage(interaction, entry):
            return
        if not channel.members:
            await send_ephemeral(interaction, "No members are currently in this voice channel.")
            return
        view = KickMemberView(self.system, channel.id, self.requester_id)
        await view.setup_options(interaction.guild)
        await send_ephemeral(
            interaction,
            embed=discord.Embed(
                title="Kick Member",
                description=f"Select a member to kick from {channel.mention}.",
                color=0xED4245,
            ),
            view=view,
        )

    async def claim_channel(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if not await self._require_manage(interaction, entry):
            return
        if not self.system.is_admin_member(interaction.user):
            await send_ephemeral(interaction, "Only Parliament/admin can claim ownership.")
            return
        entry["owner_id"] = interaction.user.id
        await self._update_state(interaction, entry, channel, "claim_admin", "Parliament/admin took ownership")
        await self._refresh_with_status(interaction, "👑 You took ownership as Parliament/admin.")

    async def transfer_owner(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if not await self._require_manage(interaction, entry):
            return
        if not channel.members:
            await send_ephemeral(interaction, "No members are currently in this voice channel.")
            return
        view = TransferOwnerView(self.system, channel.id, self.requester_id)
        await view.setup_options(interaction.guild)
        await send_ephemeral(
            interaction,
            embed=discord.Embed(
                title="Transfer Ownership",
                description=f"Select who should own {channel.mention}.",
                color=0x5865F2,
            ),
            view=view,
        )
    async def save_preset(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if not await self._require_manage(interaction, entry):
            return
        await interaction.response.send_modal(
            PresetSaveModal(
                self.system,
                channel.id,
                self.requester_id,
            )
        )

    async def load_preset(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if not await self._require_manage(interaction, entry):
            return

        bucket = await self.system.get_user_preset_bucket(interaction.guild.id, interaction.user.id)
        presets = bucket.get("presets", {})
        default_preset = bucket.get("default_preset")
        if not presets:
            await send_ephemeral(
                interaction,
                "You have no saved presets yet. Use **Save Preset** first, then load it from another temp VC.",
            )
            return

        view = PresetLoadSelectView(
            self.system,
            channel.id,
            self.requester_id,
            presets=presets,
            default_preset=default_preset,
        )
        await send_ephemeral(
            interaction,
            embed=discord.Embed(
                title="Load Temp VC Preset",
                description=f"Choose one of your saved presets ({len(presets)} total).",
                color=0x5865F2,
            ),
            view=view,
        )

    async def knock_channel(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if interaction.user.voice and interaction.user.voice.channel and interaction.user.voice.channel.id == channel.id:
            await send_ephemeral(interaction, "You're already in this VC.")
            return
        if not entry.get("locked") and not entry.get("hidden"):
            await send_ephemeral(interaction, "This VC is open right now; you can join directly.")
            return
        result = await self.system.send_knock_notification(interaction.user, channel, entry)
        await send_ephemeral(interaction, f"🚪 {result}")

    async def set_limit(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if not await self._require_manage(interaction, entry):
            return
        await interaction.response.send_modal(
            SetLimitModal(
                self.system,
                channel.id,
                self.requester_id,
                current_limit=channel.user_limit,
            )
        )

    async def increase_limit(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if not await self._require_manage(interaction, entry):
            return
        entry["user_limit"] = clamp(int(entry.get("user_limit", 0)) + 1, 0, 99)
        await self._update_state(interaction, entry, channel, "increase_limit", f"New limit {entry['user_limit']}")
        await self._refresh_with_status(interaction, f"👥 User limit increased to {entry['user_limit']}.")

    async def decrease_limit(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if not await self._require_manage(interaction, entry):
            return
        entry["user_limit"] = clamp(int(entry.get("user_limit", 0)) - 1, 0, 99)
        await self._update_state(interaction, entry, channel, "decrease_limit", f"New limit {entry['user_limit']}")
        await self._refresh_with_status(interaction, f"👥 User limit decreased to {entry['user_limit']}.")

    async def rename_channel(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if not await self._require_manage(interaction, entry):
            return
        await interaction.response.send_modal(
            RenameModal(
                self.system,
                channel.id,
                self.requester_id,
                current_name=channel.name,
            )
        )

    async def set_bitrate(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if not await self._require_manage(interaction, entry):
            return
        await interaction.response.send_modal(
            SetBitrateModal(
                self.system,
                channel.id,
                self.requester_id,
                current_bitrate=channel.bitrate,
            )
        )

    async def change_region(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if not await self._require_manage(interaction, entry):
            return
        view = RegionSelectView(self.system, channel.id, self.requester_id)
        await send_ephemeral(
            interaction,
            embed=discord.Embed(
                title="🌍 Select Region",
                description=f"Choose the voice region for {channel.mention}.",
                color=0x5865F2,
            ),
            view=view,
        )

    async def apply_template(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if not await self._require_manage(interaction, entry):
            return
        config = await self.system.get_guild_config(interaction.guild.id)
        view = TemplateSelectView(self.system, channel.id, self.requester_id, config.get("templates", {}))
        await send_ephemeral(
            interaction,
            embed=discord.Embed(
                title="Apply Template",
                description="Choose a pre-configured VC template.",
                color=0x5865F2,
            ),
            view=view,
        )

    async def create_invite(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if not await self._require_manage(interaction, entry):
            return
        try:
            invite = await channel.create_invite(
                max_age=3600,
                max_uses=0,
                temporary=False,
                unique=True,
                reason=f"Temporary VC invite created by {interaction.user}",
            )
        except Exception as exc:
            await send_ephemeral(interaction, f"Failed to create invite: {exc}")
            return
        self.system.add_log(entry, interaction.user.id, "invite", "Generated VC invite")
        await self.system.upsert_entry(channel.id, entry)
        await send_ephemeral(interaction, f"📨 Invite created (1 hour): {invite.url}")

    async def show_info(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        await send_ephemeral(interaction, embed=self.system.build_info_embed(channel, entry))

    async def show_logs(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        await send_ephemeral(interaction, embed=self.system.build_logs_embed(channel, entry))

    async def refresh_panel(self, interaction: discord.Interaction):
        await self._refresh_with_status(interaction, "Refreshed.")
    async def delete_channel(self, interaction: discord.Interaction):
        channel, entry = await self._get_context(interaction)
        if not channel:
            return
        if not await self._require_manage(interaction, entry):
            return

        channel_name = channel.name
        try:
            await channel.delete(reason=f"Temporary VC deleted by {interaction.user}")
        except Exception as exc:
            await send_ephemeral(interaction, f"Failed to delete VC: {exc}")
            return

        await self.system.remove_entry(channel.id)
        message = f"Deleted VC **{channel_name}**."
        if interaction.response.is_done():
            await interaction.edit_original_response(content=message, embed=None, view=None)
        else:
            await interaction.response.edit_message(content=message, embed=None, view=None)
