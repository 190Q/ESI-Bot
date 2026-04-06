import discord
from discord import app_commands
from datetime import datetime
import os
import json
import asyncio
from utils.permissions import has_roles
from utils.paths import PROJECT_ROOT, DATA_DIR, DB_DIR

REQUIRED_ROLES = []
CATEGORY_THRESHOLD = 50

# Role IDs for ticket permissions
BASE_TICKET_ROLE = 1448533030091227227       # Added to every ticket
DEVELOPER_ROLE = 1464696049896788104         # Bug Report, Suggestion, Support / Help, Other
USER_SUPPORT_ROLE = 1464695189380530423       # Suggestion, Support / Help, Other

TICKET_PERMS = discord.PermissionOverwrite(
    view_channel=True,
    send_messages=True,
    attach_files=True,
    add_reactions=True,
    use_external_emojis=True,
    use_external_stickers=True,
    read_message_history=True,
)

# Which extra roles to add per category
CATEGORY_ROLES = {
    "Bug Report":       [BASE_TICKET_ROLE, DEVELOPER_ROLE],
    "Suggestion":       [BASE_TICKET_ROLE, DEVELOPER_ROLE, USER_SUPPORT_ROLE],
    "Support / Help":   [BASE_TICKET_ROLE, DEVELOPER_ROLE, USER_SUPPORT_ROLE],
    "Other":            [BASE_TICKET_ROLE, DEVELOPER_ROLE, USER_SUPPORT_ROLE],
}

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    async def get_or_create_category(guild, category_name):
        """Get existing category with space or create a new one"""
        base_name = category_name.split(' #')[0]
        
        # Load existing category tracking
        tickets_file = os.path.join(str(PROJECT_ROOT), "data", "support_tickets.json")
        try:
            with open(tickets_file, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {"tickets": {}, "categories": {}}
        
        if "categories" not in data:
            data["categories"] = {}
        
        # Clean up deleted categories from tracking
        tracked_categories = data["categories"].get(base_name, [])
        valid_categories = []
        for cat_id in tracked_categories:
            category = guild.get_channel(cat_id)
            if category and len(category.channels) < CATEGORY_THRESHOLD:
                valid_categories.append(cat_id)
                # Return first category with space
                if len(valid_categories) == len(tracked_categories):
                    return category
            elif category and len(category.channels) >= CATEGORY_THRESHOLD:
                valid_categories.append(cat_id)
            # If category doesn't exist, don't add to valid list
        
        data["categories"][base_name] = valid_categories
        
        # Return existing category with space
        for cat_id in valid_categories:
            category = guild.get_channel(cat_id)
            if category and len(category.channels) < CATEGORY_THRESHOLD:
                with open(tickets_file, "w") as f:
                    json.dump(data, f, indent=4)
                return category
        
        # Create new category
        try:
            next_number = len(valid_categories) + 1
            new_category_name = f"{category_name} #{next_number}" if next_number > 1 else category_name
            
            position = None
            if valid_categories:
                last_cat = guild.get_channel(valid_categories[-1])
                if last_cat:
                    position = last_cat.position + 1
            
            new_category = await guild.create_category(new_category_name, position=position)
            
            # Track new category
            data["categories"][base_name].append(new_category.id)
            with open(tickets_file, "w") as f:
                json.dump(data, f, indent=4)
            
            print(f"Created new category: {new_category_name} at position {position}")
            return new_category
        except Exception as e:
            print(f"Error creating category: {e}")
            return None
    
    class SupportModal(discord.ui.Modal, title="Contact Support"):
        subject = discord.ui.TextInput(
            label="Subject",
            placeholder="Brief summary of your issue",
            required=True,
            max_length=100
        )
        
        message = discord.ui.TextInput(
            label="Message",
            placeholder="Describe your issue in detail",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1000
        )

        def __init__(self, category: str):
            super().__init__()
            self.category = category
        
        async def on_submit(self, interaction: discord.Interaction):
            # Defer immediately to prevent timeout
            await interaction.response.defer(ephemeral=True)
            
            guild_id = 1448532791686860923
            bot_name = bot.user.name if bot.user else "Bot"
            pending_category_name = f"{bot_name} - Pending Tickets"
            acknowledged_category_name = f"{bot_name} - Acknowledged Tickets"
            
            try:
                guild = bot.get_guild(guild_id)
                if not guild:
                    guild = await bot.fetch_guild(guild_id)
                
                # Get or create the pending tickets category
                category = await get_or_create_category(guild, pending_category_name)
                if not category:
                    error_embed = discord.Embed(
                        title="Configuration Error",
                        description="Could not create or find ticket category. Please contact the bot administrator.",
                        color=0xFF0000
                    )
                    await interaction.followup.send(embed=error_embed, ephemeral=True)
                    return
                
                # Create ticket channel
                sanitized_subject = "".join(c for c in self.subject.value if c.isalnum() or c in ('-', '_')).lower()[:50]
                channel_name = f"{interaction.user.name}-{sanitized_subject}"
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                    guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
                }

                # Add role overwrites based on ticket category
                role_ids_for_category = CATEGORY_ROLES.get(self.category, [BASE_TICKET_ROLE])
                for role_id in role_ids_for_category:
                    role = guild.get_role(role_id)
                    if role:
                        overwrites[role] = TICKET_PERMS

                ticket_channel = await guild.create_text_channel(
                    name=channel_name,
                    category=category,
                    topic=f"[{self.category}] Support ticket from {interaction.user.name} ({interaction.user.id})",
                    overwrites=overwrites
                )
                
                # Get current timestamp for Discord formatting (timezone-aware to avoid offset bugs)
                from datetime import timezone
                current_timestamp = int(datetime.now(timezone.utc).timestamp())
                
                description = f'**"{self.subject.value}"** from {interaction.user.mention}\n'
                description += f"**Category:** {self.category}\n"
                description += f"```\n{self.message.value}\n```\n\n"
                description += f"**Server:** {interaction.guild.name if interaction.guild else 'None'}\n"
                description += f"**Submitted:** <t:{current_timestamp}:F> (<t:{current_timestamp}:R>)"
                
                # Send embed to ticket channel first to get message ID
                support_embed = discord.Embed(
                    title=f"[{self.category}] Support request submitted by {interaction.user.name}",
                    description=description,
                    color=0x00FF00
                )

                view = AcknowledgmentView(bot, interaction.user, ticket_channel.id)
                message = await ticket_channel.send(embed=support_embed, view=view)
                
                # Create ticket data with message ID as ticket_id
                ticket_data = {
                    "ticket_id": message.id,
                    "channel_id": ticket_channel.id,
                    "category": self.category,
                    "subject": self.subject.value,
                    "message": self.message.value,
                    "user_id": interaction.user.id,
                    "guild_id": interaction.guild.id if interaction.guild else None,
                    "guild_name": interaction.guild.name if interaction.guild else "DM",
                    "created_at": datetime.utcnow().isoformat(),
                    "status": "open"
                }
                
                # Save to JSON using channel_id as key
                tickets_file = os.path.join(str(PROJECT_ROOT), "data", "support_tickets.json")
                try:
                    with open(tickets_file, "r") as f:
                        data = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError):
                    data = {"tickets": {}, "categories": {}}

                if "tickets" not in data:
                    data["tickets"] = {}
                if "categories" not in data:
                    data["categories"] = {}

                data["tickets"][str(ticket_channel.id)] = ticket_data

                with open(tickets_file, "w") as f:
                    json.dump(data, f, indent=4)
                
                # Send confirmation to user
                try:
                    dm_embed = discord.Embed(
                        title="Support Ticket Created",
                        description=f"**Subject [{self.category}]:** {self.subject.value}\n\n**Your Message:**\n{self.message.value}\n\nYour support request has been submitted and a ticket has been created. User support will review your request and may reach out to you via DM.",
                        color=0x00FF00
                    )
                    dm_embed.add_field(name="Ticket ID", value=str(message.id), inline=False)
                    
                    await interaction.user.send(embed=dm_embed)
                    
                    success_embed = discord.Embed(
                        title="Support Ticket Created",
                        description="Your ticket has been created successfully. Check your DMs for confirmation.",
                        color=0x00FF00
                    )
                    await interaction.followup.send(embed=success_embed, ephemeral=True)
                    
                except discord.Forbidden:
                    success_embed = discord.Embed(
                        title="Support Ticket Created",
                        description="Your ticket has been created successfully. However, I couldn't DM you. Please enable DMs to receive updates.",
                        color=0xFFAA00
                    )
                    await interaction.followup.send(embed=success_embed, ephemeral=True)
                
                print(f"Support ticket created: {channel_name} from {interaction.user} ({interaction.user.id}): {self.subject.value}")
                
            except discord.Forbidden:
                error_embed = discord.Embed(
                    title="Error",
                    description="I don't have permission to create channels in the ticket category.",
                    color=0xFF0000
                )
                await interaction.followup.send(embed=error_embed, ephemeral=True)
            except Exception as e:
                print(f"Error creating ticket: {e}")
                error_embed = discord.Embed(
                    title="Error",
                    description="An error occurred while creating your ticket. Please try again later.",
                    color=0xFF0000
                )
                await interaction.followup.send(embed=error_embed, ephemeral=True)
    
    class AcknowledgmentView(discord.ui.View):
        def __init__(self, bot, user, ticket_channel_id):
            super().__init__(timeout=None)  # timeout=None makes the view persistent
            self.bot = bot
            self.user = user
            self.ticket_channel_id = ticket_channel_id
        
        @discord.ui.button(label="Acknowledge Ticket", style=discord.ButtonStyle.success, custom_id="acknowledge_support_persistent")
        async def acknowledge_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            owner_id = int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else None
            
            if interaction.user.id != owner_id:
                await interaction.response.send_message("Only the bot owner can use this button!", ephemeral=True)
                return
            
            try:
                # Get ticket subject from JSON
                tickets_file = os.path.join(str(PROJECT_ROOT), "data", "support_tickets.json")
                ticket_subject = "your support ticket"
                try:
                    with open(tickets_file, "r") as f:
                        data = json.load(f)
                    if "tickets" in data and str(self.ticket_channel_id) in data["tickets"]:
                        ticket_subject = data["tickets"][str(self.ticket_channel_id)].get("subject", "your support ticket")
                except:
                    pass
                
                # Send acknowledgment DM to user
                acknowledgment_embed = discord.Embed(
                    title="Support Request Acknowledged",
                    description=f"Your support request **\"{ticket_subject}\"** has been acknowledged by {interaction.user.mention} ({interaction.user.id}) and is currently being worked on. You will receive a DM if more details are needed or when the issue has been resolved.",
                    color=0x0099FF
                )
                acknowledgment_embed.set_footer(text=f"Acknowledged by {interaction.user.name}", icon_url=interaction.user.display_avatar.url)
                
                await self.user.send(embed=acknowledgment_embed)
                
                # Move ticket to acknowledged category
                ticket_channel = self.bot.get_channel(self.ticket_channel_id)
                
                if ticket_channel:
                    bot_name = self.bot.user.name if self.bot.user else "Bot"
                    acknowledged_category_name = f"{bot_name} - Acknowledged Tickets"
                    
                    # Get or create acknowledged category
                    acknowledged_category = await get_or_create_category(ticket_channel.guild, acknowledged_category_name)
                    
                    if acknowledged_category:
                        await ticket_channel.edit(category=acknowledged_category)
                        
                        # Clean up empty pending categories
                        bot_name = self.bot.user.name if self.bot.user else "Bot"
                        pending_base = f"{bot_name} - Pending Tickets"
                        await cleanup_empty_categories(ticket_channel.guild, pending_base)
                
                # Update ticket status in JSON to track acknowledgment
                tickets_file = os.path.join(str(PROJECT_ROOT), "data", "support_tickets.json")
                try:
                    with open(tickets_file, "r") as f:
                        data = json.load(f)
                    
                    if "tickets" in data and str(self.ticket_channel_id) in data["tickets"]:
                        data["tickets"][str(self.ticket_channel_id)]["acknowledged"] = True
                        
                        with open(tickets_file, "w") as f:
                            json.dump(data, f, indent=4)
                except Exception as json_error:
                    print(f"Warning: Could not update ticket acknowledgment status: {json_error}")
                
                owner_confirmation_embed = discord.Embed(
                    title="Ticket Acknowledged",
                    description=f"The user {self.user.mention} has been notified and the ticket has been moved.",
                    color=0x00FF00
                )
                await interaction.response.send_message(embed=owner_confirmation_embed, ephemeral=True)

                # Add close button to the same view
                close_button = discord.ui.Button(
                    label="Close Ticket",
                    style=discord.ButtonStyle.danger,
                    custom_id="close_support_ticket_persistent"
                )
                close_button.callback = lambda i: self.close_ticket_callback(i, close_button)
                self.add_item(close_button)

                button.disabled = True
                button.label = "Ticket Acknowledged"
                await interaction.message.edit(view=self)
                
            except discord.Forbidden:
                error_embed = discord.Embed(
                    title="Error",
                    description="Could not send acknowledgment to the user. Their DMs might be closed.",
                    color=0xFF0000
                )
                await interaction.response.send_message(embed=error_embed, ephemeral=True)
            except Exception as e:
                print(f"Error acknowledging ticket: {e}")
                error_embed = discord.Embed(
                    title="Error",
                    description="An error occurred while acknowledging the ticket.",
                    color=0xFF0000
                )
                await interaction.response.send_message(embed=error_embed, ephemeral=True)

        async def close_ticket_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            owner_id = int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else None
            
            if interaction.user.id != owner_id:
                await interaction.response.send_message("Only the bot owner can use this button!", ephemeral=True)
                return
            
            try:
                ticket_channel = self.bot.get_channel(self.ticket_channel_id)
                
                # Read ticket data before archiving so we have it for the DM
                ticket_data = None
                tickets_file = os.path.join(str(PROJECT_ROOT), "data", "support_tickets.json")
                try:
                    with open(tickets_file, "r") as f:
                        data = json.load(f)
                    if "tickets" in data and str(self.ticket_channel_id) in data["tickets"]:
                        ticket_data = data["tickets"][str(self.ticket_channel_id)]
                except Exception:
                    pass
                
                if ticket_channel:
                    bot_name = self.bot.user.name if self.bot.user else "Bot"
                    archived_category_name = f"{bot_name} - Archived Tickets"
                    
                    # Get or create archived category
                    archived_category = await get_or_create_category(ticket_channel.guild, archived_category_name)
                    
                    if archived_category:
                        # Remove all added role permission overwrites before archiving
                        ticket_cat = ticket_data.get("category") if ticket_data else None
                        role_ids_to_remove = CATEGORY_ROLES.get(ticket_cat, [BASE_TICKET_ROLE])
                        for role_id in role_ids_to_remove:
                            role = ticket_channel.guild.get_role(role_id)
                            if role and role in ticket_channel.overwrites:
                                await ticket_channel.set_permissions(role, overwrite=None)

                        await ticket_channel.edit(category=archived_category)
                        
                        # Archive ticket in JSON
                        try:
                            with open(tickets_file, "r") as f:
                                data = json.load(f)
                            
                            if "tickets" in data and str(self.ticket_channel_id) in data["tickets"]:
                                td = data["tickets"][str(self.ticket_channel_id)]
                                
                                # Create archived ticket entry with minimal data
                                archived_ticket = {
                                    "user_id": td["user_id"],
                                    "opened_at": td["created_at"],
                                    "closed_at": datetime.utcnow().isoformat()
                                }
                                
                                # Initialize archived_tickets if it doesn't exist
                                if "archived_tickets" not in data:
                                    data["archived_tickets"] = {}
                                
                                # Save archived ticket with channel_id as key
                                data["archived_tickets"][str(self.ticket_channel_id)] = archived_ticket
                                
                                # Remove from active tickets
                                del data["tickets"][str(self.ticket_channel_id)]
                                
                                with open(tickets_file, "w") as f:
                                    json.dump(data, f, indent=4)
                                
                                print(f"[SUPPORT] Archived ticket {self.ticket_channel_id}")
                        except Exception as archive_error:
                            print(f"[SUPPORT] Error archiving ticket: {archive_error}")
                        
                        # Clean up empty acknowledged categories
                        acknowledged_base = f"{bot_name} - Acknowledged Tickets"
                        await cleanup_empty_categories(ticket_channel.guild, acknowledged_base)
                        
                        # Send closure notification to user
                        try:
                            # Get ticket subject
                            ticket_subject = ticket_data.get("subject", "Your support ticket") if ticket_data else "Your support ticket"
                            
                            closure_embed = discord.Embed(
                                title="Support Ticket Closed",
                                description=f"Your support ticket **\"{ticket_subject}\"** has been resolved and closed. Thank you for contacting support!",
                                color=0x808080
                            )
                            await self.user.send(embed=closure_embed)
                        except discord.Forbidden:
                            pass
                
                owner_confirmation_embed = discord.Embed(
                    title="Ticket Closed",
                    description=f"The ticket has been archived.",
                    color=0xFF0000
                )
                await interaction.response.send_message(embed=owner_confirmation_embed, ephemeral=True)
                
                button.disabled = True
                button.label = "Ticket Closed"
                await interaction.message.edit(view=self)
                
            except Exception as e:
                print(f"Error closing ticket: {e}")
                error_embed = discord.Embed(
                    title="Error",
                    description="An error occurred while closing the ticket.",
                    color=0xFF0000
                )
                await interaction.response.send_message(embed=error_embed, ephemeral=True)
    
    async def restore_ticket_views(bot):
        """Restore views for all open tickets - same logic as ticket_handler refresh"""
        print("[SUPPORT] restore_ticket_views() called")
        tickets_file = os.path.join(str(PROJECT_ROOT), "data", "support_tickets.json")
        try:
            with open(tickets_file, "r") as f:
                data = json.load(f)
            print(f"[SUPPORT] Loaded tickets file, found {len(data.get('tickets', {}))} tickets")
        except Exception as e:
            print(f"[SUPPORT] Failed to load tickets file: {e}")
            return 0, 0
        
        if "tickets" not in data:
            return 0, 0
        
        restored_count = 0
        failed_count = 0
        
        for channel_id, ticket_data in data["tickets"].items():
            # Only restore views for open tickets (not closed)
            if ticket_data.get("status") == "closed":
                continue
                
            try:
                channel_id_int = int(channel_id)
                ticket_channel = bot.get_channel(channel_id_int)
                
                if not ticket_channel:
                    subject = ticket_data.get("subject", "Unknown")
                    user_id = ticket_data.get("user_id", "Unknown")
                    print(f"[SUPPORT] ❌ Channel {channel_id} not found - Subject: '{subject}', User: {user_id}")
                    failed_count += 1
                    continue
                
                message_id = ticket_data.get("ticket_id")
                if not message_id:
                    subject = ticket_data.get("subject", "Unknown")
                    print(f"[SUPPORT] ❌ No ticket_id for channel {channel_id} - Subject: '{subject}'")
                    failed_count += 1
                    continue
                
                try:
                    # Fetch the message
                    message = await ticket_channel.fetch_message(message_id)
                    user = await bot.fetch_user(ticket_data["user_id"])
                    
                    # Create the view like ticket_handler does
                    view = AcknowledgmentView(bot, user, channel_id_int)
                    
                    # Check if acknowledged and modify view accordingly
                    acknowledged = ticket_data.get("acknowledged", False)
                    if acknowledged:
                        # Find and disable the acknowledge button
                        for item in view.children:
                            if isinstance(item, discord.ui.Button) and item.custom_id == "acknowledge_support_persistent":
                                item.disabled = True
                                item.label = "Ticket Acknowledged"
                                break
                        
                        # Add close button
                        close_button = discord.ui.Button(
                            label="Close Ticket",
                            style=discord.ButtonStyle.danger,
                            custom_id="close_support_ticket_persistent"
                        )
                        close_button.callback = lambda i, v=view, b=close_button: v.close_ticket_callback(i, b)
                        view.add_item(close_button)
                    
                    # Edit the message with the restored view
                    await message.edit(view=view)
                    
                    status = "acknowledged" if acknowledged else "pending"
                    print(f"[SUPPORT] ✅ Restored {status} ticket in channel {channel_id}")
                    restored_count += 1
                    
                except discord.NotFound:
                    subject = ticket_data.get("subject", "Unknown")
                    print(f"[SUPPORT] ❌ Message {message_id} not found in channel {channel_id} - Subject: '{subject}'")
                    failed_count += 1
                except Exception as e:
                    subject = ticket_data.get("subject", "Unknown")
                    user_id = ticket_data.get("user_id", "Unknown")
                    print(f"[SUPPORT] ❌ Error restoring ticket {channel_id} - Subject: '{subject}', User: {user_id}, Error: {e}")
                    failed_count += 1
                    
            except Exception as e:
                print(f"[SUPPORT] ❌ Failed to process ticket {channel_id}: {e}")
                failed_count += 1
        
        print(f"[SUPPORT] 📊 Restoration complete: {restored_count} restored, {failed_count} failed")
        return restored_count, failed_count
    
    async def cleanup_empty_categories(guild, base_name):
        """Remove empty numbered categories and update tracking"""
        tickets_file = os.path.join(str(PROJECT_ROOT), "data", "support_tickets.json")
        try:
            with open(tickets_file, "r") as f:
                data = json.load(f)
        except:
            return
        
        if "categories" not in data or base_name not in data["categories"]:
            return
        
        tracked = data["categories"][base_name]
        valid = []
        
        for cat_id in tracked:
            category = guild.get_channel(cat_id)
            if category:
                if '#' in category.name and len(category.channels) == 0:
                    try:
                        await category.delete()
                        print(f"Deleted empty category: {category.name}")
                        # Don't add to valid list - it's been deleted
                    except Exception as e:
                        print(f"Failed to delete category: {e}")
                        valid.append(cat_id)
                else:
                    valid.append(cat_id)
            # If category doesn't exist, don't add to valid list
        
        data["categories"][base_name] = valid
        with open(tickets_file, "w") as f:
            json.dump(data, f, indent=4)
    
    @bot.tree.command(
        name="contact_support",
        description="Send a message to the bot owner for support"
    )
    @app_commands.describe(category="The type of support request")
    @app_commands.choices(category=[
        app_commands.Choice(name="Bug Report", value="Bug Report"),
        app_commands.Choice(name="Suggestion", value="Suggestion"),
        app_commands.Choice(name="Help", value="Help"),
        app_commands.Choice(name="Other", value="Other"),
    ])
    async def contact_support(interaction: discord.Interaction, category: app_commands.Choice[str]):
        """Command to contact the bot owner"""

        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return
        
        await interaction.response.send_modal(SupportModal(category=category.value))
    
    @bot.tree.command(
        name="refresh_support_tickets",
        description="Manually refresh all open support ticket views (Owner only)"
    )
    async def refresh_support_tickets(interaction: discord.Interaction):
        """Manually restore views for all open tickets"""
        owner_id = int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else None
        
        if interaction.user.id != owner_id:
            await interaction.response.send_message("❌ Only the bot owner can use this command!", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        # Call the restore function
        tickets_file = os.path.join(str(PROJECT_ROOT), "data", "support_tickets.json")
        try:
            with open(tickets_file, "r") as f:
                data = json.load(f)
        except:
            await interaction.followup.send("❌ Could not load support tickets file.", ephemeral=True)
            return
        
        if "tickets" not in data or len(data["tickets"]) == 0:
            await interaction.followup.send("ℹ️ No tickets found to refresh.", ephemeral=True)
            return
        
        restored_count = 0
        failed_count = 0
        
        for channel_id, ticket_data in data["tickets"].items():
            if ticket_data.get("status") != "closed":
                try:
                    channel_id_int = int(channel_id)
                    ticket_channel = bot.get_channel(channel_id_int)
                    
                    if not ticket_channel:
                        subject = ticket_data.get("subject", "Unknown")
                        user_id = ticket_data.get("user_id", "Unknown")
                        print(f"[REFRESH] ❌ Channel {channel_id} not found - Subject: '{subject}', User: {user_id}")
                        failed_count += 1
                        continue
                    
                    user = await bot.fetch_user(ticket_data["user_id"])
                    message_id = ticket_data.get("ticket_id")
                    
                    if message_id:
                        try:
                            message = await ticket_channel.fetch_message(message_id)
                            view = AcknowledgmentView(bot, user, channel_id_int)
                            
                            # Check if acknowledged and modify view
                            acknowledged = ticket_data.get("acknowledged", False)
                            if acknowledged:
                                for item in view.children:
                                    if isinstance(item, discord.ui.Button) and item.custom_id == "acknowledge_support_persistent":
                                        item.disabled = True
                                        item.label = "Ticket Acknowledged"
                                        break
                                
                                close_button = discord.ui.Button(
                                    label="Close Ticket",
                                    style=discord.ButtonStyle.danger,
                                    custom_id="close_support_ticket_persistent"
                                )
                                close_button.callback = lambda i, v=view, b=close_button: v.close_ticket_callback(i, b)
                                view.add_item(close_button)
                            
                            await message.edit(view=view)
                            print(f"[REFRESH] ✅ Restored ticket {channel_id} - Subject: '{ticket_data.get('subject', 'Unknown')}'")
                            restored_count += 1
                        except discord.NotFound:
                            subject = ticket_data.get("subject", "Unknown")
                            print(f"[REFRESH] ❌ Message {message_id} not found in channel {channel_id} - Subject: '{subject}'")
                            failed_count += 1
                    else:
                        subject = ticket_data.get("subject", "Unknown")
                        print(f"[REFRESH] ❌ No ticket_id for channel {channel_id} - Subject: '{subject}'")
                        failed_count += 1
                        
                except Exception as e:
                    subject = ticket_data.get("subject", "Unknown")
                    user_id = ticket_data.get("user_id", "Unknown")
                    print(f"[REFRESH] ❌ Error restoring ticket {channel_id} - Subject: '{subject}', User: {user_id}, Error: {e}")
                    failed_count += 1
        
        result_embed = discord.Embed(
            title="🔄 Support Tickets Refreshed",
            description=f"✅ **Restored:** {restored_count}\n❌ **Failed:** {failed_count}",
            color=0x00FF00 if failed_count == 0 else 0xFFAA00
        )
        await interaction.followup.send(embed=result_embed, ephemeral=True)
    
    # Store the restore function on the bot for on_ready to call
    bot._restore_support_ticket_views = lambda: restore_ticket_views(bot)
    
    print("[OK] Loaded contact_support command")
    print("[OK] Loaded refresh_support_tickets command")
