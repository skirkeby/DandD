import discord
from discord.ext import commands
import os
import aiosqlite
from dotenv import load_dotenv
import asyncio
import json

# Import your DndEngine from main.py
from main import DndEngine, MockAsyncRedisClient, init_db, DB_PATH, Stat, Condition

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# Define bot intents
intents = discord.Intents.default()
intents.message_content = True  # Required for prefix commands

# Initialize Bot
bot = commands.Bot(command_prefix='!', intents=intents)

# Global variables for the engine and connections
engine = None
db_conn = None

@bot.event
async def on_ready():
    global engine, db_conn
    
    print(f'Logged in as {bot.user.name} (ID: {bot.user.id})')
    print('------')
    
    # Initialize DB (creates table/mocks if missing)
    await init_db()
    
    # Setup sqlite connection
    db_conn = await aiosqlite.connect(DB_PATH)
    db_conn.row_factory = aiosqlite.Row
    
    # Mock redis and Engine
    redis_client = MockAsyncRedisClient()
    engine = DndEngine(db_conn, redis_client)

    print("DndEngine is ready and linked to Discord!")

# -----------------------------------------------------------------------------
# DISCORD COMMANDS
# -----------------------------------------------------------------------------

@bot.command(name='roll')
async def roll_check(ctx, stat: str, dc: int):
    """
    Rolls an ability check.
    Example: !roll Dex 15
    """
    try:
        # Match the provided stat string to the Stat Enum (case insensitive ideally)
        stat_enum = None
        for s in Stat:
            if s.value.lower() == stat.lower():
                stat_enum = s
                break
                
        if not stat_enum:
            await ctx.send(f"❌ Invalid stat '{stat}'. Valid stats are: {', '.join([s.value for s in Stat])}")
            return
            
        # Hardcoding "Player1" for demonstration
        # Ideally, we map discord User ID to character ID in the future
        player_id = "Player1"
        channel_id = str(ctx.channel.id)
        
        result = await engine.perform_ability_check(channel_id, player_id, stat_enum, dc)
        
        if "error" in result:
             await ctx.send(f"Error: {result['error']}")
             return
             
        # Format the output beautifully for Discord
        embed = discord.Embed(
            title=f"🎲 Ability Check: {stat_enum.value}",
            color=discord.Color.green() if result['success'] else discord.Color.red()
        )
        embed.add_field(name="Character", value=player_id, inline=True)
        embed.add_field(name="Difficulty Class (DC)", value=dc, inline=True)
        embed.add_field(name="Roll Result", value=f"1d20 ({result['roll']}) + Mod ({result['modifier']}) = **{result['total']}**", inline=False)
        embed.add_field(name="Outcome", value=result['result_text_hint'], inline=False)
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"An error occurred: {e}")


@bot.command(name='damage')
async def apply_damage_cmd(ctx, target: str, amount: int):
    """
    Applies damage to a target.
    Example: !damage Player1 8
    """
    try:
        channel_id = str(ctx.channel.id)
        result = await engine.apply_damage(channel_id, target, amount)
        
        if "error" in result:
             await ctx.send(f"❌ Error: {result['error']}")
             return
             
        embed = discord.Embed(
            title=f"⚔️ Damage Applied",
            color=discord.Color.dark_red()
        )
        embed.add_field(name="Target", value=target, inline=True)
        embed.add_field(name="Damage Taken", value=amount, inline=True)
        embed.add_field(name="Remaining HP", value=f"{result['hp_after']} (Temp: {result['temp_hp_after']})", inline=False)
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"An error occurred: {e}")

@bot.command(name='initiative')
async def roll_initiative(ctx):
    """
    Rolls initiative for the combat.
    Example: !initiative
    """
    try:
        player_id = "Player1"
        channel_id = str(ctx.channel.id)
        
        result = await engine.combat_initiative(channel_id, player_id)
        
        if "error" in result:
             await ctx.send(f"❌ Error: {result['error']}")
             return
             
        embed = discord.Embed(
            title=f"⏱️ Initiative Rolled",
            color=discord.Color.blue()
        )
        embed.add_field(name="Character", value=player_id, inline=True)
        embed.add_field(name="Initiative Score", value=f"**{result['total_initiative']}**", inline=True)
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"An error occurred: {e}")


@bot.command(name='set_engine')
async def set_llm_engine(ctx, target_engine: str):
    """
    Sets the LLM engine for the current channel.
    Example: !set_engine ollama
    """
    valid_engines = ["ollama", "gemini"]
    target_engine = target_engine.lower()
    
    if target_engine not in valid_engines:
        await ctx.send(f"❌ Invalid engine '{target_engine}'. Valid options are: {', '.join(valid_engines)}")
        return
        
    try:
        channel_id = str(ctx.channel.id)
        result = await engine.set_channel_llm_engine(channel_id, target_engine)
        
        embed = discord.Embed(
            title="🧠 LLM Engine Updated",
            color=discord.Color.purple(),
            description=f"The AI inference engine for this channel has been set to **{target_engine}**."
        )
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"An error occurred: {e}")


@bot.command(name='ask')
async def ask_ai(ctx, *, prompt: str):
    """
    Ask the AI engine a question.
    Example: !ask What is a goblin?
    """
    try:
        channel_id = str(ctx.channel.id)
        
        async with ctx.typing():
            response = await engine.generate_ai_response(channel_id, prompt)
            
        if len(response) > 1990:
            response = response[:1990] + "..."
            
        await ctx.send(response)
        
    except Exception as e:
        await ctx.send(f"An error occurred: {e}")

@bot.command(name='new_game')
async def new_game_cmd(ctx):
    """
    Wipes the current channel's game state and starts a new one based on user prompts.
    """
    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel
        
    await ctx.send("⚠️ **WARNING:** This will wipe the current campaign state for this channel. Are you sure? (Type `yes` or `no`)")
    try:
        msg = await bot.wait_for('message', timeout=30.0, check=check)
        if msg.content.lower() not in ['yes', 'y']:
            await ctx.send("New game cancelled.")
            return
            
        await ctx.send("How many players will be in this adventure? (Enter a number)")
        msg = await bot.wait_for('message', timeout=30.0, check=check)
        try:
            num_players = int(msg.content)
            num_players = max(1, min(10, num_players)) # cap between 1 and 10
        except ValueError:
            await ctx.send("❌ Invalid number. Cancelling new game.")
            return
            
        characters_info = []

        for i in range(num_players):
            await ctx.send(f"**--- Player {i+1} ---**\nWhat is your character's Name?")
            name_msg = await bot.wait_for('message', timeout=60.0, check=check)
            
            await ctx.send(f"What is {name_msg.content}'s Race?")
            race_msg = await bot.wait_for('message', timeout=60.0, check=check)
            
            await ctx.send(f"What is {name_msg.content}'s Class?")
            class_msg = await bot.wait_for('message', timeout=60.0, check=check)
            
            await ctx.send(f"What is one defining physical feature for {name_msg.content}?")
            phys_msg = await bot.wait_for('message', timeout=60.0, check=check)
            
            await ctx.send(f"What is one defining personality trait for {name_msg.content}?")
            pers_msg = await bot.wait_for('message', timeout=60.0, check=check)
            
            characters_info.append({
                "name": name_msg.content,
                "race": race_msg.content,
                "class": class_msg.content,
                "physical": phys_msg.content,
                "personality": pers_msg.content
            })
            
        await ctx.send("Finally, what kind of adventure is this? (e.g., 'dungeon delve', 'city heist', 'forest mystery')")
        msg = await bot.wait_for('message', timeout=60.0, check=check)
        adventure_type = msg.content
        
        channel_id = str(ctx.channel.id)
        
        await ctx.send(f"⏳ Generating a new {adventure_type} adventure for {num_players} players... Stand by.")
        
        async with ctx.typing():
            intro = await engine.start_new_game(channel_id, characters_info, adventure_type)
            
        if len(intro) > 1990:
            intro = intro[:1990] + "..."
            
        embed = discord.Embed(
            title="⚔️ A New Adventure Begins! ⚔️",
            color=discord.Color.gold(),
            description=intro
        )
        embed.set_footer(text=f"The realm has been reset with {num_players} heroes. Good luck!")
        await ctx.send(embed=embed)

    except asyncio.TimeoutError:
        await ctx.send("⏳ Time ran out! Cancelling new game.")
    except Exception as e:
        await ctx.send(f"An error occurred: {e}")

if __name__ == "__main__":
    if not TOKEN or TOKEN == "your_token_here_replace_this":
        print("ERROR: Please set your Discord Token in the .env file.")
    else:
        bot.run(TOKEN)
