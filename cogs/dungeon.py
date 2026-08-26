"""Dungeons: pick a workload, run the benchmark, take the rewards.

Resolution is instant and text-only -- one command, one embed, no reaction
games or button views. Failing costs nothing but the cooldown: no gear loss,
no currency loss, you simply miss the reward.
"""

import discord
from discord.ext import commands

import config
from core import combat
from core.loadout import build_loadout, grant_pet_xp

OUTCOME_STYLE = {
    "flawless": ("✨ FLAWLESS", discord.Color.gold()),
    "cleared": ("✅ CLEARED", discord.Color.green()),
    "partial": ("⚠️ PARTIAL CLEAR", discord.Color.orange()),
    "wiped": ("💀 THERMAL SHUTDOWN", discord.Color.dark_red()),
}

RESULT_ICON = {"win": "✅", "partial": "🟡", "fail": "❌"}


def human_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


class DungeonCog(commands.Cog, name="Dungeon"):
    def __init__(self, bot):
        self.bot = bot

    @property
    def repo(self):
        return self.bot.repo

    @commands.command(aliases=["dungeons", "adventure"])
    async def dungeon(self, ctx, *, name: str = None):
        player = await self.repo.get_player(ctx.author.id)
        remaining = await self.repo.dungeon_cooldown_remaining(ctx.author.id)

        if name is None:
            await self._show_list(ctx, player, remaining)
            return

        target = await self.repo.find_dungeon(name)
        if target is None:
            await ctx.send(
                f"❌ No dungeon called **{name}**. Run `!dungeon` to see the list."
            )
            return

        if player.level < target.min_level:
            await ctx.send(
                f"🔒 **{target.name}** needs **Level {target.min_level}**. "
                f"You're **{player.level}**."
            )
            return

        if remaining > 0:
            await ctx.send(
                f"⏳ Your rig is still cooling down — **{human_duration(remaining)}** left."
            )
            return

        await self._run(ctx, player, target)

    # ========================================================
    async def _show_list(self, ctx, player, remaining):
        dungeons = await self.repo.get_dungeons()
        stats = (await build_loadout(self.repo, ctx.author.id)).stats

        status = (
            f"⏳ Cooling down — **{human_duration(remaining)}** left"
            if remaining > 0
            else "✅ **Ready to run**"
        )
        embed = discord.Embed(
            title="🗺️ Available Workloads",
            description=(
                f"{status}\n"
                f"Your build: ⚔️ **{stats.power}** POWER · ❄️ **{stats.hp}** HP · "
                f"⏱️ **{stats.crit_chance:.0f}%** crit · 📡 **{stats.bandwidth}** BW"
            ),
            color=discord.Color.blurple(),
        )

        for d in dungeons:
            locked = player.level < d.min_level
            odds = "" if locked else f" · you clear ~{self._clear_odds(stats, d):.0f}%"
            embed.add_field(
                name=(
                    f"{d.emoji} {d.name}"
                    + (f"  🔒 Lvl {d.min_level}" if locked else "")
                ),
                value=(
                    f"*{d.description}*\n"
                    f"`!dungeon {d.dungeon_id}`\n"
                    f"Difficulty **{d.difficulty}** · {d.encounters} encounters · "
                    f"cooldown {human_duration(d.cooldown_seconds)}\n"
                    f"💾 {d.ram_reward_min}–{d.ram_reward_max}GB · ⭐ {d.xp_reward} XP · "
                    f"🎁 {d.drop_chance * 100:.0f}% drop{odds}"
                ),
                inline=False,
            )
        if not player.class_id:
            embed.set_footer(text="You have no class yet — !class for a big head start.")
        await ctx.send(embed=embed)

    @staticmethod
    def _clear_odds(stats, dungeon) -> float:
        """Rough per-encounter win chance, for the listing. score = POWER + d100."""
        needed = dungeon.difficulty - stats.power
        if needed <= 1:
            return 100.0
        if needed > 100:
            return 0.0
        return (100 - needed + 1)

    # ========================================================
    async def _run(self, ctx, player, dungeon):
        lo = await build_loadout(self.repo, ctx.author.id)
        klass, stats = lo.player_class, lo.stats
        loot = await self.repo.get_loot_table(dungeon.dungeon_id)

        result = combat.resolve_run(
            stats, dungeon, klass, loot, prestige=player.prestige
        )

        # An active pet adds its own $RAM bonus on top of prestige. resolve_run
        # already applied the prestige multiplier, so only the pet part is left.
        pet_bonus = combat.pet_ram_multiplier(lo.pet)
        if pet_bonus != 1.0:
            result.ram_earned = int(result.ram_earned * pet_bonus)

        # Persist first, so the cooldown lands even if rendering fails.
        await self.repo.record_dungeon_run(
            ctx.author.id, dungeon.dungeon_id, player.class_id, result
        )
        if result.ram_earned:
            await self.repo.adjust_balance(
                ctx.author.id, result.ram_earned, f"dungeon:{dungeon.dungeon_id}"
            )
            # Clan contribution is EARNED, never deposited -- no bank to drain.
            # No-op for clanless players.
            await self.repo.add_contribution(ctx.author.id, result.ram_earned)

        levelled, new_level, bonus = (False, player.level, 0)
        pet_levelled, pet_after = False, None
        if result.xp_earned:
            levelled, new_level, bonus = await self.repo.add_xp(
                ctx.author.id, result.xp_earned,
                config.xp_for_level, config.LEVEL_UP_BONUS_PER_LEVEL,
            )
            pet_after, pet_levelled, _ = await grant_pet_xp(
                self.repo, ctx.author.id, result.xp_earned
            )

        drop_line = await self._award_drop(ctx.author.id, result)

        title, color = OUTCOME_STYLE.get(result.outcome, ("RESULT", discord.Color.blurple()))
        embed = discord.Embed(
            title=f"{dungeon.emoji} {dungeon.name} — {title}",
            color=color,
        )

        log = []
        for i, enc in enumerate(result.encounters, 1):
            icon = RESULT_ICON.get(enc.result, "•")
            crit = " 💥**CRIT**" if enc.crit else ""
            dmg = f" — took **{enc.damage}** heat" if enc.damage else ""
            log.append(
                f"{icon} **{i}. {enc.name}**{crit}\n"
                f"　`{enc.score}` vs `{enc.threshold}`{dmg}"
            )
        if len(result.encounters) < result.total:
            log.append(f"　*…shut down before encounter {len(result.encounters) + 1}*")
        embed.description = "\n".join(log)

        embed.add_field(
            name="Rig",
            value=(
                f"❄️ HP **{result.hp_remaining}/{result.hp_max}**\n"
                f"Cleared **{result.wins}/{result.total}**"
                + (f" (+{result.partials} partial)" if result.partials else "")
            ),
            inline=True,
        )
        embed.add_field(
            name="Haul",
            value=(
                f"💾 **{result.ram_earned}GB $RAM**\n"
                f"⭐ **{result.xp_earned} XP**"
            ),
            inline=True,
        )
        if drop_line:
            embed.add_field(name="Drop", value=drop_line, inline=False)

        footer = f"Cooldown: {human_duration(result.cooldown_seconds)}"
        if klass:
            footer += f" · ran as {klass.name}"
        if result.outcome == "wiped":
            footer += " · no penalty, you just missed the rest"
        embed.set_footer(text=footer)
        await ctx.send(embed=embed)

        if levelled:
            await ctx.send(
                f"⬆️ **LEVEL UP!** {ctx.author.mention} is now **Level {new_level}**! "
                f"Bonus: +{bonus}GB $RAM 💾"
            )
        if pet_levelled and pet_after is not None:
            await ctx.send(
                f"🐾 **{pet_after.display_name}** reached **Level {pet_after.level}**!"
            )

    async def _award_drop(self, user_id, result):
        """Give the drop. Stackable items stack, duplicate gear salvages."""
        code = result.item_dropped
        if not code:
            return None

        # Stackable items (eggs) have no gear_stats row, so they must be handled
        # before the gear path -- otherwise a second egg would hit ON CONFLICT
        # DO NOTHING and vanish without a trace.
        if await self.repo.is_stackable(code):
            await self.repo.add_item(user_id, code, 1, stackable=True)
            item = await self.repo.get_shop_item(code)
            name = item.display_name if item else code
            held = await self.repo.get_item_quantity(user_id, code)
            return (
                f"🥚 **{name}** dropped! You now hold **{held}**. "
                f"`!pet hatch` to open one."
            )

        gear = await self.repo.get_gear(code)
        name = gear.display_name if gear else code
        rarity = config.RARITY_EMOJI.get(gear.rarity, "") if gear else ""

        owned = code in await self.repo.get_inventory(user_id)
        if owned and gear:
            # Duplicate: auto-salvage rather than silently vanishing.
            await self.repo.adjust_balance(
                user_id, gear.salvage_value, f"dungeon:dupe:{gear.item_code}"
            )
            return (
                f"{rarity} **{name}** — already had one, stripped it for parts: "
                f"+**{gear.salvage_value}GB $RAM**"
            )

        await self.repo.add_item(user_id, code)
        stat_line = f"\n`{gear.stat_line()}`" if gear else ""
        return f"{rarity} **{name}** dropped! `!equip {code}`{stat_line}"


async def setup(bot):
    await bot.add_cog(DungeonCog(bot))
