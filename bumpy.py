import discord
from discord.ext import commands, tasks
import json
import os
from datetime import datetime, timedelta
import pytz

TOKEN = os.getenv("DISCORD_TOKEN")
CONFIG_FILE = "users.json"

# ---------------- Storage helpers ----------------
def load_users():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}

def save_users(users):
    with open(CONFIG_FILE, "w") as f:
        json.dump(users, f, indent=4)

def ensure_user(user_id: int):
    users = load_users()
    sid = str(user_id)
    if sid not in users:
        users[sid] = {
            "increment": None,
            "unit": "oz",
            "interval": None,            # minutes between reminders
            "progress": 0,               # running total today
            "timezone": "UTC",
            "last_reset": None,          # yyyy-mm-dd in user's tz
            "last_reminder": None,       # ISO timestamp in user's tz
            "reminder_channel": None,
            "log_channel": None,
            "ping_self": False,
            "coach_role": None,
            "coach_ping_logs": False,

            # NEW: historical logs & event timeline
            "daily_logs": {},            # {"YYYY-MM-DD": total_amount}
            "events": {},                # {"YYYY-MM-DD": [ {ts, amount, unit, kind, where} ]}
            "last_archived_for": None    # last date (yyyy-mm-dd) we already archived & posted summary for
        }
        save_users(users)
    return users

def tz_now(tz_name: str) -> datetime:
    tz = pytz.timezone(tz_name or "UTC")
    return datetime.now(tz)

def date_str(dt: datetime) -> str:
    return dt.date().isoformat()

def add_event(user: dict, when: datetime, amount: int, unit: str, kind: str, where: str):
    d = date_str(when)
    if "events" not in user or not isinstance(user["events"], dict):
        user["events"] = {}
    if d not in user["events"]:
        user["events"][d] = []
    # Save full ISO timestamp WITH timezone info
    user["events"][d].append({
        "ts": when.isoformat(),
        "amount": amount,
        "unit": unit,
        "kind": kind,        # "reminder" or "manual"
        "where": where       # "dm" or "<#channel_id>"
    })

# ---------------- Bot setup ----------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="$", intents=intents, help_command=None)

# ---------------- Interactive CONFIG ----------------
@bot.command(name="config")
async def config(ctx):
    def check(m): return m.author == ctx.author and m.channel == ctx.channel

    users = ensure_user(ctx.author.id)
    user = users[str(ctx.author.id)]

    # Amount
    await ctx.send("💧 How much water per interval? (e.g., `8`)")
    try:
        amount = int((await bot.wait_for("message", check=check)).content.strip())
    except:
        return await ctx.send("❌ Please provide a number (e.g., `8`). Start `$config` again.")

    # Unit
    await ctx.send("⚖️ What unit? (`oz` or `ml`)")
    unit = (await bot.wait_for("message", check=check)).content.strip().lower()
    if unit not in ["oz", "ml"]:
        return await ctx.send("❌ Invalid unit. Start `$config` again.")

    # Interval
    await ctx.send("⏱ How often? Type like `1 hour` or `30 min`")
    parts = (await bot.wait_for("message", check=check)).content.strip().split()
    if len(parts) < 2:
        return await ctx.send("❌ Invalid format. Example: `1 hour` or `30 min`")
    try:
        interval_n = int(parts[0]); per = parts[1].lower()
    except:
        return await ctx.send("❌ Invalid format. Example: `1 hour` or `30 min`")
    minutes = interval_n * 60 if per.startswith("hour") else interval_n

    # Timezone
    await ctx.send("🌍 What’s your timezone? (Example: `America/Chicago`)")
    tz = (await bot.wait_for("message", check=check)).content.strip()
    try:
        pytz.timezone(tz)
    except:
        return await ctx.send("❌ Invalid timezone. Example: `America/Chicago`")

    # Channels (pick from list)
    channels = [c for c in ctx.guild.text_channels]
    if not channels:
        return await ctx.send("❌ No text channels found in this server.")
    listing = "\n".join([f"{i+1}. {c.mention}" for i, c in enumerate(channels)])
    await ctx.send(f"🔔 Choose a **reminder** channel (type the number):\n{listing}")
    try:
        choice = int((await bot.wait_for("message", check=check)).content.strip())
        reminder_channel = channels[choice-1].id
    except:
        return await ctx.send("❌ Invalid selection.")

    await ctx.send(f"📜 Choose a **log** channel (type the number):\n{listing}")
    try:
        choice = int((await bot.wait_for("message", check=check)).content.strip())
        log_channel = channels[choice-1].id
    except:
        return await ctx.send("❌ Invalid selection.")

    # Ping self?
    await ctx.send("❓ Ping yourself on reminders? (yes/no)")
    ping_self = (await bot.wait_for("message", check=check)).content.strip().lower() in ["yes", "y", "true", "1"]

    # Coach pings?
    await ctx.send("❓ Enable coach pings? (yes/no)")
    coach_enable = (await bot.wait_for("message", check=check)).content.strip().lower() in ["yes", "y", "true", "1"]
    coach_role = None
    if coach_enable:
        roles = [r for r in ctx.guild.roles if not r.is_default()]
        if roles:
            rlist = "\n".join([f"{i+1}. {r.mention}" for i, r in enumerate(roles)])
            await ctx.send(f"👥 Select a coach role (type the number):\n{rlist}")
            try:
                rchoice = int((await bot.wait_for("message", check=check)).content.strip())
                coach_role = roles[rchoice-1].id
            except:
                return await ctx.send("❌ Invalid selection.")
        else:
            await ctx.send("ℹ️ No selectable roles found; skipping coach role.")

    # Ping coach on logs?
    coach_ping_logs = False
    if coach_role:
        await ctx.send("❓ Ping coach on log messages? (yes/no)")
        coach_ping_logs = (await bot.wait_for("message", check=check)).content.strip().lower() in ["yes", "y", "true", "1"]

    # Save configuration
    user.update({
        "increment": amount,
        "unit": unit,
        "interval": minutes,
        "progress": 0,
        "timezone": tz,
        "reminder_channel": reminder_channel,
        "log_channel": log_channel,
        "ping_self": ping_self,
        "coach_role": coach_role,
        "coach_ping_logs": coach_ping_logs,
        "last_reset": date_str(tz_now(tz)),   # align today's date
    })
    save_users(users)

    # Confirm
    embed = discord.Embed(
        title=f"✅ Configuration Complete for {ctx.author.display_name}",
        color=discord.Color.green()
    )
    embed.add_field(name="Goal", value=f"{amount} {unit} every {interval_n} {per}", inline=False)
    embed.add_field(name="Timezone", value=tz, inline=False)
    embed.add_field(name="Reminder Channel", value=f"<#{reminder_channel}>", inline=False)
    embed.add_field(name="Log Channel", value=f"<#{log_channel}>", inline=False)
    embed.add_field(name="Ping Yourself", value=str(ping_self), inline=True)
    embed.add_field(name="Coach Role", value=f"<@&{coach_role}>" if coach_role else "None", inline=True)
    embed.add_field(name="Ping Coach on Logs", value=str(coach_ping_logs), inline=True)
    await ctx.send(embed=embed)

# ---------------- User commands ----------------
@bot.command(name="drink")
async def drink(ctx, amount: int = None):
    if amount is None:
        return await ctx.send("Usage: `$drink <amount>` e.g. `$drink 12`")

    users = ensure_user(ctx.author.id)
    user = users[str(ctx.author.id)]
    if not user.get("increment"):
        return await ctx.send("No goal set. Run `$config` first.")

    now = tz_now(user.get("timezone", "UTC"))
    user["progress"] = int(user.get("progress", 0)) + int(amount)
    add_event(user, now, int(amount), user.get("unit", "oz"), "manual", f"<#{ctx.channel.id}>")
    save_users(users)

    await ctx.send(f"{ctx.author.mention} Logged {amount} {user['unit']} @ {now.isoformat()}\n"
                   f"Progress today: {user['progress']} {user['unit']}")

@bot.command(name="check")
async def check(ctx):
    users = ensure_user(ctx.author.id)
    user = users[str(ctx.author.id)]
    if not user.get("increment"):
        return await ctx.send("No goal set. Run `$config` first.")

    await ctx.send(
        f"{ctx.author.mention} Goal: {user['increment']} {user['unit']} every {user['interval']} min\n"
        f"Progress today: {user['progress']} {user['unit']}"
    )

@bot.command(name="status")
async def status(ctx):
    users = ensure_user(ctx.author.id)
    u = users[str(ctx.author.id)]
    tz = u.get("timezone", "UTC")
    embed = discord.Embed(title=f"📊 {ctx.author.display_name}'s Hydration Status", color=discord.Color.blurple())
    if u.get("increment") and u.get("interval"):
        embed.add_field(name="Goal", value=f"{u['increment']} {u['unit']} every {u['interval']} min", inline=False)
    else:
        embed.add_field(name="Goal", value="Not set", inline=False)
    embed.add_field(name="Progress (today)", value=f"{u.get('progress',0)} {u.get('unit','oz')}", inline=False)
    embed.add_field(name="Timezone", value=tz, inline=True)
    embed.add_field(name="Reminder Channel", value=f"<#{u['reminder_channel']}>" if u.get("reminder_channel") else "None", inline=True)
    embed.add_field(name="Log Channel", value=f"<#{u['log_channel']}>" if u.get("log_channel") else "None", inline=True)
    embed.add_field(name="Ping Yourself", value=str(u.get("ping_self", False)), inline=True)
    embed.add_field(name="Coach Role", value=f"<@&{u['coach_role']}>" if u.get("coach_role") else "None", inline=True)
    embed.add_field(name="Ping Coach on Logs", value=str(u.get("coach_ping_logs", False)), inline=True)
    await ctx.send(embed=embed)

# ---------------- Reports with insights ----------------
@bot.command(name="report")
async def report(ctx, days: int = 7):
    if days not in [7, 15, 30]:
        return await ctx.send("Usage: `$report <7|15|30>`")

    users = ensure_user(ctx.author.id)
    u = users[str(ctx.author.id)]
    logs = u.get("daily_logs", {})

    if not logs:
        return await ctx.send("No history yet. Bumpy will build logs automatically each midnight.")

    # Sort by date desc and take last N days
    sorted_days = sorted(logs.keys(), reverse=True)[:days]
    if not sorted_days:
        return await ctx.send("No data for that period.")

    records = [(d, logs[d]) for d in sorted_days]
    total = sum(v for _, v in records)
    avg = total / len(records)
    best_day, best_val = max(records, key=lambda x: x[1])
    worst_day, worst_val = min(records, key=lambda x: x[1])

    goal = None
    if u.get("increment") and u.get("interval"):
        # daily target estimated by intervals/day
        goal = int((24*60) // int(u["interval"])) * int(u["increment"])
    successes = sum(1 for _, v in records if goal is not None and v >= goal)

    embed = discord.Embed(
        title=f"📈 {ctx.author.display_name}'s {days}-Day Hydration Report",
        color=discord.Color.green()
    )
    embed.add_field(name="Average Intake", value=f"{avg:.1f} {u.get('unit','oz')}/day", inline=False)
    embed.add_field(name="Best Day", value=f"{best_day}: {best_val} {u.get('unit','oz')}", inline=True)
    embed.add_field(name="Worst Day", value=f"{worst_day}: {worst_val} {u.get('unit','oz')}", inline=True)
    if goal is not None:
        embed.add_field(name="Goal Success", value=f"{successes}/{len(records)} days met goal (~{(successes/len(records))*100:.0f}%)", inline=False)
    breakdown = "\n".join([f"{d}: {v} {u.get('unit','oz')}" for d, v in records])
    embed.add_field(name="Daily Breakdown", value=breakdown, inline=False)
    await ctx.send(embed=embed)

# ---------------- Reminder & daily archive engine ----------------
@tasks.loop(minutes=1)
async def engine():
    users = load_users()
    changed = False

    for uid, u in users.items():
        if not u.get("increment") or not u.get("interval"):
            continue

        tzname = u.get("timezone", "UTC")
        now = tz_now(tzname)
        today_str = date_str(now)

        # Initialize last_reset on first run
        if not u.get("last_reset"):
            u["last_reset"] = today_str
            changed = True

        # If we've crossed midnight in user's timezone, archive yesterday
        if u.get("last_reset") != today_str:
            # Archive previous day's total
            yday = u["last_reset"]
            if u.get("progress", 0) is not None:
                if "daily_logs" not in u or not isinstance(u["daily_logs"], dict):
                    u["daily_logs"] = {}
                u["daily_logs"][yday] = int(u.get("progress", 0))
            # Post a summary to log channel (once)
            if u.get("log_channel"):
                log_ch = bot.get_channel(u["log_channel"])
                if log_ch:
                    coach_ping = f" <@&{u['coach_role']}>" if u.get("coach_ping_logs") and u.get("coach_role") else ""
                    await log_ch.send(
                        f"🗓️ Daily summary for <@{uid}> ({yday}): {u.get('daily_logs',{}).get(yday,0)} {u.get('unit','oz')}.{coach_ping}"
                    )
            # Reset counters for new day
            u["progress"] = 0
            u["last_reset"] = today_str
            u["last_reminder"] = None
            changed = True

        # Check reminder interval
        last_ts = u.get("last_reminder")
        interval_seconds = int(u["interval"]) * 60
        should_remind = False
        if last_ts is None:
            should_remind = True
        else:
            try:
                last_dt = datetime.fromisoformat(last_ts)
                # Convert saved tz-aware string back to aware dt
                last_dt = pytz.timezone(tzname).localize(last_dt.replace(tzinfo=None)) if last_dt.tzinfo is None else last_dt
                if (now - last_dt).total_seconds() >= interval_seconds:
                    should_remind = True
            except Exception:
                should_remind = True

        if should_remind:
            # Compose & send reminder
            inc = int(u["increment"]); unit = u.get("unit", "oz")
            mention = f"<@{uid}>" if u.get("ping_self") else ""
            sent_where = "dm"
            if u.get("reminder_channel"):
                ch = bot.get_channel(u["reminder_channel"])
                if ch:
                    await ch.send(f"{mention} ⏰ Time to drink {inc} {unit}! ({now.isoformat()})")
                    sent_where = f"<#{u['reminder_channel']}>"
                else:
                    # fallback DM
                    user = await bot.fetch_user(int(uid))
                    if user:
                        await user.send(f"{mention} ⏰ Time to drink {inc} {unit}! ({now.isoformat()})")
            else:
                user = await bot.fetch_user(int(uid))
                if user:
                    await user.send(f"{mention} ⏰ Time to drink {inc} {unit}! ({now.isoformat()})")

            # Update progress and timestamps and event log
            u["progress"] = int(u.get("progress", 0)) + inc
            u["last_reminder"] = now.isoformat()
            add_event(u, now, inc, unit, "reminder", sent_where)
            changed = True

            # Also log incremental intake in log channel (optional ping coach)
            if u.get("log_channel"):
                log_ch = bot.get_channel(u["log_channel"])
                if log_ch:
                    coach_ping = f" <@&{u['coach_role']}>" if u.get("coach_ping_logs") and u.get("coach_role") else ""
                    await log_ch.send(
                        f"📥 {mention} +{inc} {unit} at {now.isoformat()} — total today: {u['progress']} {unit}.{coach_ping}"
                    )

    if changed:
        save_users(users)

@bot.event
async def on_ready():
    print(f"Bumpy online as {bot.user}")
    engine.start()

# ---------------- Optional: help summary ----------------
@bot.command(name="help")
async def help_cmd(ctx):
    embed = discord.Embed(title="💧 Bumpy — Hydration Assistant", color=discord.Color.blue())
    embed.add_field(name="$config", value="Interactive setup wizard", inline=False)
    embed.add_field(name="$drink <amount>", value="Log manual intake", inline=False)
    embed.add_field(name="$check", value="Show today’s goal & progress", inline=False)
    embed.add_field(name="$status", value="Show all settings", inline=False)
    embed.add_field(name="$report <7|15|30>", value="Multi-day report with insights", inline=False)
    await ctx.send(embed=embed)

bot.run(TOKEN)