"""Moderation: warnings, kick/ban, timeouts, purge, channel controls, word filter.

The word filter runs from screen(), which the on_message orchestrator in main.py
calls first. Returning True there stops all downstream handling -- no XP, no buy,
no AI reply, no command processing -- exactly as the old sequential on_message did.
"""

import datetime

import discord
from discord.ext import commands

import config
from core.filters import contains_banned
from core.modlog import send_log


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def repo(self):
        return self.bot.repo

    # ========================================================
    #  WORD FILTER (called by the on_message orchestrator)
    # ========================================================
    async def screen(self, message) -> bool:
        """Delete + auto-warn on a banned word. Returns True if it acted."""
        cfg = await self.repo.get_guild_config(message.guild.id)
        banned = config.DEFAULT_BANNED | set(cfg.extra_banned)

        if not contains_banned(message.content, banned):
            return False

        try:
            await message.delete()
        except Exception:
            pass

        count = await self.repo.add_warning(
            message.guild.id, message.author.id, "Banned word"
        )
        try:
            await message.channel.send(
                f"🚫 {message.author.mention} watch your language! (Warning #{count})",
                delete_after=6,
            )
        except Exception:
            pass

        await send_log(
            self.bot,
            message.guild,
            f"⚠️ Auto-warn: **{message.author}** used a banned word.",
        )
        return True

    # ========================================================
    #  WARNINGS
    # ========================================================
    @commands.command()
    @commands.has_permissions(kick_members=True)
    async def warn(self, ctx, member: discord.Member, *, reason="No reason given"):
        count = await self.repo.add_warning(ctx.guild.id, member.id, reason, ctx.author.id)
        await ctx.send(f"⚠️ {member.mention} warned. Reason: {reason} (Total: {count})")
        await send_log(self.bot, ctx.guild, f"⚠️ {ctx.author} warned {member}: {reason}")

    @commands.command()
    async def warnings(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        warns = await self.repo.get_warnings(ctx.guild.id, member.id)
        if not warns:
            await ctx.send(f"✅ {member.mention} has no warnings.")
            return
        desc = "\n".join(f"`{i+1}.` {w.reason}" for i, w in enumerate(warns))
        embed = discord.Embed(
            title=f"⚠️ Warnings for {member.name}",
            description=desc,
            color=discord.Color.orange(),
        )
        await ctx.send(embed=embed)

    @commands.command()
    @commands.has_permissions(kick_members=True)
    async def clearwarnings(self, ctx, member: discord.Member):
        await self.repo.clear_warnings(ctx.guild.id, member.id)
        await ctx.send(f"✅ Cleared all warnings for {member.mention}.")

    # ========================================================
    #  MEMBER ACTIONS
    # ========================================================
    @commands.command()
    @commands.has_permissions(kick_members=True)
    async def kick(self, ctx, member: discord.Member, *, reason="No reason"):
        await member.kick(reason=reason)
        await ctx.send(f"👢 Kicked {member} | {reason}")
        await send_log(self.bot, ctx.guild, f"👢 {ctx.author} kicked {member}: {reason}")

    @commands.command()
    @commands.has_permissions(ban_members=True)
    async def ban(self, ctx, member: discord.Member, *, reason="No reason"):
        await member.ban(reason=reason)
        await ctx.send(f"🔨 Banned {member} | {reason}")
        await send_log(self.bot, ctx.guild, f"🔨 {ctx.author} banned {member}: {reason}")

    @commands.command()
    @commands.has_permissions(ban_members=True)
    async def unban(self, ctx, *, user):
        banned = [entry async for entry in ctx.guild.bans()]
        for entry in banned:
            if user.lower() in entry.user.name.lower() or user == str(entry.user.id):
                await ctx.guild.unban(entry.user)
                await ctx.send(f"✅ Unbanned {entry.user}")
                return
        await ctx.send("❌ Couldn't find that banned user.")

    @commands.command()
    @commands.has_permissions(moderate_members=True)
    async def mute(self, ctx, member: discord.Member, minutes: int = 10, *, reason="No reason"):
        try:
            await member.timeout(datetime.timedelta(minutes=minutes), reason=reason)
            await ctx.send(f"🔇 Muted {member.mention} for {minutes} min. | {reason}")
            await send_log(
                self.bot, ctx.guild, f"🔇 {ctx.author} muted {member} for {minutes}m: {reason}"
            )
        except Exception:
            await ctx.send("❌ Couldn't mute — check my role is above theirs.")

    @commands.command()
    @commands.has_permissions(moderate_members=True)
    async def unmute(self, ctx, member: discord.Member):
        try:
            await member.timeout(None)
            await ctx.send(f"🔊 Unmuted {member.mention}.")
        except Exception:
            await ctx.send("❌ Couldn't unmute.")

    # ========================================================
    #  CHANNEL CONTROLS
    # ========================================================
    @commands.command()
    @commands.has_permissions(manage_messages=True)
    async def purge(self, ctx, amount: int = 5):
        amount = max(1, min(amount, 100))
        deleted = await ctx.channel.purge(limit=amount + 1)
        await ctx.send(f"🧹 Deleted {len(deleted) - 1} messages.", delete_after=4)

    @commands.command()
    @commands.has_permissions(manage_channels=True)
    async def slowmode(self, ctx, seconds: int = 0):
        await ctx.channel.edit(slowmode_delay=seconds)
        await ctx.send(f"🐌 Slowmode set to {seconds}s." if seconds else "✅ Slowmode off.")

    @commands.command()
    @commands.has_permissions(manage_channels=True)
    async def lock(self, ctx):
        await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
        await ctx.send("🔒 Channel locked.")

    @commands.command()
    @commands.has_permissions(manage_channels=True)
    async def unlock(self, ctx):
        await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=None)
        await ctx.send("🔓 Channel unlocked.")


async def setup(bot):
    await bot.add_cog(Moderation(bot))
