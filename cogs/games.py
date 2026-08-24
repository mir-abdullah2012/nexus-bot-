"""Mini games: coinflip, slots, rock-paper-scissors.

Odds, payouts and wording are carried over from Nexus 1.x unchanged.
"""

import random

from discord.ext import commands

import config


class Games(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def repo(self):
        return self.bot.repo

    @commands.command()
    async def coinflip(self, ctx, bet: int = 0):
        if bet < 0:
            await ctx.send("❌ Bet can't be negative lol")
            return
        if bet > await self.repo.get_balance(ctx.author.id):
            await ctx.send("❌ You don't have that much $RAM, broke ahh")
            return

        flip = random.choice(["heads", "tails"])
        # Carried over from Nexus 1.x as-is: the player never calls heads or
        # tails, so this is an unconditional 50/50 against a second draw.
        won = random.choice(["heads", "tails"]) == flip

        if bet > 0:
            await self.repo.adjust_balance(
                ctx.author.id, bet if won else -bet, "coinflip"
            )

        out = f"🪙 Landed on **{flip}**! You {'**WON**' if won else '**LOST**'}"
        if bet > 0:
            out += f" **{bet}GB $RAM**!"
        await ctx.send(out)

    @commands.command()
    async def slots(self, ctx, bet: int = 10):
        if bet <= 0:
            await ctx.send("❌ Bet must be positive")
            return
        if bet > await self.repo.get_balance(ctx.author.id):
            await ctx.send("❌ You don't have that much $RAM")
            return

        reels = [random.choice(config.SLOT_EMOJIS) for _ in range(3)]
        await self.repo.adjust_balance(ctx.author.id, -bet, "slots:stake")

        if reels[0] == reels[1] == reels[2]:
            win = bet * config.SLOTS_JACKPOT_MULTIPLIER
            await self.repo.adjust_balance(ctx.author.id, win, "slots:jackpot")
            await ctx.send(
                f"🎰 **JACKPOT!!!** {' '.join(reels)}\n+**{win}GB $RAM** 🤑"
            )
        elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
            win = bet * config.SLOTS_PAIR_MULTIPLIER
            await self.repo.adjust_balance(ctx.author.id, win, "slots:pair")
            await ctx.send(
                f"🎰 **Two of a kind!** {' '.join(reels)}\n+**{win}GB $RAM** 😎"
            )
        else:
            await ctx.send(f"🎰 {' '.join(reels)}\n-**{bet}GB $RAM** 💀")

    @commands.command()
    async def rps(self, ctx, choice: str, bet: int = 0):
        choices = {"rock": "✊", "paper": "📄", "scissors": "✂️"}
        choice = choice.lower()
        if choice not in choices:
            await ctx.send("❌ Pick rock, paper, or scissors!")
            return
        if bet > 0 and bet > await self.repo.get_balance(ctx.author.id):
            await ctx.send("❌ Not enough RAM for that bet")
            return

        bot_choice = random.choice(list(choices))
        beats = {"rock": "scissors", "paper": "rock", "scissors": "paper"}

        if choice == bot_choice:
            result = "🤝 **Tie!**"
        elif beats[choice] == bot_choice:
            if bet > 0:
                await self.repo.adjust_balance(ctx.author.id, bet, "rps:win")
            result = f"✅ **You win!**{f' +{bet}GB $RAM' if bet > 0 else ''}"
        else:
            if bet > 0:
                await self.repo.adjust_balance(ctx.author.id, -bet, "rps:loss")
            result = f"❌ **You lose!**{f' -{bet}GB $RAM' if bet > 0 else ''}"

        await ctx.send(f"{choices[choice]} vs {choices[bot_choice]}\n{result}")


async def setup(bot):
    await bot.add_cog(Games(bot))
