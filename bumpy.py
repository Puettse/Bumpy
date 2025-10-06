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
      user_id bigint references users(id) on delete cascade,
      date date not null,
      total int not null,
      primary key (user_id, date)
    );
    """)
    await conn.execute("""
    create table if not exists events (
      id bigserial primary key,
      user_id bigint references users(id) on delete cascade,
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
    except Exception:
        tz = pytz.UTC
    return datetime.now(tz)

def convert_goal(unit_choice: int, number: int):
    """Convert chosen unit to base oz/ml."""
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

def interval_choice_to_minutes(choice: int, custom_minutes: int | None = None) -> int | None:
    mapping = {
        1: 15,
        2: 30,
        3: 45,
        4: 60,
        5: 90,
        6: 120,
        7: 180
    }
    if choice in mapping:
        return mapping[choice]
    if choice == 8 and custom_minutes and custom_minutes > 0:
        return custom_minutes
    return None

def timezone_choice(choice: int, custom: str | None = None) -> str:
    mapping = {
        1: "America/New_York",
        2: "America/Chicago",
        3: "America/Denver",
        4: "America/Los_Angeles",
        5: "UTC"
    }
    if choice in mapping:
        return mapping[choice]
    if choice == 6 and custom:
        try:
            pytz.timezone(custom)
            return custom
        except Exception:
            return "UTC"
    return "UTC"

# ---------------- Commands ----------------
@bot.command(name="config")
async def config(ctx):
    def check(m): return m.author == ctx.author and m.channel == ctx.channel

    # Name
    await ctx.send("👋 What should I call you?")
    name = (await bot.wait_for("message", check=check)).content.strip()

    # Age
    await ctx.send("📅 How old are you?")
    try:
        age = int((await bot.wait_for("message", check=check)).content.strip())
    except:
        return await ctx.send("❌ Invalid number for age. Run `$config` again.")

    # Daily goal unit selection with conversion
    await ctx.send(
        "💧 Choose the unit for your daily goal:\n"
        "1) Ounces (oz)\n"
        "2) Cups (8 oz)\n"
        "3) Pints (16 oz)\n"
        "4) Gallons (128 oz)\n"
        "5) Milliliters (ml)\n"
        "6) Liters (1000 ml)"
    )
    try:
        unit_choice = int((await bot.wait_for("message", check=check)).content.strip())
    except:
        return await ctx.send("❌ Invalid choice. Run `$config` again.")

    await ctx.send("📊 Enter the number for your **daily goal** in that unit (e.g., `8` cups, `2` liters):")
    try:
        num = int((await bot.wait_for("message", check=check)).content.strip())
    except:
        return await ctx.send("❌ Invalid number. Run `$config` again.")

    daily_goal, base_unit = convert_goal(unit_choice, num)
    if not daily_goal:
        return await ctx.send("❌ Invalid unit selection. Run `$config` again.")

    # Interval picker
    await ctx.send(
        "⏱ Choose your reminder interval:\n"
        "1) every 15 minutes\n"
        "2) every 30 minutes\n"
        "3) every 45 minutes\n"
        "4) every 1 hour\n"
        "5) every 90 minutes\n"
        "6) every 2 hours\n"
        "7) every 3 hours\n"
        "8) custom (enter minutes)"
    )
    try:
        interval_choice = int((await bot.wait_for("message", check=check)).content.strip())
    except:
        return await ctx.send("❌ Invalid choice. Run `$config` again.")
    custom_minutes = None
    if interval_choice == 8:
        await ctx.send("⌨️ Enter custom interval in minutes (e.g., `75`):")
        try:
            custom_minutes = int((await bot.wait_for("message", check=check)).content.strip())
        except:
            return await ctx.send("❌ Invalid minutes. Run `$config` again.")
    interval = interval_choice_to_minutes(interval_choice, custom_minutes)
    if not interval:
        return await ctx.send("❌ Interval not recognized. Run `$config` again.")

    # Timezone picker
    await ctx.send(
        "🌍 Choose your timezone:\n"
        "1) EST (America/New_York)\n"
        "2) CST (America/Chicago)\n"
        "3) MST (America/Denver)\n"
        "4) PST (America/Los_Angeles)\n"
        "5) UTC\n"
        "6) Custom (type your own; e.g., Europe/London)"
    )
    try:
        tz_choice = int((await bot.wait_for("message", check=check)).content.strip())
    except:
        return await ctx.send("❌ Invalid choice. Run `$config` again.")
    tz_custom = None
    if tz_choice == 6:
        await ctx.send("⌨️ Enter your timezone (e.g., `Europe/London`):")
        tz_custom = (await bot.wait_for("message", check=check)).content.strip()
    tz = timezone_choice(tz_choice, tz_custom)

    # Channels
    await ctx.send("🔔 Mention **reminder** channel (#channel)")
    reminder_msg = await bot.wait_for("message", check=check)
    reminder_channel = reminder_msg.channel_mentions[0].id if reminder_msg.channel_mentions else None

    await ctx.send("📜 Mention **log** channel (#channel)")
    log_msg = await bot.wait_for("message", check=check)
    log_channel = log_msg.channel_mentions[0].id if log_msg.channel_mentions else None

    # Self-ping
    await ctx.send("👤 Ping yourself on reminders? (yes/no)")
    ping_self = (await bot.wait_for("message", check=check)).content.strip().lower() in ["yes","y","true","1"]

    # Coach setup (FIXED)
    await ctx.send("👥 Enable coach pings? (yes/no)")
    coach_enable = (await bot.wait_for("message", check=check)).content.strip().lower() in ["yes","y","true","1"]

    coach_role_id = None
    coach_ping_logs = False
    coach_ping_reminders = False
    if coach_enable:
        roles = [r for r in ctx.guild.roles if not r.is_default()]
        if not roles:
            await ctx.send("ℹ️ No selectable roles found; skipping coach role.")
        else:
            show = roles[:20]
            listing = "\n".join([f"{i+1}. {r.mention}" for i, r in enumerate(show)])
            await ctx.send(f"Select a coach role (type the **number**):\n{listing}")
            try:
                choice = int((await bot.wait_for("message", check=check)).content.strip())
                coach_role_id = show[choice-1].id
            except Exception:
                await ctx.send("❌ Invalid selection. Skipping coach role.")
                coach_role_id = None
    if coach_role_id:
        await ctx.send("📜 Ping coach role in **daily logs**? (yes/no)")
        coach_ping_logs = (await bot.wait_for("message", check=check)).content.strip().lower() in ["yes","y","true","1"]

        await ctx.send("🔔 Ping coach role in **reminders**? (yes/no)")
        import asyncio
        try:
            reply = await bot.wait_for("message", check=check, timeout=60)
            coach_ping_reminders = reply.content.strip().lower() in ["yes","y","true","1"]
            await ctx.send(f"✅ Coach role reminders set to {coach_ping_reminders}")
        except asyncio.TimeoutError:
            await ctx.send("⏳ No answer received, skipping coach reminder pings.")
            coach_ping_reminders = False

    # Save config
    await upsert_user(
        ctx.author.id,
        name=name,
        age=age,
        daily_goal=daily_goal,
        unit=base_unit,
        interval=interval,
        timezone=tz,
        reminder_channel=reminder_channel,
        log_channel=log_channel,
        ping_self=ping_self,
        coach_role=coach_role_id,
        coach_ping_logs=coach_ping_logs,
        coach_ping_reminders=coach_ping_reminders,
        last_reset=tz_now(tz).date()
    )
    await ctx.send(f"✅ Config saved! Daily goal = {daily_goal} {base_unit} • Every {interval} min • TZ: {tz}")

# --- DRINK COMMAND ---
@bot.command(name="drink", aliases=["Drink", "DRINK", "dRiNk"])
async def drink(ctx):
    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    # Step 1: Ask for unit
    await ctx.send("💧 What unit are you logging in? (oz/ml)")
    try:
        unit_msg = await bot.wait_for("message", check=check, timeout=30.0)
        log_unit = unit_msg.content.lower()
        if log_unit not in ["oz", "ml"]:
            await ctx.send("❌ Please choose either 'oz' or 'ml'.")
            return
    except asyncio.TimeoutError:
        await ctx.send("⏰ You didn’t respond with a unit in time. Try again with `$drink`.")
        return

    # Step 2: Ask for amount
    await ctx.send(f"💧 How many {log_unit}?")
    try:
        amt_msg = await bot.wait_for("message", check=check, timeout=30.0)
        log_amount = int(amt_msg.content)
    except asyncio.TimeoutError:
        await ctx.send("⏰ You didn’t respond with an amount in time. Try again with `$drink`.")
        return
    except ValueError:
        await ctx.send("❌ That wasn’t a valid number.")
        return

    # Step 3: Confirm + log to DB
    user = await get_user(ctx.author.id)
    if not user:
        return await ctx.send("Run `$config` first.")

    config_unit = user.get("unit", "ml")  # default to ml
    amount = log_amount
    final_unit = log_unit
    display_extra = ""

    # Conversion + dual display
    if log_unit != config_unit:
        if log_unit == "oz" and config_unit == "ml":
            amount = round(log_amount * 29.5735)
            display_extra = f"≈ {amount} ml"
        elif log_unit == "ml" and config_unit == "oz":
            amount = round(log_amount * 0.033814)
            display_extra = f"≈ {amount} oz"
        final_unit = config_unit
    else:
        # Show alternative unit anyway
        if log_unit == "oz":
            display_extra = f"(≈ {round(log_amount * 29.5735)} ml)"
        else:
            display_extra = f"(≈ {round(log_amount * 0.033814)} oz)"

    now_local = tz_now(user["timezone"] or "UTC")
    await log_event(ctx.author.id, now_local, amount, final_unit, "manual", str(ctx.channel.id))
    await add_daily_total(ctx.author.id, now_local.date(), amount)

    confirmation = f"✅ Logged {log_amount} {log_unit} {display_extra}"
    await ctx.send(confirmation)

    # Optional: send to log channel if configured
    if user.get("log_channel_id"):
        log_channel = bot.get_channel(user["log_channel_id"])
        if log_channel:
            msg = f"📒 {ctx.author.display_name} logged {log_amount} {log_unit} {display_extra} at {now_local.strftime('%H:%M')}."
            if user.get("coach_ping_logs") and user.get("coach_role_id"):
                msg += f" <@&{user['coach_role_id']}>"
            await log_channel.send(msg)

@bot.command(name="check")
async def check(ctx):
    user = await get_user(ctx.author.id)
    if not user:
        return await ctx.send("Run `$config` first.")
    conn = await asyncpg.connect(DATABASE_URL)
    today = tz_now(user["timezone"]).date()
    row = await conn.fetchrow(
        "SELECT total FROM daily_logs WHERE user_id=$1 AND date=$2",
        ctx.author.id, today
    )
    await conn.close()
    total = row["total"] if row else 0
    pct = (total / user["daily_goal"] * 100) if user["daily_goal"] else 0
    await ctx.send(f"💧 Progress: {total}/{user['daily_goal']} {user['unit']} ({pct:.1f}%) today.")

@bot.command(name="report")
async def report(ctx, days: int = 7):
    user = await get_user(ctx.author.id)
    if not user:
        return await ctx.send("Run `$config` first.")
    if days not in (7, 15, 30):
        days = 7
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch(
        "SELECT date,total FROM daily_logs WHERE user_id=$1 ORDER BY date DESC LIMIT $2",
        ctx.author.id, days
    )
    await conn.close()
    if not rows:
        return await ctx.send("No logs yet.")
    totals = [r["total"] for r in rows]
    avg = sum(totals) / len(totals)
    best = max(rows, key=lambda r: r["total"])
    worst = min(rows, key=lambda r: r["total"])
    msg = (
        f"📊 {days}-day report:\n"
        f"• Average: {avg:.1f} {user['unit']}/day\n"
        f"• Best: {best['total']} on {best['date']}\n"
        f"• Worst: {worst['total']} on {worst['date']}"
    )
    await ctx.send(msg)

@bot.command(name="status")
async def status(ctx):
    user = await get_user(ctx.author.id)
    if not user:
        return await ctx.send("Run `$config` first.")
    embed = discord.Embed(title=f"📊 {user['name']}'s Status", color=discord.Color.blurple())
    embed.add_field(name="Age", value=user["age"], inline=True)
    embed.add_field(name="Daily Goal", value=f"{user['daily_goal']} {user['unit']}", inline=True)
    embed.add_field(name="Interval", value=f"{user['interval']} minutes", inline=True)
    embed.add_field(name="Timezone", value=user['timezone'], inline=True)
    embed.add_field(name="Reminder Channel", value=f"<#{user['reminder_channel']}>" if user['reminder_channel'] else "None", inline=True)
    embed.add_field(name="Log Channel", value=f"<#{user['log_channel']}>" if user['log_channel'] else "None", inline=True)
    embed.add_field(name="Ping Self", value=str(user['ping_self']), inline=True)
    embed.add_field(name="Coach Role", value=f"<@&{user['coach_role']}>" if user['coach_role'] else "None", inline=True)
    embed.add_field(name="Coach Pings (logs)", value=str(user['coach_ping_logs']), inline=True)
    embed.add_field(name="Coach Pings (reminders)", value=str(user['coach_ping_reminders']), inline=True)
    await ctx.send(embed=embed)

@bot.command(name="help")
async def help_cmd(ctx):
    embed = discord.Embed(title="💧 Bumpy Help", color=discord.Color.green())
    embed.add_field(
        name="$config",
        value="Setup wizard: name, age, daily goal (with unit selection), interval (menu), timezone (menu), channels, self ping, coach role + ping toggles",
        inline=False
    )
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
    now_utc = datetime.utcnow()

    for u in users:
        if not u["interval"] or not u["daily_goal"]:
            continue

        # Due?
        due = False
        if u["last_reminder"] is None:
            due = True
        else:
            elapsed = (now_utc - u["last_reminder"]).total_seconds()
            if elapsed >= u["interval"] * 60:
                due = True
        if not due:
            continue

        tz_name = u["timezone"] or "UTC"
        local_now = tz_now(tz_name)

        # Dynamic increment (assume 16 waking hours)
        waking_hours = 16
        reminders_per_day = max(1, (waking_hours * 60) // u["interval"])
        increment = max(1, int(round(u["daily_goal"] / reminders_per_day)))

        # Compose & send
        mention = f"<@{u['id']}>" if u["ping_self"] else ""
        coach_mention = f" <@&{u['coach_role']}>" if u["coach_role"] and u["coach_ping_reminders"] else ""
        sent_where = "dm"

        if u["reminder_channel"]:
            ch = bot.get_channel(u["reminder_channel"])
            if ch:
                await ch.send(f"💧 {mention}{coach_mention} Time to drink ~ **{increment} {u['unit']}**!")
                sent_where = f"<#{u['reminder_channel']}>"
            else:
                user_obj = await bot.fetch_user(u["id"])
                if user_obj:
                    await user_obj.send(f"💧 {mention}{coach_mention} Time to drink ~ **{increment} {u['unit']}**!")
        else:
            user_obj = await bot.fetch_user(u["id"])
            if user_obj:
                await user_obj.send(f"💧 {mention}{coach_mention} Time to drink ~ **{increment} {u['unit']}**!")

        # Update reminder time
        await conn.execute("UPDATE users SET last_reminder=$1 WHERE id=$2", now_utc, u["id"])

        # Log reminder intake & add to totals
        await add_daily_total(u["id"], local_now.date(), increment)
        await log_event(u["id"], local_now, increment, u["unit"], "reminder", sent_where)

        # Optional echo to log channel
        if u["log_channel"]:
            log_ch = bot.get_channel(u["log_channel"])
            if log_ch:
                coach_for_log = f" <@&{u['coach_role']}>" if u["coach_role"] and u["coach_ping_logs"] else ""
                await log_ch.send(
                    f"📥 +{increment} {u['unit']} for <@{u['id']}> at {local_now.isoformat()}{coach_for_log}"
                )

    await conn.close()

@tasks.loop(minutes=5)
async def reset_loop():
    conn = await asyncpg.connect(DATABASE_URL)
    users = await conn.fetch("SELECT * FROM users")

    for u in users:
        tz_name = u["timezone"] or "UTC"
        local_now = tz_now(tz_name)

        # New local day?
        if u["last_reset"] != local_now.date():
            yesterday = local_now.date() - timedelta(days=1)

            row = await conn.fetchrow(
                "SELECT total FROM daily_logs WHERE user_id=$1 AND date=$2",
                u["id"], yesterday
            )
            total = row["total"] if row else 0

            if u["log_channel"]:
                channel = bot.get_channel(u["log_channel"])
                if channel:
                    percent = (total / u["daily_goal"]) * 100 if u["daily_goal"] else 0
                    emoji = "🎯" if percent >= 100 else "💤"
                    coach_mention = f" <@&{u['coach_role']}>" if u["coach_role"] and u["coach_ping_logs"] else ""
                    msg = (
                        f"📅 Daily Summary for {yesterday}\n"
                        f"💧 {total}/{u['daily_goal']} {u['unit']} ({percent:.1f}%) {emoji}{coach_mention}"
                    )
                    await channel.send(msg)

            # Mark today as reset to prevent multiple posts
            await conn.execute("UPDATE users SET last_reset=$1 WHERE id=$2", local_now.date(), u["id"])

    await conn.close()

@bot.event
async def on_ready():
    await init_db()
    reminder_loop.start()
    reset_loop.start()
    print(f"Bumpy online as {bot.user}")

bot.run(TOKEN)
