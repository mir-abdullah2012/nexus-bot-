"""Economy: mining, balance, daily, leaderboard, shop, buying, inventory, give, rob, level.

Every user-facing string is byte-identical to Nexus 1.x. What changed underneath
is storage (SQLite instead of a whole-file JSON rewrite per message) and the fact
that !give and !rob now move both balances inside a single transaction.
"""

import random
import time

import discord
from discord.ext import commands

import config
from core import combat


class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def repo(self):
        return self.bot.repo

    # ========================================================
    #  HOOKS CALLED BY THE on_message ORCHESTRATOR IN main.py
    # ========================================================
    async def grant_xp(self, message):
        """Award XP for a message and announce a level-up.

        Runs for every message including commands, exactly as before. Prestige
        scales the amount, so re-levelling after a wipe is faster each time.
        """
        player = await self.repo.get_player(message.author.id)
        amount = int(
            config.XP_PER_MESSAGE * combat.prestige_xp_multiplier(player.prestige)
        )
        levelled, new_level, bonus = await self.repo.add_xp(
            message.author.id,
            amount,
            config.xp_for_level,
            config.LEVEL_UP_BONUS_PER_LEVEL,
        )
        if not levelled:
            return
        try:
            await message.channel.send(
                f"⬆️ **LEVEL UP!** {message.author.mention} is now **Level {new_level}**! "
                f"Bonus: +{bonus}GB $RAM 💾"
            )
        except Exception:
            pass

    async def try_buy(self, message) -> bool:
        """Handle the prefix-less `buy <model>` shortcut.

        Returns True if the message was a buy attempt. Nexus 1.x stopped
        processing any message starting with "buy " even when the model was
        unknown, so this returns True in that case too.
        """
        if not message.content.lower().startswith("buy "):
            return False
        await self._handle_buy(message)
        return True

    async def _handle_buy(self, message):
        item_raw = message.content[4:].strip().upper()
        clean_name = item_raw.replace("RTX ", "").replace("RYZEN ", "").strip()

        item = await self.repo.get_shop_item(clean_name)
        if item is None:
            return

        bal = await self.repo.get_balance(message.author.id)
        if bal < item.price:
            await message.channel.send(f"❌ Low RAM! You need {item.price - bal}GB more.")
            return

        await self.repo.adjust_balance(message.author.id, -item.price, f"buy:{item.code}")
        await self.repo.add_item(message.author.id, item.code)

        role_name = item.role_name or item.code
        role = discord.utils.get(message.guild.roles, name=role_name)
        if role:
            try:
                await message.author.add_roles(role)
                await message.channel.send(
                    f"📦 **UPGRADE INSTALLED!** Enjoy your **{clean_name}**."
                )
            except Exception:
                await message.channel.send(
                    f"✅ Bought **{clean_name}**, but check my role permissions!"
                )
        else:
            await message.channel.send(
                f"✅ Purchased **{clean_name}**! Admin, create the `{clean_name}` role."
            )

    # ========================================================
    #  COMMANDS
    # ========================================================
    @commands.command()
    async def mine(self, ctx):
        player = await self.repo.get_player(ctx.author.id)
        gain = int(
            random.randint(config.MINE_MIN, config.MINE_MAX)
            * combat.prestige_ram_multiplier(player.prestige)
        )
        await self.repo.adjust_balance(ctx.author.id, gain, "mine")
        await ctx.send(f"🛠️ +{gain}GB $RAM stored. Your PC is getting beefy.")

    @commands.command()
    async def balance(self, ctx):
        player = await self.repo.get_player(ctx.author.id)
        title = combat.prestige_title(player.prestige)
        await ctx.send(
            f"💾 **{ctx.author.name}'s Wallet**"
            + (f"  ·  {title}" if title else "")
            + f"\nBalance: **{player.balance}GB $RAM**\n"
            f"Level: **{player.level}** | XP: **{player.xp}/{config.xp_for_level(player.level)}**"
        )

    @commands.command()
    async def daily(self, ctx):
        player = await self.repo.get_player(ctx.author.id)
        now = time.time()
        if now - player.last_daily < config.DAILY_COOLDOWN:
            remaining = int(config.DAILY_COOLDOWN - (now - player.last_daily))
            await ctx.send(
                f"⏳ Daily already claimed! Come back in "
                f"**{remaining // 3600}h {(remaining % 3600) // 60}m**."
            )
            return

        reward = int(
            random.randint(config.DAILY_MIN, config.DAILY_MAX)
            * combat.prestige_ram_multiplier(player.prestige)
        )
        await self.repo.adjust_balance(ctx.author.id, reward, "daily")
        await self.repo.set_last_daily(ctx.author.id, int(now))
        await ctx.send(f"🎁 Daily claimed! +**{reward}GB $RAM** 💾 Come back tomorrow!")

    @commands.command()
    async def leaderboard(self, ctx):
        top = await self.repo.top_players(config.LEADERBOARD_SIZE)
        embed = discord.Embed(title="🏆 RAM Leaderboard", color=discord.Color.gold())
        medals = ["🥇", "🥈", "🥉"]
        desc = ""
        for i, player in enumerate(top):
            medal = medals[i] if i < 3 else f"`{i+1}.`"
            try:
                user = await self.bot.fetch_user(player.user_id)
                name = user.name
            except Exception:
                name = f"User {player.user_id}"
            desc += f"{medal} **{name}** — {player.balance}GB $RAM (Lvl {player.level})\n"
        embed.description = desc or "No data yet!"
        await ctx.send(embed=embed)

    @commands.command()
    async def shop(self, ctx):
        items = await self.repo.get_shop_items()
        embed = discord.Embed(
            title="🚀 Component Shop",
            description="Type `buy [model]` (no `!`) to upgrade!",
            color=discord.Color.green(),
        )
        # Loops the categories rather than hardcoding gpu/cpu, so cooling and
        # memory show up too. Order follows config.CATEGORY_LABELS.
        seen = []
        for category in list(config.CATEGORY_LABELS) + sorted(
            {i.category for i in items} - set(config.CATEGORY_LABELS)
        ):
            listing = [i for i in items if i.category == category]
            if not listing or category in seen:
                continue
            seen.append(category)
            embed.add_field(
                name=config.CATEGORY_LABELS.get(category, category.upper()),
                value="\n".join(f"**{i.code}**: {i.price}GB" for i in listing),
                inline=True,
            )
        embed.set_footer(text="Gear grants combat stats — !equip <item> · !build")
        await ctx.send(embed=embed)

    @commands.command()
    async def inventory(self, ctx):
        inv = await self.repo.get_inventory(ctx.author.id)
        if not inv:
            await ctx.send(f"🎒 {ctx.author.mention}'s inventory is empty. Go buy some gear!")
        else:
            await ctx.send(
                f"🎒 **{ctx.author.name}'s Inventory:**\n"
                + "\n".join(f"• {i}" for i in inv)
            )

    @commands.command()
    async def give(self, ctx, member: discord.Member, amount: int):
        if amount <= 0:
            await ctx.send("❌ Amount must be positive")
            return
        if await self.repo.get_balance(ctx.author.id) < amount:
            await ctx.send("❌ You don't have enough RAM")
            return
        await self.repo.transfer(ctx.author.id, member.id, amount, "give")
        await ctx.send(
            f"💸 {ctx.author.mention} gave **{amount}GB $RAM** to {member.mention}!"
        )

    @commands.command()
    async def rob(self, ctx, member: discord.Member):
        if member.id == ctx.author.id:
            await ctx.send("💀 You can't rob yourself bro")
            return
        if member.bot:
            await ctx.send("🤖 Can't rob a bot lmao")
            return

        target_balance = await self.repo.get_balance(member.id)
        if target_balance < config.ROB_MIN_TARGET_BALANCE:
            await ctx.send(f"😭 {member.name} is broke, nothing to rob")
            return

        if random.random() < config.ROB_SUCCESS_CHANCE:
            stolen = random.randint(
                config.ROB_STEAL_MIN, min(config.ROB_STEAL_MAX, target_balance)
            )
            await self.repo.transfer(member.id, ctx.author.id, stolen, "rob:win")
            await ctx.send(
                f"🦹 **ROBBERY SUCCESS!** You stole **{stolen}GB $RAM** "
                f"from {member.mention} 😈"
            )
        else:
            fine = random.randint(config.ROB_FINE_MIN, config.ROB_FINE_MAX)
            await self.repo.adjust_balance(ctx.author.id, -fine, "rob:fine", member.id)
            await ctx.send(f"🚔 **CAUGHT!** Fined **{fine}GB $RAM** 💀")

    @commands.command()
    async def level(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        player = await self.repo.get_player(member.id)
        title = combat.prestige_title(player.prestige)
        klass = await self.repo.get_class(player.class_id) if player.class_id else None
        extra = ""
        if klass:
            extra += f"\nClass: **{klass.emoji} {klass.name}**"
        if title:
            extra += f"\nPrestige: **{player.prestige}** · {title}"
        await ctx.send(
            f"📊 **{member.name}'s Stats**\n"
            f"Level: **{player.level}**\n"
            f"XP: **{player.xp}/{config.xp_for_level(player.level)}**\n"
            f"Balance: **{player.balance}GB $RAM**" + extra
        )


async def setup(bot):
    await bot.add_cog(Economy(bot))
