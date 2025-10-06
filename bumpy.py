import discord
from discord.ext import commands, tasks
import asyncio
import json
import os
from datetime import datetime
import pytz  # pip install pytz

TOKEN = os.getenv("DISCORD_TOKEN")
CONFIG_FILE = "users.json"

# --- Load / Save Users ---
def load_users():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}

def save_users(users):
    with open(CONFIG_FILE, "w") as f:
        json.dump(users, f, indent=4)

def get_or_create_user(user_id):
    users = load_users()
    if str(user_id) not in users:
        users[str(user_id)] = {
            "increment": None,
            "unit": "oz",
            "interval": None,
            "progress": 0,
            "timezone": "UTC",
            "last_reset": str(datetime.utcnow().date()),
            "last_reminder": None,
            "reminder_channel": None,  # NEW
            "log_channel": None        # NEW
        }
        save_users(users)
    return users

# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="$", intents=intents)

# --- Commands ---

@bot.command(name="setgoal")
async def set_goal(ctx, amount: int, unit: str, every: str, interval: int, per: str = "hour"):
    users = get_or_create_user(ctx.author.id)

    if unit.lower() not in ["oz", "ml"]:
        await ctx.send("Please choose unit as 'oz' or 'ml'.")
        return

    minutes = interval * 60 if per.lower().startswith("hour") else interval

    users[str(ctx.author.id)]["increment"] = amount
    users[str(ctx.author.id)]["unit"] = unit.lower()
    users[str(ctx.author.id)]["interval"] = minutes
    users[str(ctx.author.id)]["progress"] = 0
    users[str(ctx.author.id)]["last_reminder"] = None
    save_users(users)

    # Log if a log channel is set
    log_channel_id = users[str(ctx.author.id)].get("log_channel")
    if log_channel_id:
        log_channel = bot.get_channel(log_channel_id)
        if log_channel:
            await log_channel.send(f"{ctx.author.mention} set a hydration goal: {amount} {unit} every {interval} {per}.")

    await ctx.send(f"{ctx.author.mention} Goal set: {amount} {unit} every {interval} {per}.")

@bot.command(name="check")
async def check_progress(ctx):
    users = get_or_create_user(ctx.author.id)
    user = users[str(ctx.author.id)]
    inc = user["increment"]
    unit = user["unit"]
    interval = user["interval"]
    prog = user["progress"]

    if inc is None or interval is None:
        await ctx.send("You haven’t set a goal yet. Use `$setgoal` first.")
        return

    await ctx.send(
        f"{ctx.author.mention} Your goal: {inc} {unit} every {interval} minutes.\n"
        f"Progress so far today: {prog} {unit}."
    )

@bot.command(name="modifygoal")
async def modify_goal(ctx, amount: int, unit: str, every: str, interval: int, per: str = "hour"):
    users = get_or_create_user(ctx.author.id)
    if unit.lower() not in ["oz", "ml"]:
        await ctx.send("Please choose unit as 'oz' or 'ml'.")
        return

    minutes = interval * 60 if per.lower().startswith("hour") else interval
    users[str(ctx.author.id)]["increment"] = amount
    users[str(ctx.author.id)]["unit"] = unit.lower()
    users[str(ctx.author.id)]["interval"] = minutes
    save_users(users)

    await ctx.send(f"{ctx.author.mention} Goal updated: {amount} {unit} every {interval} {per}.")

@bot.command(name="deletegoal")
async def delete_goal(ctx):
    users = load_users()
    if str(ctx.author.id) in users:
        del users[str(ctx.author.id)]
        save_users(users)
        await ctx.send(f"{ctx.author.mention} Your hydration goal has been deleted.")
    else:
        await ctx.send("No goal found to delete.")

@bot.command(name="timezone")
async def set_timezone(ctx, tz: str):
    try:
        pytz.timezone(tz)
    except pytz.UnknownTimeZoneError:
        await ctx.send("Invalid timezone. Example: `America/Chicago`")
        return

    users = get_or_create_user(ctx.author.id)
    users[str(ctx.author.id)]["timezone"] = tz
    save_users(users)
    await ctx.send(f"{ctx.author.mention} Timezone set to {tz}.")

@bot.command(name="unit")
async def set_unit(ctx, unit: str):
    if unit.lower() not in ["oz", "ml"]:
        await ctx.send("Unit must be 'oz' or 'ml'.")
        return
    users = get_or_create_user(ctx.author.id)
    users[str(ctx.author.id)]["unit"] = unit.lower()
    save_users(users)
    await ctx.send(f"{ctx.author.mention} Unit set to {unit.lower()}.")

# --- NEW: Set reminder channel ---
@bot.command(name="reminderchannel")
async def set_reminder_channel(ctx, channel_id: int):
    users = get_or_create_user(ctx.author.id)
    users[str(ctx.author.id)]["reminder_channel"] = channel_id
    save_users(users)
    await ctx.send(f"{ctx.author.mention} Reminder channel set to <#{channel_id}>.")

# --- NEW: Set log channel ---
@bot.command(name="logchannel")
async def set_log_channel(ctx, channel_id: int):
    users = get_or_create_user(ctx.author.id)
    users[str(ctx.author.id)]["log_channel"] = channel_id
    save_users(users)
    await ctx.send(f"{ctx.author.mention} Log channel set to <#{channel_id}>.")

# --- Reminder Loop ---
@tasks.loop(minutes=1)
async def reminder_loop():
    users = load_users()
    for uid, data in users.items():
        if not data["increment"] or not data["interval"]:
            continue

        tz = pytz.timezone(data.get("timezone", "UTC"))
        now = datetime.now(tz)

        # reset daily
        if data.get("last_reset") != str(now.date()):
            data["progress"] = 0
            data["last_reset"] = str(now.date())
            data["last_reminder"] = None

        # check interval
        last_reminder = data.get("last_reminder")
        if last_reminder:
            last_dt = datetime.fromisoformat(last_reminder)
            if (now - last_dt).total_seconds() < data["interval"] * 60:
                continue

        # where to send?
        inc = data["increment"]
        unit = data["unit"]
        total = data["progress"]

        reminder_msg = (
            f"⏰ Time to drink {inc} {unit}! "
            f"Progress today: {total} {unit}."
        )

        if data.get("reminder_channel"):
            channel = bot.get_channel(data["reminder_channel"])
            if channel:
                await channel.send(f"<@{uid}> {reminder_msg}")
        else:
            # fallback: DM
            user = await bot.fetch_user(int(uid))
            if user:
                await user.send(reminder_msg)

        data["progress"] += inc
        data["last_reminder"] = now.isoformat()

    save_users(users)

@bot.event
async def on_ready():
    print(f"Hydration Bumpy is online as {bot.user}")
    reminder_loop.start()

bot.run(TOKEN)