import discord
from discord import app_commands
import os
import json
import requests
import base64
from datetime import datetime
from PIL import Image
from io import BytesIO
import sqlite3
from utils.permissions import has_roles

WYNNCRAFT_KEY_11 = os.getenv('WYNNCRAFT_KEY_11')

RANK_ROLES = [
    (554506531949772812, "Emperor"),
    (554513014251061258, "Magi"),
    (554514823191199747, "Archduke"),
    (1396112289832243282, "Grand Duke"),
    (591765870272053261, "Duke"),
    (1391424890938195998, "Count"),
    (591769392828776449, "Viscount"),
    (688438690137243892, "Knight"),
    (681030746651230351, "Squire")
]

PARLIAMENT_ROLE_ID = 600185623474601995
VETERAN_ROLE_ID = 914422269802070057
EX_CITIZEN_ROLE_ID = 706338091312349195
SINDRIAN_CITIZEN_ROLE_ID = 554889169705500672

REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0,
    SINDRIAN_CITIZEN_ROLE_ID,
    VETERAN_ROLE_ID,
    EX_CITIZEN_ROLE_ID
]

def get_uuid_from_username(username):
    url = f"https://api.mojang.com/users/profiles/minecraft/{username}"
    response = requests.get(url)
    
    if response.status_code == 200:
        data = response.json()
        return data['id'], data['name']
    elif response.status_code == 204:
        return None, None
    else:
        return None, None

def get_player_profile(uuid):
    url = f"https://sessionserver.mojang.com/session/minecraft/profile/{uuid}"
    response = requests.get(url)
    
    if response.status_code == 200:
        return response.json()
    else:
        return None

def decode_skin_data(encoded_data):
    decoded = base64.b64decode(encoded_data)
    return json.loads(decoded)

def download_skin(skin_url):
    response = requests.get(skin_url)
    if response.status_code == 200:
        return Image.open(BytesIO(response.content)).convert("RGBA")
    else:
        return None

def convert_skin_to_64x64(skin):
    width, height = skin.size
    
    if width == 64 and height == 32:
        # Create a new 64x64 image
        new_skin = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        
        # Copy the old skin to the top half
        new_skin.paste(skin, (0, 0))
        
        pixels = new_skin.load()
        old_pixels = skin.load()
        
        # In 64x32 format:
        # Right leg: x: 0-16, y: 16-32 (all sides)
        # Right arm: x: 40-56, y: 16-32 (all sides)
        
        # Copy right arm to left arm position (with all 6 faces)
        # Right arm in old format: x: 40-56 (width 16), y: 16-32 (height 16)
        # Left arm in new format: x: 32-48 (width 16), y: 48-64 (height 16)
        for y in range(16):
            for x in range(16):
                # Copy and mirror the arm
                source_x = 40 + (15 - x)
                pixels[32 + x, 48 + y] = old_pixels[source_x, 16 + y]
        
        # Copy right leg to left leg position (with all 6 faces)
        # Right leg in old format: x: 0-16 (width 16), y: 16-32 (height 16)
        # Left leg in new format: x: 16-32 (width 16), y: 48-64 (height 16)
        for y in range(16):
            for x in range(16):
                # Copy and mirror the leg
                source_x = 15 - x
                pixels[16 + x, 48 + y] = old_pixels[source_x, 16 + y]
        
        return new_skin
    
    return skin

def clean_skin_layers(skin):
    width, height = skin.size
    
    if width == 64 and height == 64:
        pixels = skin.load()
        
        # Clear torso second layer
        for x in range(16, 40):
            for y in range(32, 48):
                pixels[x, y] = (0, 0, 0, 0)
        
        # Clear right arm second layer
        for x in range(40, 56):
            for y in range(32, 48):
                pixels[x, y] = (0, 0, 0, 0)
        
        # Clear left arm second layer
        for x in range(48, 64):
            for y in range(48, 64):
                pixels[x, y] = (0, 0, 0, 0)
        
        # Clear right leg second layer
        for x in range(0, 16):
            for y in range(32, 48):
                pixels[x, y] = (0, 0, 0, 0)
        
        # Clear left leg second layer
        for x in range(0, 16):
            for y in range(48, 64):
                pixels[x, y] = (0, 0, 0, 0)
    
    return skin

def apply_uniform(player_skin, uniform_path):
    if not os.path.exists(uniform_path):
        return None
    
    uniform = Image.open(uniform_path).convert("RGBA")
    
    if player_skin.size != uniform.size:
        uniform = uniform.resize(player_skin.size, Image.Resampling.LANCZOS)
    
    composite = Image.alpha_composite(player_skin, uniform)
    return composite

def get_user_rank(user):
    """Get the user's rank based on their Discord roles"""
    user_role_ids = [role.id for role in user.roles]
    
    for role_id, rank in RANK_ROLES:
        if role_id in user_role_ids:
            return rank
    
    return None

def get_discord_id_from_minecraft(minecraft_username):
    """Get Discord ID from Minecraft username using username_matches.json"""
    try:
        with open('username_matches.json', 'r') as f:
            username_matches = json.load(f)
        
        # Search for the username in the stored data
        for discord_id, user_data in username_matches.items():
            if isinstance(user_data, dict):
                stored_username = user_data.get('username', '')
            elif isinstance(user_data, str):
                stored_username = user_data
            else:
                continue
            if stored_username.lower() == minecraft_username.lower():
                return int(discord_id)
        return None
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None

def get_minecraft_username_from_discord(discord_id):
    """Get Minecraft username from Discord ID using username_matches.json"""
    try:
        with open('username_matches.json', 'r') as f:
            username_matches = json.load(f)
        
        discord_id_str = str(discord_id)
        user_data = username_matches.get(discord_id_str)
        
        # Return the username from the nested structure
        if user_data and isinstance(user_data, dict):
            return user_data.get('username')
        elif user_data and isinstance(user_data, str):
            return user_data
        return None
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    @bot.tree.command(
        name="get_uniform",
        description="Get your Minecraft character with a uniform applied"
    )
    @app_commands.describe(username="Minecraft username (optional, defaults to your linked account)")
    async def get_uniform(interaction: discord.Interaction, username: str = None):
        """Get uniform command"""
        
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
        
        await interaction.response.defer()
        
        minecraft_username = username
        target_user = interaction.user # Default to the user who triggered the command
        
        if not minecraft_username:
            minecraft_username = get_minecraft_username_from_discord(interaction.user.id)
            
            if not minecraft_username:
                error_embed = discord.Embed(
                    title="Error",
                    description="No Minecraft account linked to your Discord account. Please provide a username.",
                    color=0xFF0000,
                    timestamp=datetime.utcnow()
                )
                await interaction.followup.send(embed=error_embed, ephemeral=True)
                return
        else:
            # Username was provided, try to find the linked Discord user
            discord_id = get_discord_id_from_minecraft(minecraft_username)
            if discord_id:
                target_user = interaction.guild.get_member(discord_id)
                if not target_user:
                    # User not in server
                    error_embed = discord.Embed(
                        title="Error",
                        description=f"The Discord user linked to `{minecraft_username}` is not in the server.",
                        color=0xFF0000,
                        timestamp=datetime.utcnow()
                    )
                    await interaction.followup.send(embed=error_embed, ephemeral=True)
                    return
            else:
                # No linked Discord account found - show error instead of falling back
                error_embed = discord.Embed(
                    title="Error",
                    description=f"No Discord account is linked to the Minecraft username `{minecraft_username}`.\nContact a <@600185623474601995> member to link your Discord account to your Minecarft username first!",
                    color=0xFF0000,
                    timestamp=datetime.utcnow()
                )
                await interaction.followup.send(embed=error_embed, ephemeral=True)
                return
        
        # Check if target user has Sindrian Citizen, Veteran, or Ex-Citizen role
        user_role_ids = [role.id for role in target_user.roles]
        has_valid_role = (
            SINDRIAN_CITIZEN_ROLE_ID in user_role_ids or
            VETERAN_ROLE_ID in user_role_ids or
            EX_CITIZEN_ROLE_ID in user_role_ids
        )
        
        if not has_valid_role:
            error_embed = discord.Embed(
                title="Error",
                description=f"The user linked to `{minecraft_username}` does not have a Sindrian Citizen, Veteran, or Ex-Citizen role.",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.followup.send(embed=error_embed, ephemeral=True)
            return
        
        user_rank = None
        if not (
            VETERAN_ROLE_ID in user_role_ids or
            EX_CITIZEN_ROLE_ID in user_role_ids
        ):
            user_rank = get_user_rank(target_user)
        
            if not user_rank:
                error_embed = discord.Embed(
                    title="Error",
                    description=f"The user linked to `{minecraft_username}` does not have a valid rank role.",
                    color=0xFF0000,
                    timestamp=datetime.utcnow()
                )
                await interaction.followup.send(embed=error_embed, ephemeral=True)
                return
            
        user_role_ids = [role.id for role in target_user.roles]
        is_parliament = PARLIAMENT_ROLE_ID in user_role_ids
        is_veteran = VETERAN_ROLE_ID in user_role_ids
        is_ex_citizen = EX_CITIZEN_ROLE_ID in user_role_ids
        
        # Determine uniform type (priority: parliament > veteran > ex-citizen > sindrian citizen)
        if is_parliament:
            uniform_type = "Parliament Uniform"
        elif is_veteran:
            uniform_type = "Veteran Uniform"
        elif is_ex_citizen:
            uniform_type = "Ex-Citizen Uniform"
        else:
            uniform_type = "Sindrian Citizen Uniform"
        
        uuid, current_name = get_uuid_from_username(minecraft_username)
        
        if not uuid:
            error_embed = discord.Embed(
                title="Error",
                description=f"Minecraft player '{minecraft_username}' not found.",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.followup.send(embed=error_embed, ephemeral=True)
            return
        
        profile = get_player_profile(uuid)
        
        skin_url = None
        skin_model = "steve"
        
        if profile and 'properties' in profile:
            for prop in profile['properties']:
                texture_data = decode_skin_data(prop['value'])
                textures = texture_data.get('textures', {})
                
                if 'SKIN' in textures:
                    skin = textures['SKIN']
                    skin_url = skin.get('url', None)
                    
                    metadata = skin.get('metadata', {})
                    if metadata:
                        model = metadata.get('model', 'classic')
                        skin_model = "alex" if model == "slim" else "steve"
                    else:
                        skin_model = "steve"
        
        if not skin_url:
            skin_url = "http://assets.mojang.com/SkinTemplates/steve.png"
        
        player_skin = download_skin(skin_url)
        
        if not player_skin:
            error_embed = discord.Embed(
                title="Error",
                description="Failed to download player skin.",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.followup.send(embed=error_embed, ephemeral=True)
            return
        
        player_skin = convert_skin_to_64x64(player_skin)
        player_skin = clean_skin_layers(player_skin)
        
        # Determine uniform folder based on role priority
        if is_parliament:
            uniform_folder = "parliament"
        elif is_veteran or is_ex_citizen:
            uniform_folder = "ex_citizen"
        else:
            uniform_folder = "sindrian_cit"
        
        if is_veteran:
            uniform_filename = f"veteran_{skin_model}.png"
        elif is_ex_citizen:
            uniform_filename = f"ex_citizen_{skin_model}.png"
        else:
            uniform_filename = f"{user_rank.replace('Emperor', 'archduke').replace('Magi', 'archduke').lower().strip()}_{skin_model}.png"
        uniform_path = os.path.join("./images/uniforms", uniform_folder, uniform_filename).replace(' ', '_')
        
        result_skin = apply_uniform(player_skin, uniform_path)
        
        if not result_skin:
            error_embed = discord.Embed(
                title="Error",
                description=f"Failed to apply uniform. Uniform file not found at: {uniform_path}",
                color=0xFF0000,
                timestamp=datetime.utcnow()
            )
            await interaction.followup.send(embed=error_embed, ephemeral=True)
            return
                
        uniform_type_short = "parliament" if is_parliament else "normal"
        if is_veteran:
            output_filename = f"{current_name.lower().strip()}_veteran.png"
        elif is_ex_citizen:
            output_filename = f"{current_name.lower().strip()}_ex_citizen.png"
        else:
            output_filename = f"{current_name.lower().strip()}_{user_rank.lower().replace(' ', '_')}.png"
        
        img_bytes = BytesIO()
        result_skin.save(img_bytes, format="png")    
        img_bytes.seek(0)
        
        if is_veteran:
            rank = "Veteran"
        elif is_ex_citizen:
            rank = "Ex-Citizen"
        else:
            rank = user_rank
        
        description = f"⚠️ Please make sure the uniform generated correctly, the bot can make errors!\n\n"
        description += f"Generated the {uniform_type.lower()} for the `{rank}` rank on `{current_name}` successfully!\n"
        description += f"To apply the uniform follow these steps:\n"
        description += f" - Open the Minecraft Launcher.\n"
        description += f" - Go to the 'Skins' tab.\n"
        description += f" - Click 'New Skin', then 'Browse' and upload this png file.\n"
        description += f" - Then finally, click 'Save & Use'.\n"
        
        success_embed = discord.Embed(
            title="Uniform Generated Successfully",
            description=description,
            color=0x00FF00,
            timestamp=datetime.utcnow()
        )
        success_embed.set_image(url=f"attachment://{output_filename}")
        
        file = discord.File(img_bytes, filename=output_filename)
        await interaction.followup.send(embed=success_embed, file=file)
    
    print("[OK] Loaded get_uniform command")