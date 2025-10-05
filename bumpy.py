import discord
from discord.ext import commands
import asyncio
import json
import os

TOKEN = os.getenv("DISCORD_TOKEN")  # Railway variable
CONFIG_FILE = "config.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {"bump_channel_id": None, "log_channel_id": None}

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

config = load_config()
bump_channel_id = config.get("bump_channel_id")
log_channel_id = config.get("log_channel_id")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="$", intents=intents)

bump_task = None

async def log_message(msg: str):
    if log_channel_id:
        channel = bot.get_channel(log_channel_id)
        if channel:
            await channel.send(msg)
    print(msg)

async def bump_loop():
    await bot.wait_until_ready()
    while True:
        if bump_channel_id:
            channel = bot.get_channel(bump_channel_id)
            if channel:
                await channel.send("/bump")
                await log_message(f"Bumpy bumped in <#{bump_channel_id}>")
        await asyncio.sleep(2 * 60 * 60 + 60)  # 2h 1m

@bot.event
async def on_ready():
    print(f"Bumpy is online as {bot.user}!")

@bot.command(name="start")
async def start_bump(ctx):
    global bump_task
    if bump_task is None or bump_task.done():
        bump_task = asyncio.create_task(bump_loop())
        await ctx.send("Bumpy started bumping.")
    else:
        await ctx.send("Bumpy is already bumping.")

@bot.command(name="end")
async def end_bump(ctx):
    global bump_task
    if bump_task and not bump_task.done():
        bump_task.cancel()
        bump_task = None
        await ctx.send("Bumpy stopped bumping.")
    else:
        await ctx.send("No bump loop is running.")

@bot.command(name="assign")
async def assign_channel(ctx, *, arg):
    global bump_channel_id
    try:
        bump_channel_id = int(arg.replace("channel:", "").strip())
        config["bump_channel_id"] = bump_channel_id
        save_config(config)
        await ctx.send(f"Bump channel set to <#{bump_channel_id}> and saved.")
    except ValueError:
        await ctx.send("Invalid channel ID.")

@bot.command(name="log")
async def log_channel(ctx, *, arg):
    global log_channel_id
    try:
        log_channel_id = int(arg.replace("channel:", "").strip())
        config["log_channel_id"] = log_channel_id
        save_config(config)
        await ctx.send(f"Log channel set to <#{log_channel_id}> and saved.")
    except ValueError:
        await ctx.send("Invalid channel ID.")

@bot.command(name="restart")
async def restart_bump(ctx):
    await end_bump(ctx)
    await start_bump(ctx)

bot.run(TOKEN)