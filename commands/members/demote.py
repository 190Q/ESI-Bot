import discord
from discord import app_commands
from datetime import datetime
import os
import io
from utils.permissions import has_roles

REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0,
    600185623474601995 # Parliament
]

# Role IDs
SINDRIAN_CITIZEN_ID = 554889169705500672
EX_CITIZEN_ID = 706338091312349195
VETERAN_ID = 914422269802070057

BADGE_ROLES = {
    "War Badges": {
        "10k": 1426633275635404981,
        "6k": 1426633206857465888,
        "3k": 1426633036736368861,
        "1.5k": 1426632920528846880,
        "750": 1426633144093638778,
        "300": 1426632862207049778,
        "100": 1426632780615385098
    },
    "Quest Badges": {
        "350": 1426636141242617906,
        "225": 1426636108321525891,
        "150": 1426636066856898593,
        "90": 1426636018664341675,
        "50": 1426635982614040676,
        "25": 1426635948992761988,
        "10": 1426635880462024937
    },
    "Recruitment Badges": {
        "250": 1426637291706912788,
        "150": 1426637244109946920,
        "80": 1426637209301160039,
        "50": 1426637168071282808,
        "25": 1426637134378303619,
        "10": 1426637094339608586,
        "5": 1426636993630175447
    },
    "Raid Badges": {
        "6k": 1426634664025526405,
        "3.5k": 1426634622791323938,
        "2k": 1426634579644514347,
        "1k": 1426634531284324353,
        "500": 1426634469401432194,
        "100": 1426634408370114773,
        "50": 1426634317970542613
    },
    "Event Badges": {
        "100": 1440682465717915779,
        "75": 1440682471086751815,
        "55": 1440682473641083011,
        "35": 1440682477055115304,
        "20": 1440682480846897232,
        "10": 1440682485548711997,
        "3": 1440682762133569730
    }
}

# Rank role IDs (in hierarchy order)
RANK_ROLES = {
    554506531949772812: "Emperor",
    554514823191199747: "Archduke",
    1396112289832243282: "Grand Duke",
    591765870272053261: "Duke",
    1391424890938195998: "Count",
    591769392828776449: "Viscount",
    688438690137243892: "Knight",
    681030746651230351: "Squire",
    954566591520063510: "Juror",
    1419316008044466196: "Event Planner",
    683448131148447929: "Sindrian Pride",
    554530279516274709: "Nobility",
    1412134734238191790: "Aristocrat",
    1287496473323503738: "Gentry",
    1356564816701030461: "Duke Perms",
}

# Rank hierarchy (ordered list for demotion logic)
RANK_HIERARCHY = [
    (554506531949772812, "Emperor"),
    (554514823191199747, "Archduke"),
    (1396112289832243282, "Grand Duke"),
    (591765870272053261, "Duke"),
    (1391424890938195998, "Count"),
    (591769392828776449, "Viscount"),
    (688438690137243892, "Knight"),
    (681030746651230351, "Squire")
]

# Category separator role IDs
CATEGORY_SEPARATORS = {
    1426272204521341101: "Badges",
    1426273223569576057: "War Roles",
    1426273593335091200: "Notification Roles",
    1426274338373369929: "Access Roles",
    968121600488656906: "Miscellaneous"
}

# Badge role IDs (category marker to next category marker)
BADGE_MARKER_START = 1426272204521341101
WAR_ROLES_MARKER = 1426273223569576057

# War role IDs
WAR_ROLE_IDS = [
    995285468222603314,  # HQ Team
    722856382025564161,  # Sindrian Vanguard
    891933320856895498,  # Sindrian Crusader
    1284853392849637399,  # Tank
    1284853744705474610,  # Healer
    1284854049325322250,  # DPS
]

# Notification role IDs (specific ones)
NOTIFICATION_ROLE_IDS = {
    767252412989702157: "Parliament Candidate",
    1320710418900979732: "Election Candidate",
    800547586694971443: "Guild Parties",
    1297620620628201504: "Guild Raids",
    1054877074491453510: "Guild Quests",
    1370477190524833902: "NOL LFG",
    1384811398667702344: "TNA LFG",
    1370477368057008220: "TCC LFG",
    1357064338615304412: "NOTG LFG"
}

# Access role IDs
ACCESS_ROLE_IDS = {
    1077661051799216128: "Builder",
    1328051378823757907: "Government-Approved Builder"
}

class ConfirmDemotionView(discord.ui.View):
    def __init__(self, member: discord.Member, roles_to_remove: list, roles_to_add: list, new_nickname: str = None):
        super().__init__(timeout=180)
        self.member = member
        self.roles_to_remove = roles_to_remove
        self.roles_to_add = roles_to_add
        self.new_nickname = new_nickname
    
    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Defer the response immediately to prevent timeout
        await interaction.response.defer()
        
        try:
            # Remove roles
            roles_to_remove_objects = [interaction.guild.get_role(role_id) for role_id in self.roles_to_remove]
            roles_to_remove_objects = [role for role in roles_to_remove_objects if role is not None]
            
            if roles_to_remove_objects:
                await self.member.remove_roles(*roles_to_remove_objects, reason=f"Demotion by {interaction.user.name}")
            
            # Add roles
            roles_to_add_objects = [interaction.guild.get_role(role_id) for role_id in self.roles_to_add]
            roles_to_add_objects = [role for role in roles_to_add_objects if role is not None]
            
            if roles_to_add_objects:
                await self.member.add_roles(*roles_to_add_objects, reason=f"Demotion by {interaction.user.name}")
            
            # Change nickname if applicable
            if self.new_nickname:
                await self.member.edit(nick=self.new_nickname, reason=f"Demotion by {interaction.user.name}")
            
            # Log the demotion
            try:
                from rank_logger import log_rank_change
                
                # Get current rank from member before demotion
                current_rank = "None"
                for rank_id, rank_name in RANK_ROLES.items():
                    if rank_id in [role.id for role in self.member.roles]:
                        current_rank = rank_name
                        break
                
                # Determine new rank from roles being added
                new_rank = "Unknown"
                for role_id in self.roles_to_add:
                    if role_id == EX_CITIZEN_ID:
                        new_rank = "Ex-Citizen"
                        break
                    elif role_id == VETERAN_ID:
                        new_rank = "Veteran"
                        break
                    else:
                        # Check if it's a rank role
                        role = interaction.guild.get_role(role_id)
                        if role and role_id in RANK_ROLES:
                            new_rank = RANK_ROLES[role_id]
                            break
                
                # Additional info for demotion logs
                additional_info = {
                    'roles_removed_count': len(self.roles_to_remove),
                    'roles_added_count': len(self.roles_to_add),
                    'nickname_changed': self.new_nickname is not None,
                    'new_nickname': self.new_nickname
                }
                
                log_rank_change(
                    target_user_id=self.member.id,
                    target_username=str(self.member),
                    executor_user_id=interaction.user.id,
                    executor_username=str(interaction.user),
                    previous_rank=current_rank,
                    new_rank=new_rank,
                    action_type='demote',
                    guild_id=interaction.guild.id,
                    guild_name=interaction.guild.name,
                    additional_info=additional_info
                )
            except Exception as e:
                print(f"[WARN] Failed to log demotion: {e}")
            
            # Success embed
            success_embed = discord.Embed(
                title="✅ Demotion Completed",
                description=f"Successfully demoted {self.member.mention}",
                color=0x00FF00,
                timestamp=datetime.utcnow()
            )
            
            if self.new_nickname:
                success_embed.add_field(name="New Nickname", value=self.new_nickname, inline=False)
            
            success_embed.add_field(name="Roles Removed", value=str(len(self.roles_to_remove)), inline=True)
            success_embed.add_field(name="Roles Added", value=str(len(self.roles_to_add)), inline=True)
            
            await interaction.followup.edit_message(message_id=interaction.message.id, embed=success_embed, view=None)
            
        except discord.Forbidden:
            error_embed = discord.Embed(
                title="❌ Permission Error",
                description="I don't have permission to modify this user's roles or nickname.",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.followup.edit_message(message_id=interaction.message.id, embed=error_embed, view=None)
        except Exception as e:
            error_embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred: {str(e)}",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.followup.edit_message(message_id=interaction.message.id, embed=error_embed, view=None)
    
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        cancel_embed = discord.Embed(
            title="❌ Demotion Cancelled",
            description="The demotion has been cancelled.",
            color=0xFF0000,
            timestamp=datetime.utcnow()
        )
        await interaction.response.edit_message(embed=cancel_embed, view=None)

class DemoteView(discord.ui.View):
    def __init__(self, member: discord.Member, current_rank: str):
        super().__init__(timeout=180)
        self.member = member
        self.current_rank = current_rank
        
        # Add select menu
        self.add_item(RankSelect(member, current_rank))

class RankSelect(discord.ui.Select):
    def __init__(self, member: discord.Member, current_rank: str):
        self.member = member
        self.current_rank = current_rank
        
        # Define rank hierarchy order
        rank_order = {
            "ex-citizen": -1,
            "veteran": 0,
            "squire": 1,
            "knight": 2,
            "viscount": 3,
            "count": 4,
            "duke": 5,
            "grand duke": 6,
            "archduke": 7,
            "emperor": 8
        }
        
        # Get current rank value
        current_rank_value = rank_order.get(current_rank.lower(), 0)
        
        # All possible options
        all_options = [
            ("Ex-Citizen", "ex-citizen", -1),
            ("Veteran", "veteran", 0),
            ("Squire", "squire", 1),
            ("Knight", "knight", 2),
            ("Viscount", "viscount", 3),
            ("Count", "count", 4),
        ]
        
        # Filter options to only show ranks below current rank
        options = [
            discord.SelectOption(label=label, value=value)
            for label, value, rank_value in all_options
            if rank_value < current_rank_value
        ]
        
        # If no valid options, add a placeholder
        if len(options) == 0:
            options = [
                discord.SelectOption(label="No valid demotion targets", value="none", description="This user cannot be demoted further")
            ]
        
        super().__init__(
            placeholder="Select target rank...",
            min_values=1,
            max_values=1,
            options=options
        )
    
    async def callback(self, interaction: discord.Interaction):
        target_rank = self.values[0]
        
        user_role_ids = [role.id for role in self.member.roles]
        roles_to_remove = []
        roles_to_add = []
        
        # Define rank hierarchy order for comparison
        rank_order = {
            "emperor": 8,
            "archduke": 7,
            "grand duke": 6,
            "duke": 5,
            "count": 4,
            "viscount": 3,
            "knight": 2,
            "squire": 1,
            "veteran": 0,
            "ex-citizen": -1
        }
        
        # Get current rank order value
        current_rank_value = rank_order.get(self.current_rank.lower(), 0)
        target_rank_value = rank_order.get(target_rank, 0)
        
        # Check if this is a promotion (not allowed)
        if target_rank_value > current_rank_value:
            error_embed = discord.Embed(
                title="Invalid Demotion",
                description=f"You cannot promote {self.member.mention} from **{self.current_rank}** to **{target_rank.title()}**. You can only demote users.",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.response.edit_message(embed=error_embed, view=None)
            return
        
        # Get all badge roles
        badge_role_ids = []
        for category in BADGE_ROLES.values():
            for badge_id in category.values():
                if badge_id in user_role_ids:
                    badge_role_ids.append(badge_id)
        
        # Get all war roles
        war_role_ids = [role_id for role_id in WAR_ROLE_IDS if role_id in user_role_ids]
        
        # Get all notification roles
        notification_role_ids = [role_id for role_id in NOTIFICATION_ROLE_IDS.keys() if role_id in user_role_ids]
        
        # Get all access roles
        access_role_ids = [role_id for role_id in ACCESS_ROLE_IDS.keys() if role_id in user_role_ids]
        
        # Determine new nickname
        new_nickname = None
        if target_rank in ["ex-citizen", "veteran"]:
            # Remove rank prefix from display name if it exists
            display_name = self.member.display_name
            preserved_suffix = ""  # To store "ess" if found
            
            # Remove "Ex-" prefix if it exists
            if display_name.startswith("Ex-"):
                # Extract the old rank after "Ex-"
                parts = display_name.split(" ", 2)  # Split into ["Ex-Rank", "restofname"] or ["Ex-Rank", "part2", "restofname"]
                if len(parts) >= 2:
                    display_name = " ".join(parts[1:])  # Remove the "Ex-Rank" part
            
            # Remove current rank prefix if it exists (including variations like "Viscountess")
            for rank_name in RANK_ROLES.values():
                # Check for feminine variation first (e.g., "Viscountess" for "Viscount")
                if display_name.startswith(f"{rank_name}ess "):
                    preserved_suffix = "ess"
                    display_name = display_name[len(rank_name) + 4:]  # +4 for "ess "
                    break
                # Check for exact match
                elif display_name.startswith(f"{rank_name} "):
                    display_name = display_name[len(rank_name) + 1:]  # +1 for the space
                    break
            
            new_nickname = f"Ex-{self.current_rank}{preserved_suffix} {display_name}"
        
        if target_rank in ["ex-citizen", "veteran"]:
            # Remove all badge roles
            for role_id in badge_role_ids:
                roles_to_remove.append(role_id)
                
            # Remove all guild ranks
            for rank_id in RANK_ROLES.keys():
                if rank_id in user_role_ids:
                    roles_to_remove.append(rank_id)
            
            # Remove Sindrian Citizen
            if SINDRIAN_CITIZEN_ID in user_role_ids:
                roles_to_remove.append(SINDRIAN_CITIZEN_ID)
            
            # Remove all war roles
            for role_id in war_role_ids:
                roles_to_remove.append(role_id)
            
            # Remove all notification roles
            for role_id in notification_role_ids:
                roles_to_remove.append(role_id)
            
            # Remove all access roles
            for role_id in access_role_ids:
                roles_to_remove.append(role_id)
            
            # Add appropriate role
            if target_rank == "ex-citizen":
                roles_to_add.append(EX_CITIZEN_ID)
            else:  # veteran
                roles_to_add.append(VETERAN_ID)
            
            # Remove category separators only if we're removing roles from that category
            if badge_role_ids and BADGE_MARKER_START in user_role_ids:
                roles_to_remove.append(BADGE_MARKER_START)
            
            if war_role_ids and WAR_ROLES_MARKER in user_role_ids:
                roles_to_remove.append(WAR_ROLES_MARKER)
            
            notification_separator = 1426273593335091200
            if notification_role_ids and notification_separator in user_role_ids:
                roles_to_remove.append(notification_separator)
            
            access_separator = 1426274338373369929
            if access_role_ids and access_separator in user_role_ids:
                roles_to_remove.append(access_separator)
            
        else:
            # Find target rank in hierarchy
            target_rank_map = {
                "count": (1391424890938195998, "Count"),
                "viscount": (591769392828776449, "Viscount"),
                "knight": (688438690137243892, "Knight"),
                "squire": (681030746651230351, "Squire")
            }
            
            target_rank_id, target_rank_name = target_rank_map[target_rank]
            
            # Find current rank index
            current_rank_index = None
            for i, (rank_id, rank_name) in enumerate(RANK_HIERARCHY):
                if rank_id in user_role_ids:
                    current_rank_index = i
                    break
            
            # Find target rank index
            target_rank_index = None
            for i, (rank_id, rank_name) in enumerate(RANK_HIERARCHY):
                if rank_id == target_rank_id:
                    target_rank_index = i
                    break
            
            if current_rank_index is not None and target_rank_index is not None:
                # Remove all ranks above target rank (including current)
                for i in range(current_rank_index, target_rank_index):
                    rank_id, _ = RANK_HIERARCHY[i]
                    if rank_id in user_role_ids:
                        roles_to_remove.append(rank_id)
                
                # Add target rank if they don't have it
                if target_rank_id not in user_role_ids:
                    roles_to_add.append(target_rank_id)
        
        # Create confirmation embed
        confirm_embed = discord.Embed(
            title="Demotion Preview",
            description=f"**User:** {self.member.mention}\n**Current Rank:** {self.current_rank}\n**Target Rank:** {target_rank.title()}\n\nPlease review the changes below and confirm.",
            color=0xFFA500,
            timestamp=datetime.utcnow()
        )
        
        # Add nickname change info if applicable
        if new_nickname:
            confirm_embed.add_field(
                name="Nickname Change",
                value=f"Will be renamed to: **{new_nickname}**",
                inline=False
            )
        
        # Show roles to remove
        remove_list = []
        for role_id in roles_to_remove:
            role = interaction.guild.get_role(role_id)
            if role:
                remove_list.append(role.mention)
        
        if remove_list:
            # Split into chunks if too long
            remove_text = "\n".join(remove_list)
            if len(remove_text) > 1024:
                remove_text = "\n".join(remove_list[:10]) + f"\n...and {len(remove_list) - 10} more"
            
            confirm_embed.add_field(
                name=f"Roles to Remove ({len(remove_list)})",
                value=remove_text,
                inline=False
            )
        
        # Show roles to add
        add_list = []
        for role_id in roles_to_add:
            role = interaction.guild.get_role(role_id)
            if role:
                add_list.append(role.mention)
        
        if add_list:
            confirm_embed.add_field(
                name=f"Roles to Add ({len(add_list)})",
                value="\n".join(add_list),
                inline=False
            )
        
        # Create and send confirmation view
        confirmation_view = ConfirmDemotionView(self.member, roles_to_remove, roles_to_add, new_nickname)
        await interaction.response.edit_message(embed=confirm_embed, view=confirmation_view)

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""

    @bot.tree.command(name="demote", description="Demote a user to a lower rank")
    @app_commands.describe(member="The member to demote")
    async def demote_command(interaction: discord.Interaction, member: discord.Member):
        """Demote a user via slash command"""
        
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
        
        user_role_ids = [role.id for role in member.roles]
        
        # Check if user has Sindrian Citizen role
        print(f"User {member.id} has Sindrian Citizen role: {SINDRIAN_CITIZEN_ID in [role.id for role in member.roles]}")
        if SINDRIAN_CITIZEN_ID not in user_role_ids:
            error_embed = discord.Embed(
                title="❌ Cannot Demote",
                description=f"Cannot demote {member.mention} - they don't have the **Sindrian Citizen** role.\n\nUsers without this role cannot be demoted.",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.response.send_message(embed=error_embed, ephemeral=True)
            return
        
        # Get current rank
        current_rank = "None"
        for rank_id, rank_name in RANK_ROLES.items():
            if rank_id in user_role_ids:
                current_rank = rank_name
                break
        
        # Define rank hierarchy order
        rank_order = {
            "emperor": 8,
            "archduke": 7,
            "grand duke": 6,
            "duke": 5,
            "count": 4,
            "viscount": 3,
            "knight": 2,
            "squire": 1,
            "veteran": 0,
            "ex-citizen": -1
        }
        
        # Check if user is above Duke
        current_rank_value = rank_order.get(current_rank.lower(), 0)
        if current_rank_value > 5:  # Above Duke (Grand Duke, Archduke, Emperor)
            error_embed = discord.Embed(
                title="❌ Cannot Demote",
                description=f"Cannot demote {member.mention} - they are **{current_rank}** which is above Duke rank.\n\nOnly users with rank Duke or below can be demoted using this command.",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.response.send_message(embed=error_embed, ephemeral=True)
            return
        
        # Create and send view with select menu
        view = DemoteView(member, current_rank)
        
        select_embed = discord.Embed(
            title="Demote User",
            description=f"**User:** {member.mention}\n**Current Rank:** {current_rank}\n\nSelect the rank to demote this user to:",
            color=0x3498db,
            timestamp=datetime.utcnow()
        )
        
        await interaction.response.send_message(embed=select_embed, view=view, ephemeral=True)
    
    @bot.tree.context_menu(name="Demote User")
    async def demote_user(interaction: discord.Interaction, member: discord.Member):
        """Demote a user via context menu"""
        
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
        
        user_role_ids = [role.id for role in member.roles]
        
        # Check if user has Sindrian Citizen role
        print(f"User {member.id} has Sindrian Citizen role: {SINDRIAN_CITIZEN_ID in [role.id for role in member.roles]}")
        if SINDRIAN_CITIZEN_ID not in user_role_ids:
            error_embed = discord.Embed(
                title="❌ Cannot Demote",
                description=f"Cannot demote {member.mention} - they don't have the **Sindrian Citizen** role.\n\nUsers without this role cannot be demoted.",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.response.send_message(embed=error_embed, ephemeral=True)
            return
        
        # Get current rank
        current_rank = "None"
        for rank_id, rank_name in RANK_ROLES.items():
            if rank_id in user_role_ids:
                current_rank = rank_name
                break
        
        # Define rank hierarchy order
        rank_order = {
            "emperor": 8,
            "archduke": 7,
            "grand duke": 6,
            "duke": 5,
            "count": 4,
            "viscount": 3,
            "knight": 2,
            "squire": 1,
            "veteran": 0,
            "ex-citizen": -1
        }
        
        # Check if user is above Duke
        current_rank_value = rank_order.get(current_rank.lower(), 0)
        if current_rank_value > 5:  # Above Duke (Grand Duke, Archduke, Emperor)
            error_embed = discord.Embed(
                title="❌ Cannot Demote",
                description=f"Cannot demote {member.mention} - they are **{current_rank}** which is above Duke rank.\n\nOnly users with rank Duke or below can be demoted using this command.",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.response.send_message(embed=error_embed, ephemeral=True)
            return
        
        # Create and send view with select menu
        view = DemoteView(member, current_rank)
        
        select_embed = discord.Embed(
            title="Demote User",
            description=f"**User:** {member.mention}\n**Current Rank:** {current_rank}\n\nSelect the rank to demote this user to:",
            color=0x3498db,
            timestamp=datetime.utcnow()
        )
        
        await interaction.response.send_message(embed=select_embed, view=view, ephemeral=True)
    
    print("[OK] Loaded listroles command")
    print("[OK] Loaded demote user context menu")