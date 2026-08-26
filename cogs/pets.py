"""Pets -- the !pet command family.

Acquisition rides entirely on systems that already exist: an egg is a
shop_items row, so buying one goes through !buy and finding one goes through the
dungeon loot table. Nothing new was needed to get a pet into a player's hands.

Progression is passive. The active pet earns a share of whatever XP you were
already earning, so pets never become a second grind bolted onto dungeons.
There is deliberately no feeding or happiness decay -- those punish you for not
logging in.
"""

import random
import time

import discord
from discord.ext import commands

import config
from core import combat
from core.filters import contains_banned


class Pets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._release_pending: dict[int, tuple] = {}   # user_id -> (pet_id, ts)

    @property
    def repo(self):
        return self.bot.repo

    # ========================================================
    #  VIEW
    # ========================================================
    @commands.group(name="pet", aliases=["pets"], invoke_without_command=True)
    async def pet(self, ctx):
        active = await self.repo.get_active_pet(ctx.author.id)
        if active is None:
            owned = await self.repo.count_pets(ctx.author.id)
            eggs = await self.repo.get_item_quantity(ctx.author.id, config.EGG_ITEM_CODE)
            hint = (
                f"You own **{owned}** pet(s) — `!pet list` and `!pet active <id>`."
                if owned else
                (f"You have **{eggs}** egg(s) — `!pet hatch` to open one."
                 if eggs else
                 f"No pets yet. Buy an egg with `buy {config.EGG_ITEM_CODE}` "
                 f"({config.EGG_PRICE:,}GB) or find one in a dungeon.")
            )
            await ctx.send(f"🥚 No active companion.\n{hint}")
            return
        await ctx.send(embed=await self._pet_embed(ctx.author, active))

    async def _pet_embed(self, user, pet):
        contribution = combat.pet_contribution(pet)
        rarity = config.RARITY_EMOJI.get(pet.species.rarity, "")
        at_cap = pet.level >= config.PET_MAX_LEVEL

        embed = discord.Embed(
            title=f"{pet.emoji} {pet.display_name}",
            description=f"*{pet.species.description}*",
            color=discord.Color.teal(),
        )
        embed.add_field(
            name="Species",
            value=f"{rarity} {pet.species.name} · `{pet.species.rarity}`",
            inline=True,
        )
        embed.add_field(
            name="Level",
            value=(
                f"**{pet.level}**/{config.PET_MAX_LEVEL}"
                + ("  ⭐ MAX" if at_cap else "")
            ),
            inline=True,
        )
        embed.add_field(
            name="XP",
            value=("—" if at_cap
                   else f"{pet.xp}/{config.pet_xp_for_level(pet.level)}"),
            inline=True,
        )

        stat_bits = [
            f"⚔️ POWER +{contribution['power']}" if contribution["power"] else None,
            f"❄️ THERMALS +{contribution['thermals']}" if contribution["thermals"] else None,
            f"⏱️ CLOCK +{contribution['clock']}" if contribution["clock"] else None,
            f"📡 BANDWIDTH +{contribution['bandwidth']}" if contribution["bandwidth"] else None,
        ]
        embed.add_field(
            name="Contributing",
            value="\n".join(b for b in stat_bits if b) or "nothing yet — level it up",
            inline=False,
        )
        bonus = (combat.pet_ram_multiplier(pet) - 1) * 100
        embed.add_field(
            name="Passive", value=f"💾 +{bonus:.1f}% $RAM from mining, dailies and dungeons",
            inline=False,
        )
        embed.set_footer(text=f"id {pet.pet_id} · earns {int(config.PET_XP_SHARE * 100)}% "
                              f"of the XP you earn")
        return embed

    @pet.command(name="list")
    async def pet_list(self, ctx):
        pets = await self.repo.get_pets(ctx.author.id)
        if not pets:
            await ctx.send("You don't own any pets yet. `!pet hatch` if you have an egg.")
            return

        active = await self.repo.get_active_pet(ctx.author.id)
        active_id = active.pet_id if active else None

        lines = []
        for p in pets:
            rarity = config.RARITY_EMOJI.get(p.species.rarity, "")
            marker = "**▸**" if p.pet_id == active_id else "　"
            lines.append(
                f"{marker} `{p.pet_id}` {rarity} {p.emoji} **{p.display_name}** — "
                f"Lv {p.level}"
                + (f" *({p.species.name})*" if p.name else "")
            )
        embed = discord.Embed(
            title=f"🐾 {ctx.author.name}'s Companions "
                  f"({len(pets)}/{config.PET_MAX_OWNED})",
            description="\n".join(lines),
            color=discord.Color.teal(),
        )
        embed.set_footer(text="▸ = active · !pet active <id> · !pet name <text>")
        await ctx.send(embed=embed)

    @pet.command(name="species", aliases=["dex", "collection"])
    async def pet_species(self, ctx):
        species = await self.repo.get_species_list()
        owned = {p.species_id for p in await self.repo.get_pets(ctx.author.id)}
        total_weight = sum(s.hatch_weight for s in species) or 1

        embed = discord.Embed(
            title="📖 Species Catalogue",
            description=f"Collected **{len(owned)}/{len(species)}**",
            color=discord.Color.teal(),
        )
        for s in species:
            have = "✅" if s.species_id in owned else "🔒"
            rarity = config.RARITY_EMOJI.get(s.rarity, "")
            stats = " · ".join(
                f"{k} +{v}" for k, v in (
                    ("PWR", s.base_power), ("THRM", s.base_thermals),
                    ("CLK", s.base_clock), ("BW", s.base_bandwidth),
                ) if v
            ) or "—"
            embed.add_field(
                name=f"{have} {s.emoji} {s.name} {rarity}",
                value=(
                    f"*{s.description}*\n"
                    f"`{stats}` · hatch {100 * s.hatch_weight / total_weight:.1f}%"
                ),
                inline=False,
            )
        await ctx.send(embed=embed)

    # ========================================================
    #  HATCH
    # ========================================================
    @pet.command(name="hatch")
    async def pet_hatch(self, ctx):
        owned = await self.repo.count_pets(ctx.author.id)
        if owned >= config.PET_MAX_OWNED:
            await ctx.send(
                f"❌ You're at the limit of **{config.PET_MAX_OWNED}** pets. "
                f"`!pet release <id>` to make room."
            )
            return

        eggs = await self.repo.get_item_quantity(ctx.author.id, config.EGG_ITEM_CODE)
        if eggs < 1:
            await ctx.send(
                f"❌ No eggs. Buy one with `buy {config.EGG_ITEM_CODE}` "
                f"({config.EGG_PRICE:,}GB $RAM) or find one in a dungeon."
            )
            return

        if not await self.repo.consume_item(ctx.author.id, config.EGG_ITEM_CODE, 1):
            await ctx.send("❌ Couldn't consume the egg. Try again.")
            return

        pet = await self.repo.hatch_pet(ctx.author.id, random)
        if pet is None:
            await ctx.send("⚠️ Something went wrong hatching — no species available.")
            return

        # First pet auto-equips; there is no reason to make someone run a second
        # command to use the only companion they own.
        first = owned == 0
        if first:
            await self.repo.set_active_pet(ctx.author.id, pet.pet_id)

        rarity = config.RARITY_EMOJI.get(pet.species.rarity, "")
        embed = discord.Embed(
            title="🥚 …it's hatching…",
            description=(
                f"# {pet.emoji} {pet.species.name}\n"
                f"{rarity} **{pet.species.rarity.upper()}**\n\n"
                f"*{pet.species.description}*"
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(
            text=(f"id {pet.pet_id} · now active · !pet name <text> to name it"
                  if first else
                  f"id {pet.pet_id} · !pet active {pet.pet_id} to equip it")
        )
        await ctx.send(embed=embed)
        remaining = await self.repo.get_item_quantity(ctx.author.id, config.EGG_ITEM_CODE)
        if remaining:
            await ctx.send(f"🥚 {remaining} egg(s) left.", delete_after=8)

    # ========================================================
    #  MANAGE
    # ========================================================
    @pet.command(name="active", aliases=["equip", "use"])
    async def pet_active(self, ctx, pet_id: int):
        pet = await self.repo.get_pet(pet_id)
        if pet is None or pet.user_id != ctx.author.id:
            await ctx.send(f"❌ You don't own a pet with id `{pet_id}`. `!pet list`")
            return
        await self.repo.set_active_pet(ctx.author.id, pet_id)
        await ctx.send(f"🐾 {pet.emoji} **{pet.display_name}** is now your companion.")

    @pet.command(name="name", aliases=["rename"])
    async def pet_name(self, ctx, *, text: str):
        pet = await self.repo.get_active_pet(ctx.author.id)
        if pet is None:
            await ctx.send("❌ No active pet. `!pet active <id>` first.")
            return

        text = " ".join(text.split())
        if text.lower() in ("reset", "clear", "none"):
            await self.repo.rename_pet(pet.pet_id, None)
            await ctx.send(f"✅ Name cleared — back to **{pet.species.name}**.")
            return
        if len(text) > config.PET_NAME_MAX:
            await ctx.send(f"❌ Max {config.PET_NAME_MAX} characters.")
            return
        # Same filter the rest of the bot uses, so naming can't become a bypass.
        if contains_banned(text, config.DEFAULT_BANNED):
            await ctx.send("❌ Pick a different name.")
            return

        await self.repo.rename_pet(pet.pet_id, text)
        await ctx.send(f"✅ {pet.emoji} Renamed to **{text}**.")

    # ========================================================
    #  HOUSE + SLEEP
    # ========================================================
    @pet.command(name="house", aliases=["home"])
    async def pet_house(self, ctx):
        home = await self.repo.get_home(ctx.author.id)
        active = await self.repo.get_active_pet(ctx.author.id)
        sleeps = active.sleep_count if active else 0
        chance = combat.sleep_chance(home, sleeps)

        embed = discord.Embed(
            title=f"🏠 {ctx.author.name}'s Pet Home",
            color=discord.Color.teal(),
        )

        rows = []
        for slot in config.HOME_SLOTS:
            label = config.HOME_SLOT_LABELS.get(slot, slot)
            item = home.get(slot)
            if item:
                rows.append(
                    f"{label}: **{item.display_name}** "
                    f"(+{item.sleep_bonus * 100:.0f}%)\n　*{item.description}*"
                )
            else:
                rows.append(f"{label}: *empty*")
        embed.add_field(name="Furnishings", value="\n".join(rows), inline=False)

        rest = min(config.SLEEP_REST_CAP, sleeps * config.SLEEP_REST_PER_SLEEP)
        breakdown = [f"base **{config.SLEEP_BASE_CHANCE * 100:.0f}%**"]
        for slot in config.HOME_SLOTS:
            item = home.get(slot)
            if item:
                breakdown.append(f"{slot} **+{item.sleep_bonus * 100:.0f}%**")
        breakdown.append(
            f"rest **+{rest * 100:.1f}%** ({sleeps} sleep(s))"
        )
        capped = " *(capped)*" if chance >= config.SLEEP_MAX_CHANCE else ""
        embed.add_field(
            name="Sleep chance",
            value=f"# {chance * 100:.0f}%{capped}\n" + " · ".join(breakdown),
            inline=False,
        )

        if active:
            embed.add_field(
                name="Resident",
                value=(
                    f"{active.emoji} **{active.display_name}** (Lv {active.level})\n"
                    f"Sleep bonuses earned: **{active.total_sleep_bonus}"
                    f"/{config.SLEEP_BONUS_CAP}** "
                    f"(THRM +{active.bonus_thermals} · CLK +{active.bonus_clock} · "
                    f"BW +{active.bonus_bandwidth})"
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="Resident", value="*No active pet* — `!pet active <id>`",
                inline=False,
            )

        embed.set_footer(
            text="!pet place <item> · !pet sleep · buy DRIVEBAY / FOAM / SILICA"
        )
        await ctx.send(embed=embed)

    @pet.command(name="place")
    async def pet_place(self, ctx, *, item: str):
        code = item.strip().upper()
        home_item = await self.repo.get_home_item(code)
        if home_item is None:
            await ctx.send(
                f"❌ **{code}** isn't a house or furniture. See `!shop` for what fits."
            )
            return
        if code not in await self.repo.get_inventory(ctx.author.id):
            await ctx.send(f"❌ You don't own a **{code}**. `buy {code}` to get one.")
            return

        previous = await self.repo.place_home_item(ctx.author.id, code, home_item.slot)
        label = config.HOME_SLOT_LABELS.get(home_item.slot, home_item.slot)
        swapped = f" (replaced **{previous}**)" if previous else ""
        await ctx.send(
            f"🏠 Placed **{home_item.display_name}** as your {label}{swapped}\n"
            f"*{home_item.description}*  ·  sleep chance **+"
            f"{home_item.sleep_bonus * 100:.0f}%**"
        )

    @pet.command(name="unplace", aliases=["takeout"])
    async def pet_unplace(self, ctx, *, slot: str):
        slot = slot.strip().lower()
        if slot not in config.HOME_SLOTS:
            valid = ", ".join(f"`{s}`" for s in config.HOME_SLOTS)
            await ctx.send(f"❌ Unknown slot. Valid: {valid}")
            return
        code = await self.repo.unplace_home_item(ctx.author.id, slot)
        if code is None:
            await ctx.send(f"Nothing placed in {config.HOME_SLOT_LABELS.get(slot, slot)}.")
            return
        await ctx.send(
            f"📦 Took **{code}** out of your "
            f"{config.HOME_SLOT_LABELS.get(slot, slot)}. It's still in your inventory."
        )

    @pet.command(name="sleep")
    async def pet_sleep(self, ctx):
        active = await self.repo.get_active_pet(ctx.author.id)
        if active is None:
            await ctx.send("❌ No active pet to put to bed. `!pet active <id>`")
            return

        remaining = await self.repo.sleep_cooldown_remaining(
            active, config.SLEEP_COOLDOWN
        )
        if remaining > 0:
            hours, rem = divmod(remaining, 3600)
            await ctx.send(
                f"😴 **{active.display_name}** is wide awake. "
                f"Try again in **{hours}h {rem // 60}m**."
            )
            return

        home = await self.repo.get_home(ctx.author.id)
        chance = combat.sleep_chance(home, active.sleep_count)
        result = await self.repo.sleep_pet(
            active, chance, random, config.SLEEP_BONUS_CAP, combat.pick_sleep_stat
        )

        where = (
            home["house"].display_name if "house" in home
            else "a cardboard box on the floor"
        )

        if result["at_cap"]:
            await ctx.send(
                f"😴 **{active.display_name}** sleeps soundly in {where}.\n"
                f"It's already fully rested — **{config.SLEEP_BONUS_CAP}"
                f"/{config.SLEEP_BONUS_CAP}** bonuses earned. Nothing left to gain, "
                f"but it looks happy."
            )
            return

        if result["success"]:
            stat = result["stat"]
            icons = {"thermals": "❄️ THERMALS", "clock": "⏱️ CLOCK",
                     "bandwidth": "📡 BANDWIDTH"}
            embed = discord.Embed(
                title=f"🌙 {active.display_name} had a great night",
                description=(
                    f"Slept in **{where}** and woke up stronger.\n\n"
                    f"# +1 {icons.get(stat, stat)}"
                ),
                color=discord.Color.gold(),
            )
            embed.set_footer(
                text=(
                    f"{active.total_sleep_bonus}/{config.SLEEP_BONUS_CAP} lifetime "
                    f"bonuses · rolled at {chance * 100:.0f}% · "
                    f"next sleep in {config.SLEEP_COOLDOWN // 3600}h"
                )
            )
            await ctx.send(embed=embed)
        else:
            next_chance = combat.sleep_chance(home, result["sleep_count"])
            await ctx.send(
                f"😴 **{active.display_name}** slept in {where} — just a normal night. "
                f"*(rolled at {chance * 100:.0f}%)*\n"
                f"That's **{result['sleep_count']}** sleep(s) logged; next one is at "
                f"**{next_chance * 100:.0f}%**. Nothing lost."
            )

    @pet.command(name="release")
    async def pet_release(self, ctx, pet_id: int, confirm: str = None):
        pet = await self.repo.get_pet(pet_id)
        if pet is None or pet.user_id != ctx.author.id:
            await ctx.send(f"❌ You don't own a pet with id `{pet_id}`. `!pet list`")
            return

        if confirm is None or confirm.lower() != "confirm":
            self._release_pending[ctx.author.id] = (pet_id, time.time())
            await ctx.send(
                f"⚠️ Release {pet.emoji} **{pet.display_name}** (Lv {pet.level})? "
                f"Its levels are gone for good and there's no refund.\n"
                f"Type `!pet release {pet_id} confirm` within 60s."
            )
            return

        pending = self._release_pending.get(ctx.author.id)
        if pending is None or pending[0] != pet_id or time.time() - pending[1] > 60:
            self._release_pending.pop(ctx.author.id, None)
            await ctx.send("⏳ That confirmation expired. Run `!pet release` again.")
            return

        self._release_pending.pop(ctx.author.id, None)
        await self.repo.release_pet(pet_id, ctx.author.id)
        await ctx.send(f"🕊️ {pet.emoji} **{pet.display_name}** was released.")


async def setup(bot):
    await bot.add_cog(Pets(bot))
