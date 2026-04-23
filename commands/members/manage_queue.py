import discord
from discord import app_commands
from discord.ui import Select, View, Modal, TextInput
import os
import sys
from pathlib import Path
from datetime import datetime, timezone
import json

# Add tickets directory to path for guild_queue imports
_TICKETS_DIR = str(Path(__file__).parent.parent / "tickets")
if _TICKETS_DIR not in sys.path:
    sys.path.insert(0, _TICKETS_DIR)

from utils.permissions import has_roles
from guild_queue import (
    load_queue, save_queue, add_to_queue, remove_from_queue,
    get_queue_position, move_in_queue, switch_queue_type,
    _effective_position, VETERAN_ROLE_ID, get_guild_capacity,
)

REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0,
    1356674258390225076,  # Admin
    600185623474601995,   # Parliament
]

JUROR_ROLE_ID = 954566591520063510

ALLOWED_ROLES = REQUIRED_ROLES + [JUROR_ROLE_ID]


def is_restricted_user(user):
    """Check if user only has juror access (can view but not manage)."""
    return not has_roles(user, REQUIRED_ROLES) and has_roles(user, [JUROR_ROLE_ID])


def _build_queue_embed():
    """Build the main queue overview embed."""
    queue = load_queue()
    vet = queue.get("veteran", [])
    norm = queue.get("normal", [])

    if not vet and not norm:
        embed = discord.Embed(
            title="📋 Guild Member Queue",
            description="The queue is empty.",
            color=0x808080,
            timestamp=datetime.now(timezone.utc),
        )
        _add_capacity_field(embed)
        embed.set_footer(text="0 total")
        return embed

    lines = []
    if vet:
        lines.append("**⭐ Veteran Queue**")
        for entry in vet:
            eff = _effective_position(queue, "veteran", entry["position"])
            ts = ""
            if entry.get("queued_at"):
                try:
                    dt = datetime.fromisoformat(entry["queued_at"])
                    ts = f" - <t:{int(dt.timestamp())}:R>"
                except Exception:
                    pass
            lines.append(f"`#{eff}` **{entry['username']}** (<@{entry['discord_id']}>){ts}")

    if norm:
        if vet:
            lines.append("")
        lines.append("**Normal Queue**")
        for entry in norm:
            eff = _effective_position(queue, "normal", entry["position"])
            ts = ""
            if entry.get("queued_at"):
                try:
                    dt = datetime.fromisoformat(entry["queued_at"])
                    ts = f" - <t:{int(dt.timestamp())}:R>"
                except Exception:
                    pass
            lines.append(f"`#{eff}` **{entry['username']}** (<@{entry['discord_id']}>){ts}")

    embed = discord.Embed(
        title="📋 Guild Member Queue",
        description="\n".join(lines),
        color=0x0099FF,
        timestamp=datetime.now(timezone.utc),
    )
    _add_capacity_field(embed)
    embed.set_footer(text=f"{len(vet)} veteran, {len(norm)} normal - {len(vet) + len(norm)} total")
    return embed


def _add_capacity_field(embed):
    """Add guild capacity info to an embed if available.

    ``effective_player_count`` (= ``player_count`` + ``pending_count``) is what
    the queue math actually uses, so display that here. When there are pending
    invites, add a breakdown line so staff understand why slots look reserved.
    """
    capacity = get_guild_capacity()
    player_count = capacity.get('player_count')
    pending_count = capacity.get('pending_count', 0) or 0
    effective_count = capacity.get('effective_player_count')
    if effective_count is None:
        effective_count = player_count
    max_slots = capacity.get('max_slots')
    if effective_count is None or max_slots is None:
        return

    slots = max_slots - effective_count
    breakdown = ""
    if pending_count > 0 and player_count is not None:
        breakdown = (
            f"\n({player_count} in guild + {pending_count} pending "
            f"invite{'s' if pending_count != 1 else ''})"
        )
    if slots > 0:
        emoji = "🟢" if slots >= 5 else "🟡" if slots >= 1 else "🔴"
        embed.add_field(
            name="Guild Capacity",
            value=(
                f"{emoji} **{effective_count}/{max_slots}** - "
                f"{slots} slot{'s' if slots != 1 else ''} available{breakdown}"
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="Guild Capacity",
            value=f"🔴 **{effective_count}/{max_slots}** - Guild is full!{breakdown}",
            inline=False,
        )


def _build_player_options():
    """Return a list of SelectOption for every queued player (max 25)."""
    queue = load_queue()
    options = []
    for qt in ("veteran", "normal"):
        for entry in queue.get(qt, []):
            eff = _effective_position(queue, qt, entry["position"])
            label = f"#{eff} {entry['username']}"
            queue_label = "⭐ Veteran" if qt == "veteran" else "Normal"
            options.append(discord.SelectOption(
                label=label[:100],
                value=str(entry["discord_id"]),
                description=f"{queue_label} - Position #{eff}",
            ))
    return options[:25]


def _build_ticket_options():
    """Return SelectOptions for queued players that have linked forwarded applications."""
    from ticket_handler import load_forwarded_apps
    queue = load_queue()
    apps = load_forwarded_apps()

    # Build lookup: discord_id -> (msg_id, app_data)
    user_tickets = {}
    for msg_id, app_data in apps.items():
        uid = app_data.get('user_id')
        if uid and uid not in user_tickets:
            user_tickets[uid] = (msg_id, app_data)

    options = []
    for qt in ("veteran", "normal"):
        for entry in queue.get(qt, []):
            did = entry["discord_id"]
            if did in user_tickets:
                msg_id, app_data = user_tickets[did]
                eff = _effective_position(queue, qt, entry["position"])
                status = app_data.get('status', 'pending')
                status_emoji = {'pending': '\u23f3', 'accepted': '\u2705', 'denied': '\u274c'}.get(status, '\u23f3')
                label = f"#{eff} {entry['username']}"
                desc = f"{status_emoji} {app_data.get('app_type', 'Unknown')} \u2014 {status}"
                options.append(discord.SelectOption(
                    label=label[:100],
                    value=msg_id,
                    description=desc[:100],
                ))
    return options[:25]


def _build_ticket_detail_embed(app_data, guild):
    """Build a ticket detail embed for queue context."""
    from manage_tickets import EmbedBuilder
    from ticket_handler import calculate_threshold

    threshold = app_data.get('threshold', calculate_threshold(guild))
    approve_voters = app_data.get('approve_voters', [])
    deny_voters = app_data.get('deny_voters', [])

    embed = EmbedBuilder.build_ticket_embed(app_data, guild, "Ticket Details")

    approve_field = EmbedBuilder.build_vote_display(approve_voters, threshold, "Approve")
    deny_field = EmbedBuilder.build_vote_display(deny_voters, threshold, "Deny")

    embed.add_field(**approve_field)
    embed.add_field(**deny_field)
    EmbedBuilder.add_queue_field(embed, app_data)
    embed.set_footer(text="Application submitted")

    return embed


async def _set_queue_locked(discord_id, locked, bot=None, message_id=None):
    """Set or clear queue_locked on a user's forwarded app. Optionally re-edit the message."""
    try:
        from ticket_handler import load_forwarded_apps, save_forwarded_apps, ApplicationMixedView
        apps = load_forwarded_apps()

        # If a specific message_id is provided, target that ticket directly
        if message_id:
            msg_id = str(message_id)
            app_data = apps.get(msg_id)
            if not app_data:
                print(f"[QUEUE CMD] Ticket {msg_id} not found in forwarded apps")
                return
        else:
            # Fall back to searching by discord_id
            app_data = None
            msg_id = None
            for mid, adata in apps.items():
                if adata.get('user_id') == discord_id:
                    app_data = adata
                    msg_id = mid
                    break
            if not app_data:
                return

        if app_data.get('queue_locked') == locked:
            return
        app_data['queue_locked'] = locked
        save_forwarded_apps(apps)

        # If unlocking, try to re-enable the accept button
        if not locked and bot:
            try:
                ch = bot.get_channel(app_data['channel_id'])
                if ch:
                    msg = await ch.fetch_message(app_data['message_id'])
                    if msg:
                        threshold = app_data.get('threshold', 5)
                        approve_count = app_data.get('approve_count', 0)
                        deny_count = app_data.get('deny_count', 0)
                        show_approve = approve_count >= threshold or app_data.get('approve_notified', False)
                        show_deny = deny_count >= threshold or app_data.get('deny_notified', False)
                        view = ApplicationMixedView(
                            app_data, approve_count, deny_count,
                            show_approve_action=show_approve,
                            show_deny_action=show_deny,
                            threshold=threshold,
                        )
                        await msg.edit(view=view)
                        print(f"[QUEUE CMD] Updated accept button for msg {msg_id}")
            except Exception as e:
                print(f"[QUEUE CMD] Failed to edit message view: {e}")
    except Exception as e:
        print(f"[QUEUE CMD] Failed to update queue_locked: {e}")


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class QueueMainView(View):
    """Main view with action buttons for queue management."""
    def __init__(self, bot, restricted=False):
        super().__init__(timeout=None)
        self.bot = bot
        self.restricted = restricted

        queue = load_queue()
        has_players = bool(queue.get("veteran") or queue.get("normal"))

        if not restricted:
            add_btn = discord.ui.Button(label="Add Player", style=discord.ButtonStyle.success, emoji="\u2795")
            add_btn.callback = self.add_callback
            self.add_item(add_btn)

            remove_btn = discord.ui.Button(label="Remove Player", style=discord.ButtonStyle.danger, emoji="\u2796", disabled=not has_players)
            remove_btn.callback = self.remove_callback
            self.add_item(remove_btn)

            move_btn = discord.ui.Button(label="Move Player", style=discord.ButtonStyle.primary, emoji="\u2195\ufe0f", disabled=not has_players)
            move_btn.callback = self.move_callback
            self.add_item(move_btn)

            switch_btn = discord.ui.Button(label="Switch Queue", style=discord.ButtonStyle.secondary, emoji="\U0001f504", disabled=not has_players)
            switch_btn.callback = self.switch_callback
            self.add_item(switch_btn)

        has_tickets = bool(_build_ticket_options()) if has_players else False
        tickets_btn = discord.ui.Button(label="View Tickets", style=discord.ButtonStyle.secondary, emoji="\U0001f3ab", disabled=not has_tickets)
        tickets_btn.callback = self.tickets_callback
        self.add_item(tickets_btn)

    async def add_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddPlayerModal(self.bot))

    async def remove_callback(self, interaction: discord.Interaction):
        options = _build_player_options()
        if not options:
            await interaction.response.send_message("❌ Queue is empty.", ephemeral=True)
            return
        embed = _build_queue_embed()
        embed.title = "➖ Remove Player"
        embed.description = "Select a player to remove from the queue.\n\n" + (embed.description or "")
        view = QueuePlayerSelectView(self.bot, action="remove")
        await interaction.response.edit_message(embed=embed, view=view)

    async def move_callback(self, interaction: discord.Interaction):
        options = _build_player_options()
        if not options:
            await interaction.response.send_message("❌ Queue is empty.", ephemeral=True)
            return
        embed = _build_queue_embed()
        embed.title = "↕️ Move Player"
        embed.description = "Select a player to move.\n\n" + (embed.description or "")
        view = QueuePlayerSelectView(self.bot, action="move")
        await interaction.response.edit_message(embed=embed, view=view)

    async def switch_callback(self, interaction: discord.Interaction):
        options = _build_player_options()
        if not options:
            await interaction.response.send_message("❌ Queue is empty.", ephemeral=True)
            return
        embed = _build_queue_embed()
        embed.title = "🔄 Switch Queue"
        embed.description = "Select a player to switch between veteran ↔ normal.\n\n" + (embed.description or "")
        view = QueuePlayerSelectView(self.bot, action="switch")
        await interaction.response.edit_message(embed=embed, view=view)

    async def tickets_callback(self, interaction: discord.Interaction):
        ticket_options = _build_ticket_options()
        if not ticket_options:
            await interaction.response.send_message("❌ No queued players have linked tickets.", ephemeral=True)
            return
        embed = _build_queue_embed()
        embed.title = "🎫 View Tickets"
        embed.description = "Select a queued player's ticket to view details.\n\n" + (embed.description or "")
        view = QueueTicketSelectView(self.bot, restricted=self.restricted)
        await interaction.response.edit_message(embed=embed, view=view)


class QueuePlayerSelectView(View):
    """View with a player select dropdown and a back button."""
    def __init__(self, bot, action: str):
        super().__init__(timeout=None)
        self.bot = bot
        self.action = action

        back_btn = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary, emoji="◀️")
        back_btn.callback = self.back_callback
        self.add_item(back_btn)

        options = _build_player_options()
        if options:
            select = Select(
                placeholder="Select a player...",
                min_values=1,
                max_values=1,
                options=options,
            )
            select.callback = self.select_callback
            self.add_item(select)

    async def back_callback(self, interaction: discord.Interaction):
        embed = _build_queue_embed()
        view = QueueMainView(self.bot)
        await interaction.response.edit_message(embed=embed, view=view)

    async def select_callback(self, interaction: discord.Interaction):
        discord_id = int(interaction.data['values'][0])

        if self.action == "remove":
            await self._do_remove(interaction, discord_id)
        elif self.action == "move":
            await self._do_move_prompt(interaction, discord_id)
        elif self.action == "switch":
            await self._do_switch(interaction, discord_id)

    async def _do_remove(self, interaction: discord.Interaction, discord_id: int):
        existing = get_queue_position(discord_id)
        removed = remove_from_queue(discord_id)
        if removed:
            await _set_queue_locked(discord_id, False, self.bot)
            pos_text = f" (was position **#{existing[0]}** in **{existing[1]}**)" if existing else ""
            embed = _build_queue_embed()
            embed.title = "✅ Player Removed"
            embed.description = f"<@{discord_id}> removed from the queue{pos_text}.\n\n" + (embed.description or "")
        else:
            embed = _build_queue_embed()
            embed.title = "❌ Not Found"
            embed.description = f"<@{discord_id}> is not in the queue.\n\n" + (embed.description or "")

        view = QueueMainView(self.bot)
        await interaction.response.edit_message(embed=embed, view=view)

    async def _do_move_prompt(self, interaction: discord.Interaction, discord_id: int):
        await interaction.response.send_modal(MovePositionModal(self.bot, discord_id))

    async def _do_switch(self, interaction: discord.Interaction, discord_id: int):
        result = switch_queue_type(discord_id)
        if result:
            eff_pos, new_qt, old_qt = result
            embed = _build_queue_embed()
            embed.title = "✅ Queue Switched"
            embed.description = (
                f"<@{discord_id}> moved from **{old_qt}** → **{new_qt}** queue "
                f"(now position **#{eff_pos}**).\n\n" + (embed.description or "")
            )
        else:
            embed = _build_queue_embed()
            embed.title = "❌ Not Found"
            embed.description = f"<@{discord_id}> is not in the queue.\n\n" + (embed.description or "")

        view = QueueMainView(self.bot)
        await interaction.response.edit_message(embed=embed, view=view)


class QueueTicketSelectView(View):
    """Select a queued player's ticket to view."""
    def __init__(self, bot, restricted=False):
        super().__init__(timeout=None)
        self.bot = bot
        self.restricted = restricted

        back_btn = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary, emoji="\u25c0\ufe0f")
        back_btn.callback = self.back_callback
        self.add_item(back_btn)

        options = _build_ticket_options()
        if options:
            select = Select(
                placeholder="Select a ticket...",
                min_values=1,
                max_values=1,
                options=options,
            )
            select.callback = self.select_callback
            self.add_item(select)

    async def back_callback(self, interaction: discord.Interaction):
        embed = _build_queue_embed()
        view = QueueMainView(self.bot, restricted=self.restricted)
        await interaction.response.edit_message(embed=embed, view=view)

    async def select_callback(self, interaction: discord.Interaction):
        msg_id = interaction.data['values'][0]
        from ticket_handler import load_forwarded_apps
        apps = load_forwarded_apps()
        app_data = apps.get(msg_id)
        if not app_data:
            await interaction.response.send_message("\u274c Ticket not found!", ephemeral=True)
            return

        embed = _build_ticket_detail_embed(app_data, interaction.guild)
        view = QueueTicketDetailView(self.bot, msg_id, interaction.guild, restricted=self.restricted)
        await interaction.response.edit_message(embed=embed, view=view)


class QueueTicketDetailView(View):
    """Ticket detail view accessed from queue management."""
    def __init__(self, bot, message_id, guild, restricted=False):
        super().__init__(timeout=None)
        self.bot = bot
        self.message_id = message_id
        self.guild = guild
        self.restricted = restricted

        # Back to ticket selector
        back_btn = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary, emoji="\u25c0\ufe0f")
        back_btn.callback = self.back_callback
        self.add_item(back_btn)

        # Jump to message
        from ticket_handler import load_forwarded_apps
        apps = load_forwarded_apps()
        app_data = apps.get(message_id)
        if app_data:
            jump_btn = discord.ui.Button(
                label="Jump to Message",
                url=f"https://discord.com/channels/{guild.id}/{app_data['channel_id']}/{app_data['message_id']}",
                emoji="\U0001f517"
            )
            self.add_item(jump_btn)

        if not restricted:
            # Toggle buttons
            toggle_btn = discord.ui.Button(label="Toggle Buttons", style=discord.ButtonStyle.secondary, emoji="\U0001f501")
            toggle_btn.callback = self.toggle_buttons_callback
            self.add_item(toggle_btn)

            # Reload application
            reload_btn = discord.ui.Button(label="Reload Application", style=discord.ButtonStyle.success, emoji="\U0001f6e0\ufe0f")
            reload_btn.callback = self.reload_callback
            self.add_item(reload_btn)

    async def back_callback(self, interaction: discord.Interaction):
        ticket_options = _build_ticket_options()
        if ticket_options:
            embed = _build_queue_embed()
            embed.title = "\U0001f3ab View Tickets"
            embed.description = "Select a queued player's ticket to view details.\n\n" + (embed.description or "")
            view = QueueTicketSelectView(self.bot, restricted=self.restricted)
        else:
            embed = _build_queue_embed()
            view = QueueMainView(self.bot, restricted=self.restricted)
        await interaction.response.edit_message(embed=embed, view=view)

    async def toggle_buttons_callback(self, interaction: discord.Interaction):
        from ticket_handler import (
            load_forwarded_apps, save_forwarded_apps,
            ApplicationMixedView, ApplicationVoteView, calculate_threshold
        )
        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        if not app_data:
            await interaction.response.send_message("\u274c Ticket not found!", ephemeral=True)
            return

        await interaction.response.defer()

        try:
            channel = interaction.guild.get_channel(app_data['channel_id'])
            if not channel:
                channel = interaction.guild.get_thread(app_data['channel_id'])
            if not channel:
                await interaction.followup.send("\u274c Channel not found!", ephemeral=True)
                return

            message = await channel.fetch_message(app_data['message_id'])

            current_state = app_data.get('buttons_enabled', True)
            new_state = not current_state

            apps[self.message_id]['buttons_enabled'] = new_state
            if new_state and 'status' in apps[self.message_id]:
                del apps[self.message_id]['status']
            save_forwarded_apps(apps)

            apps = load_forwarded_apps()
            app_data = apps[self.message_id]

            threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
            approve_count = app_data.get('approve_count', 0)
            deny_count = app_data.get('deny_count', 0)

            approve_met = approve_count >= threshold or app_data.get('approve_notified', False)
            deny_met = deny_count >= threshold or app_data.get('deny_notified', False)

            if approve_met or deny_met:
                new_view = ApplicationMixedView(
                    app_data, approve_count, deny_count,
                    show_approve_action=approve_met,
                    show_deny_action=deny_met,
                    threshold=threshold
                )
            else:
                new_view = ApplicationVoteView(
                    app_data, approve_count, deny_count, threshold
                )

            if not new_state:
                for item in new_view.children:
                    item.disabled = True

            await message.edit(view=new_view)

            # Refresh the detail embed
            embed = _build_ticket_detail_embed(app_data, interaction.guild)
            state_text = "enabled" if new_state else "disabled"
            embed.add_field(name="\u2705 Action", value=f"Buttons **{state_text}**.", inline=False)
            view = QueueTicketDetailView(self.bot, self.message_id, self.guild, restricted=self.restricted)
            await interaction.edit_original_response(embed=embed, view=view)

        except Exception as e:
            await interaction.followup.send(f"\u274c Error toggling buttons: {e}", ephemeral=True)

    async def reload_callback(self, interaction: discord.Interaction):
        from ticket_handler import load_forwarded_apps, save_forwarded_apps, calculate_threshold
        from manage_tickets import VoteManager

        apps = load_forwarded_apps()
        app_data = apps.get(self.message_id)
        if not app_data:
            await interaction.response.send_message("\u274c Ticket not found!", ephemeral=True)
            return

        await interaction.response.defer()

        try:
            threshold = app_data.get('threshold', calculate_threshold(interaction.guild))
            approve_count = app_data.get('approve_count', 0)
            deny_count = app_data.get('deny_count', 0)

            if approve_count < threshold and deny_count < threshold:
                if 'status' in app_data:
                    del app_data['status']
                apps[self.message_id] = app_data
                save_forwarded_apps(apps)

            await VoteManager.update_message_view(interaction, app_data)

            # Refresh the detail embed
            apps = load_forwarded_apps()
            app_data = apps.get(self.message_id, app_data)
            embed = _build_ticket_detail_embed(app_data, interaction.guild)
            embed.add_field(name="\u2705 Action", value="Application reloaded.", inline=False)
            view = QueueTicketDetailView(self.bot, self.message_id, self.guild, restricted=self.restricted)
            await interaction.edit_original_response(embed=embed, view=view)

        except Exception as e:
            await interaction.followup.send(f"\u274c Error reloading: {e}", ephemeral=True)


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------

class AddPlayerModal(Modal, title="Add Player to Queue"):
    discord_id_input = TextInput(
        label="Discord User ID",
        placeholder="e.g. 123456789012345678",
        required=True,
        max_length=20,
    )
    username_input = TextInput(
        label="Wynncraft Username",
        placeholder="e.g. PlayerName",
        required=True,
        max_length=32,
    )
    queue_type_input = TextInput(
        label="Queue Type (veteran / normal / auto)",
        placeholder="auto",
        required=False,
        default="auto",
        max_length=10,
    )
    ticket_id_input = TextInput(
        label="Ticket Message ID (optional)",
        placeholder="e.g. 1234567890123456789",
        required=False,
        max_length=25,
    )

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        # Parse discord ID
        try:
            discord_id = int(self.discord_id_input.value.strip())
        except ValueError:
            await interaction.response.send_message("❌ Invalid Discord User ID.", ephemeral=True)
            return

        username = self.username_input.value.strip()
        qt_raw = (self.queue_type_input.value or "auto").strip().lower()
        ticket_id = self.ticket_id_input.value.strip() if self.ticket_id_input.value else None

        # Validate ticket ID if provided
        if ticket_id:
            from ticket_handler import load_forwarded_apps
            apps = load_forwarded_apps()
            if ticket_id not in apps:
                await interaction.response.send_message(
                    f"❌ Ticket with message ID `{ticket_id}` not found in forwarded applications.",
                    ephemeral=True
                )
                return

        # Check if already queued
        existing = get_queue_position(discord_id)
        if existing:
            pos, qt = existing
            embed = _build_queue_embed()
            embed.title = "❌ Already Queued"
            embed.description = (
                f"<@{discord_id}> is already in the **{qt}** queue at position **#{pos}**.\n\n"
                + (embed.description or "")
            )
            view = QueueMainView(self.bot)
            await interaction.response.edit_message(embed=embed, view=view)
            return

        # Determine veteran status
        if qt_raw in ("veteran", "vet", "v"):
            is_vet = True
        elif qt_raw in ("normal", "norm", "n"):
            is_vet = False
        else:
            # Auto-detect from roles
            member = interaction.guild.get_member(discord_id)
            is_vet = member is not None and any(r.id == VETERAN_ROLE_ID for r in member.roles)

        pos, qt = add_to_queue(username, None, discord_id, is_veteran=is_vet)
        await _set_queue_locked(discord_id, True, message_id=ticket_id)

        embed = _build_queue_embed()
        embed.title = "✅ Player Added"
        ticket_text = f"\nLinked to ticket `{ticket_id}`." if ticket_id else ""
        embed.description = (
            f"**{username}** (<@{discord_id}>) added to the **{qt}** queue at position **#{pos}**.{ticket_text}\n\n"
            + (embed.description or "")
        )
        view = QueueMainView(self.bot)
        await interaction.response.edit_message(embed=embed, view=view)


class MovePositionModal(Modal, title="Move Player"):
    position_input = TextInput(
        label="New position (within their queue, 1 = first)",
        placeholder="e.g. 1",
        required=True,
        max_length=5,
    )

    def __init__(self, bot, discord_id: int):
        super().__init__()
        self.bot = bot
        self.discord_id = discord_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_pos = int(self.position_input.value.strip())
        except ValueError:
            await interaction.response.send_message("❌ Position must be a number.", ephemeral=True)
            return

        if new_pos < 1:
            await interaction.response.send_message("❌ Position must be at least 1.", ephemeral=True)
            return

        result = move_in_queue(self.discord_id, new_pos)
        if result:
            eff_pos, qt = result
            embed = _build_queue_embed()
            embed.title = "✅ Player Moved"
            embed.description = (
                f"<@{self.discord_id}> moved to position **#{eff_pos}** in the **{qt}** queue.\n\n"
                + (embed.description or "")
            )
        else:
            embed = _build_queue_embed()
            embed.title = "❌ Not Found"
            embed.description = f"<@{self.discord_id}> is not in the queue.\n\n" + (embed.description or "")

        view = QueueMainView(self.bot)
        await interaction.response.edit_message(embed=embed, view=view)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""

    @bot.tree.command(
        name="manage_queue",
        description="Manage the guild member waiting queue"
    )
    async def manage_queue(interaction: discord.Interaction):
        """Manage queue command with interactive buttons"""

        if not has_roles(interaction.user, ALLOWED_ROLES):
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000,
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return

        restricted = is_restricted_user(interaction.user)
        embed = _build_queue_embed()
        view = QueueMainView(bot, restricted=restricted)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    print("[OK] Loaded manage_queue command")
