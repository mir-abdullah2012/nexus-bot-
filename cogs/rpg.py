"""RPG core: classes, equipment, the build sheet, salvaging, and prestige.

Class switching is free and unlimited by design. The choice still matters
because every class bonus is applied at the moment a dungeon resolves, and the
resulting cooldown is stored on the run -- so you cannot clear as a Data Miner
for the loot bonus and then switch to Sysadmin to shorten the wait.
"""

import time

import discord
from discord.ext import commands

import config
from core import combat


class RPG(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # user_id -> unix ts of a pending !prestige confirmation
        self._prestige_pending: dict[int, float] = {}

    @property
    def repo(self):
        return self.bot.repo

    # ========================================================
    #  CLASSES
    # ========================================================
    @commands.command(name="class")
    async def class_(self, ctx, *, name: str = None):
        classes = await self.repo.get_classes()

        if name is None:
            player = await self.repo.get_player(ctx.author.id)
            current = (
                await self.repo.get_class(player.class_id) if player.class_id else None
            )
            embed = discord.Embed(
                title="🧬 Classes",
                description=(
                    f"You are: **{current.emoji} {current.name}**"
                    if current
                    else "You haven't picked a class yet — `!class <name>`"
                ),
                color=discord.Color.blurple(),
            )
            for k in classes:
                embed.add_field(
                    name=f"{k.emoji} {k.name}",
                    value=(
                        f"{k.description}\n"
                        f"`PWR {k.base_power} · THRM {k.base_thermals} · "
                        f"CLK {k.base_clock} · BW {k.base_bandwidth}`\n"
                        f"$RAM ×{k.ram_multiplier} · XP ×{k.xp_multiplier} · "
                        f"cooldown ×{k.cooldown_modifier}"
                    ),
                    inline=False,
                )
            embed.set_footer(text="Switching is free and unlimited — !class <name>")
            await ctx.send(embed=embed)
            return

        chosen = await self.repo.find_class(name)
        if chosen is None:
            options = ", ".join(f"`{k.class_id}`" for k in classes)
            await ctx.send(f"❌ No class called **{name}**. Options: {options}")
            return

        player = await self.repo.get_player(ctx.author.id)
        if player.class_id == chosen.class_id:
            await ctx.send(f"You're already a **{chosen.emoji} {chosen.name}**.")
            return

        await self.repo.set_class(ctx.author.id, chosen.class_id)
        await ctx.send(
            f"{chosen.emoji} Rebuilt as a **{chosen.name}**. {chosen.description}"
        )

    # ========================================================
    #  EQUIPMENT
    # ========================================================
    @commands.command()
    async def equip(self, ctx, *, item: str):
        code = item.strip().upper().replace("RTX ", "").replace("RYZEN ", "").strip()

        gear = await self.repo.get_gear(code)
        if gear is None:
            await ctx.send(
                f"❌ **{code}** isn't equippable gear. Check `!inventory` or `!shop`."
            )
            return

        inventory = await self.repo.get_inventory(ctx.author.id)
        if code not in inventory:
            await ctx.send(
                f"❌ You don't own a **{code}**. Buy it with `buy {code}` "
                f"or find one in a dungeon."
            )
            return

        previous = (await self.repo.get_equipped_map(ctx.author.id)).get(gear.slot)
        if previous and previous.item_code == code:
            await ctx.send(f"**{code}** is already in your {gear.slot.upper()} slot.")
            return

        await self.repo.equip(ctx.author.id, code, gear.slot)
        rarity = config.RARITY_EMOJI.get(gear.rarity, "")
        swapped = f" (replaced **{previous.item_code}**)" if previous else ""
        await ctx.send(
            f"🔩 Installed {rarity} **{gear.display_name}** into "
            f"{config.SLOT_LABELS.get(gear.slot, gear.slot)}{swapped}\n"
            f"`{gear.stat_line()}`"
        )

    @commands.command()
    async def unequip(self, ctx, *, slot: str):
        slot = slot.strip().lower()
        if slot not in config.EQUIPMENT_SLOTS:
            valid = ", ".join(f"`{s}`" for s in config.EQUIPMENT_SLOTS)
            await ctx.send(f"❌ Unknown slot. Valid slots: {valid}")
            return
        if await self.repo.unequip(ctx.author.id, slot):
            await ctx.send(f"🔧 Pulled your {config.SLOT_LABELS.get(slot, slot)}.")
        else:
            await ctx.send(f"Nothing installed in {config.SLOT_LABELS.get(slot, slot)}.")

    @commands.command(aliases=["gear"])
    async def build(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        player = await self.repo.get_player(member.id)
        klass = await self.repo.get_class(player.class_id) if player.class_id else None
        equipped = await self.repo.get_equipped_map(member.id)
        stats = combat.compute_stats(player, klass, equipped.values())

        title = combat.prestige_title(player.prestige)
        header = f"🖥️ {member.name}'s Build"
        embed = discord.Embed(title=header, color=discord.Color.blurple())

        embed.add_field(
            name="Operator",
            value=(
                f"{(klass.emoji + ' ' + klass.name) if klass else 'No class — `!class`'}\n"
                f"Level **{player.level}**"
                + (f" · {title}" if title else "")
            ),
            inline=False,
        )

        loadout = []
        for slot in config.EQUIPMENT_SLOTS:
            label = config.SLOT_LABELS.get(slot, slot)
            item = equipped.get(slot)
            if item:
                rarity = config.RARITY_EMOJI.get(item.rarity, "")
                loadout.append(f"{label}: {rarity} **{item.display_name}** — `{item.stat_line()}`")
            else:
                loadout.append(f"{label}: *empty*")
        embed.add_field(name="Loadout", value="\n".join(loadout), inline=False)

        embed.add_field(
            name="Combat Stats",
            value=(
                f"⚔️ **POWER** {stats.power}\n"
                f"❄️ **THERMALS** {stats.thermals}  (HP {stats.hp})\n"
                f"⏱️ **CLOCK** {stats.clock}  (crit {stats.crit_chance:.0f}%)\n"
                f"📡 **BANDWIDTH** {stats.bandwidth}"
            ),
            inline=True,
        )

        breakdown = (
            f"class `{stats.from_class['power']}/{stats.from_class['thermals']}/"
            f"{stats.from_class['clock']}/{stats.from_class['bandwidth']}`\n"
            f"gear `{stats.from_gear['power']}/{stats.from_gear['thermals']}/"
            f"{stats.from_gear['clock']}/{stats.from_gear['bandwidth']}`\n"
            f"level `{stats.from_level['power']}/{stats.from_level['thermals']}/0/0`"
        )
        if player.prestige:
            breakdown += f"\nprestige ×{stats.prestige_multiplier:.2f}"
        embed.add_field(name="Sources (P/T/C/B)", value=breakdown, inline=True)
        embed.set_footer(text="!equip <item> · !unequip <slot> · !dungeon")
        await ctx.send(embed=embed)

    # ========================================================
    #  SALVAGE
    # ========================================================
    @commands.command()
    async def salvage(self, ctx, *, item: str):
        code = item.strip().upper().replace("RTX ", "").replace("RYZEN ", "").strip()

        gear = await self.repo.get_gear(code)
        if gear is None:
            await ctx.send(f"❌ **{code}** isn't salvageable gear.")
            return
        if code not in await self.repo.get_inventory(ctx.author.id):
            await ctx.send(f"❌ You don't own a **{code}**.")
            return
        if await self.repo.is_equipped(ctx.author.id, code):
            await ctx.send(
                f"❌ **{code}** is currently installed. `!unequip {gear.slot}` first."
            )
            return

        await self.repo.remove_item(ctx.author.id, code)
        await self.repo.adjust_balance(
            ctx.author.id, gear.salvage_value, f"salvage:{code}"
        )
        await ctx.send(
            f"🔨 Stripped **{gear.display_name}** for parts. "
            f"+**{gear.salvage_value}GB $RAM** 💾"
        )

    # ========================================================
    #  PRESTIGE
    # ========================================================
    @commands.command()
    async def prestige(self, ctx, confirm: str = None):
        player = await self.repo.get_player(ctx.author.id)

        if player.level < config.PRESTIGE_MIN_LEVEL:
            await ctx.send(
                f"🔒 Prestige unlocks at **Level {config.PRESTIGE_MIN_LEVEL}**. "
                f"You're **{player.level}** — {config.PRESTIGE_MIN_LEVEL - player.level} to go."
            )
            return

        inventory = await self.repo.get_inventory(ctx.author.id)
        next_prestige = player.prestige + 1
        title = combat.prestige_title(next_prestige)

        if confirm is None or confirm.lower() != "confirm":
            self._prestige_pending[ctx.author.id] = time.time()
            embed = discord.Embed(
                title="⚠️ PRESTIGE — FULL SYSTEM WIPE",
                description=(
                    "This tears the whole rig down. **It cannot be undone.**"
                ),
                color=discord.Color.red(),
            )
            embed.add_field(
                name="You will LOSE",
                value=(
                    f"💾 **{player.balance}GB $RAM** (everything)\n"
                    f"📊 **Level {player.level}** → Level 1\n"
                    f"🎒 **{len(inventory)} item(s)** — inventory and loadout wiped"
                ),
                inline=False,
            )
            embed.add_field(
                name="You will GAIN — permanently",
                value=(
                    f"🏅 Prestige **{next_prestige}** · {title}\n"
                    f"⚔️ **+{int(config.PRESTIGE_STAT_BONUS * next_prestige * 100)}%** "
                    f"to all combat stats\n"
                    f"💾 **+{int(config.PRESTIGE_RAM_BONUS * next_prestige * 100)}%** "
                    f"$RAM from mining, dailies and dungeons\n"
                    f"⭐ **+{int(config.PRESTIGE_XP_BONUS * next_prestige * 100)}%** XP"
                ),
                inline=False,
            )
            embed.set_footer(
                text=f"Type !prestige confirm within {config.PRESTIGE_CONFIRM_WINDOW}s "
                     f"to go through with it."
            )
            await ctx.send(embed=embed)
            return

        started = self._prestige_pending.get(ctx.author.id)
        if started is None or time.time() - started > config.PRESTIGE_CONFIRM_WINDOW:
            self._prestige_pending.pop(ctx.author.id, None)
            await ctx.send(
                "⏳ That confirmation expired. Run `!prestige` again to see what "
                "you'd be burning."
            )
            return

        self._prestige_pending.pop(ctx.author.id, None)
        burned = await self.repo.prestige_reset(ctx.author.id)

        embed = discord.Embed(
            title="🔥 SYSTEM WIPED — PRESTIGE COMPLETE",
            description=(
                f"{ctx.author.mention} tore it all down and came back as "
                f"**{combat.prestige_title(burned['new_prestige'])}**."
            ),
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="Burned",
            value=(
                f"Level {burned['level_burned']} · "
                f"{burned['balance_burned']}GB $RAM · "
                f"{burned['items_burned']} item(s)"
            ),
            inline=False,
        )
        embed.add_field(
            name="Permanent bonuses now active",
            value=(
                f"⚔️ +{int(config.PRESTIGE_STAT_BONUS * burned['new_prestige'] * 100)}% stats · "
                f"💾 +{int(config.PRESTIGE_RAM_BONUS * burned['new_prestige'] * 100)}% $RAM · "
                f"⭐ +{int(config.PRESTIGE_XP_BONUS * burned['new_prestige'] * 100)}% XP"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(RPG(bot))
