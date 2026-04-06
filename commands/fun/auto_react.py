import discord
from discord import app_commands
from discord.ui import View, Button, Modal, TextInput, Select
import os
import json
from pathlib import Path
from datetime import datetime, timezone
import asyncio
from utils.permissions import has_roles

AUTO_REACT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "auto_reactions.json"
ITEMS_PER_PAGE = 5

REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0
]

def load_auto_reactions() -> dict:
    """Load auto-reactions database from JSON file."""
    try:
        if AUTO_REACT_DB_PATH.exists():
            with open(AUTO_REACT_DB_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[WARN] Failed to load auto reactions: {e}")
    return {}

def save_auto_reactions(data: dict):
    """Save auto-reactions database to JSON file."""
    try:
        with open(AUTO_REACT_DB_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] Failed to save auto reactions: {e}")

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    class AddReactionModal(Modal, title="Add Auto-Reaction"):
        """Modal for adding a new auto-reaction"""
        user_id_input = TextInput(
            label="User ID",
            placeholder="Enter the user ID (e.g., 123456789012345678)",
            required=True,
            max_length=20
        )
        emoji_input = TextInput(
            label="Emoji(s)",
            placeholder="Enter emoji(s) separated by spaces (e.g., 👍 🎉 ❤️)",
            required=True,
            max_length=100
        )
        
        def __init__(self, view: "AutoReactManageView"):
            super().__init__()
            self.manage_view = view
        
        async def on_submit(self, interaction: discord.Interaction):
            try:
                user_id = int(self.user_id_input.value.strip())
            except ValueError:
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="❌ Invalid User ID",
                        description="Please enter a valid numeric user ID.",
                        color=0xFF0000
                    ),
                    ephemeral=True
                )
                return
            
            # Try to fetch the user
            try:
                target_user = await bot.fetch_user(user_id)
            except discord.NotFound:
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="❌ User Not Found",
                        description=f"Could not find a user with ID `{user_id}`.",
                        color=0xFF0000
                    ),
                    ephemeral=True
                )
                return
            
            # Parse emojis (split by spaces)
            emojis = self.emoji_input.value.strip().split()
            
            # Load and update database
            auto_reactions = load_auto_reactions()
            user_id_str = str(user_id)
            
            if user_id_str not in auto_reactions:
                auto_reactions[user_id_str] = {
                    "target_username": str(target_user),
                    "target_user_id": user_id,
                    "emojis": []
                }
            
            added_emojis = []
            for emoji in emojis:
                if emoji and emoji not in auto_reactions[user_id_str]["emojis"]:
                    auto_reactions[user_id_str]["emojis"].append(emoji)
                    added_emojis.append(emoji)
            
            save_auto_reactions(auto_reactions)
            
            # Update the view and refresh
            self.manage_view.auto_reactions = auto_reactions
            embed = self.manage_view.build_embed()
            self.manage_view.update_buttons()
            
            await interaction.response.edit_message(embed=embed, view=self.manage_view)
            
            if added_emojis:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="✅ Auto-Reaction Added",
                        description=f"Added {' '.join(added_emojis)} for {target_user.mention}",
                        color=0x00FF00
                    ),
                    ephemeral=True
                )
    
    class RemoveUserSelect(Select):
        """Select menu for choosing a user to remove reactions from"""
        def __init__(self, view: "AutoReactManageView"):
            self.manage_view = view
            options = []
            
            for user_id, data in list(view.auto_reactions.items())[:25]:  # Discord limit: 25 options
                emojis_preview = " ".join(data.get("emojis", []))[:50]
                options.append(discord.SelectOption(
                    label=f"{data.get('target_username', 'Unknown')}"[:100],
                    description=f"Emojis: {emojis_preview}"[:100] if emojis_preview else "No emojis",
                    value=user_id
                ))
            
            if not options:
                options.append(discord.SelectOption(label="No users", value="none"))
            
            super().__init__(
                placeholder="Select a user to manage...",
                options=options,
                row=1
            )
        
        async def callback(self, interaction: discord.Interaction):
            if self.values[0] == "none":
                await interaction.response.defer()
                return
            
            user_id = self.values[0]
            data = self.manage_view.auto_reactions.get(user_id, {})
            
            # Create a view with emoji removal options
            remove_view = RemoveEmojiView(self.manage_view, user_id, data)
            
            embed = discord.Embed(
                title=f"Manage Reactions for {data.get('target_username', 'Unknown')}",
                description=f"**User ID:** {user_id}\n**Current Emojis:** {' '.join(data.get('emojis', []))}",
                color=0x3498DB,
                timestamp=datetime.now(timezone.utc)
            )
            
            await interaction.response.edit_message(embed=embed, view=remove_view)
    
    class RemoveEmojiView(View):
        """View for removing emojis from a specific user"""
        def __init__(self, parent_view: "AutoReactManageView", user_id: str, data: dict):
            super().__init__(timeout=300)
            self.parent_view = parent_view
            self.user_id = user_id
            self.data = data
            
            # Add emoji select if there are emojis
            emojis = data.get("emojis", [])
            if emojis:
                options = [
                    discord.SelectOption(label=emoji, value=emoji)
                    for emoji in emojis[:25]
                ]
                self.emoji_select = Select(
                    placeholder="Select emoji(s) to remove...",
                    options=options,
                    min_values=1,
                    max_values=min(len(options), 25),
                    row=0
                )
                self.emoji_select.callback = self.emoji_select_callback
                self.add_item(self.emoji_select)
        
        async def emoji_select_callback(self, interaction: discord.Interaction):
            selected_emojis = self.emoji_select.values
            auto_reactions = load_auto_reactions()
            
            if self.user_id in auto_reactions:
                for emoji in selected_emojis:
                    if emoji in auto_reactions[self.user_id]["emojis"]:
                        auto_reactions[self.user_id]["emojis"].remove(emoji)
                
                # Remove user if no emojis left
                if not auto_reactions[self.user_id]["emojis"]:
                    auto_reactions.pop(self.user_id)
                
                save_auto_reactions(auto_reactions)
            
            # Update parent view and go back
            self.parent_view.auto_reactions = auto_reactions
            embed = self.parent_view.build_embed()
            self.parent_view.update_buttons()
            
            await interaction.response.edit_message(embed=embed, view=self.parent_view)
        
        @discord.ui.button(label="Remove All Emojis", style=discord.ButtonStyle.danger, row=1)
        async def remove_all(self, interaction: discord.Interaction, button: Button):
            auto_reactions = load_auto_reactions()
            
            if self.user_id in auto_reactions:
                auto_reactions.pop(self.user_id)
                save_auto_reactions(auto_reactions)
            
            self.parent_view.auto_reactions = auto_reactions
            embed = self.parent_view.build_embed()
            self.parent_view.update_buttons()
            
            await interaction.response.edit_message(embed=embed, view=self.parent_view)
        
        @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=1)
        async def back(self, interaction: discord.Interaction, button: Button):
            embed = self.parent_view.build_embed()
            self.parent_view.update_buttons()
            await interaction.response.edit_message(embed=embed, view=self.parent_view)
    
    class AutoReactManageView(View):
        """Main view for managing auto-reactions with pagination"""
        def __init__(self, user_id: int):
            super().__init__(timeout=300)
            self.user_id = user_id
            self.page = 0
            self.auto_reactions = load_auto_reactions()
            self.update_buttons()
        
        @property
        def total_pages(self) -> int:
            total = len(self.auto_reactions)
            return max(1, (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
        
        def get_page_items(self) -> list:
            items = list(self.auto_reactions.items())
            start = self.page * ITEMS_PER_PAGE
            end = start + ITEMS_PER_PAGE
            return items[start:end]
        
        def build_embed(self) -> discord.Embed:
            embed = discord.Embed(
                title="🎭 Auto-Reaction Manager",
                color=0x3498DB,
                timestamp=datetime.now(timezone.utc)
            )
            
            if not self.auto_reactions:
                embed.description = "No auto-reactions are currently set up.\n\nUse the **Add Reaction** button to add one!"
                return embed
            
            embed.description = f"**{len(self.auto_reactions)}** user(s) with auto-reactions\nPage {self.page + 1}/{self.total_pages}"
            
            for user_id, data in self.get_page_items():
                emojis_str = " ".join(data.get("emojis", []))
                embed.add_field(
                    name=f"{data.get('target_username', 'Unknown')}",
                    value=f"**ID:** `{user_id}`\n**Emojis:** {emojis_str}",
                    inline=False
                )
            
            return embed
        
        def update_buttons(self):
            # Update navigation buttons
            self.prev_button.disabled = self.page <= 0
            self.next_button.disabled = self.page >= self.total_pages - 1
            
            # Update remove select
            for item in self.children[:]:
                if isinstance(item, RemoveUserSelect):
                    self.remove_item(item)
            
            if self.auto_reactions:
                self.add_item(RemoveUserSelect(self))
        
        @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=0)
        async def prev_button(self, interaction: discord.Interaction, button: Button):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("This is not your menu!", ephemeral=True)
                return
            self.page = max(0, self.page - 1)
            self.update_buttons()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        
        @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=0)
        async def next_button(self, interaction: discord.Interaction, button: Button):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("This is not your menu!", ephemeral=True)
                return
            self.page = min(self.total_pages - 1, self.page + 1)
            self.update_buttons()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        
        @discord.ui.button(label="Add Reaction", style=discord.ButtonStyle.success, emoji="➕", row=0)
        async def add_button(self, interaction: discord.Interaction, button: Button):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("This is not your menu!", ephemeral=True)
                return
            await interaction.response.send_modal(AddReactionModal(self))
        
        @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary, emoji="🔄", row=0)
        async def refresh_button(self, interaction: discord.Interaction, button: Button):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("This is not your menu!", ephemeral=True)
                return
            self.auto_reactions = load_auto_reactions()
            self.page = min(self.page, self.total_pages - 1)
            self.update_buttons()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
    
    @bot.tree.command(
        name="auto_react_manage",
        description="Manage auto-reactions: view, add, and remove reactions for users"
    )
    async def auto_react_manage(interaction: discord.Interaction):
        """Open the auto-reaction management interface"""
        
        # Check permissions
        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000,
                timestamp=datetime.now(timezone.utc)
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return
        
        view = AutoReactManageView(interaction.user.id)
        embed = view.build_embed()
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    @bot.listen()
    async def on_message(message: discord.Message):
        """React to messages from users with auto-reactions set"""
        
        # Ignore bot messages
        if message.author.bot:
            return
        
        # Load auto-reactions
        auto_reactions = load_auto_reactions()
        
        # Check if this user has auto-reactions
        user_id_str = str(message.author.id)
        if user_id_str in auto_reactions:
            emojis = auto_reactions[user_id_str].get("emojis", [])
            
            # React with each emoji, with a small delay between reactions to avoid API rate limiting
            for emoji in emojis:
                try:
                    await message.add_reaction(emoji)
                    # Add a small delay between reactions to avoid hitting Discord API limits
                    await asyncio.sleep(0.2)
                except discord.HTTPException as e:
                    print(f"[WARN] Failed to add reaction {emoji}: {e}")
                except Exception as e:
                    print(f"[ERROR] Error adding reaction {emoji}: {e}")
    
    print("[OK] Loaded auto-reaction commands")
