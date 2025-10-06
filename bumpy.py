import discord
from discord.ext import commands, tasks
import asyncpg
import os
from datetime import datetime
import pytz

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="$", intents=intents, help_command=None)

# ---------------- Database ----------------
async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
    create table if not exists users (
      id bigint primary key,
      increment int,
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

# ---------------- Commands ----------------
@bot.command(name="config")
async def config(ctx):
    def check(m): return m.author == ctx.author and m.channel == ctx.channel

    # Amount
    await ctx.send("💧 How much water per interval? (e.g., `8`)")
    msg = await bot.wait_for("message", check=check)
    try:
        amount = int(msg.content)
    except:
        return await ctx.send("❌ Invalid number.")

    # Unit
    await ctx.send("⚖️ Unit? (`oz` or `ml`)")
    unit = (await bot.wait_for("message", check=check)).content.lower()
    if unit not in ["oz", "ml"]:
        return await ctx.send("❌ Invalid unit.")

    # Interval
    await ctx.send("⏱ How often? (`30 min` or `1 hour`)")
    parts = (await bot.wait_for("message", check=check)).content.split()
    try:
        num = int(parts[0]); per = parts[1].lower()
        minutes = num * 60 if per.startswith("hour") else num
    except:
        return await ctx.send("❌ Invalid format.")

    # Timezone
    await ctx.send("🌍 Your timezone? (e.g., `America/Chicago`)")
    tz = (await bot.wait_for("message", check=check)).content
    try:
        pytz.timezone(tz)
    except:
        return await ctx.send("❌ Invalid timezone.")

    # Channels
    await ctx.send("🔔 Mention reminder channel (#channel)")
    reminder_msg = await bot.wait_for("message", check=check)
    reminder_channel = reminder_msg.channel_mentions[0].id if reminder_msg.channel_mentions else None
    await ctx.send("📜 Mention log channel (#channel)")
    log_msg = await bot.wait_for("message", check=check)
    log_channel = log_msg.channel_mentions[0].id if log_msg.channel_mentions else None

    # Ping self
    await ctx.send("Ping yourself on reminders? (yes/no)")
    ping_self = (await bot.wait_for("message", check=check)).content.lower() in ["yes","y"]

    # Coach role
    await ctx.send("Enable coach pings? (yes/no)")
    coach_enable = (await bot.wait_for("message", check=check)).content.lower() in ["yes","y"]
    coach_role = None
    coach_ping_logs = False
    if coach_enable:
        roles = [r for r in ctx.guild.roles if not r.is_default()]
        if roles:
            rlist = "\n".join([f"{i+1}. {r.mention}" for i, r in enumerate(roles)])
            await ctx.send(f"👥 Select coach role (type number):\n{rlist}")
            rchoice = int((await bot.wait_for("message", check=check)).content.strip())
            coach_role = roles[rchoice-1].id
            await ctx.send("Ping coach on logs? (yes/no)")
            coach_ping_logs = (await bot.wait_for("message", check=check)).content.lower() in ["yes","y"]

    # Save
    await upsert_user(ctx.author.id,
        increment=amount,
        unit=unit,
        interval=minutes,
        timezone=tz,
        reminder_channel=reminder_channel,
        log_channel=log_channel,
        ping_self=ping_self,
        coach_role=coach_role,
        coach_ping_logs=coach_ping_logs,
        last_reset=tz_now(tz).date()
    )
    await ctx.send("✅ Configuration saved!")

@bot.command(name="drink")
async def drink(ctx, amount: int):
    user = await get_user(ctx.author.id)
    if not user:
        return await ctx.send("Run `$config` first.")
    now = tz_now(user["timezone"])
    await log_event(ctx.author.id, now, amount, user["unit"], "manual", f"<#{ctx.channel.id}>")
    await add_daily_total(ctx.author.id, now.date(), amount)
    await ctx.send(f"💧 Logged {amount} {user['unit']} for {ctx.author.mention}")

@bot.command(name="check")
async def check(ctx):
    user = await get_user(ctx.author.id)
    if not user:
        return await ctx.send("Run `$config` first.")
    conn = await asyncpg.connect(DATABASE_URL)
    today_total = await conn.fetchval(
        "select coalesce(total,0) from daily_logs where user_id=$1 and date=$2",
        ctx.author.id, tz_now(user["timezone"]).date()
    )
    await conn.close()
    await ctx.send(f"{ctx.author.mention} Today: {today_total} {user['unit']} (Goal: {user['increment']} every {user['interval']}m)")

@bot.command(name="report")
async def report(ctx, days: int = 7):
    if days not in [7,15,30]:
        return await ctx.send("Use `$report 7`, `$report 15`, or `$report 30`.")
    user = await get_user(ctx.author.id)
    if not user:
        return await ctx.send("Run `$config` first.")
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("""
        select date, total from daily_logs
        where user_id=$1
        order by date desc
        limit $2
    """, ctx.author.id, days)
    await conn.close()
    if not rows:
        return await ctx.send("No history yet.")
    totals = [r["total"] for r in rows]
    avg = sum(totals)/len(totals)
    best = max(rows, key=lambda r: r["total"])
    worst = min(rows, key=lambda r: r["total"])
    embed = discord.Embed(title=f"{ctx.author.display_name}'s {days}-day report", color=discord.Color.blue())
    embed.add_field(name="Average", value=f"{avg:.1f} {user['unit']}/day", inline=False)
    embed.add_field(name="Best", value=f"{best['date']}: {best['total']} {user['unit']}", inline=True)
    embed.add_field(name="Worst", value=f"{worst['date']}: {worst['total']} {user['unit']}", inline=True)
    breakdown = "\n".join([f"{r['date']}: {r['total']} {user['unit']}" for r in rows])
    embed.add_field(name="Breakdown", value=breakdown, inline=False)
    await ctx.send(embed=embed)

@bot.command(name="status")
async def status(ctx):
    user = await get_user(ctx.author.id)
    if not user:
        return await ctx.send("Run `$config` first.")
    embed = discord.Embed(title=f"📊 {ctx.author.display_name}'s Status", color=discord.Color.blurple())
    embed.add_field(name="Goal", value=f"{user['increment']} {user['unit']} every {user['interval']}m", inline=False)
    embed.add_field(name="Timezone", value=user['timezone'], inline=True)
    embed.add_field(name="Reminder Channel", value=f"<#{user['reminder_channel']}>" if user['reminder_channel'] else "None", inline=True)
    embed.add_field(name="Log Channel", value=f"<#{user['log_channel']}>" if user['log_channel'] else "None", inline=True)
    embed.add_field(name="Ping Self", value=str(user['ping_self']), inline=True)
    embed.add_field(name="Coach Role", value=f"<@&{user['coach_role']}>" if user['coach_role'] else "None", inline=True)
    embed.add_field(name="Coach Ping Logs", value=str(user['coach_ping_logs']), inline=True)
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

# ---------------- Reminder Loop ----------------
@tasks.loop(minutes=1)
async def reminder_loop():
    conn = await asyncpg.connect(DATABASE_URL)
    users = await conn.fetch("select * from users")
    now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)

    for u in users:
        if not u["increment"] or not u["interval"]:
            continue
        tz = u["timezone"] or "UTC"
        now = tz_now(tz)
        today = now.date()

        # Reset at midnight
        if u["last_reset"] != today:
            total = await conn.fetchval("select coalesce(total,0) from daily_logs where user_id=$1 and date=$2",
                                        u["id"], u["last_reset"])
            if u["log_channel"]:
                ch = bot.get_channel(u["log_channel"])
                if ch:
                    coach_ping = f" <@&{u['coach_role']}>" if u["coach_ping_logs"] and u["coach_role"] else ""
                    await ch.send(f"🗓️ Daily summary for <@{u['id']}> {u['last_reset']}: {total} {u['unit']}{coach_ping}")
            await conn.execute("update users set last_reset=$1 where id=$2", today, u["id"])

        # Reminder check
        remind = False
        if u["last_reminder"] is None:
            remind = True
        else:
            delta = now_utc - u["last_reminder"]
            if delta.total_seconds() >= u["interval"]*60:
                remind = True

        if remind:
            msg = f"⏰ Time to drink {u['increment']} {u['unit']}!"
            if u["ping_self"]:
                msg = f"<@{u['id']}> " + msg
            sent_where = "dm"
            if u["reminder_channel"]:
                ch = bot.get_channel(u["reminder_channel"])
                if ch:
                    await ch.send(msg)
                    sent_where = f"<#{u['reminder_channel']}>"
            else:
                user = await bot.fetch_user(u["id"])
                if user:
                    await user.send(msg)
            await conn.execute("update users set last_reminder=$1 where id=$2", now_utc, u["id"])
            await add_daily_total(u["id"], today, u["increment"])
            await log_event(u["id"], now, u["increment"], u["unit"], "reminder", sent_where)

            if u["log_channel"]:
                log_ch = bot.get_channel(u["log_channel"])
                if log_ch:
                    coach_ping = f" <@&{u['coach_role']}>" if u["coach_ping_logs"] and u["coach_role"] else ""
                    await log_ch.send(f"📥 +{u['increment']} {u['unit']} for <@{u['id']}> at {now.isoformat()}{coach_ping}")

    await conn.close()

@bot.event
async def on_ready():
    await init_db()
    reminder_loop.start()
    print(f"Bumpy online as {bot.user}")

bot.run(TOKEN)