"""PvP duels.

Reuses the Phase 2 combat system rather than paralleling it: identical
compute_stats(), identical crit roll, identical HP = BASE_HP + THERMALS. The
only new logic is core.combat.resolve_duel(), which swaps the fixed dungeon
threshold for an opposed roll between two players.

Elo is what keeps this honest -- beating someone 400 points below you is worth
about one rating point, so farming a weak alt earns nothing.
"""

import time

import discord
from discord.ext import commands

import config
from core import combat


def human_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


class PendingDuel:
    __slots__ = ("challenger_id", "opponent_id", "wager", "channel_id", "created_at")

    def __init__(self, challenger_id, opponent_id, wager, channel_id):
        self.challenger_id = challenger_id
        self.opponent_id = opponent_id
        self.wager = wager
        self.channel_id = channel_id
        self.created_at = time.time()

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > config.DUEL_CHALLENGE_TIMEOUT


class Duel(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # opponent_id -> PendingDuel. In-memory on purpose: a 120s challenge
        # does not need to survive a restart.
        self._pending: dict[int, PendingDuel] = {}

    @property
    def repo(self):
        return self.bot.repo

    async def _build_stats(self, user_id):
        player = await self.repo.get_player(user_id)
        klass = await self.repo.get_class(player.class_id) if player.class_id else None
        equipped = await self.repo.get_equipped_map(user_id)
        return player, klass, combat.compute_stats(player, klass, equipped.values())

    # ========================================================
    #  CHALLENGE
    # ========================================================
    @commands.group(invoke_without_command=True)
    async def duel(self, ctx, member: discord.Member = None, wager: int = 0):
        if member is None:
            await ctx.send(
                "⚔️ `!duel @user [wager]` to challenge · `!duel accept` / "
                "`!duel decline` · `!duelstats` · `!duelrank`"
            )
            return
        if member.id == ctx.author.id:
            await ctx.send("💀 You can't duel yourself.")
            return
        if member.bot:
            await ctx.send("🤖 Bots don't duel.")
            return

        challenger = await self.repo.get_player(ctx.author.id)
        opponent = await self.repo.get_player(member.id)
        if challenger.level < config.DUEL_MIN_LEVEL:
            await ctx.send(f"🔒 Duelling unlocks at **Level {config.DUEL_MIN_LEVEL}**.")
            return
        if opponent.level < config.DUEL_MIN_LEVEL:
            await ctx.send(
                f"🔒 {member.display_name} needs to be **Level "
                f"{config.DUEL_MIN_LEVEL}** to duel."
            )
            return

        remaining = await self.repo.duel_cooldown_remaining(
            ctx.author.id, config.DUEL_COOLDOWN
        )
        if remaining > 0:
            await ctx.send(f"⏳ You're still cooling down — **{human_duration(remaining)}**.")
            return

        if wager < 0:
            await ctx.send("❌ Wager can't be negative.")
            return
        wager = min(wager, config.DUEL_MAX_WAGER)
        if wager > challenger.balance:
            await ctx.send(
                f"❌ You only have **{challenger.balance:,}GB $RAM**."
            )
            return
        if wager > opponent.balance:
            await ctx.send(
                f"❌ {member.display_name} only has **{opponent.balance:,}GB** — "
                f"lower the wager."
            )
            return

        existing = self._pending.get(member.id)
        if existing and not existing.expired:
            await ctx.send(f"❌ {member.display_name} already has a pending challenge.")
            return

        self._pending[member.id] = PendingDuel(
            ctx.author.id, member.id, wager, ctx.channel.id
        )

        _, a_class, a_stats = await self._build_stats(ctx.author.id)
        _, b_class, b_stats = await self._build_stats(member.id)

        embed = discord.Embed(
            title="⚔️ DUEL CHALLENGE",
            description=(
                f"{ctx.author.mention} challenges {member.mention}!"
                + (f"\n💾 Wager: **{wager:,}GB $RAM** each" if wager else
                   "\n*Rating only — no wager.*")
            ),
            color=discord.Color.orange(),
        )
        # Show both builds up front. A big POWER gap makes duels near-deterministic,
        # so the person accepting should be able to see what they are accepting.
        for user, klass, stats in (
            (ctx.author, a_class, a_stats), (member, b_class, b_stats)
        ):
            embed.add_field(
                name=f"{user.display_name}",
                value=(
                    f"{(klass.emoji + ' ' + klass.name) if klass else 'No class'}\n"
                    f"⚔️ **{stats.power}** POWER\n"
                    f"❄️ **{stats.hp}** HP\n"
                    f"⏱️ **{stats.crit_chance:.0f}%** crit"
                ),
                inline=True,
            )
        embed.set_footer(
            text=f"{member.display_name}: !duel accept  or  !duel decline "
                 f"({config.DUEL_CHALLENGE_TIMEOUT}s)"
        )
        await ctx.send(embed=embed)

    @duel.command(name="decline")
    async def duel_decline(self, ctx):
        pending = self._pending.get(ctx.author.id)
        if pending is None or pending.expired:
            self._pending.pop(ctx.author.id, None)
            await ctx.send("Nothing to decline.")
            return
        self._pending.pop(ctx.author.id, None)
        await ctx.send(f"🚪 {ctx.author.display_name} declined the duel.")

    @duel.command(name="accept")
    async def duel_accept(self, ctx):
        pending = self._pending.get(ctx.author.id)
        if pending is None:
            await ctx.send("Nobody has challenged you. `!duel @user` to start one.")
            return
        if pending.expired:
            self._pending.pop(ctx.author.id, None)
            await ctx.send("⏳ That challenge expired.")
            return

        self._pending.pop(ctx.author.id, None)

        challenger_id, opponent_id, wager = (
            pending.challenger_id, pending.opponent_id, pending.wager
        )

        # Re-check everything: balances and cooldowns can have moved since the
        # challenge was issued.
        a_player, a_class, a_stats = await self._build_stats(challenger_id)
        b_player, b_class, b_stats = await self._build_stats(opponent_id)

        if wager > a_player.balance or wager > b_player.balance:
            await ctx.send("❌ Someone can no longer cover the wager. Duel cancelled.")
            return
        if await self.repo.duel_cooldown_remaining(challenger_id, config.DUEL_COOLDOWN):
            await ctx.send("⏳ The challenger went on cooldown. Duel cancelled.")
            return

        result = combat.resolve_duel(a_stats, b_stats)

        a_stats_rec = await self.repo.get_duel_stats(challenger_id, config.ELO_START)
        b_stats_rec = await self.repo.get_duel_stats(opponent_id, config.ELO_START)

        if result.winner == "draw":
            score_a, a_outcome, b_outcome = 0.5, "draw", "draw"
            winner_id = None
        elif result.winner == "a":
            score_a, a_outcome, b_outcome = 1.0, "win", "loss"
            winner_id = challenger_id
        else:
            score_a, a_outcome, b_outcome = 0.0, "loss", "win"
            winner_id = opponent_id

        delta = combat.elo_change(a_stats_rec.rating, b_stats_rec.rating, score_a)
        new_a = await self.repo.apply_duel_result(challenger_id, delta, a_outcome)
        new_b = await self.repo.apply_duel_result(opponent_id, -delta, b_outcome)

        if wager and winner_id is not None:
            loser_id = opponent_id if winner_id == challenger_id else challenger_id
            await self.repo.transfer(loser_id, winner_id, wager, "duel:wager")
            await self.repo.add_contribution(winner_id, wager)

        await self.repo.record_duel(
            challenger_id, opponent_id, winner_id, wager, result, delta
        )

        await self._render(
            ctx, challenger_id, opponent_id, a_class, b_class,
            result, wager, delta, new_a, new_b, winner_id,
        )

    # ========================================================
    async def _render(self, ctx, a_id, b_id, a_class, b_class, result,
                      wager, delta, new_a, new_b, winner_id):
        try:
            a_user = await self.bot.fetch_user(a_id)
            b_user = await self.bot.fetch_user(b_id)
            a_name, b_name = a_user.display_name, b_user.display_name
        except Exception:
            a_name, b_name = f"User {a_id}", f"User {b_id}"

        if result.winner == "draw":
            title, color = "🤝 DRAW", discord.Color.light_grey()
        else:
            winner_name = a_name if result.winner == "a" else b_name
            title = f"🏆 {winner_name} WINS"
            color = discord.Color.green()

        embed = discord.Embed(title=title, color=color)

        log = []
        for r in result.rounds:
            a_mark = " 💥" if r.a_crit else ""
            b_mark = " 💥" if r.b_crit else ""
            if r.winner == "tie":
                verdict = "— tie"
            else:
                hit = a_name if r.winner == "a" else b_name
                verdict = f"→ **{hit}** hits for **{r.damage}**"
            log.append(
                f"**R{r.index}** `{r.a_score}`{a_mark} vs `{r.b_score}`{b_mark} {verdict}\n"
                f"　{a_name} {r.a_hp}/{result.a_hp_max} · {b_name} {r.b_hp}/{result.b_hp_max}"
            )
        embed.description = "\n".join(log)

        decided = {
            "knockout": "knockout",
            "rounds": f"rounds won {result.a_rounds_won}–{result.b_rounds_won}",
            "health": "remaining HP",
            "draw": "dead even",
        }[result.decided_by]
        embed.add_field(name="Decided by", value=decided, inline=True)

        if wager and winner_id is not None:
            embed.add_field(name="Pot", value=f"💾 **{wager:,}GB $RAM**", inline=True)
        elif wager:
            embed.add_field(name="Pot", value="Returned — draw", inline=True)

        embed.add_field(
            name="Rating",
            value=(
                f"{a_name}: **{new_a}** ({delta:+d})\n"
                f"{b_name}: **{new_b}** ({-delta:+d})"
            ),
            inline=False,
        )
        embed.set_footer(text=f"Cooldown: {human_duration(config.DUEL_COOLDOWN)}")
        await ctx.send(embed=embed)

    # ========================================================
    #  STATS
    # ========================================================
    @commands.command()
    async def duelstats(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        stats = await self.repo.get_duel_stats(member.id, config.ELO_START)
        _, klass, combat_stats = await self._build_stats(member.id)

        if stats.streak > 0:
            streak = f"🔥 {stats.streak} win streak"
        elif stats.streak < 0:
            streak = f"❄️ {abs(stats.streak)} loss streak"
        else:
            streak = "—"

        embed = discord.Embed(
            title=f"⚔️ {member.display_name}'s Duel Record",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Rating", value=f"**{stats.rating}**", inline=True
        )
        embed.add_field(
            name="Record",
            value=f"**{stats.wins}**W / **{stats.losses}**L / **{stats.draws}**D",
            inline=True,
        )
        embed.add_field(name="Win rate", value=f"{stats.winrate:.0f}%", inline=True)
        embed.add_field(name="Streak", value=streak, inline=True)
        embed.add_field(name="Best streak", value=f"{stats.best_streak}", inline=True)
        embed.add_field(
            name="Build",
            value=(
                f"{(klass.emoji + ' ' + klass.name) if klass else 'No class'} · "
                f"⚔️ {combat_stats.power} · ❄️ {combat_stats.hp} HP"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

    @commands.command(aliases=["duelrank", "pvprank"])
    async def duelboard(self, ctx):
        top = await self.repo.top_duelists(10)
        if not top:
            await ctx.send("Nobody has duelled yet. `!duel @user` to open the ladder.")
            return
        embed = discord.Embed(title="🏅 Duel Ladder", color=discord.Color.gold())
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, s in enumerate(top):
            rank = medals[i] if i < 3 else f"`{i+1}.`"
            try:
                user = await self.bot.fetch_user(s.user_id)
                name = user.name
            except Exception:
                name = f"User {s.user_id}"
            lines.append(
                f"{rank} **{name}** — {s.rating} "
                f"({s.wins}W/{s.losses}L, {s.winrate:.0f}%)"
            )
        embed.description = "\n".join(lines)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Duel(bot))
