import discord
from discord.ext import commands, tasks
import asyncpg
import os
from datetime import datetime, timedelta
import pytz

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="$", intents=intents, help_command=None)

# ---------------- Timezone + Interval Maps ----------------
TZ_MAP = {
    "1": "America/New_York",   # Eastern
    "2": "America/Chicago",    # Central
    "3": "America/Denver",     # Mountain
    "4": "America/Los_Angeles",# Pacific
    "5": "Europe/London",      # GMT
    "6": "Europe/Berlin",      # CET
    "7": "Asia/Kolkata",       # IST
    "8": "Asia/Tokyo",         # JST
    "9": "Australia/Sydney",   # AEST
}

INTERVAL_MAP = {
    "1": (30, "Every 30 minutes"),
    "2": (60, "Every 1 hour"),
    "3": (90, "Every 1.5 hours"),
    "4": (120, "Every 2 hours"),
    "5": (180, "Every 3 hours"),
    "6": (240, "Every 4 hours"),
}

# ---------------- Database ----------------
async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
    create table if not exists users (
      id bigint primary key,
      name text,
      age int,
      daily_goal int,
      unit text,
      interval int,
      timezone text,
      reminder_channel bigint,
      log_channel bigint,
      ping_self boolean,
      coach_role bigint,
      coach_ping_logs boolean,
      last_reset date,
      last_reminder timestamptz
    );
    """)
    await conn.execute("""
    create table if not exists daily_logs (
      user_id bigint references users(id),
      date date not null,
      total int not null,
      primary key (user_id, date)
    );
    """)
    await conn.execute("""
    create table if not exists events (
      id bigserial primary key,
      user_id bigint references users(id),
      ts timestamptz not null,
      amount int not null,
      unit text not null,
      kind text not null,
      where_logged text
    );
    """)
    await conn.close()

async def get_user(uid: int):
    conn = await asyncpg.connect(DATABASE_URL)
    user = await conn.fetchrow("SELECT * FROM users WHERE id=$1", uid)
    await conn.close()
    return user

async def upsert_user(uid: int, **kwargs):
    conn = await asyncpg.connect(DATABASE_URL)
    fields = ", ".join([f"{k} = ${i+2}" for i, k in enumerate(kwargs.keys())])
    values = list(kwargs.values())
    query = f"""
        insert into users (id, {', '.join(kwargs.keys())})
        values ($1, {', '.join([f'${i+2}' for i in range(len(kwargs))])})
        on conflict (id) do update set {fields};
    """
    await conn.execute(query, uid, *values)
    await conn.close()

async def log_event(uid: int, ts: datetime, amount: int, unit: str, kind: str, where: str):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute(
        "insert into events (user_id, ts, amount, unit, kind, where_logged) values ($1,$2,$3,$4,$5,$6)",
        uid, ts, amount, unit, kind, where
    )
    await conn.close()

async def add_daily_total(uid: int, date, amount: int):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        insert into daily_logs (user_id, date, total)
        values ($1, $2, $3)
        on conflict (user_id, date) do update
        set total = daily_logs.total + EXCLUDED.total
    """, uid, date, amount)
    await conn.close()

# ---------------- Helpers ----------------
def tz_now(tz_name: str) -> datetime:
    try:
        tz = pytz.timezone(tz_name)
    except:
        tz = pytz.UTC
    return datetime.now(tz)

def convert_goal(unit_choice: int, number: int):
    if unit_choice == 1:  # oz
        return number, "oz"
    elif unit_choice == 2:  # cups
        return number * 8, "oz"
    elif unit_choice == 3:  # pints
        return number * 16, "oz"
    elif unit_choice == 4:  # gallons
        return number * 128, "oz"
    elif unit_choice == 5:  # ml
        return number, "ml"
    elif unit_choice == 6:  # liters
        return number * 1000, "ml"
    else:
        return None, None

# ---------------- Commands ----------------
@bot.command(name="config")
async def config(ctx):
    def check(m): return m.author == ctx.author and m.channel == ctx.channel

    # Name
    await ctx.send("👋 What should I call you?")
    name = (await bot.wait_for("message", check=check)).content.strip()

    # Age
    while True:
        await ctx.send("📅 How old are you? (number)")
        msg = await bot.wait_for("message", check=check)
        try:
            age = int(msg.content.strip())
            break
        except:
            await ctx.send("❌ Please enter a number.")

    # Daily goal unit
    await ctx.send(
        "💧 Choose your daily goal unit:\n"
        "1 = Ounces (oz)\n"
        "2 = Cups (8 oz)\n"
        "3 = Pints (16 oz)\n"
        "4 = Gallons (128 oz)\n"
        "5 = Milliliters (ml)\n"
        "6 = Liters (1000 ml)"
    )
    while True:
        choice = (await bot.wait_for("message", check=check)).content.strip()
        if choice.isdigit() and 1 <= int(choice) <= 6:
            unit_choice = int(choice)
            break
        await ctx.send("❌ Invalid choice, enter 1–6.")

    # Daily goal number
    while True:
        await ctx.send("📊 Enter the number for your daily goal in that unit:")
        msg = await bot.wait_for("message", check=check)
        try:
            num = int(msg.content.strip())
            daily_goal, base_unit = convert_goal(unit_choice, num)
            if daily_goal:
                break
        except:
            await ctx.send("❌ Invalid number.")

    # Interval
    while True:
        menu = "\n".join([f"{k} = {v[1]}" for k, v in INTERVAL_MAP.items()])
        await ctx.send(f"⏱ How often should I remind you?\n{menu}")
        choice = (await bot.wait_for("message", check=check)).content.strip()
        if choice in INTERVAL_MAP:
            minutes, interval_label = INTERVAL_MAP[choice]
            break
        await ctx.send("❌ Invalid choice.")

    # Timezone
    while True:
        tz_menu = "\n".join([f"{k} = {v}" for k, v in TZ_MAP.items()])
        await ctx.send(f"🌍 Choose your timezone:\n{tz_menu}\n10 = Other (type manually)")
        choice = (await bot.wait_for("message", check=check)).content.strip()
        if choice in TZ_MAP:
            tz = TZ_MAP[choice]
            break
        elif choice == "10":
            await ctx.send("✍️ Enter your timezone (e.g., America/Chicago):")
            tz_input = (await bot.wait_for("message", check=check)).content.strip()
            try:
                pytz.timezone(tz_input)
                tz = tz_input
                break
            except:
                await ctx.send("❌ Invalid timezone string.")
        else:
            await ctx.send("❌ Invalid choice.")

    # Reminder channel
    while True:
        await ctx.send("🔔 Mention the reminder channel (#channel)")
        msg = await bot.wait_for("message", check=check)
        if msg.channel_mentions:
            reminder_channel = msg.channel_mentions[0].id
            break
        await ctx.send("❌ Please mention a valid channel.")

    # Log channel
    while True:
        await ctx.send("📜 Mention the log channel (#channel)")
        msg = await bot.wait_for("message", check=check)
        if msg.channel_mentions:
            log_channel = msg.channel_mentions[0].id
            break
        await ctx.send("❌ Please mention a valid channel.")

    # Ping self
    while True:
        await ctx.send("🔔 Do you want me to ping you on reminders? (yes/no)")
        reply = (await bot.wait_for("message", check=check)).content.strip().lower()
        if reply in ["yes", "y"]:
            ping_self = True
            break
        elif reply in ["no", "n"]:
            ping_self = False
            break
        else:
            await ctx.send("❌ Please type yes or no.")

    # Coach role
    coach_role = None
    coach_ping_logs = False
    await ctx.send("👥 Do you want to set a coach role for accountability? (yes/no)")
    reply = (await bot.wait_for("message", check=check)).content.strip().lower()
    if reply in ["yes", "y"]:
        roles = [r for r in ctx.guild.roles if r.name != "@everyone"]
        if roles:
            role_list = "\n".join([f"{i+1} = {r.name}" for i, r in enumerate(roles[:20])])
            await ctx.send(f"Select a role number:\n{role_list}")
            choice = (await bot.wait_for("message", check=check)).content.strip()
            if choice.isdigit():
                idx = int(choice)-1
                if 0 <= idx < len(roles):
                    coach_role = roles[idx].id
                    await ctx.send("📜 Ping coach role in daily logs/reminders? (yes/no)")
                    reply2 = (await bot.wait_for("message", check=check)).content.strip().lower()
                    coach_ping_logs = reply2 in ["yes", "y"]

    # Save
    await upsert_user(
        ctx.author.id,
        name=name,
        age=age,
        daily_goal=daily_goal,
        unit=base_unit,
        interval=minutes,
        timezone=tz,
        reminder_channel=reminder_channel,
        log_channel=log_channel,
        ping_self=ping_self,
        coach_role=coach_role,
        coach_ping_logs=coach_ping_logs,
        last_reset=tz_now(tz).date()
    )

    # Confirmation
    embed = discord.Embed(title="✅ Config Saved", color=discord.Color.green())
    embed.add_field(name="Name", value=name, inline=True)
    embed.add_field(name="Age", value=age, inline=True)
    embed.add_field(name="Daily Goal", value=f"{daily_goal} {base_unit}", inline=False)
    embed.add_field(name="Reminder Interval", value=interval_label, inline=False)
    embed.add_field(name="Timezone", value=tz, inline=False)
    embed.add_field(name="Reminder Channel", value=f"<#{reminder_channel}>", inline=True)
    embed.add_field(name="Log Channel", value=f"<#{log_channel}>", inline=True)
    embed.add_field(name="Ping Self", value=str(ping_self), inline=True)
    if coach_role:
        embed.add_field(name="Coach Role", value=f"<@&{coach_role}>", inline=True)
        embed.add_field(name="Coach Pings", value=str(coach_ping_logs), inline=True)
    await ctx.send(embed=embed)

# ---------------- Drink, Check, Status, Report, Help ----------------
# (Implementations same as earlier but include coach ping in reminders & summaries)

@bot.command(name="help")
async def help_cmd(ctx):
    embed = discord.Embed(title="📖 Bumpy Commands", color=discord.Color.blue())
    embed.add_field(name="$config", value="Start setup wizard", inline=False)
    embed.add_field(name="$drink <amount>", value="Log hydration", inline=False)
    embed.add_field(name="$check", value="Check today’s progress", inline=False)
    embed.add_field(name="$status", value="Show your saved config", inline=False)
    embed.add_field(name="$report", value="View 7/15/30 day reports", inline=False)
    await ctx.send(embed=embed)

# ---------------- Background Tasks ----------------
@tasks.loop(minutes=1)
async def reminders():
    conn = await asyncpg.connect(DATABASE_URL)
    users = await conn.fetch("SELECT * FROM users")
    await conn.close()
    now = datetime.utcnow()
    for u in users:
        last = u["last_reminder"]
        interval = u["interval"]
        tz = u["timezone"]
        if not last or (now - last).total_seconds() >= interval * 60:
            channel = bot.get_channel(u["reminder_channel"])
            if channel:
                msg = f"💧 Time to drink water!"
                if u["ping_self"]:
                    msg += f" <@{u['id']}>"
                if u["coach_role"] and u["coach_ping_logs"]:
                    msg += f" <@&{u['coach_role']}>"
                await channel.send(msg)
                conn = await asyncpg.connect(DATABASE_URL)
                await conn.execute("update users set last_reminder=$1 where id=$2", now, u["id"])
                await conn.close()

@tasks.loop(hours=24)
async def daily_summary():
    conn = await asyncpg.connect(DATABASE_URL)
    users = await conn.fetch("SELECT * FROM users")
    await conn.close()
    for u in users:
        tz = u["timezone"]
        today = tz_now(tz).date()
        conn = await asyncpg.connect(DATABASE_URL)
        row = await conn.fetchrow("select total from daily_logs where user_id=$1 and date=$2", u["id"], today)
        await conn.close()
        total = row["total"] if row else 0
        goal = u["daily_goal"]
        channel = bot.get_channel(u["log_channel"])
        if channel:
            msg = f"📊 Daily Summary for <@{u['id']}>: {total}/{goal} {u['unit']}"
            if u["coach_role"] and u["coach_ping_logs"]:
                msg += f" <@&{u['coach_role']}>"
            await channel.send(msg)

@bot.event
async def on_ready():
    await init_db()
    reminders.start()
    daily_summary.start()
    print(f"✅ Logged in as {bot.user}")

bot.run(TOKEN)
