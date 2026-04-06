import discord
import re
import os
import random
from utils.permissions import has_roles

# Add specific user IDs who should get the GIF response
SPECIAL_USERS = [
    1176255313096232964, # Sir Crab
    int(os.getenv('OWNER_ID')) if int(os.getenv('OWNER_ID')) else 0
]

# GIF list with optional custom percentages
# Format: (url, percentage) where percentage is None for equal share of remaining probability
HELLO_GIFS = [
    ("https://tenor.com/view/crab-rave-dancing-dancing-crab-gif-16543314", None),
    ("https://tenor.com/view/crab-lara-waving-hi-hello-gif-10815615172503534079", None),
    ("https://tenor.com/view/yeeclaw-gif-21242385", None),
    ("https://tenor.com/view/wave-crab-crab-wave-underwater-crab-say-hi-gif-9901533013817257281", None),
    ("https://tenor.com/view/sonic-movie-3-gerald-robotnik-holy-crab-jim-carrey-sonic-the-hedgehog-3-gif-16518693201413902436", None),
    ("https://tenor.com/view/lobster-lobster-stare-you-will-pay-for-your-crimes-silly-sea-animal-gif-17625369597744969252", None),
    ("https://cdn.discordapp.com/attachments/1415428699490222121/1470262400136642754/togif.gif", 2), # Skilly aura fail
    ("https://cdn.discordapp.com/attachments/1415428699490222121/1470263635442929684/SPOILER_togif.gif", 2), # OkGoogle guy embezzling guild funds
    ("https://cdn.discordapp.com/attachments/1415428699490222121/1470264194762018836/togif.gif", 2), # Well, I guess I'm in charge now
]

def select_random_gif():
    """Select a random GIF based on weighted probabilities."""
    # Calculate fixed percentages and count normal GIFs
    fixed_total = sum(pct for _, pct in HELLO_GIFS if pct is not None)
    normal_count = sum(1 for _, pct in HELLO_GIFS if pct is None)
    
    # Remaining percentage split equally among normal GIFs
    remaining = 100 - fixed_total
    normal_pct = remaining / normal_count if normal_count > 0 else 0
    
    # Build weights list
    weights = [pct if pct is not None else normal_pct for _, pct in HELLO_GIFS]
    urls = [url for url, _ in HELLO_GIFS]
    
    return random.choices(urls, weights=weights, k=1)[0]

# Store reference to listener for cleanup
_listener = None

def setup(bot, has_required_role, config):
    """Setup function for bot integration"""
    global _listener
    
    async def on_message(message):
        # Ignore bot's own messages
        if message.author == bot.user:
            return
        
        # Check if user is in the special users list
        if message.author.id in SPECIAL_USERS:
            # Check if message is a greeting (case-insensitive)
            greeting_pattern = r'\b(hello again esi|hello esi|happy new year again esi|happy new year esi)\b'
            if re.search(greeting_pattern, message.content.lower()):
                await message.channel.send(select_random_gif())
        
        # Process commands as normal
        await bot.process_commands(message)
    
    _listener = on_message
    bot.add_listener(on_message, 'on_message')
    
    print("[OK] Loaded template command with hello GIF responder")

def teardown(bot):
    """Cleanup function called when module is unloaded"""
    global _listener
    if _listener is not None:
        bot.remove_listener(_listener, 'on_message')
        _listener = None
    print("[OK] Unloaded crab_hello listener")
