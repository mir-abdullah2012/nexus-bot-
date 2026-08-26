"""Utility: polls, weather, reminders, and the help card.

Reminders now live in SQLite, so the poll loop is an indexed query for due rows
instead of rewriting reminders.json every 20 seconds.
"""

import asyncio
import re
import time

import discord
import requests
from discord.ext import commands, tasks

import config

POLL_NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def parse_duration(s):
    m = re.fullmatch(r"(\d+)\s*([smhd])", s.lower().strip())
    if not m:
        return None
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


class Utility(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.reminder_loop.start()

    def cog_unload(self):
        self.reminder_loop.cancel()

    @property
    def repo(self):
        return self.bot.repo

    # ========================================================
    #  POLLS
    # ========================================================
    @commands.command()
    async def poll(self, ctx, question, *options):
        if len(options) > 10:
            await ctx.send("❌ Max 10 options.")
            return

        embed = discord.Embed(title="📊 " + question, color=discord.Color.blurple())
        if not options:
            embed.description = "👍 Yes  |  👎 No"
            msg = await ctx.send(embed=embed)
            await msg.add_reaction("👍")
            await msg.add_reaction("👎")
            return

        embed.description = "\n".join(
            f"{POLL_NUMBERS[i]} {opt}" for i, opt in enumerate(options)
        )
        msg = await ctx.send(embed=embed)
        for i in range(len(options)):
            await msg.add_reaction(POLL_NUMBERS[i])

    # ========================================================
    #  WEATHER
    # ========================================================
    @commands.command()
    async def weather(self, ctx, *, city):
        if not config.WEATHER_API_KEY:
            await ctx.send("❌ Weather isn't set up (missing WEATHER_API_KEY).")
            return
        try:
            params = {"q": city, "appid": config.WEATHER_API_KEY, "units": "metric"}
            r = await asyncio.to_thread(
                requests.get, config.WEATHER_URL, params=params,
                timeout=config.WEATHER_TIMEOUT,
            )
            data = r.json()
            if str(data.get("cod")) != "200":
                await ctx.send("❌ City not found.")
                return

            embed = discord.Embed(
                title=f"🌤️ Weather in {data['name']}", color=discord.Color.blue()
            )
            embed.add_field(
                name="Temp",
                value=f"{data['main']['temp']}°C (feels {data['main']['feels_like']}°C)",
                inline=True,
            )
            embed.add_field(
                name="Condition",
                value=data["weather"][0]["description"].title(),
                inline=True,
            )
            embed.add_field(
                name="Humidity", value=f"{data['main']['humidity']}%", inline=True
            )
            await ctx.send(embed=embed)
        except Exception as e:
            print(f"weather error: {e}")
            await ctx.send("⚠️ Couldn't fetch weather right now.")

    # ========================================================
    #  REMINDERS (survive restarts)
    # ========================================================
    @commands.command()
    async def remind(self, ctx, duration, *, text):
        seconds = parse_duration(duration)
        if seconds is None:
            await ctx.send(
                "❌ Use a time like `10m`, `2h`, `1d`. Example: `!remind 30m drink water`"
            )
            return
        await self.repo.add_reminder(
            user_id=ctx.author.id,
            channel_id=ctx.channel.id,
            remind_at=int(time.time() + seconds),
            text=text,
            guild_id=ctx.guild.id if ctx.guild else None,
        )
        await ctx.send(f"⏰ Got it! I'll remind you in **{duration}**.")

    @tasks.loop(seconds=config.REMINDER_POLL_SECONDS)
    async def reminder_loop(self):
        try:
            due = await self.repo.due_reminders(int(time.time()))
            if not due:
                return
            for reminder in due:
                channel = self.bot.get_channel(reminder.channel_id)
                if channel:
                    try:
                        await channel.send(
                            f"⏰ <@{reminder.user_id}> reminder: **{reminder.text}**"
                        )
                    except Exception:
                        pass
            # Mark fired either way: a reminder whose channel vanished was
            # dropped by Nexus 1.x too, rather than retried forever.
            await self.repo.mark_reminders_fired([r.id for r in due])
        except Exception as e:
            print(f"reminder loop error: {e}")

    @reminder_loop.before_loop
    async def _before_reminder(self):
        await self.bot.wait_until_ready()

    # ========================================================
    #  HELP
    # ========================================================
    @commands.command()
    async def help(self, ctx):
        embed = discord.Embed(title="🤖 Bot Commands", color=discord.Color.blurple())
        embed.add_field(name="💰 Economy", value=(
            "`!mine` `!balance` `!daily` `!leaderboard`\n"
            "`!inventory` `!shop` `buy [item]`\n"
            "`!give @user amt` `!rob @user`"
        ), inline=False)
        embed.add_field(
            name="🎮 Games",
            value="`!coinflip [bet]` `!slots [bet]` `!rps rock/paper/scissors [bet]`",
            inline=False,
        )
        embed.add_field(name="📊 Stats", value="`!level [@user]`", inline=False)
        embed.add_field(name="🧬 RPG", value=(
            "`!class [name]` — pick a class (free to switch, anytime)\n"
            "`!equip <item>` `!unequip <slot>` `!build`\n"
            "`!salvage <item>` — scrap spare gear for $RAM\n"
            "`!prestige` — full wipe for permanent bonuses"
        ), inline=False)
        embed.add_field(name="⚔️ Dungeons", value=(
            "`!dungeon` — list workloads + cooldown\n"
            "`!dungeon <name>` — run it"
        ), inline=False)
        embed.add_field(name="🛡️ Guilds", value=(
            "`!guild` `!guild list` `!guild create <name>` `!guild join <name>`\n"
            "`!guild leave` `!guild kick/promote/demote @user`\n"
            "`!guild war` — 7-day guild standings"
        ), inline=False)
        embed.add_field(name="🏪 Market", value=(
            "`!market [page]` — browse player listings\n"
            "`!market sell <item> <price>` `!market buy <id>`\n"
            "`!market mine` `!market cancel <id>` `!market find <item|category>`"
        ), inline=False)
        embed.add_field(name="🐾 Pets", value=(
            "`buy EGG` — 3,000GB, or find one in a dungeon\n"
            "`!pet hatch` `!pet list` `!pet active <id>`\n"
            "`!pet name <text>` `!pet species` `!pet release <id>`"
        ), inline=False)
        embed.add_field(name="🤺 PvP", value=(
            "`!duel @user [wager]` — challenge someone\n"
            "`!duel accept` / `!duel decline`\n"
            "`!duelstats [@user]` `!duelboard`"
        ), inline=False)
        embed.add_field(name="🛡️ Mod", value=(
            "`!warn` `!warnings` `!clearwarnings`\n"
            "`!kick` `!ban` `!unban` `!mute` `!unmute`\n"
            "`!purge` `!slowmode` `!lock` `!unlock`"
        ), inline=False)
        embed.add_field(
            name="🎭 Roles / Fun",
            value='`!role <name>` `!roles` `!poll "Q" opt1 opt2` `!weather <city>` `!remind 10m text`',
            inline=False,
        )
        embed.add_field(
            name="⚙️ Admin",
            value="`!setwelcome` `!setlog` `!addword` `!addselfrole` `!toggleai`",
            inline=False,
        )
        embed.add_field(
            name="💬 AI", value="Mention me or reply to me and I'll chat back!", inline=False
        )
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Utility(bot))
