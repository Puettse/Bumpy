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

bot = commands.Bot(
    command_prefix="$",
    intents=intents,
    help_command=None,
    allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False)
)

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
      coach_ping_reminders boolean,
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
    """Convert user input to consistent oz/ml base unit"""
    if unit_choice == 1:  # oz
        return number, "oz"
    elif unit_choice == 2:  # cups (8 oz)
        return number * 8, "oz"
    elif unit_choice == 3:  # pints (16 oz)
        return number * 16, "oz"
    elif unit_choice == 4:  # gallons (128 oz)
        return number * 128, "oz"
    elif unit_choice == 5:  # ml
        return number, "ml"
    elif unit_choice == 6:  # liters (1000 ml)
        return number * 1000, "ml"
    else:
        return None, None

# ---------------- Commands ----------------
@bot.command(name="config")
async def config(ctx):
    def check(m): return m.author == ctx.author and m.channel == ctx.channel

    await ctx.send("👋 What should I call you?")
    name = (await bot.wait_for("message", check=check)).content.strip()

    await ctx.send("📅 How old are you?")
    try:
        age = int((await bot.wait_for("message", check=check)).content.strip())
    except:
        return await ctx.send("❌ Invalid number for age.")

    await ctx.send(
        "💧 Choose the unit for your daily goal:\n"
        "1 = Ounces (oz)\n2 = Cups (8 oz)\n3 = Pints (16 oz)\n"
        "4 = Gallons (128 oz)\n5 = Milliliters (ml)\n6 = Liters (1000 ml)"
    )
    try:
        unit_choice = int((await bot.wait_for("message", check=check)).content.strip())
    except:
        return await ctx.send("❌ Invalid choice.")

    await ctx.send("📊 Enter the number for your daily goal in that unit:")
    try:
        num = int((await bot.wait_for("message", check=check)).content.strip())
    except:
        return await ctx.send("❌ Invalid number.")

    daily_goal, base_unit = convert_goal(unit_choice, num)
    if not daily_goal:
        return await ctx.send("❌ Invalid selection.")

    await ctx.send("⏱ How often should I remind you? (`30 min` or `1 hour`)")
    parts = (await bot.wait_for("message", check=check)).content.split()
    try:
        num_int = int(parts[0]); per = parts[1].lower()
        minutes = num_int * 60 if per.startswith("hour") else num_int
    except:
        return await ctx.send("❌ Invalid format.")

    await ctx.send("🌍 Your timezone? (e.g., `America/Chicago`)")
    tz = (await bot.wait_for("message", check=check)).content
    try:
        pytz.timezone(tz)
    except:
        return await ctx.send("❌ Invalid timezone.")

    await ctx.send("🔔 Mention reminder channel (#channel)")
    reminder_msg = await bot.wait_for("message", check=check)
    reminder_channel = reminder_msg.channel_mentions[0].id if reminder_msg.channel_mentions else None

    await ctx.send("📜 Mention log channel (#channel)")
    log_msg = await bot.wait_for("message", check=check)
    log_channel = log_msg.channel_mentions[0].id if log_msg.channel_mentions else None

    await ctx.send("Ping yourself on reminders? (yes/no)")
    ping_self = (await bot.wait_for("message", check=check)).content.lower() in ["yes","y"]

    await ctx.send("👥 Enable coach pings? (yes/no)")
    coach_enable = (await bot.wait_for("message", check=check)).content.strip().lower() in ["yes","y","true","1"]

    coach_role_id = None
    coach_ping_logs = False
    coach_ping_reminders = False

    if coach_enable:
        roles = [r for r in ctx.guild.roles if not r.is_default()]
        if roles:
            show = roles[:20]
            listing = "\n".join([f"{i+1}. {r.mention}" for i, r in enumerate(show)])
            await ctx.send(f"Select a coach role (type the number):\n{listing}")
            try:
                choice = int((await bot.wait_for("message", check=check)).content.strip())
                coach_role_id = show[choice-1].id
            except Exception:
                await ctx.send("❌ Invalid selection. Skipping coach role.")
                coach_role_id = None
            if coach_role_id:
                await ctx.send("📜 Ping coach role in **daily logs**? (yes/no)")
                coach_ping_logs = (await bot.wait_for("message", check=check)).content.strip().lower() in ["yes","y"]
                await ctx.send("🔔 Ping coach role in **reminders**? (yes/no)")
                coach_ping_reminders = (await bot.wait_for("message", check=check)).content.strip().lower() in ["yes","y"]

    await upsert_user(ctx.author.id,
        name=name,
        age=age,
        daily_goal=daily_goal,
        unit=base_unit,
        interval=minutes,
        timezone=tz,
        reminder_channel=reminder_channel,
        log_channel=log_channel,
        ping_self=ping_self,
        coach_role=coach_role_id,
        coach_ping_logs=coach_ping_logs,
        coach_ping_reminders=coach_ping_reminders,
        last_reset=tz_now(tz).date()
    )
    await ctx.send(f"✅ Config saved! Daily goal = {daily_goal} {base_unit}")

@bot.command(name="drink")
async def drink(ctx, amount: int):
    user = await get_user(ctx.author.id)
    if not user:
        return await ctx.send("Run `$config` first.")
    await log_event(ctx.author.id, tz_now(user["timezone"]), amount, user["unit"], "manual", str(ctx.channel.id))
    await add_daily_total(ctx.author.id, tz_now(user["timezone"]).date(), amount)
    await ctx.send(f"✅ Logged {amount} {user['unit']}")

@bot.command(name="check")
async def check(ctx):
    user = await get_user(ctx.author.id)
    if not user:
        return await ctx.send("Run `$config` first.")
    conn = await asyncpg.connect(DATABASE_URL)
    row = await conn.fetchrow("SELECT total FROM daily_logs WHERE user_id=$1 AND date=$2", ctx.author.id, tz_now(user["timezone"]).date())
    await conn.close()
    total = row["total"] if row else 0
    await ctx.send(f"💧 Progress: {total}/{user['daily_goal']} {user['unit']} today.")

@bot.command(name="report")
async def report(ctx, days: int = 7):
    user = await get_user(ctx.author.id)
    if not user:
        return await ctx.send("Run `$config` first.")
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("SELECT date,total FROM daily_logs WHERE user_id=$1 ORDER BY date DESC LIMIT $2", ctx.author.id, days)
    await conn.close()
    if not rows:
        return await ctx.send("No logs yet.")
    avg = sum(r["total"] for r in rows)//len(rows)
    best = max(rows, key=lambda r: r["total"])
    worst = min(rows, key=lambda r: r["total"])
    msg = f"📊 {days}-day report:\nAvg: {avg} {user['unit']}\nBest: {best['total']} on {best['date']}\nWorst: {worst['total']} on {worst['date']}"
    await ctx.send(msg)

@bot.command(name="status")
async def status(ctx):
    user = await get_user(ctx.author.id)
    if not user:
        return await ctx.send("Run `$config` first.")
    embed = discord.Embed(title=f"📊 {user['name']}'s Status", color=discord.Color.blurple())
    embed.add_field(name="Age", value=user["age"], inline=True)
    embed.add_field(name="Daily Goal", value=f"{user['daily_goal']} {user['unit']}", inline=False)
    embed.add_field(name="Interval", value=f"{user['interval']} minutes", inline=False)
    embed.add_field(name="Timezone", value=user['timezone'], inline=True)
    embed.add_field(name="Reminder Channel", value=f"<#{user['reminder_channel']}>" if user['reminder_channel'] else "None", inline=True)
    embed.add_field(name="Log Channel", value=f"<#{user['log_channel']}>" if user['log_channel'] else "None", inline=True)
    embed.add_field(name="Ping Self", value=str(user['ping_self']), inline=True)
    embed.add_field(name="Coach Role", value=f"<@&{user['coach_role']}>" if user['coach_role'] else "None", inline=True)
    embed.add_field(name="Coach Pings Logs", value=str(user['coach_ping_logs']), inline=True)
    embed.add_field(name="Coach Pings Reminders", value=str(user['coach_ping_reminders']), inline=True)
    await ctx.send(embed=embed)

@bot.command(name="help")
async def help_cmd(ctx):
    embed = discord.Embed(title="💧 Bumpy Help", color=discord.Color.green())
    embed.add_field(name="$config", value="Setup wizard", inline=False)
    embed.add_field(name="$drink <amount>", value="Log intake", inline=False)
    embed.add_field(name="$check", value="Check today’s progress", inline=False)
    embed.add_field(name="$status", value="Show your config", inline=False)
    embed.add_field(name="$report <7|15|30>", value="Hydration reports", inline=False)
    await ctx.send(embed=embed)

# ---------------- Loops ----------------
@tasks.loop(minutes=1)
async def reminder_loop():
    conn = await asyncpg.connect(DATABASE_URL)
    users = await conn.fetch("SELECT * FROM users")
    now = datetime.utcnow()

    for user in users:
        tz = user["timezone"] or "UTC"
        local_now = tz_now(tz)
        last = user["last_reminder"]

        waking_hours = 16
        reminders_per_day = max(1, waking_hours * 60 // user["interval"])
        increment = user["daily_goal"] // reminders_per_day

        if not last or (now - last).total_seconds() >= user["interval"] * 60:
            channel_id = user["reminder_channel"]
            if channel_id:
                channel = bot.get_channel(channel_id)
                if channel:
                    mention = f"<@{user['id']}>" if user["ping_self"] else ""
                    coach_mention = f" <@&{user['coach_role']}>" if user["coach_role"] and user["coach_ping_reminders"] else ""
                    await channel.send(f"💧 {mention}{coach_mention} Time to drink ~ **{increment} {user['unit']}**!")
                    await conn.execute("UPDATE users SET last_reminder=$1 WHERE id=$2", now, user["id"])

    await conn.close()

@tasks.loop(minutes=5)
async def reset_loop():
    conn = await asyncpg.connect(DATABASE_URL)
    users = await conn.fetch("SELECT * FROM users")

    for user in users:
        tz = user["timezone"] or "UTC"
        local_now = tz_now(tz)

        if user["last_reset"] != local_now.date():
            row = await conn.fetchrow(
                "SELECT total FROM daily_logs WHERE user_id=$1 AND date=$2",
                user["id"], local_now.date() - timedelta(days=1)
            )
            total = row["total"] if row else 0
            channel_id = user["log_channel"]
            if channel_id:
                channel = bot.get_channel(channel_id)
                if channel:
                    percent = (total / user["daily_goal"]) * 100 if user["daily_goal"] else 0
                    emoji = "🎯" if percent >= 100 else "💤"
                    coach_mention = f" <@&{user['coach_role']}>" if user["coach_role"] and user["coach_ping_logs"] else ""
                    msg = (
                        f"📅 Daily Summary for {local_now.date() - timedelta(days=1)}\n"
                        f"💧 You drank {total}/{user['daily_goal']} {user['unit']} ({percent:.1f}%) {emoji}{coach_mention}"
                    )
                    await channel.send(msg)

            await conn.execute("UPDATE users SET last_reset=$1 WHERE id=$2", local_now.date(), user["id"])

    await conn.close()

@bot.event
async def on_ready():
    await init_db()
    reminder_loop.start()
    reset_loop.start()
    print(f"Bumpy online as {bot.user}")

bot.run(TOKEN)