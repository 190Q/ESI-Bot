import discord
from typing import Dict, Set, Optional, List
from dataclasses import dataclass

@dataclass
class RoleMapping:
    """Configuration for role mappings"""
    
    # Category role IDs
    WAR_CATEGORY = 1426273223569576057
    BADGE_CATEGORY = 1426272204521341101
    NOTIFICATIONS_CATEGORY = 1426273593335091200
    ACCESS_CATEGORY = 1426274338373369929
    MISC_CATEGORY = 968121600488656906
    
    # Role mappings: specific role ID -> category role ID
    MAPPINGS: Dict[int, int] = None
    
    def __post_init__(self):
        """Initialize role mappings"""
        if self.MAPPINGS is None:
            self.MAPPINGS = {
                # War Roles -> War Category
                995285468222603314: self.WAR_CATEGORY,
                722856382025564161: self.WAR_CATEGORY,
                891933320856895498: self.WAR_CATEGORY,
                1284853392849637399: self.WAR_CATEGORY,
                1284853744705474610: self.WAR_CATEGORY,
                1284854049325322250: self.WAR_CATEGORY,
                1296448399419244644: self.WAR_CATEGORY,
                1296449721795084320: self.WAR_CATEGORY,
                
                # Badge Roles -> Badge Category
                1426633275635404981: self.BADGE_CATEGORY,
                1426633206857465888: self.BADGE_CATEGORY,
                1426633036736368861: self.BADGE_CATEGORY,
                1426632920528846880: self.BADGE_CATEGORY,
                1426633144093638778: self.BADGE_CATEGORY,
                1426632862207049778: self.BADGE_CATEGORY,
                1426632780615385098: self.BADGE_CATEGORY,
                1426634664025526405: self.BADGE_CATEGORY,
                1426634622791323938: self.BADGE_CATEGORY,
                1426634579644514347: self.BADGE_CATEGORY,
                1426634531284324353: self.BADGE_CATEGORY,
                1426634469401432194: self.BADGE_CATEGORY,
                1426634408370114773: self.BADGE_CATEGORY,
                1426634317970542613: self.BADGE_CATEGORY,
                1426636141242617906: self.BADGE_CATEGORY,
                1426636108321525891: self.BADGE_CATEGORY,
                1426636066856898593: self.BADGE_CATEGORY,
                1426636018664341675: self.BADGE_CATEGORY,
                1426635982614040676: self.BADGE_CATEGORY,
                1426635948992761988: self.BADGE_CATEGORY,
                1426635880462024937: self.BADGE_CATEGORY,
                1426637291706912788: self.BADGE_CATEGORY,
                1426637244109946920: self.BADGE_CATEGORY,
                1426637209301160039: self.BADGE_CATEGORY,
                1426637168071282808: self.BADGE_CATEGORY,
                1426637134378303619: self.BADGE_CATEGORY,
                1426637094339608586: self.BADGE_CATEGORY,
                1426636993630175447: self.BADGE_CATEGORY,
                
                # Notification Roles -> Notifications Category
                767252412989702157: self.NOTIFICATIONS_CATEGORY,
                1320710418900979732: self.NOTIFICATIONS_CATEGORY,
                928041302434676766: self.NOTIFICATIONS_CATEGORY,
                1325122692214558832: self.NOTIFICATIONS_CATEGORY,
                800547586694971443: self.NOTIFICATIONS_CATEGORY,
                1297620620628201504: self.NOTIFICATIONS_CATEGORY,
                1054877074491453510: self.NOTIFICATIONS_CATEGORY,
                1289889075242995722: self.NOTIFICATIONS_CATEGORY,
                1370477190524833902: self.NOTIFICATIONS_CATEGORY,
                1357064338615304412: self.NOTIFICATIONS_CATEGORY,
                1370477368057008220: self.NOTIFICATIONS_CATEGORY,
                1384811398667702344: self.NOTIFICATIONS_CATEGORY,
                1330945217821413386: self.NOTIFICATIONS_CATEGORY,
                1330945442166472746: self.NOTIFICATIONS_CATEGORY,
                1330945617467543642: self.NOTIFICATIONS_CATEGORY,
                1330945850746208257: self.NOTIFICATIONS_CATEGORY,
                1330947496918257745: self.NOTIFICATIONS_CATEGORY,
                1330946084108767292: self.NOTIFICATIONS_CATEGORY,
                1330946297288720437: self.NOTIFICATIONS_CATEGORY,
                1330946531724886068: self.NOTIFICATIONS_CATEGORY,
                1330949861037703178: self.NOTIFICATIONS_CATEGORY,
                1330950030936379422: self.NOTIFICATIONS_CATEGORY,
                1330959665453863012: self.NOTIFICATIONS_CATEGORY,
                1419029641926017125: self.NOTIFICATIONS_CATEGORY,
                
                # Access Roles -> Access Category
                669375775551782929: self.ACCESS_CATEGORY,
                728104157852205056: self.ACCESS_CATEGORY,
                786035931647180810: self.ACCESS_CATEGORY,
                1077661051799216128: self.ACCESS_CATEGORY,
                1328051378823757907: self.ACCESS_CATEGORY,
            }
    
    def get_reverse_mapping(self) -> Dict[int, Set[int]]:
        """Create reverse mapping: category ID -> set of role IDs"""
        reverse = {}
        for role_id, category_id in self.MAPPINGS.items():
            if category_id not in reverse:
                reverse[category_id] = set()
            reverse[category_id].add(role_id)
        return reverse

class RoleCache:
    """Caches role and position lookups to reduce API calls"""
    
    def __init__(self):
        self._role_cache: Dict[int, discord.Role] = {}
        self._position_cache: Dict[int, int] = {}
    
    def get_role(self, guild: discord.Guild, role_id: int) -> Optional[discord.Role]:
        """Get role with caching"""
        if role_id not in self._role_cache:
            role = guild.get_role(role_id)
            if role:
                self._role_cache[role_id] = role
                self._position_cache[role_id] = role.position
        return self._role_cache.get(role_id)
    
    def get_position(self, role_id: int) -> Optional[int]:
        """Get cached role position"""
        return self._position_cache.get(role_id)
    
    def clear(self):
        """Clear cache"""
        self._role_cache.clear()
        self._position_cache.clear()

class RoleProcessor:
    """Processes role additions and removals"""
    
    def __init__(self, mapping: RoleMapping):
        self.mapping = mapping
        self.reverse_mapping = mapping.get_reverse_mapping()
        self.cache = RoleCache()
    
    async def process_added_roles(
        self,
        member: discord.Member,
        added_roles: Set[discord.Role]
    ) -> List[discord.Role]:
        """Process added roles and return category roles to add"""
        roles_to_add = []
        
        # Get misc category position once
        misc_role = self.cache.get_role(member.guild, self.mapping.MISC_CATEGORY)
        misc_position = misc_role.position if misc_role else None
        
        for added_role in added_roles:
            # Check mapped roles
            if added_role.id in self.mapping.MAPPINGS:
                category_id = self.mapping.MAPPINGS[added_role.id]
                category_role = self.cache.get_role(member.guild, category_id)
                
                if category_role and category_role not in member.roles:
                    roles_to_add.append(category_role)
                    print(f"[Role Manager] Queued category role {category_role.name} for {member.name}")
            
            # Check misc category (roles below misc position)
            elif misc_position is not None and added_role.position < misc_position:
                if misc_role and misc_role not in member.roles:
                    roles_to_add.append(misc_role)
                    print(f"[Role Manager] Queued misc category role for {member.name}")
        
        return roles_to_add
    
    async def process_removed_roles(
        self,
        member: discord.Member,
        removed_roles: Set[discord.Role]
    ) -> List[discord.Role]:
        """Process removed roles and return category roles to remove"""
        roles_to_remove = []
        
        # Get misc category position
        misc_role = self.cache.get_role(member.guild, self.mapping.MISC_CATEGORY)
        misc_position = misc_role.position if misc_role else None
        
        # Get current role IDs for fast lookup
        current_role_ids = {role.id for role in member.roles}
        
        for removed_role in removed_roles:
            # Check mapped roles
            if removed_role.id in self.mapping.MAPPINGS:
                category_id = self.mapping.MAPPINGS[removed_role.id]
                category_role = self.cache.get_role(member.guild, category_id)
                
                if category_role and category_role in member.roles:
                    # Check if member has any other roles in this category
                    roles_in_category = self.reverse_mapping[category_id]
                    
                    if not (roles_in_category & current_role_ids):
                        roles_to_remove.append(category_role)
                        print(f"[Role Manager] Queued removal of {category_role.name} from {member.name}")
            
            # Check misc category
            elif misc_position is not None and removed_role.position < misc_position:
                if misc_role and misc_role in member.roles:
                    # Check if member still has any misc roles
                    has_misc = any(
                        role.position < misc_position and
                        role.id != self.mapping.MISC_CATEGORY and
                        role.id not in self.mapping.MAPPINGS
                        for role in member.roles
                    )
                    
                    if not has_misc:
                        roles_to_remove.append(misc_role)
                        print(f"[Role Manager] Queued removal of misc category from {member.name}")
        
        return roles_to_remove
    
    async def apply_role_changes(
        self,
        member: discord.Member,
        roles_to_add: List[discord.Role],
        roles_to_remove: List[discord.Role]
    ):
        """Apply role changes in batch"""
        try:
            # Remove duplicates and filter out None
            roles_to_add = list(set(r for r in roles_to_add if r))
            roles_to_remove = list(set(r for r in roles_to_remove if r))
            
            # Apply additions
            if roles_to_add:
                await member.add_roles(*roles_to_add, reason="Auto role management")
                print(f"[Role Manager] Added {len(roles_to_add)} role(s) to {member.name}")
            
            # Apply removals
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="Auto role management")
                print(f"[Role Manager] Removed {len(roles_to_remove)} role(s) from {member.name}")
                
        except discord.Forbidden:
            print(f"[Role Manager] Missing permissions to modify roles for {member.name}")
        except discord.HTTPException as e:
            print(f"[Role Manager] HTTP error modifying roles: {e}")
        except Exception as e:
            print(f"[Role Manager] Error applying role changes: {e}")

class RoleManager:
    """Main role management system"""
    
    def __init__(self):
        self.mapping = RoleMapping()
        self.processor = RoleProcessor(self.mapping)
    
    async def handle_member_update(
        self,
        before: discord.Member,
        after: discord.Member
    ):
        """Handle member role updates"""
        # Calculate role changes
        added_roles = set(after.roles) - set(before.roles)
        removed_roles = set(before.roles) - set(after.roles)
        
        # Skip if no changes
        if not added_roles and not removed_roles:
            return
        
        # Process additions
        roles_to_add = []
        if added_roles:
            roles_to_add = await self.processor.process_added_roles(after, added_roles)
        
        # Process removals
        roles_to_remove = []
        if removed_roles:
            roles_to_remove = await self.processor.process_removed_roles(after, removed_roles)
        
        # Apply changes in batch
        if roles_to_add or roles_to_remove:
            await self.processor.apply_role_changes(after, roles_to_add, roles_to_remove)

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    # Initialize role manager
    role_manager = RoleManager()
    
    @bot.event
    async def on_member_update(before: discord.Member, after: discord.Member):
        """Listen for role changes and manage category roles"""
        await role_manager.handle_member_update(before, after)
    
    print("[Role Manager] Loaded - automatically assigns and removes category roles")
