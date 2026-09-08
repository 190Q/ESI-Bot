"""
Self-Serve Inactivity Exemption Hub & Ticket System for ESI-Bot.
"""

import os
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Tuple
import discord
from discord import app_commands

from utils.permissions import has_roles
from utils import errors
from utils.paths import PROJECT_ROOT, DATA_DIR
from utils.tickets import (
    get_or_create_category,
    cleanup_empty_categories,
    TICKET_GUILD_ID,
    RECRUITMENT_MANAGER_ROLE_ID,
    PARLIAMENT_ROLE_ID,
    JUROR_ROLE_ID,
    SINDIAN_CITIZEN_ROLE_ID,
    TICKET_PERMS,
)
from commands.members.inactivity_check import (
    get_future_weeks,
    load_exemptions,
    save_exemptions,
    get_user_exemption_data,
    cleanup_expired_exemptions,
    _is_week_valid,
    is_restricted_user,
    REQUIRED_ROLES,
)

INACTIVITY_HUB_PATH = DATA_DIR / "inactivity_hub.json"
INACTIVITY_REQUESTS_PATH = DATA_DIR / "inactivity_requests.json"


def load_hub_data() -> dict:
    """Load inactivity hub configuration."""
    if not INACTIVITY_HUB_PATH.exists():
        return {}
    try:
        with open(INACTIVITY_HUB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_hub_data(data: dict) -> bool:
    """Save inactivity hub configuration."""
    INACTIVITY_HUB_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(INACTIVITY_HUB_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        return True
    except Exception as e:
        print(f"[INACT_HUB] Error saving hub data: {e}")
        return False


def load_requests_data() -> dict:
    """Load inactivity requests data."""
    if not INACTIVITY_REQUESTS_PATH.exists():
        return {"tickets": {}, "categories": {}, "archived_tickets": {}}
    try:
        with open(INACTIVITY_REQUESTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"tickets": {}, "categories": {}, "archived_tickets": {}}
            if "tickets" not in data:
                data["tickets"] = {}
            if "categories" not in data:
                data["categories"] = {}
            if "archived_tickets" not in data:
                data["archived_tickets"] = {}
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"tickets": {}, "categories": {}, "archived_tickets": {}}


def save_requests_data(data: dict) -> bool:
    """Save inactivity requests data."""
    INACTIVITY_REQUESTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(INACTIVITY_REQUESTS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        return True
    except Exception as e:
        print(f"[INACT_HUB] Error saving requests data: {e}")
        return False


def format_week_label(week_value: str) -> str:
    """Format a stored week string into a human-readable label."""
    if week_value == "permanent":
        return "🔒 **Permanent**"
    try:
        start_str, end_str = week_value.split("_")
        start_date = datetime.fromisoformat(start_str).date()
        end_date = datetime.fromisoformat(end_str).date()
        return format_date_range(start_date, end_date)
    except Exception:
        return week_value


def format_date_range(start_date, end_date) -> str:
    """Format a start and end date range nicely."""
    if start_date.month == end_date.month and start_date.year == end_date.year:
        return f"{start_date.strftime('%b %d')} - {end_date.strftime('%d')}, {end_date.year}"
    elif start_date.year == end_date.year:
        return f"{start_date.strftime('%b %d')} - {end_date.strftime('%b %d')}, {end_date.year}"
    else:
        return f"{start_date.strftime('%b %d, %Y')} - {end_date.strftime('%b %d, %Y')}"


def get_overall_date_range(weeks: List[str]) -> Optional[str]:
    """Calculate the overall start date and end date across all valid weeks."""
    parsed_dates = []
    for w in weeks:
        if w == "permanent":
            continue
        try:
            start_str, end_str = w.split("_")
            s = datetime.fromisoformat(start_str).date()
            e = datetime.fromisoformat(end_str).date()
            parsed_dates.append((s, e))
        except Exception:
            continue

    if not parsed_dates:
        return None

    min_start = min(s for s, _ in parsed_dates)
    max_end = max(e for _, e in parsed_dates)
    return format_date_range(min_start, max_end)


# Public roster embed and live sync
def build_roster_embed() -> discord.Embed:
    """Build the active public exemptions roster embed."""
    cleanup_expired_exemptions()
    exemptions = load_exemptions()
    now = datetime.now(timezone.utc)

    roster_lines = []

    for user_key, data in exemptions.items():
        if isinstance(data, list):
            # Old format has no public opt-in, skip
            continue
        if not isinstance(data, dict):
            continue

        weeks = data.get("weeks", [])
        # Permanent exemptions are NEVER public
        if "permanent" in weeks:
            continue

        public_duration = data.get("public_duration", False)
        public_reason = data.get("public_reason", False) and bool(data.get("reason"))

        # If neither duration nor reason is public, skip
        if not public_duration and not public_reason:
            continue

        # Filter to currently valid weeks
        valid_weeks = [w for w in weeks if _is_week_valid(w, now)]
        if not valid_weeks:
            continue

        duration_range = get_overall_date_range(valid_weeks) if public_duration else None
        reason_text = data.get("reason") if public_reason else None

        if duration_range and reason_text:
            line = f"• <@{user_key}>: `{duration_range}` *(Reason: {reason_text})*"
        elif duration_range:
            line = f"• <@{user_key}>: `{duration_range}`"
        elif reason_text:
            line = f"• <@{user_key}>: *(Reason: {reason_text})*"
        else:
            continue

        roster_lines.append(line)

    if roster_lines:
        description = "**Currently active exemptions shared by members:**\n\n" + "\n".join(roster_lines)
    else:
        description = "*No active public exemptions at this time.*"

    embed = discord.Embed(
        title="Active Public Exemptions",
        description=description,
        color=0x5865f2,
        timestamp=datetime.now(timezone.utc),
    )
    return embed


async def update_hub_roster(bot) -> bool:
    """Update the live roster message in the hub channel."""
    hub_data = load_hub_data()
    channel_id = hub_data.get("channel_id")
    roster_message_id = hub_data.get("roster_message_id")

    if not channel_id:
        return False

    try:
        channel = bot.get_channel(channel_id)
        if not channel:
            channel = await bot.fetch_channel(channel_id)

        if not channel:
            return False

        roster_embed = build_roster_embed()

        if roster_message_id:
            try:
                roster_msg = await channel.fetch_message(roster_message_id)
                await roster_msg.edit(embed=roster_embed)
                return True
            except discord.NotFound:
                pass

        # If not found or not set, create a new roster message
        new_roster_msg = await channel.send(embed=roster_embed)
        hub_data["roster_message_id"] = new_roster_msg.id
        save_hub_data(hub_data)
        print(f"[INACT_HUB] Created new roster message {new_roster_msg.id} in channel {channel_id}")
        return True

    except Exception as e:
        print(f"[INACT_HUB] Error updating hub roster: {e}")
        return False


# Self-serve request UI
class RequestReasonModal(discord.ui.Modal, title="Exemption Reason"):
    """Modal for entering the self-serve exemption reason."""
    reason_input = discord.ui.TextInput(
        label="Reason",
        placeholder="Enter the reason for this inactivity exemption request...",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view
        if parent_view.reason:
            self.reason_input.default = parent_view.reason

    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.reason = self.reason_input.value.strip() or None

        if self.parent_view.reason:
            self.parent_view.reason_btn.label = "Edit Reason"
            self.parent_view.reason_btn.style = discord.ButtonStyle.primary
        else:
            self.parent_view.reason_btn.label = "Add Reason"
            self.parent_view.reason_btn.style = discord.ButtonStyle.secondary
            self.parent_view.public_reason = False

        self.parent_view.update_button_states()
        await self.parent_view.update_embed(interaction)


class SelfServeRequestView(discord.ui.View):
    """View presented to members to configure and submit an inactivity exemption request."""
    def __init__(
        self,
        user_id: int,
        bot,
        existing_weeks: Optional[List[str]] = None,
        existing_reason: Optional[str] = None,
        existing_public_duration: bool = False,
        existing_public_reason: bool = False,
    ):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.bot = bot
        self.existing_weeks = list(existing_weeks or [])
        self.existing_reason = existing_reason
        self.public_duration = existing_public_duration
        self.public_reason = existing_public_reason and bool(existing_reason)

        # Build week options (without permanent option for self-serve)
        future_weeks = get_future_weeks(15, second_check=True)
        options = []
        for i, (label, start_date, end_date) in enumerate(future_weeks):
            value = f"{start_date.isoformat()}_{end_date.isoformat()}"
            is_default = value in self.existing_weeks
            options.append(discord.SelectOption(
                label=label,
                value=value,
                description="Current/Next check" if i == 0 else "Future week",
                default=is_default,
            ))
        options = options[:25]

        # Pre-populate selections with active exemptions matching future options
        self.selected_weeks: List[str] = [opt.value for opt in options if opt.default]
        self.original_exemptions = set(self.selected_weeks)
        self.reason: Optional[str] = self.existing_reason
        self.original_reason = self.existing_reason
        self.original_public_duration = self.public_duration
        self.original_public_reason = self.public_reason

        self.week_select = discord.ui.Select(
            placeholder="Select weeks to request exemption for...",
            min_values=0,
            max_values=len(options),
            options=options,
            row=0,
        )
        self.week_select.callback = self.select_callback
        self.add_item(self.week_select)

        self.reason_btn = discord.ui.Button(
            label="Edit Reason" if self.reason else "Add Reason",
            style=discord.ButtonStyle.primary if self.reason else discord.ButtonStyle.secondary,
            row=1,
        )
        self.reason_btn.callback = self.reason_callback
        self.add_item(self.reason_btn)

        self.toggle_duration_btn = discord.ui.Button(
            label="Private Duration" if self.public_duration else "Public Duration",
            style=discord.ButtonStyle.success if self.public_duration else discord.ButtonStyle.secondary,
            row=1,
        )
        self.toggle_duration_btn.callback = self.toggle_duration_callback
        self.add_item(self.toggle_duration_btn)

        self.toggle_reason_btn = discord.ui.Button(
            label="Private Reason" if self.public_reason else "Public Reason",
            style=discord.ButtonStyle.success if self.public_reason else discord.ButtonStyle.secondary,
            row=1,
            disabled=not bool(self.reason),
        )
        self.toggle_reason_btn.callback = self.toggle_reason_callback
        self.add_item(self.toggle_reason_btn)

        has_anything = len(self.selected_weeks) > 0 or bool(self.reason)
        self.clear_btn = discord.ui.Button(
            label="Clear All",
            style=discord.ButtonStyle.danger,
            row=2,
            disabled=not has_anything,
        )
        self.clear_btn.callback = self.clear_callback
        self.add_item(self.clear_btn)

        self.submit_btn = discord.ui.Button(
            label="Submit Request",
            style=discord.ButtonStyle.success,
            row=2,
            disabled=True,
        )
        self.submit_btn.callback = self.submit_callback
        self.add_item(self.submit_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await errors.send_custom_error(
                interaction,
                "Not Allowed",
                "Only the person who opened this request can interact with it.",
            )
            return False
        return True

    def update_button_states(self):
        """Update enabled/disabled states of buttons based on current selection and changes."""
        weeks_changed = set(self.selected_weeks) != self.original_exemptions
        reason_changed = self.reason != self.original_reason
        duration_vis_changed = self.public_duration != self.original_public_duration
        reason_vis_changed = self.public_reason != self.original_public_reason
        has_req_changes = weeks_changed or reason_changed
        has_vis_changes = duration_vis_changed or reason_vis_changed
        has_selection = len(self.selected_weeks) > 0

        # Update toggle buttons
        self.toggle_duration_btn.label = "Private Duration" if self.public_duration else "Public Duration"
        self.toggle_duration_btn.style = discord.ButtonStyle.success if self.public_duration else discord.ButtonStyle.secondary

        self.toggle_reason_btn.label = "Private Reason" if self.public_reason else "Public Reason"
        self.toggle_reason_btn.style = discord.ButtonStyle.success if self.public_reason else discord.ButtonStyle.secondary
        self.toggle_reason_btn.disabled = not bool(self.reason)

        # Clear button
        self.clear_btn.disabled = not (len(self.selected_weeks) > 0 or bool(self.reason))

        # Submit button behavior:
        if len(self.selected_weeks) == 0 and bool(self.existing_weeks):
            self.submit_btn.disabled = False
            self.submit_btn.label = "Remove Exemption"
            self.submit_btn.style = discord.ButtonStyle.danger
        elif not has_req_changes and has_vis_changes and bool(self.existing_weeks) and has_selection:
            self.submit_btn.disabled = False
            self.submit_btn.label = "Update Visibility"
            self.submit_btn.style = discord.ButtonStyle.primary
        elif (has_req_changes or has_vis_changes) and has_selection:
            self.submit_btn.disabled = False
            self.submit_btn.label = "Submit Request"
            self.submit_btn.style = discord.ButtonStyle.success
        else:
            self.submit_btn.disabled = True
            self.submit_btn.label = "Submit Request"
            self.submit_btn.style = discord.ButtonStyle.success

    async def update_embed(self, interaction: discord.Interaction):
        """Update the ephemeral message embed preview."""
        weeks_changed = set(self.selected_weeks) != self.original_exemptions
        reason_changed = self.reason != self.original_reason
        duration_vis_changed = self.public_duration != self.original_public_duration
        reason_vis_changed = self.public_reason != self.original_public_reason
        has_req_changes = weeks_changed or reason_changed
        has_vis_changes = duration_vis_changed or reason_vis_changed

        week_labels = [format_week_label(w) for w in self.selected_weeks]

        if len(self.selected_weeks) == 0 and bool(self.existing_weeks):
            description = (
                f"No weeks selected for **{interaction.user.mention}**.\n\n"
                f"**Click Remove Exemption below to cancel and remove your active exemption.**"
            )
        elif week_labels:
            description = (
                f"Exemption request for **{interaction.user.mention}**:\n\n"
                + "\n".join([f"• {label}" for label in week_labels])
            )
        else:
            description = (
                f"Select one or more upcoming weeks from the dropdown above.\n\n"
                f"⚠️ *You must select at least one week to submit a request.*"
            )

        if self.reason:
            description += f"\n\n**Reason:** {self.reason}"

        vis_text = f"• Duration: `{'Public' if self.public_duration else 'Private'}`\n"
        vis_text += f"• Reason: `{'Public' if self.public_reason else 'Private'}`"

        if len(self.selected_weeks) == 0 and bool(self.existing_weeks):
            pass
        elif not has_req_changes and has_vis_changes and bool(self.existing_weeks):
            description += (
                f"\n\n*You modified your visibility settings. Click **Update Visibility** below to apply them.*"
            )
        elif not (has_req_changes or has_vis_changes) and self.original_exemptions:
            description += (
                f"\n\n*You currently have active exemptions for the selected weeks. "
                f"Modify your selections or reason above to request changes from staff, or toggle visibility to update it.*"
            )
        elif week_labels:
            description += "\n\n**Click Submit Request below to send your request to staff.**"

        embed = discord.Embed(
            title="Request Inactivity Exemption",
            description=description,
            color=0xFF0000 if (len(self.selected_weeks) == 0 and bool(self.existing_weeks)) else (0x5865f2 if week_labels else 0xFFA500),
            timestamp=datetime.now(timezone.utc),
        )

        embed.add_field(name="Visibility Settings", value=vis_text, inline=False)

        if self.existing_weeks:
            curr_labels = [format_week_label(w) for w in self.existing_weeks]
            embed.add_field(
                name="Current Active Exemptions",
                value="\n".join([f"• {l}" for l in curr_labels]),
                inline=False,
            )

        await interaction.response.edit_message(embed=embed, view=self)

    async def toggle_duration_callback(self, interaction: discord.Interaction):
        self.public_duration = not self.public_duration
        self.update_button_states()
        await self.update_embed(interaction)

    async def toggle_reason_callback(self, interaction: discord.Interaction):
        if self.reason:
            self.public_reason = not self.public_reason
        else:
            self.public_reason = False
        self.update_button_states()
        await self.update_embed(interaction)

    async def select_callback(self, interaction: discord.Interaction):
        self.selected_weeks = list(self.week_select.values)
        for option in self.week_select.options:
            option.default = option.value in self.selected_weeks
        self.update_button_states()
        await self.update_embed(interaction)

    async def reason_callback(self, interaction: discord.Interaction):
        modal = RequestReasonModal(self)
        await interaction.response.send_modal(modal)

    async def clear_callback(self, interaction: discord.Interaction):
        self.selected_weeks = []
        self.reason = None
        self.public_reason = False
        for option in self.week_select.options:
            option.default = False
        self.reason_btn.label = "Add Reason"
        self.reason_btn.style = discord.ButtonStyle.secondary
        self.update_button_states()
        await self.update_embed(interaction)

    async def submit_callback(self, interaction: discord.Interaction):
        if len(self.selected_weeks) == 0 and bool(self.existing_weeks):
            exemptions = load_exemptions()
            user_key = str(self.user_id)
            if user_key in exemptions:
                del exemptions[user_key]
                save_exemptions(exemptions)
                await update_hub_roster(self.bot)

            conf_embed = discord.Embed(
                title="Exemption Removed",
                description="Your inactivity exemption has been removed. You are no longer exempt from upcoming inactivity checks.",
                color=0x00FF00,
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.response.edit_message(embed=conf_embed, view=None)
            print(f"[INACT_HUB] User {self.user_id} removed their active exemption directly.")
            return

        weeks_changed = set(self.selected_weeks) != self.original_exemptions
        reason_changed = self.reason != self.original_reason
        has_req_changes = weeks_changed or reason_changed
        has_vis_changes = (
            self.public_duration != self.original_public_duration
            or self.public_reason != self.original_public_reason
        )

        # If ONLY visibility changed for existing exemptions
        if not has_req_changes and has_vis_changes and bool(self.existing_weeks) and len(self.selected_weeks) > 0:
            exemptions = load_exemptions()
            user_key = str(self.user_id)
            if user_key in exemptions:
                if isinstance(exemptions[user_key], list):
                    exemptions[user_key] = {"weeks": exemptions[user_key], "reason": None}
                exemptions[user_key]["public_duration"] = self.public_duration
                exemptions[user_key]["public_reason"] = self.public_reason
                save_exemptions(exemptions)
                await update_hub_roster(self.bot)

                vis_text = f"• Duration: `{'Public' if self.public_duration else 'Private'}`\n"
                vis_text += f"• Reason: `{'Public' if self.public_reason else 'Private'}`"

                conf_embed = discord.Embed(
                    title="✅ Visibility Settings Updated",
                    description=(
                        f"Your exemption visibility settings have been updated.\n\n"
                        f"**Current Visibility:**\n{vis_text}"
                    ),
                    color=0x00FF00,
                    timestamp=datetime.now(timezone.utc),
                )
                await interaction.response.edit_message(embed=conf_embed, view=None)
                print(f"[INACT_HUB] User {self.user_id} updated their exemption visibility.")
                return

        if not self.selected_weeks:
            await errors.send_custom_error(
                interaction,
                "No Weeks Selected",
                "Please select at least one week before submitting your request.",
            )
            return

        # Check for existing open request
        data = load_requests_data()
        for ch_id, ticket in data.get("tickets", {}).items():
            if ticket.get("user_id") == interaction.user.id and ticket.get("status") == "open":
                existing_ticket_id = ticket.get("ticket_id", "Unknown")
                embed = discord.Embed(
                    title="⚠️ Open Request Already Exists",
                    description=(
                        f"You already have a pending inactivity exemption request "
                        f"(Ticket Reference ID: `{existing_ticket_id}`).\n\n"
                        f"Please wait for staff to review your existing request before submitting a new one."
                    ),
                    color=0xFFAA00,
                )
                await interaction.response.edit_message(embed=embed, view=None)
                return

        await create_inactivity_ticket(
            self.bot,
            interaction,
            self.selected_weeks,
            self.reason,
            self.public_duration,
            self.public_reason,
        )


# Ticket creation logic
async def create_inactivity_ticket(
    bot,
    interaction: discord.Interaction,
    selected_weeks: List[str],
    reason: Optional[str],
    public_duration: bool = False,
    public_reason: bool = False,
):
    """Create a new inactivity exemption ticket channel on the ticket guild."""
    await interaction.response.defer(ephemeral=True)

    bot_name = bot.user.name if bot.user else "Bot"
    pending_category_name = f"{bot_name} - Pending Tickets"

    try:
        guild = bot.get_guild(TICKET_GUILD_ID)
        if not guild:
            guild = await bot.fetch_guild(TICKET_GUILD_ID)

        # Reuses the standard pending tickets category
        category = await get_or_create_category(
            guild,
            pending_category_name,
        )
        if not category:
            await errors.send_custom_error(
                interaction,
                "Configuration Error",
                "Could not create or find the ticket category. Please contact staff.",
            )
            return

        # Permission overwrites
        sanitized_name = "".join(
            c for c in interaction.user.name if c.isalnum() or c in ("-", "_")
        ).lower()[:35]
        channel_name = f"inact-{sanitized_name}"

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }

        # Recruitment Manager role gets access to inactivity tickets
        rm_role = guild.get_role(RECRUITMENT_MANAGER_ROLE_ID)
        if rm_role:
            overwrites[rm_role] = TICKET_PERMS

        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            topic=f"[Inactivity Request] Request from {interaction.user.name} ({interaction.user.id})",
            overwrites=overwrites,
        )

        current_timestamp = int(datetime.now(timezone.utc).timestamp())
        week_labels = [f"• {format_week_label(w)}" for w in selected_weeks]

        description = (
            f"**Requester:** {interaction.user.mention} (`{interaction.user.id}`)\n"
            f"**Requested Weeks:**\n" + "\n".join(week_labels) + "\n\n"
            f"**Reason:**\n```{reason if reason else 'No reason provided.'}```\n\n"
            f"**Visibility (If Approved):**\n"
            f"• Duration: `{'Public' if public_duration else 'Private'}`\n"
            f"• Reason: `{'Public' if public_reason else 'Private'}`\n\n"
            f"**Server:** {interaction.guild.name if interaction.guild else 'None'}\n"
            f"**Submitted:** <t:{current_timestamp}:F> (<t:{current_timestamp}:R>)"
        )

        ticket_embed = discord.Embed(
            title=f"Inactivity Exemption Request: {interaction.user.name}",
            description=description,
            color=0x5865f2,
            timestamp=datetime.now(timezone.utc),
        )

        view = StaffReviewView(bot, ticket_channel.id)
        message = await ticket_channel.send(embed=ticket_embed, view=view)

        ticket_data = {
            "ticket_id": message.id,
            "channel_id": ticket_channel.id,
            "user_id": interaction.user.id,
            "user_name": str(interaction.user),
            "weeks": list(selected_weeks),
            "reason": reason,
            "public_duration": public_duration,
            "public_reason": public_reason,
            "status": "open",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "guild_id": interaction.guild.id if interaction.guild else None,
            "guild_name": interaction.guild.name if interaction.guild else "DM",
        }

        data = load_requests_data()
        data["tickets"][str(ticket_channel.id)] = ticket_data
        save_requests_data(data)

        # DM Requester
        dm_sent = False
        try:
            dm_embed = discord.Embed(
                title="Inactivity Exemption Request Submitted",
                description=(
                    f"Your inactivity exemption request has been submitted to staff for review.\n\n"
                    f"**Requested Weeks:**\n" + "\n".join(week_labels) + "\n\n"
                    f"**Reason:**\n{reason if reason else '*None provided*'}\n\n"
                    f"Staff will review your request and you will receive a DM once a decision has been made."
                ),
                color=0x00FF00,
                timestamp=datetime.now(timezone.utc),
            )
            dm_embed.add_field(name="Ticket Reference ID", value=str(message.id), inline=False)
            await interaction.user.send(embed=dm_embed)
            dm_sent = True
        except discord.Forbidden:
            dm_sent = False

        if dm_sent:
            success_embed = discord.Embed(
                title="Request Submitted",
                description=(
                    f"Your inactivity exemption request has been submitted successfully "
                    f"(Reference ID: `{message.id}`).\n\n"
                    f"Check your DMs for confirmation and status updates."
                ),
                color=0x00FF00,
            )
        else:
            success_embed = discord.Embed(
                title="Request Submitted",
                description=(
                    f"Your inactivity exemption request has been submitted successfully "
                    f"(Reference ID: `{message.id}`).\n\n"
                    f"⚠️ *However, a DM could not be send to you. Please enable DMs to receive status updates.*"
                ),
                color=0xFFAA00,
            )

        await interaction.edit_original_response(embed=success_embed, view=None)
        print(f"[INACT_HUB] Created ticket {channel_name} ({ticket_channel.id}) for {interaction.user}")

    except discord.Forbidden:
        await errors.send_custom_error(
            interaction,
            "Ticket Creation Failed",
            "I don't have permission to create channels in the ticket category.",
        )
    except Exception as e:
        print(f"[INACT_HUB] Error creating ticket: {e}")
        import traceback
        traceback.print_exc()
        await errors.UNEXPECTED_ERROR.send(interaction)


# Staff review UI
class StaffEditReasonModal(discord.ui.Modal, title="Edit Exemption Reason"):
    """Modal for staff to edit the reason on a ticket."""
    reason_input = discord.ui.TextInput(
        label="Reason",
        placeholder="Enter or edit the reason...",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view
        if parent_view.reason:
            self.reason_input.default = parent_view.reason

    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.reason = self.reason_input.value.strip() or None

        if self.parent_view.reason:
            self.parent_view.reason_btn.label = "Edit Reason"
            self.parent_view.reason_btn.style = discord.ButtonStyle.primary
        else:
            self.parent_view.reason_btn.label = "Add Reason"
            self.parent_view.reason_btn.style = discord.ButtonStyle.secondary
            self.parent_view.public_reason = False

        self.parent_view.update_confirm_button_state()
        await self.parent_view.update_embed(interaction)


class StaffEditRequestView(discord.ui.View):
    """Ephemeral view for staff to edit weeks/reason/visibility on an open request ticket."""
    def __init__(self, bot, ticket_channel_id: int, ticket_data: dict, staff_user_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.ticket_channel_id = ticket_channel_id
        self.ticket_data = ticket_data
        self.staff_user_id = staff_user_id

        self.selected_weeks: List[str] = list(ticket_data.get("weeks", []))
        self.original_weeks = set(self.selected_weeks)
        self.reason: Optional[str] = ticket_data.get("reason")
        self.original_reason = self.reason
        self.had_permanent = "permanent" in self.selected_weeks

        self.public_duration = False if self.had_permanent else ticket_data.get("public_duration", False)
        self.public_reason = False if self.had_permanent else ticket_data.get("public_reason", False)
        self.original_public_duration = self.public_duration
        self.original_public_reason = self.public_reason

        # Options include permanent for staff
        future_weeks = get_future_weeks(15, second_check=True)
        options = [
            discord.SelectOption(
                label="🔒 Permanent Exemption",
                value="permanent",
                description="Exempt from all future inactivity checks (always private)",
                default="permanent" in self.selected_weeks,
            )
        ]

        for i, (label, start_date, end_date) in enumerate(future_weeks):
            value = f"{start_date.isoformat()}_{end_date.isoformat()}"
            options.append(discord.SelectOption(
                label=label,
                value=value,
                description="Current/Next check" if i == 0 else "Future week",
                default=value in self.selected_weeks,
            ))
        options = options[:25]

        self.week_select = discord.ui.Select(
            placeholder="Select weeks to exempt...",
            min_values=0,
            max_values=len(options),
            options=options,
            row=0,
        )
        self.week_select.callback = self.select_callback
        self.add_item(self.week_select)

        self.reason_btn = discord.ui.Button(
            label="Edit Reason" if self.reason else "Add Reason",
            style=discord.ButtonStyle.primary if self.reason else discord.ButtonStyle.secondary,
            row=1,
        )
        self.reason_btn.callback = self.reason_callback
        self.add_item(self.reason_btn)

        self.confirm_btn = discord.ui.Button(
            label="Save Edits",
            style=discord.ButtonStyle.success,
            row=1,
            disabled=True,
        )
        self.confirm_btn.callback = self.confirm_callback
        self.add_item(self.confirm_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.staff_user_id:
            await errors.send_custom_error(
                interaction,
                "Not Allowed",
                "Only the staff member who opened this editor can interact with it.",
            )
            return False
        return True

    def update_confirm_button_state(self):
        selection_changed = set(self.selected_weeks) != self.original_weeks
        reason_changed = self.reason != self.original_reason
        has_changes = selection_changed or reason_changed
        invalid_state = bool(self.reason and not self.selected_weeks)
        self.confirm_btn.disabled = not has_changes or invalid_state

    async def update_embed(self, interaction: discord.Interaction):
        week_labels = [format_week_label(w) for w in self.selected_weeks]

        if week_labels:
            description = (
                f"Editing request for **{self.ticket_data.get('user_name', 'Requester')}**:\n\n"
                + "\n".join([f"• {label}" for label in week_labels])
            )
        else:
            description = (
                f"No weeks selected for **{self.ticket_data.get('user_name', 'Requester')}**."
            )

        if self.reason:
            description += f"\n\n**Reason:** {self.reason}"

        if "permanent" in self.selected_weeks:
            vis_text = "*Permanent exemptions cannot be made public in any way.*"
        else:
            vis_text = (
                f"• Duration: `{'Public' if self.public_duration else 'Private'}`\n"
                f"• Reason: `{'Public' if self.public_reason else 'Private'}`\n"
            )

        description += "\n\n**Click Save Edits to update the ticket embed.**"

        embed = discord.Embed(
            title="Edit Inactivity Request",
            description=description,
            color=0x5865f2 if week_labels else 0xFFA500,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Visibility Settings (Member-Configured)", value=vis_text, inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    async def select_callback(self, interaction: discord.Interaction):
        self.selected_weeks = list(self.week_select.values)

        # Handle permanent option toggle
        has_perm_now = "permanent" in self.selected_weeks
        has_other = any(w != "permanent" for w in self.selected_weeks)

        if has_perm_now and has_other:
            if self.had_permanent:
                self.selected_weeks = [w for w in self.selected_weeks if w != "permanent"]
            else:
                self.selected_weeks = ["permanent"]

        self.had_permanent = "permanent" in self.selected_weeks

        if self.had_permanent:
            self.public_duration = False
            self.public_reason = False
        else:
            self.public_duration = self.original_public_duration
            self.public_reason = self.original_public_reason

        for option in self.week_select.options:
            option.default = option.value in self.selected_weeks

        self.update_confirm_button_state()
        await self.update_embed(interaction)

    async def reason_callback(self, interaction: discord.Interaction):
        modal = StaffEditReasonModal(self)
        await interaction.response.send_modal(modal)

    async def confirm_callback(self, interaction: discord.Interaction):
        # Update ticket data in JSON
        data = load_requests_data()
        channel_key = str(self.ticket_channel_id)
        if channel_key not in data.get("tickets", {}):
            await errors.send_custom_error(
                interaction,
                "Ticket Not Found",
                "This ticket record could not be found in active requests.",
            )
            return

        td = data["tickets"][channel_key]
        td["weeks"] = list(self.selected_weeks)
        td["reason"] = self.reason
        td["public_duration"] = False if "permanent" in self.selected_weeks else self.public_duration
        td["public_reason"] = False if "permanent" in self.selected_weeks else self.public_reason
        td["edited_by"] = interaction.user.id
        td["edited_at"] = datetime.now(timezone.utc).isoformat()
        save_requests_data(data)

        # Update the ticket message embed
        channel = self.bot.get_channel(self.ticket_channel_id)
        if channel and td.get("ticket_id"):
            try:
                ticket_msg = await channel.fetch_message(td["ticket_id"])
                user_id = td.get("user_id")
                user_name = td.get("user_name", "Unknown")
                week_labels = [f"• {format_week_label(w)}" for w in self.selected_weeks]

                created_dt_str = td.get("created_at", datetime.now(timezone.utc).isoformat())
                try:
                    created_dt = datetime.fromisoformat(created_dt_str)
                    created_ts = int(created_dt.timestamp())
                except Exception:
                    created_ts = int(datetime.now(timezone.utc).timestamp())

                if "permanent" in self.selected_weeks:
                    vis_text = "`Permanent: Always Private`"
                else:
                    vis_text = (
                        f"• Duration: `{'Public' if td['public_duration'] else 'Private'}`\n"
                        f"• Reason: `{'Public' if td['public_reason'] else 'Private'}`"
                    )

                description = (
                    f"**Requester:** <@{user_id}> (`{user_id}`)\n"
                    f"**Requested Weeks:**\n" + "\n".join(week_labels) + "\n\n"
                    f"**Reason:**\n```{self.reason if self.reason else 'No reason provided.'}```\n\n"
                    f"**Visibility (If Approved):**\n{vis_text}\n\n"
                    f"**Server:** {td.get('guild_name', 'None')}\n"
                    f"**Submitted:** <t:{created_ts}:F> (<t:{created_ts}:R>)"
                )

                embed = discord.Embed(
                    title=f"Inactivity Exemption Request: {user_name}",
                    description=description,
                    color=0x5865f2,
                    timestamp=datetime.now(timezone.utc),
                )
                embed.set_footer(
                    text=f"Edited by {interaction.user.name}",
                    icon_url=interaction.user.display_avatar.url,
                )
                await ticket_msg.edit(embed=embed)
            except Exception as e:
                print(f"[INACT_HUB] Error updating ticket message embed: {e}")

        conf_embed = discord.Embed(
            title="Request Updated",
            description="Ticket request details have been updated. The user has **not** been notified.",
            color=0x00FF00,
        )
        await interaction.response.edit_message(embed=conf_embed, view=None)


class DenyRequestModal(discord.ui.Modal, title="Deny Inactivity Request"):
    """Modal for staff to specify an optional reason when denying a request."""
    reason = discord.ui.TextInput(
        label="Reason",
        placeholder="Optional reason for denying this request...",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, staff_view, message: discord.Message):
        super().__init__()
        self.staff_view = staff_view
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.staff_view.process_denial(interaction, self.reason.value.strip() or None, self.message)


class StaffReviewView(discord.ui.View):
    """Persistent staff review view attached to open inactivity request channels."""
    def __init__(self, bot, ticket_channel_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.ticket_channel_id = ticket_channel_id

    def _can_manage(self, user: discord.Member) -> bool:
        """Check if user has permission to manage/approve/deny requests."""
        if is_restricted_user(user):
            return False

        owner_id = int(os.getenv("OWNER_ID", "0"))
        if user.id == owner_id:
            return True

        if hasattr(user, "guild_permissions") and user.guild_permissions.administrator:
            return True

        user_role_ids = [r.id for r in getattr(user, "roles", [])]
        if PARLIAMENT_ROLE_ID in user_role_ids or RECRUITMENT_MANAGER_ROLE_ID in user_role_ids:
            return True

        return has_roles(user, REQUIRED_ROLES)

    @discord.ui.button(
        label="Edit Request",
        style=discord.ButtonStyle.primary,
        custom_id="inactivity_request_edit_btn",
    )
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._can_manage(interaction.user):
            await errors.NO_PERMISSION.send(interaction)
            return

        data = load_requests_data()
        ticket_data = data.get("tickets", {}).get(str(self.ticket_channel_id))
        if not ticket_data:
            await errors.send_custom_error(
                interaction,
                "Ticket Not Found",
                "This ticket could not be found in active requests.",
            )
            return

        edit_view = StaffEditRequestView(
            self.bot,
            self.ticket_channel_id,
            ticket_data,
            interaction.user.id,
        )
        embed = discord.Embed(
            title="Edit Inactivity Request",
            description=f"Editing exemption request for **{ticket_data.get('user_name', 'Requester')}**.\n\nMake your changes below and click Save Edits.",
            color=0x5865f2,
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.response.send_message(embed=embed, view=edit_view, ephemeral=True)

    @discord.ui.button(
        label="Approve & Grant",
        style=discord.ButtonStyle.success,
        custom_id="inactivity_request_approve_btn",
    )
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._can_manage(interaction.user):
            await errors.NO_PERMISSION.send(interaction)
            return

        await interaction.response.defer(ephemeral=True)

        data = load_requests_data()
        ticket_data = data.get("tickets", {}).get(str(self.ticket_channel_id))
        if not ticket_data:
            await errors.send_custom_error(
                interaction,
                "Ticket Not Found",
                "This ticket could not be found in active requests.",
            )
            return

        user_id = ticket_data.get("user_id")
        weeks = ticket_data.get("weeks", [])
        reason = ticket_data.get("reason")
        is_perm = "permanent" in weeks
        public_duration = False if is_perm else ticket_data.get("public_duration", True)
        public_reason = False if is_perm else ticket_data.get("public_reason", False)

        # 1. Update data/inactivity_exemptions.json
        exemptions = load_exemptions()
        user_key = str(user_id)
        if weeks or reason:
            exemptions[user_key] = {
                "weeks": list(weeks),
                "reason": reason,
                "public_duration": public_duration,
                "public_reason": public_reason,
            }
        elif user_key in exemptions:
            del exemptions[user_key]

        if not save_exemptions(exemptions):
            await errors.send_custom_error(
                interaction,
                "Save Failed",
                "Failed to save exemptions to inactivity_exemptions.json.",
            )
            return

        # 2. DM the user
        try:
            target_user = await self.bot.fetch_user(user_id)
            week_labels = [f"• {format_week_label(w)}" for w in weeks]
            dm_embed = discord.Embed(
                title="Inactivity Exemption Approved",
                description=(
                    f"Your inactivity exemption request has been **approved** by staff.\n\n"
                    f"**Granted Weeks:**\n" + "\n".join(week_labels) + "\n\n"
                    f"**Reason:**\n{reason if reason else '*None provided*'}"
                ),
                color=0x00FF00,
                timestamp=datetime.now(timezone.utc),
            )
            dm_embed.set_footer(
                text=f"Approved by {interaction.user.name}",
                icon_url=interaction.user.display_avatar.url,
            )
            await target_user.send(embed=dm_embed)
        except Exception:
            pass  # User DMs closed / forbidden

        # 3. Archive channel directly to standard Archived Tickets category
        ticket_channel = self.bot.get_channel(self.ticket_channel_id)
        bot_name = self.bot.user.name if self.bot.user else "Bot"
        archived_category_name = f"{bot_name} - Archived Tickets"
        pending_base = f"{bot_name} - Pending Tickets"

        if ticket_channel:
            # Strip extra staff role overwrites
            rm_role = ticket_channel.guild.get_role(RECRUITMENT_MANAGER_ROLE_ID)
            if rm_role and rm_role in ticket_channel.overwrites:
                try:
                    await ticket_channel.set_permissions(rm_role, overwrite=None)
                except Exception:
                    pass

            archived_cat = await get_or_create_category(
                ticket_channel.guild,
                archived_category_name,
            )
            if archived_cat:
                try:
                    await ticket_channel.edit(category=archived_cat)
                except Exception as e:
                    print(f"[INACT_HUB] Error moving channel to archive: {e}")

            await cleanup_empty_categories(
                ticket_channel.guild,
                pending_base,
            )

        # 4. Move record to archived_tickets
        ticket_data["status"] = "approved"
        ticket_data["resolved_at"] = datetime.now(timezone.utc).isoformat()
        ticket_data["resolved_by"] = interaction.user.id
        ticket_data["resolved_by_name"] = str(interaction.user)

        data["archived_tickets"][str(self.ticket_channel_id)] = ticket_data
        if str(self.ticket_channel_id) in data["tickets"]:
            del data["tickets"][str(self.ticket_channel_id)]
        save_requests_data(data)

        # 5. Disable buttons on ticket message
        for item in self.children:
            item.disabled = True
        button.label = "Approved & Granted"
        button.style = discord.ButtonStyle.success

        if interaction.message:
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass

        # 6. Update the live public roster in the hub channel
        await update_hub_roster(self.bot)

        conf_embed = discord.Embed(
            title="Request Approved",
            description=f"Exemptions granted for <@{user_id}>. The user was notified via DM, the channel was archived, and the public roster was updated.",
            color=0x00FF00,
        )
        await interaction.followup.send(embed=conf_embed, ephemeral=True)
        print(f"[INACT_HUB] Ticket {self.ticket_channel_id} approved by {interaction.user}")

    @discord.ui.button(
        label="Deny",
        style=discord.ButtonStyle.danger,
        custom_id="inactivity_request_deny_btn",
    )
    async def deny_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._can_manage(interaction.user):
            await errors.NO_PERMISSION.send(interaction)
            return

        modal = DenyRequestModal(self, interaction.message)
        await interaction.response.send_modal(modal)

    async def process_denial(self, interaction: discord.Interaction, deny_reason: Optional[str], message: discord.Message):
        """Process request denial after modal submission."""
        data = load_requests_data()
        ticket_data = data.get("tickets", {}).get(str(self.ticket_channel_id))
        if not ticket_data:
            await errors.send_custom_error(
                interaction,
                "Ticket Not Found",
                "This ticket could not be found in active requests.",
            )
            return

        user_id = ticket_data.get("user_id")

        # 1. DM the user
        try:
            target_user = await self.bot.fetch_user(user_id)
            dm_embed = discord.Embed(
                title="Inactivity Exemption Denied",
                description="Your inactivity exemption request has been **denied** by staff.",
                color=0xFF0000,
                timestamp=datetime.now(timezone.utc),
            )
            if deny_reason:
                dm_embed.add_field(name="Reason", value=deny_reason, inline=False)
            dm_embed.set_footer(
                text=f"Reviewed by {interaction.user.name}",
                icon_url=interaction.user.display_avatar.url,
            )
            await target_user.send(embed=dm_embed)
        except Exception:
            pass

        # 2. Archive channel directly to standard Archived Tickets category
        ticket_channel = self.bot.get_channel(self.ticket_channel_id)
        bot_name = self.bot.user.name if self.bot.user else "Bot"
        archived_category_name = f"{bot_name} - Archived Tickets"
        pending_base = f"{bot_name} - Pending Tickets"

        if ticket_channel:
            rm_role = ticket_channel.guild.get_role(RECRUITMENT_MANAGER_ROLE_ID)
            if rm_role and rm_role in ticket_channel.overwrites:
                try:
                    await ticket_channel.set_permissions(rm_role, overwrite=None)
                except Exception:
                    pass

            archived_cat = await get_or_create_category(
                ticket_channel.guild,
                archived_category_name,
            )
            if archived_cat:
                try:
                    await ticket_channel.edit(category=archived_cat)
                except Exception as e:
                    print(f"[INACT_HUB] Error moving channel to archive: {e}")

            await cleanup_empty_categories(
                ticket_channel.guild,
                pending_base,
            )

        # 3. Update JSON
        ticket_data["status"] = "denied"
        if deny_reason:
            ticket_data["deny_reason"] = deny_reason
        ticket_data["resolved_at"] = datetime.now(timezone.utc).isoformat()
        ticket_data["resolved_by"] = interaction.user.id
        ticket_data["resolved_by_name"] = str(interaction.user)

        data["archived_tickets"][str(self.ticket_channel_id)] = ticket_data
        if str(self.ticket_channel_id) in data["tickets"]:
            del data["tickets"][str(self.ticket_channel_id)]
        save_requests_data(data)

        # 4. Disable buttons
        for item in self.children:
            item.disabled = True
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.custom_id == "inactivity_request_deny_btn":
                item.label = "Request Denied"
                break

        if message:
            try:
                await message.edit(view=self)
            except Exception:
                pass

        conf_embed = discord.Embed(
            title="Request Denied",
            description=f"Inactivity request for <@{user_id}> was denied and the channel was archived.",
            color=0xFF0000,
        )
        await interaction.followup.send(embed=conf_embed, ephemeral=True)
        print(f"[INACT_HUB] Ticket {self.ticket_channel_id} denied by {interaction.user}")


class PermanentExemptionActiveView(discord.ui.View):
    """View shown to permanently exempt users, allowing them to remove their permanent exemption."""
    def __init__(self, user_id: int, bot):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await errors.send_custom_error(
                interaction,
                "Not Allowed",
                "Only the person who opened this view can interact with it.",
            )
            return False
        return True

    @discord.ui.button(
        label="Remove Permanent Exemption",
        style=discord.ButtonStyle.danger,
        custom_id="remove_permanent_exemption_btn",
    )
    async def remove_perm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        exemptions = load_exemptions()
        user_key = str(self.user_id)
        if user_key in exemptions:
            del exemptions[user_key]
            save_exemptions(exemptions)
            await update_hub_roster(self.bot)

        embed = discord.Embed(
            title="Permanent Exemption Removed",
            description="Your permanent inactivity exemption has been removed. You are no longer exempt from upcoming inactivity checks.",
            color=0x00FF00,
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.response.edit_message(embed=embed, view=None)
        print(f"[INACT_HUB] User {self.user_id} removed their permanent exemption directly.")


# Hub persistent view and restoration
class InactivityHubView(discord.ui.View):
    """Persistent view attached to the main Inactivity Exemption Hub embed."""
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Request Exemption",
        style=discord.ButtonStyle.primary,
        custom_id="request_inactivity_exemption",
    )
    async def request_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 0. Check for required Sindrian Citizen role
        user_role_ids = [r.id for r in getattr(interaction.user, "roles", [])]
        owner_id = int(os.getenv("OWNER_ID", "0"))
        is_owner = interaction.user.id == owner_id
        is_admin = (
            hasattr(interaction.user, "guild_permissions")
            and interaction.user.guild_permissions.administrator
        )
        has_citizen = SINDIAN_CITIZEN_ROLE_ID in user_role_ids
        has_parliament = PARLIAMENT_ROLE_ID in user_role_ids

        if not (has_citizen or is_owner or is_admin or has_parliament):
            await errors.send_custom_error(
                interaction,
                "Permission Denied",
                "You must have the **Sindrian Citizen** role to request an inactivity exemption.",
                steps=["Make sure you have the Sindrian Citizen role assigned to your account."],
            )
            return

        # 1. Cleanup expired exemptions
        cleanup_expired_exemptions()

        # 2. Check for open requests in progress
        data = load_requests_data()
        for ch_id, ticket in data.get("tickets", {}).items():
            if ticket.get("user_id") == interaction.user.id and ticket.get("status") == "open":
                ticket_id = ticket.get("ticket_id", "Unknown")
                embed = discord.Embed(
                    title="⚠️ Open Request Already Exists",
                    description=(
                        f"You already have a pending inactivity exemption request "
                        f"(Ticket Reference ID: `{ticket_id}`).\n\n"
                        f"Please wait for staff to review your existing request before submitting a new one."
                    ),
                    color=0xFFAA00,
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

        # 3. Check if user is already permanently exempt
        existing_weeks, existing_reason = get_user_exemption_data(interaction.user.id)
        if "permanent" in existing_weeks:
            embed = discord.Embed(
                title="🔒 Permanent Exemption Active",
                description=(
                    f"**{interaction.user.mention}**, you are currently **permanently exempt** "
                    f"from all inactivity checks.\n\n"
                    f"If you wish to remove your permanent exemption, click the button below."
                ),
                color=0x00FF00,
                timestamp=datetime.now(timezone.utc),
            )
            if existing_reason:
                embed.add_field(name="Reason", value=existing_reason, inline=False)
            perm_view = PermanentExemptionActiveView(interaction.user.id, self.bot)
            await interaction.response.send_message(embed=embed, view=perm_view, ephemeral=True)
            return

        # 4. Load any existing visibility preferences
        exemptions = load_exemptions()
        user_ex_data = exemptions.get(str(interaction.user.id), {})
        existing_public_duration = False
        existing_public_reason = False
        if isinstance(user_ex_data, dict):
            existing_public_duration = user_ex_data.get("public_duration", False)
            existing_public_reason = user_ex_data.get("public_reason", False)

        # 5. Open self-serve view pre-populated with active exemptions (if any)
        view = SelfServeRequestView(
            interaction.user.id,
            self.bot,
            existing_weeks=existing_weeks,
            existing_reason=existing_reason,
            existing_public_duration=existing_public_duration,
            existing_public_reason=existing_public_reason,
        )

        week_labels = [format_week_label(w) for w in view.selected_weeks]
        if week_labels:
            description = (
                f"Exemption request for **{interaction.user.mention}**:\n\n"
                + "\n".join([f"• {label}" for label in week_labels])
            )
            if existing_reason:
                description += f"\n\n**Reason:** {existing_reason}"
            description += (
                f"\n\n*You currently have active exemptions for the selected weeks. "
                f"Modify your selections, reason, or privacy toggles above to submit an updated request.*"
            )
        else:
            description = (
                f"Select one or more upcoming weeks from the dropdown below.\n\n"
                f"⚠️ *You must select at least one week to submit a request.*"
            )

        vis_text = f"• Duration: `{'Public' if view.public_duration else 'Private'}`\n"
        vis_text += f"• Reason: `{'Public' if view.public_reason else 'Private'}`"

        embed = discord.Embed(
            title="Request Inactivity Exemption",
            description=description,
            color=0x5865f2 if week_labels else 0xFFA500,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Visibility Settings (If Approved)", value=vis_text, inline=False)

        if existing_weeks:
            curr_labels = [format_week_label(w) for w in existing_weeks]
            embed.add_field(
                name="Current Active Exemptions",
                value="\n".join([f"• {l}" for l in curr_labels]),
                inline=False,
            )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def restore_inactivity_hub_view(bot):
    """Restore the persistent view on the hub message and refresh the roster embed."""
    bot.add_view(InactivityHubView(bot))
    hub_data = load_hub_data()
    channel_id = hub_data.get("channel_id")
    message_id = hub_data.get("message_id")

    restored_hub = False
    if channel_id and message_id:
        try:
            channel = bot.get_channel(channel_id)
            if not channel:
                channel = await bot.fetch_channel(channel_id)
            if channel:
                message = await channel.fetch_message(message_id)
                await message.edit(view=InactivityHubView(bot))
                print(f"[INACT_HUB] Restored hub view in channel {channel_id}")
                restored_hub = True
        except Exception as e:
            print(f"[INACT_HUB] ⚠️ Could not fetch/edit hub message: {e}")

    # Also update roster message
    await update_hub_roster(bot)
    return restored_hub


async def restore_inactivity_requests_views(bot) -> Tuple[int, int]:
    """Restore views for all open inactivity request tickets."""
    data = load_requests_data()
    tickets = data.get("tickets", {})
    if not tickets:
        return 0, 0

    restored = 0
    failed = 0

    for channel_id_str, ticket_data in tickets.items():
        if ticket_data.get("status") != "open":
            continue

        try:
            channel_id = int(channel_id_str)
            channel = bot.get_channel(channel_id)
            if not channel:
                channel = await bot.fetch_channel(channel_id)

            if not channel:
                failed += 1
                continue

            message_id = ticket_data.get("ticket_id")
            if not message_id:
                failed += 1
                continue

            view = StaffReviewView(bot, channel_id)
            bot.add_view(view)

            try:
                message = await channel.fetch_message(message_id)
                await message.edit(view=view)
                restored += 1
            except Exception:
                failed += 1

        except Exception as e:
            print(f"[INACT_HUB] Error restoring ticket {channel_id_str}: {e}")
            failed += 1

    print(f"[INACT_HUB] Restored {restored} request views, {failed} failed")
    return restored, failed


# Module setup and commands
def setup(bot, has_required_role, config):
    """Setup function for bot integration."""

    @bot.tree.command(
        name="inactivity_hub_setup",
        description="Set up the Inactivity Exemption Hub in a channel (Admin/Owner only)",
    )
    @app_commands.describe(channel="The channel to turn into the Inactivity Exemption Hub")
    async def inactivity_hub_setup(interaction: discord.Interaction, channel: discord.TextChannel):
        """Set up the persistent Inactivity Exemption Hub."""
        owner_id = int(os.getenv("OWNER_ID", "0"))
        is_owner = interaction.user.id == owner_id
        is_admin = (
            hasattr(interaction.user, "guild_permissions")
            and interaction.user.guild_permissions.administrator
        )
        user_role_ids = [r.id for r in getattr(interaction.user, "roles", [])]
        has_parliament = PARLIAMENT_ROLE_ID in user_role_ids

        if not (is_owner or is_admin or has_parliament):
            await errors.NO_PERMISSION.send(interaction)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            # Message 1 (Top): Live Public Roster Embed
            roster_embed = build_roster_embed()
            roster_message = await channel.send(embed=roster_embed)

            # Message 2 (Bottom): Main Hub Instructions & Request Button
            hub_embed = discord.Embed(
                title="Inactivity Exemption Hub",
                description=(
                    "Welcome to the **Inactivity Exemption Hub**.\n\n"
                    "If you will be unable to meet the weekly playtime requirement on Wynncraft due to "
                    "IRL commitments, vacations, exams, or other reasons, you can request an inactivity exemption here. "
                    "For more info go to the [Inactivity Limit](https://discord.com/channels/554418045397762048/1515398140797387012) post.\n\n"
                    "**How it works:**\n"
                    "1. Click the **Request Exemption** button below.\n"
                    "2. Select the upcoming week(s) you need exemption for.\n"
                    "3. Optionally add a reason explaining your absence.\n"
                    "4. Submit your request. Staff will review it and you will receive a decision via DM.\n\n"
                ),
                color=0x5865f2,
            )

            view = InactivityHubView(bot)
            hub_message = await channel.send(embed=hub_embed, view=view)

            save_hub_data({
                "channel_id": channel.id,
                "message_id": hub_message.id,
                "roster_message_id": roster_message.id,
                "guild_id": channel.guild.id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

            result_embed = discord.Embed(
                title="Hub Configured",
                description=f"Inactivity Exemption Hub and Public Roster have been successfully configured in {channel.mention}.",
                color=0x00FF00,
            )
            await interaction.followup.send(embed=result_embed, ephemeral=True)
            print(f"[INACT_HUB] Hub setup in channel {channel.name} ({channel.id}) by {interaction.user}")

        except discord.Forbidden:
            await errors.send_custom_error(
                interaction,
                "Missing Permissions",
                f"I don't have permission to send messages or embed links in {channel.mention}.",
            )
        except Exception as e:
            print(f"[INACT_HUB] Error setting up hub: {e}")
            import traceback
            traceback.print_exc()
            await errors.UNEXPECTED_ERROR.send(interaction)

    @bot.tree.command(
        name="refresh_inactivity_requests",
        description="Manually refresh open inactivity request views & public roster (Staff only)",
    )
    async def refresh_inactivity_requests(interaction: discord.Interaction):
        """Manually refresh views for all open inactivity requests, hub, and public roster."""
        owner_id = int(os.getenv("OWNER_ID", "0"))
        is_owner = interaction.user.id == owner_id
        is_admin = (
            hasattr(interaction.user, "guild_permissions")
            and interaction.user.guild_permissions.administrator
        )
        user_role_ids = [r.id for r in getattr(interaction.user, "roles", [])]
        has_parliament = PARLIAMENT_ROLE_ID in user_role_ids

        if not (is_owner or is_admin or has_parliament):
            await errors.NO_PERMISSION.send(interaction)
            return

        await interaction.response.defer(ephemeral=True)

        restored, failed = await restore_inactivity_requests_views(bot)
        hub_ok = await restore_inactivity_hub_view(bot)
        roster_ok = await update_hub_roster(bot)

        result_embed = discord.Embed(
            title="🔄 Inactivity Views & Roster Refreshed",
            description=(
                f"**Request Tickets:** {restored} restored, {failed} failed\n"
                f"**Hub Embed:** {'✅ Restored' if hub_ok else '⚠️ Not found or failed'}\n"
                f"**Public Roster:** {'✅ Updated' if roster_ok else '⚠️ Not configured'}"
            ),
            color=0x00FF00 if failed == 0 else 0xFFAA00,
        )
        await interaction.followup.send(embed=result_embed, ephemeral=True)

    # Attach restore hooks to bot instance
    bot._restore_inactivity_requests_views = lambda: restore_inactivity_requests_views(bot)
    bot._restore_inactivity_hub_view = lambda: restore_inactivity_hub_view(bot)

    # Re-register view immediately for persistent button handling
    bot.add_view(InactivityHubView(bot))

    print("[OK] Loaded inactivity_hub_setup command")
    print("[OK] Loaded refresh_inactivity_requests command")
