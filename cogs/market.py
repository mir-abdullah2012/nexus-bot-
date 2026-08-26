"""Player-to-player marketplace -- the !market command family.

Fixed-price listings, no bidding. The defining property is ESCROW: listing an
item removes it from the seller's inventory and the listing row holds it. If it
stayed in inventory the seller could equip it, salvage it for $RAM, or relist it
while it was still for sale -- salvaging a listed 5090 would pay 1,250GB and
leave it on the market, which is money from nothing.

Every operation that moves an item is a single transaction, so an item is always
in exactly one place: inventory, escrow, or converted to $RAM with a logged reason.
"""

import time

import discord
from discord.ext import commands, tasks

import config


def human_remaining(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


class Market(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.expiry_sweep.start()

    def cog_unload(self):
        self.expiry_sweep.cancel()

    @property
    def repo(self):
        return self.bot.repo

    # ========================================================
    #  BROWSE
    # ========================================================
    @commands.group(name="market", aliases=["ah"], invoke_without_command=True)
    async def market(self, ctx, page: int = 1):
        page = max(1, page)
        total = await self.repo.count_active_listings()
        if total == 0:
            await ctx.send(
                "🏪 The market is empty. `!market sell <item> <price>` to be the first."
            )
            return

        pages = max(1, -(-total // config.MARKET_PAGE_SIZE))
        page = min(page, pages)
        listings = await self.repo.browse_listings(
            (page - 1) * config.MARKET_PAGE_SIZE, config.MARKET_PAGE_SIZE
        )
        embed = self._listing_embed(
            f"🏪 Marketplace — page {page}/{pages}", listings,
            footer=f"{total} active listing(s) · !market buy <id> · !market <page>",
        )
        await ctx.send(embed=embed)

    @market.command(name="find", aliases=["search"])
    async def market_find(self, ctx, *, query: str):
        listings = await self.repo.find_listings(query, config.MARKET_PAGE_SIZE * 2)
        if not listings:
            await ctx.send(
                f"🔍 Nothing listed for **{query}**. "
                f"Try an item code like `5090` or a category like `gpu`."
            )
            return
        await ctx.send(embed=self._listing_embed(
            f"🔍 Listings for “{query}”", listings, footer="cheapest first",
        ))

    @market.command(name="mine")
    async def market_mine(self, ctx):
        listings = await self.repo.seller_listings(ctx.author.id)
        if not listings:
            await ctx.send("You have no active listings. `!market sell <item> <price>`")
            return
        held = sum(l.price for l in listings)
        await ctx.send(embed=self._listing_embed(
            f"📋 {ctx.author.name}'s Listings "
            f"({len(listings)}/{config.MARKET_MAX_ACTIVE_LISTINGS})",
            listings, footer=f"asking {held:,}GB total · !market cancel <id>",
            show_seller=False,
        ))

    def _listing_embed(self, title, listings, footer="", show_seller=True):
        embed = discord.Embed(title=title, color=discord.Color.dark_gold())
        now = time.time()
        rows = []
        for l in listings:
            qty = f" ×{l.quantity}" if l.quantity > 1 else ""
            unit = (
                f"  *({l.unit_price:,}GB each)*" if l.quantity > 1 else ""
            )
            seller = f" · <@{l.seller_id}>" if show_seller else ""
            rows.append(
                f"`{l.listing_id:>4}`  **{l.name}**{qty} — **{l.price:,}GB**{unit}\n"
                f"　　{human_remaining(l.expires_at - now)} left{seller}"
            )
        embed.description = "\n".join(rows)
        if footer:
            embed.set_footer(text=footer)
        return embed

    # ========================================================
    #  SELL
    # ========================================================
    @market.command(name="sell", aliases=["list"])
    async def market_sell(self, ctx, item: str, price: int, quantity: int = 1):
        code = item.strip().upper().replace("RTX ", "").replace("RYZEN ", "").strip()

        active = await self.repo.count_active_listings(ctx.author.id)
        if active >= config.MARKET_MAX_ACTIVE_LISTINGS:
            await ctx.send(
                f"❌ You already have **{active}** active listings "
                f"(max {config.MARKET_MAX_ACTIVE_LISTINGS}). "
                f"`!market cancel <id>` to free a slot."
            )
            return

        if not (config.MARKET_MIN_PRICE <= price <= config.MARKET_MAX_PRICE):
            await ctx.send(
                f"❌ Price must be between **{config.MARKET_MIN_PRICE:,}** and "
                f"**{config.MARKET_MAX_PRICE:,}**GB."
            )
            return
        if quantity < 1:
            await ctx.send("❌ Quantity must be at least 1.")
            return

        held = await self.repo.get_item_quantity(ctx.author.id, code)
        if held < 1:
            await ctx.send(f"❌ You don't own a **{code}**. Check `!inventory`.")
            return

        stackable = await self.repo.is_stackable(code)
        if not stackable and quantity != 1:
            await ctx.send(f"❌ **{code}** isn't stackable — list it one at a time.")
            return
        if held < quantity:
            await ctx.send(f"❌ You only hold **{held}** × **{code}**.")
            return

        # An equipped item must come out first, same rule !salvage already uses.
        # Otherwise the equipment row would survive the sale and the seller would
        # keep the stats for free.
        if await self.repo.is_equipped(ctx.author.id, code):
            gear = await self.repo.get_gear(code)
            slot = gear.slot if gear else "the slot"
            await ctx.send(f"❌ **{code}** is installed. `!unequip {slot}` first.")
            return

        fee = config.market_listing_fee(price)
        balance = await self.repo.get_balance(ctx.author.id)
        if balance < fee:
            await ctx.send(
                f"❌ The listing fee is **{fee:,}GB** and you have **{balance:,}GB**."
            )
            return

        expires_at = int(time.time()) + config.MARKET_LISTING_DAYS * 86400
        listing_id = await self.repo.create_listing(
            ctx.author.id, code, quantity, price, fee, expires_at, stackable
        )
        if listing_id is None:
            await ctx.send("❌ Couldn't list that — your inventory changed. Try again.")
            return

        tax = config.market_sale_tax(price)
        note = ""
        gear = await self.repo.get_gear(code)
        if gear and price < gear.salvage_value:
            # Not blocked -- just a heads-up that scrapping pays better.
            note = (
                f"\n⚠️ Heads up: salvaging this pays **{gear.salvage_value:,}GB**, "
                f"more than you're asking."
            )

        qty_label = f" ×{quantity}" if quantity > 1 else ""
        await ctx.send(
            f"🏪 **LISTED** `#{listing_id}` — **{code}**{qty_label} for "
            f"**{price:,}GB $RAM**\n"
            f"Fee: −{fee:,}GB (non-refundable) · you'll net "
            f"**{price - tax:,}GB** after {int(config.MARKET_SALE_TAX_RATE * 100)}% tax\n"
            f"Expires in **{config.MARKET_LISTING_DAYS} days**{note}"
        )

    # ========================================================
    #  BUY
    # ========================================================
    @market.command(name="buy")
    async def market_buy(self, ctx, listing_id: int):
        result = await self.repo.buy_listing(
            listing_id, ctx.author.id, config.MARKET_SALE_TAX_RATE
        )

        if not result["ok"]:
            reason = result["reason"]
            if reason == "gone":
                await ctx.send(f"❌ Listing `#{listing_id}` is no longer available.")
            elif reason == "own":
                await ctx.send("❌ That's your own listing. `!market cancel` instead.")
            elif reason == "duplicate":
                await ctx.send(
                    "❌ You already own one of those, and gear doesn't stack — "
                    "you'd be paying for something you can't receive."
                )
            elif reason == "funds":
                await ctx.send(
                    f"❌ Not enough $RAM — you're **{result['short']:,}GB** short."
                )
            return

        listing = result["listing"]
        qty = f" ×{listing.quantity}" if listing.quantity > 1 else ""
        await ctx.send(
            f"🤝 **SOLD** — {ctx.author.mention} bought **{listing.name}**{qty} "
            f"for **{result['paid']:,}GB $RAM**\n"
            f"<@{listing.seller_id}> receives **{result['seller_net']:,}GB** "
            f"(after {result['tax']:,}GB tax)"
        )

    @market.command(name="cancel", aliases=["unlist"])
    async def market_cancel(self, ctx, listing_id: int):
        result = await self.repo.cancel_listing(listing_id, ctx.author.id)
        if not result["ok"]:
            if result["reason"] == "not_yours":
                await ctx.send("❌ That isn't your listing.")
            else:
                await ctx.send(f"❌ Listing `#{listing_id}` is no longer active.")
            return

        listing = result["listing"]
        if result["outcome"] == "salvage":
            await ctx.send(
                f"↩️ Pulled `#{listing_id}`. You already own a **{listing.item_code}**, "
                f"so it was stripped for parts: +**{result['amount']:,}GB $RAM**\n"
                f"*(The {listing.fee_paid:,}GB listing fee isn't refunded.)*"
            )
        else:
            await ctx.send(
                f"↩️ Pulled `#{listing_id}` — **{listing.item_code}** is back in your "
                f"inventory. *(The {listing.fee_paid:,}GB listing fee isn't refunded.)*"
            )

    # ========================================================
    #  EXPIRY SWEEP
    # ========================================================
    @tasks.loop(minutes=config.MARKET_SWEEP_MINUTES)
    async def expiry_sweep(self):
        """Return expired listings to their sellers.

        A background sweep rather than lazy expiry-on-browse, so items don't sit
        in limbo whenever nobody happens to open the market.
        """
        try:
            expired = await self.repo.expire_listings(int(time.time()))
            for entry in expired:
                listing = entry["listing"]
                kind = "salvaged (duplicate)" if entry["outcome"] == "salvage" else "returned"
                print(f"[market] listing {listing.listing_id} expired -> {kind} "
                      f"({listing.item_code} x{listing.quantity})")
        except Exception as e:
            print(f"market sweep error: {type(e).__name__}: {e}")

    @expiry_sweep.before_loop
    async def _before_sweep(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Market(bot))
