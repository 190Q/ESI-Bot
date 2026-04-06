import discord
from discord import app_commands
import os
from PIL import Image, ImageDraw, ImageFont
import aiohttp
from io import BytesIO
import sqlite3
from datetime import datetime
import random
from utils.permissions import has_roles

REQUIRED_ROLES = [
    int(os.getenv('OWNER_ID')) if os.getenv('OWNER_ID') else 0
]
SHIP_REQUIRED_ROLES = []

# Special ship pair - these users will always have high compatibility with each other
SPECIAL_PAIR = [
    (628627658917281813, 529034530435366952),
    (789679098779009085, 419206871761551360)
]
PROTECTED_USER = SPECIAL_PAIR[0][0]
SPECIAL_PAIR_MIN = 80
SPECIAL_PAIR_MAX = 100
OUTSIDER_WITH_SPECIAL_MIN = 0
OUTSIDER_WITH_SPECIAL_MAX = 20

# Try to determine font path once at startup
FONT_PATH = None
for path in [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "arialbd.ttf",
    "Arial-Bold.ttf"
]:
    try:
        ImageFont.truetype(path, 10)  # Test load
        FONT_PATH = path
        break
    except:
        continue

_SHIP_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'databases', 'ship_scores.db')

def init_db():
    """Initialize the database"""
    conn = sqlite3.connect(_SHIP_DB)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS ship_scores
                 (user1_id INTEGER, user2_id INTEGER, score INTEGER, date TEXT,
                  PRIMARY KEY (user1_id, user2_id, date))''')
    conn.commit()
    conn.close()

def get_or_create_ship_score(user1_id, user2_id):
    """Get existing score or create new one for today"""
    # Sort IDs to ensure consistent pairing regardless of order
    id1, id2 = sorted([user1_id, user2_id])
    
    today = datetime.now().strftime('%Y-%m-%d')
    
    conn = sqlite3.connect(_SHIP_DB)
    c = conn.cursor()
    
    # Check if score exists for today
    c.execute('SELECT score FROM ship_scores WHERE user1_id=? AND user2_id=? AND date=?',
              (id1, id2, today))
    result = c.fetchone()
    
    if result:
        score = result[0]
    else:
        # Check if this is any special pair
        is_special_pair = False
        
        for pair_user1, pair_user2 in SPECIAL_PAIR:
            # Check if this is the special pair
            if set([user1_id, user2_id]) == set([pair_user1, pair_user2]):
                is_special_pair = True
                break
        
        if is_special_pair:
            # Special pair gets high score
            score = random.randint(SPECIAL_PAIR_MIN, SPECIAL_PAIR_MAX)
        elif PROTECTED_USER in [user1_id, user2_id]:
            # Only the protected user with someone else gets low score
            score = random.randint(OUTSIDER_WITH_SPECIAL_MIN, OUTSIDER_WITH_SPECIAL_MAX)
        else:
            # Normal users get normal random score
            score = random.randint(1, 100)
        
        c.execute('INSERT INTO ship_scores (user1_id, user2_id, score, date) VALUES (?, ?, ?, ?)',
                  (id1, id2, score, today))
        conn.commit()
    
    conn.close()
    return score

async def download_image(url):
    """Download image from URL"""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.read()
    return None

def create_side_by_side_image(img1_bytes, img2_bytes, ship_score):
    """Create a side-by-side image from two profile pictures with heart overlay"""
    # Open images
    img1 = Image.open(BytesIO(img1_bytes)).convert('RGBA')
    img2 = Image.open(BytesIO(img2_bytes)).convert('RGBA')
    
    # Resize to same height (e.g., 512x512)
    size = (512, 512)
    img1 = img1.resize(size, Image.Resampling.LANCZOS)
    img2 = img2.resize(size, Image.Resampling.LANCZOS)
    
    # Add spacing between images
    spacing = 140
    side_spacing = 40
    
    # Create new image with transparent background
    combined_width = img1.width + img2.width + spacing + (side_spacing * 2)
    combined_height = max(img1.height, img2.height)
    combined_img = Image.new('RGBA', (combined_width, combined_height), color=(0, 0, 0, 0))
    
    # Paste images side by side with spacing
    combined_img.paste(img1, (side_spacing, 0), img1)
    combined_img.paste(img2, (img1.width + spacing + side_spacing, 0), img2)
    
    # Load heart image
    try:
        heart_img = Image.open('images/pink_heart.png').convert('RGBA')
    except FileNotFoundError:
        print("Warning: Heart image not found at images/pink_heart.png")
        # Create a fallback simple heart if image not found
        heart_img = Image.new('RGBA', (200, 200), (0, 0, 0, 0))
    
    # Calculate center position (between the two images)
    center_x = side_spacing + img1.width + (spacing // 2)
    center_y = combined_height // 2
    
    # Calculate heart size based on score (min 180, max 350)
    heart_size = int(180 + (ship_score * 1.7))
    
    # Resize heart image based on score
    heart_img = heart_img.resize((heart_size, heart_size), Image.Resampling.LANCZOS)
    
    # Calculate position to center the heart
    heart_x = center_x - (heart_size // 2)
    heart_y = center_y - (heart_size // 2)
    
    # Paste heart onto combined image
    combined_img.paste(heart_img, (heart_x, heart_y), heart_img)
    
    # Create overlay for text
    overlay = Image.new('RGBA', combined_img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    # Add text with percentage
    if FONT_PATH:
        font = ImageFont.truetype(FONT_PATH, int(heart_size * 0.28))
    else:
        print("Warning: No suitable font found")
        font = ImageFont.load_default()
        
    text = f"{ship_score}%"
    
    # Get text bounding box for centering
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    
    # Position text in the center of the heart, slightly higher
    text_x = heart_x + (heart_size // 2) - (text_width // 2)
    text_y = heart_y + (heart_size // 2) - (text_height // 2) - int(heart_size * 0.07)
    
    # Draw white text (no shadow)
    draw.text((text_x, text_y), 
              text, fill=(255, 255, 255, 255), font=font)
    
    # Composite overlay onto combined image
    combined_img = Image.alpha_composite(combined_img, overlay)
    
    # Save to bytes
    output = BytesIO()
    combined_img.save(output, format='PNG')
    output.seek(0)
    
    return output

def force_set_ship_score(user1_id, user2_id, score):
    """Force set a ship score for today, overwriting any existing score"""
    # Sort IDs to ensure consistent pairing regardless of order
    id1, id2 = sorted([user1_id, user2_id])
    
    today = datetime.now().strftime('%Y-%m-%d')
    
    conn = sqlite3.connect(_SHIP_DB)
    c = conn.cursor()
    
    # Delete existing score if it exists
    c.execute('DELETE FROM ship_scores WHERE user1_id=? AND user2_id=? AND date=?',
              (id1, id2, today))
    
    # Insert new score
    c.execute('INSERT INTO ship_scores (user1_id, user2_id, score, date) VALUES (?, ?, ?, ?)',
              (id1, id2, score, today))
    conn.commit()
    conn.close()

def remove_ship_score(user1_id, user2_id):
    """Remove a ship score for today"""
    # Sort IDs to ensure consistent pairing regardless of order
    id1, id2 = sorted([user1_id, user2_id])
    
    today = datetime.now().strftime('%Y-%m-%d')
    
    conn = sqlite3.connect(_SHIP_DB)
    c = conn.cursor()
    
    # Delete existing score if it exists
    c.execute('DELETE FROM ship_scores WHERE user1_id=? AND user2_id=? AND date=?',
              (id1, id2, today))
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    
    return deleted

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    
    # Initialize database
    init_db()
    
    @bot.tree.command(
        name="ship",
        description="Ship two users together and see their compatibility"
    )
    @app_commands.describe(
        user="First user to ship",
        other_user="Second user to ship (optional, defaults to you)"
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def ship(
        interaction: discord.Interaction,
        user: discord.Member,
        other_user: discord.Member = None
    ):
        """Ship two users together"""

        # Check permissions if required
        if interaction.guild:
            if not has_roles(interaction.user, SHIP_REQUIRED_ROLES) and SHIP_REQUIRED_ROLES:
                missing_roles_embed = discord.Embed(
                    title="Permission Denied",
                    description="You don't have permission to use this command!",
                    color=0xFF0000
                )
                await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
                return
        
        # Defer response since this might take a moment
        await interaction.response.defer()
        
        # If other_user is not provided, use the command sender
        if other_user is None:
            other_user = interaction.user
        
        try:
            # Get or generate ship score
            ship_score = get_or_create_ship_score(user.id, other_user.id)
            
            # Download both profile pictures (using global avatar)
            img1_bytes = await download_image(user.avatar.url if user.avatar else user.display_avatar.url)
            img2_bytes = await download_image(other_user.avatar.url if other_user.avatar else other_user.display_avatar.url)
            
            if not img1_bytes or not img2_bytes:
                await interaction.followup.send("Failed to download profile pictures!", ephemeral=True)
                return
            
            # Create combined image with heart
            combined_image = create_side_by_side_image(img1_bytes, img2_bytes, ship_score)
            
            # Create file
            file = discord.File(combined_image, filename="ship.png")
            
            # Send with ship score
            await interaction.followup.send(
                file=file
            )
            
            print(f"Ship command used: {user.name} x {other_user.name} = {ship_score}%")
            
        except Exception as e:
            print(f"Error in ship command: {e}")
            await interaction.followup.send("An error occurred while creating the ship!", ephemeral=True)
    
    @bot.tree.command(
        name="ship_force",
        description="Force a ship score between two users"
    )
    @app_commands.describe(
        user="First user to ship",
        other_user="Second user to ship",
        score="Ship score"
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def forceship(
        interaction: discord.Interaction,
        user: discord.Member,
        other_user: discord.Member,
        score: int
    ):
        """Force ship two users together with a specific score"""

        # Check permissions
        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return
        
        # Defer response since this might take a moment
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Force set the ship score
            force_set_ship_score(user.id, other_user.id, score)
            
            # Send confirmation embed
            success_embed = discord.Embed(
                title="Ship score set",
                description=f"{user.mention} x {other_user.mention} -> {score}%",
                color=0x00FF00
            )
            await interaction.followup.send(embed=success_embed, ephemeral=True)
            
            print(f"Force ship command used: {user.name} x {other_user.name} = {score}%")
            
        except Exception as e:
            print(f"Error in forceship command: {e}")
            await interaction.followup.send("An error occurred while creating the ship!", ephemeral=True)

    @bot.tree.command(
        name="ship_remove",
        description="Remove a forced ship score between two users"
    )
    @app_commands.describe(
        user="First user",
        other_user="Second user"
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def removeship(
        interaction: discord.Interaction,
        user: discord.Member,
        other_user: discord.Member
    ):
        """Remove a ship score between two users"""

        # Check permissions
        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return
        
        # Defer response (ephemeral)
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Remove the ship score
            deleted = remove_ship_score(user.id, other_user.id)
            
            if deleted:
                success_embed = discord.Embed(
                    title="Ship score removed",
                    description=f"{user.mention} x {other_user.mention} ship score has been removed",
                    color=0x00FF00
                )
            else:
                success_embed = discord.Embed(
                    title="No ship score found",
                    description=f"{user.mention} x {other_user.mention} had no ship score for today",
                    color=0xFFA500
                )
            
            await interaction.followup.send(embed=success_embed, ephemeral=True)
            
            print(f"Remove ship command used: {user.name} x {other_user.name} - Deleted: {deleted}")
            
        except Exception as e:
            print(f"Error in removeship command: {e}")
            await interaction.followup.send("An error occurred while removing the ship!", ephemeral=True)

    @bot.tree.command(
        name="ship_list",
        description="List all ship scores for today"
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def listships(
        interaction: discord.Interaction
    ):
        """List all ship scores for today"""

        # Check permissions
        if not has_roles(interaction.user, REQUIRED_ROLES) and REQUIRED_ROLES:
            missing_roles_embed = discord.Embed(
                title="Permission Denied",
                description="You don't have permission to use this command!",
                color=0xFF0000
            )
            await interaction.response.send_message(embed=missing_roles_embed, ephemeral=True)
            return
        
        # Defer response (ephemeral)
        await interaction.response.defer(ephemeral=True)
        
        try:
            today = datetime.now().strftime('%Y-%m-%d')
            
            conn = sqlite3.connect(_SHIP_DB)
            c = conn.cursor()
            
            # Get all ships for today
            c.execute('SELECT user1_id, user2_id, score FROM ship_scores WHERE date=? ORDER BY score DESC',
                      (today,))
            ships = c.fetchall()
            conn.close()
            
            if not ships:
                no_ships_embed = discord.Embed(
                    title="Ship Scores for Today",
                    description="No ship scores recorded for today yet!",
                    color=0xFFA500
                )
                await interaction.followup.send(embed=no_ships_embed, ephemeral=True)
                return
            
            # Build the list
            ship_list = []
            for user1_id, user2_id, score in ships:
                try:
                    user1 = await interaction.client.fetch_user(user1_id)
                    user2 = await interaction.client.fetch_user(user2_id)
                    ship_list.append(f"**{score}%** - {user1.name} x {user2.name}")
                except:
                    ship_list.append(f"**{score}%** - User {user1_id} x User {user2_id}")
            
            # Create embed
            list_embed = discord.Embed(
                title=f"Ship Scores for Today ({len(ships)} total)",
                description="\n".join(ship_list),
                color=0xFF69B4
            )
            
            await interaction.followup.send(embed=list_embed, ephemeral=True)
            
            print(f"List ships command used by {interaction.user.name}")
            
        except Exception as e:
            print(f"Error in listships command: {e}")
            await interaction.followup.send("An error occurred while listing ships!", ephemeral=True)

    print("[OK] Loaded ship command")