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
