"""Self-assignable roles: !role, !roles, !addselfrole."""

import discord
from discord.ext import commands


class Roles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def repo(self):
        return self.bot.repo

    @commands.command()
    async def role(self, ctx, *, role_name):
        cfg = await self.repo.get_guild_config(ctx.guild.id)
        allowed = [r.lower() for r in cfg.self_roles]
        if role_name.lower() not in allowed:
            await ctx.send("❌ That role isn't self-assignable. Type `!roles` to see options.")
            return

        role_obj = discord.utils.get(ctx.guild.roles, name=role_name)
        if not role_obj:
            await ctx.send("❌ That role doesn't exist on the server.")
            return

        try:
            if role_obj in ctx.author.roles:
                await ctx.author.remove_roles(role_obj)
                await ctx.send(f"➖ Removed **{role_name}**.")
            else:
                await ctx.author.add_roles(role_obj)
                await ctx.send(f"➕ Gave you **{role_name}**.")
        except Exception:
            await ctx.send("❌ Couldn't change role — check my permissions.")

    @commands.command()
    async def roles(self, ctx):
        cfg = await self.repo.get_guild_config(ctx.guild.id)
        if not cfg.self_roles:
            await ctx.send("No self-assignable roles set. Admins: `!addselfrole <name>`")
            return
        await ctx.send(
            "🎭 Self-assignable roles:\n" + "\n".join(f"• {r}" for r in cfg.self_roles)
        )

    @commands.command()
    @commands.has_permissions(manage_roles=True)
    async def addselfrole(self, ctx, *, role_name):
        await self.repo.add_self_role(ctx.guild.id, role_name)
        await ctx.send(f"✅ `{role_name}` is now self-assignable.")


async def setup(bot):
    await bot.add_cog(Roles(bot))
