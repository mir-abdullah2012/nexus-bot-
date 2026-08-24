"""Admin config commands: !setwelcome, !setlog, !addword, !toggleai.

These write to guild_config (and its child tables) and invalidate the repository's
config cache, so a change takes effect on the very next message.
"""

import discord
from discord.ext import commands

import config


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def repo(self):
        return self.bot.repo

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def setwelcome(self, ctx, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        await self.repo.set_guild_field(ctx.guild.id, "welcome_channel", channel.id)
        await ctx.send(f"✅ Welcome messages will go to {channel.mention}.")

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def setlog(self, ctx, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        await self.repo.set_guild_field(ctx.guild.id, "log_channel", channel.id)
        await ctx.send(f"✅ Logs will go to {channel.mention}.")

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def addword(self, ctx, *, word):
        await self.repo.add_banned_word(ctx.guild.id, word, ctx.author.id)
        try:
            await ctx.message.delete()
        except Exception:
            pass
        await ctx.send("✅ Word added to the filter.", delete_after=5)

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def testmoney(self, ctx, amount: int = config.TESTMONEY_DEFAULT):
        """Testing tool: mint $RAM into your own balance. Admin only.

        Self-only on purpose -- it cannot target another member, so the blast
        radius is one account. Negative amounts work too, for testing the
        broke-player paths.

        The amount is clamped to config.MAX_BALANCE. SQLite INTEGER is signed
        64-bit and overflow there does not raise -- it quietly turns the column
        into a float -- so the clamp is what keeps a fat-fingered number from
        corrupting the balance.
        """
        player = await self.repo.get_player(ctx.author.id)
        requested = amount

        headroom = config.MAX_BALANCE - player.balance
        granted = max(-player.balance, min(amount, headroom))

        if granted == 0:
            await ctx.send(
                f"⚠️ Nothing to do — you're already at the "
                f"{config.MAX_BALANCE:,}GB test ceiling."
            )
            return

        new_balance = await self.repo.adjust_balance(
            ctx.author.id, granted, "testmoney"
        )

        note = ""
        if granted != requested:
            note = (
                f"\n⚠️ Clamped from `{requested:,}` — the ceiling is "
                f"**{config.MAX_BALANCE:,}GB** (SQLite's 64-bit integer limit)."
            )
        await ctx.send(
            f"🧪 **TEST GRANT** +{granted:,}GB $RAM\n"
            f"Balance: **{new_balance:,}GB $RAM**{note}"
        )

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def toggleai(self, ctx):
        cfg = await self.repo.get_guild_config(ctx.guild.id)
        new = not cfg.ai_enabled
        await self.repo.set_guild_field(ctx.guild.id, "ai_enabled", 1 if new else 0)
        await ctx.send(f"🤖 AI chat is now **{'ON' if new else 'OFF'}**.")


async def setup(bot):
    await bot.add_cog(Admin(bot))
