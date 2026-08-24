"""Gateway events: joins, leaves, message edits/deletes, and the error handler.

These are plain listeners with no early-return coupling, so unlike on_message
they are safe to keep as ordinary Cog listeners.
"""

import discord
from discord.ext import commands

from core.modlog import send_log


class Events(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def repo(self):
        return self.bot.repo

    # ========================================================
    #  READY
    # ========================================================
    @commands.Cog.listener()
    async def on_ready(self):
        print(f"🚀 Bot live as {self.bot.user} | {len(self.bot.guilds)} servers")

    # ========================================================
    #  MEMBERSHIP
    # ========================================================
    @commands.Cog.listener()
    async def on_member_join(self, member):
        try:
            cfg = await self.repo.get_guild_config(member.guild.id)
            if cfg.welcome_channel:
                channel = member.guild.get_channel(cfg.welcome_channel)
                if channel:
                    embed = discord.Embed(
                        title="👋 Welcome!",
                        description=(
                            f"Hey {member.mention}, welcome to **{member.guild.name}**!\n"
                            f"Type `!help` to get started."
                        ),
                        color=discord.Color.green(),
                    )
                    await channel.send(embed=embed)
            await send_log(self.bot, member.guild, f"📥 {member} joined the server.")
        except Exception as e:
            print(f"join error: {e}")

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        try:
            await send_log(self.bot, member.guild, f"📤 {member} left the server.")
        except Exception as e:
            print(f"leave error: {e}")

    # ========================================================
    #  MESSAGE AUDIT
    # ========================================================
    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if message.author and message.author.bot:
            return
        if message.guild:
            content = message.content or "*(no text / embed or image)*"
            await send_log(
                self.bot,
                message.guild,
                f"🗑️ Message by **{message.author}** deleted in "
                f"{message.channel.mention}:\n> {content[:300]}",
            )

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if before.author and before.author.bot:
            return
        if before.guild and before.content != after.content:
            await send_log(
                self.bot,
                before.guild,
                f"✏️ **{before.author}** edited a message in {before.channel.mention}\n"
                f"Before: > {(before.content or '')[:150]}\n"
                f"After: > {(after.content or '')[:150]}",
            )

    # ========================================================
    #  ERRORS
    # ========================================================
    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You don't have permission for that.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"❌ You're missing something: `{error.param.name}`")
        # Order preserved from Nexus 1.x on purpose. MemberNotFound subclasses
        # BadArgument, so BadArgument catches it first and the MemberNotFound
        # branch below never runs. Swapping them would change what users see,
        # so it stays as-is until you decide to fix it.
        elif isinstance(error, commands.BadArgument):
            await ctx.send("❌ That argument doesn't look right.")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("❌ Couldn't find that member.")
        else:
            print(f"Command error: {error}")
            try:
                await ctx.send("⚠️ Something went wrong, try again.")
            except Exception:
                pass


async def setup(bot):
    await bot.add_cog(Events(bot))
