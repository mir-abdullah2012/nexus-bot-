"""Clans -- the !guild command family.

Naming note: the command is !guild because that is what players call it, but
everything in the schema and code says CLAN. discord.py already uses "guild" to
mean a Discord server, and guild_config / guild_self_roles / guild_banned_words
are all keyed by server id. Mixing the two words is how you get a 2am bug.

Membership rules are enforced by the database, not by application checks:
clan_members.user_id is the PRIMARY KEY, so "one clan per player" cannot be
raced. players.clan_id is a synced pointer written in the same transaction.
"""

import re
import time

import discord
from discord.ext import commands

import config
from core.filters import contains_banned

NAME_ALLOWED = re.compile(r"^[A-Za-z0-9 '\-_]+$")
TAG_ALLOWED = re.compile(r"^[A-Za-z0-9]+$")


def derive_tag(name: str) -> str:
    """Build a default tag from a clan name: initials if multi-word, else prefix."""
    words = [w for w in re.split(r"[\s_\-]+", name) if w]
    if len(words) >= 2:
        tag = "".join(w[0] for w in words)[:config.CLAN_TAG_MAX]
    else:
        tag = words[0][:config.CLAN_TAG_MAX] if words else "CLAN"
    tag = re.sub(r"[^A-Za-z0-9]", "", tag).upper()
    while len(tag) < config.CLAN_TAG_MIN:
        tag += "X"
    return tag


class Clans(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._disband_pending: dict[int, float] = {}   # user_id -> ts

    @property
    def repo(self):
        return self.bot.repo

    # ========================================================
    #  HELPERS
    # ========================================================
    async def _require_membership(self, ctx):
        member = await self.repo.get_membership(ctx.author.id)
        if member is None:
            await ctx.send("❌ You're not in a guild. `!guild list` to find one.")
            return None, None
        clan = await self.repo.get_clan(member.clan_id)
        if clan is None:                       # defensive: orphaned membership
            await self.repo.remove_from_clan(ctx.author.id)
            await ctx.send("⚠️ Your guild no longer exists — membership cleared.")
            return None, None
        return member, clan

    async def _resolve_target(self, ctx, target: discord.Member, clan):
        """Confirm a mentioned member is in the same clan. Returns their record."""
        record = await self.repo.get_membership(target.id)
        if record is None or record.clan_id != clan.clan_id:
            await ctx.send(f"❌ {target.display_name} isn't in **{clan.name}**.")
            return None
        return record

    # ========================================================
    #  GROUP
    # ========================================================
    @commands.group(name="guild", aliases=["clan"], invoke_without_command=True)
    async def guild(self, ctx, *, name: str = None):
        """!guild — your guild, or !guild <name> to look one up."""
        if name:
            clan = await self.repo.find_clan(name)
            if clan is None:
                await ctx.send(f"❌ No guild called **{name}**. Try `!guild list`.")
                return
            await self._send_info(ctx, clan)
            return

        member = await self.repo.get_membership(ctx.author.id)
        if member is None:
            await ctx.send(
                "🛡️ You're not in a guild yet.\n"
                "`!guild list` to browse · `!guild join <name>` to join · "
                "`!guild create <name>` to found one."
            )
            return
        clan = await self.repo.get_clan(member.clan_id)
        await self._send_info(ctx, clan)

    @guild.command(name="info")
    async def guild_info(self, ctx, *, name: str = None):
        await self.guild(ctx, name=name)

    async def _send_info(self, ctx, clan):
        members = await self.repo.get_clan_members(clan.clan_id)
        embed = discord.Embed(
            title=clan.label(),
            description=clan.description or "*No description set.*",
            color=discord.Color.blurple(),
        )

        lines = []
        for m in members:
            try:
                user = await self.bot.fetch_user(m.user_id)
                display = user.name
            except Exception:
                display = f"User {m.user_id}"
            role_label = config.CLAN_ROLE_LABELS.get(m.role, m.role)
            lines.append(f"{role_label} **{display}** — {m.contribution:,}GB earned")

        embed.add_field(
            name=f"Members ({len(members)}/{clan.max_members})",
            value="\n".join(lines) or "*empty*",
            inline=False,
        )
        total = sum(m.contribution for m in members)
        embed.add_field(name="Total earned", value=f"💾 {total:,}GB", inline=True)
        embed.add_field(
            name="Recruiting", value="✅ Open" if clan.is_open else "🔒 Closed", inline=True
        )
        embed.set_footer(text=f"Founded · !guild war for the standings")
        await ctx.send(embed=embed)

    # ========================================================
    #  CREATE / JOIN / LEAVE
    # ========================================================
    @guild.command(name="create")
    async def guild_create(self, ctx, *, name: str):
        name = " ".join(name.split())          # collapse whitespace

        if await self.repo.get_membership(ctx.author.id):
            await ctx.send("❌ You're already in a guild — `!guild leave` first.")
            return

        if not (config.CLAN_NAME_MIN <= len(name) <= config.CLAN_NAME_MAX):
            await ctx.send(
                f"❌ Name must be {config.CLAN_NAME_MIN}–{config.CLAN_NAME_MAX} characters."
            )
            return
        if not NAME_ALLOWED.match(name):
            await ctx.send("❌ Letters, numbers, spaces, hyphens and underscores only.")
            return
        # Reuse the existing word filter so !guild create can't become a bypass.
        if contains_banned(name, config.DEFAULT_BANNED):
            await ctx.send("❌ That name won't fly. Pick another.")
            return

        player = await self.repo.get_player(ctx.author.id)
        if player.level < config.CLAN_MIN_LEVEL:
            await ctx.send(
                f"🔒 Founding a guild needs **Level {config.CLAN_MIN_LEVEL}**. "
                f"You're **{player.level}**."
            )
            return
        if player.balance < config.CLAN_CREATE_COST:
            await ctx.send(
                f"❌ Founding costs **{config.CLAN_CREATE_COST:,}GB $RAM**. "
                f"You have **{player.balance:,}GB**."
            )
            return

        tag = derive_tag(name)
        taken = await self.repo.name_or_tag_taken(name, tag)
        if taken == "name":
            await ctx.send(f"❌ A guild called **{name}** already exists.")
            return
        if taken == "tag":
            # Auto-generated tag collided; nudge rather than silently mangling it.
            for suffix in "23456789":
                candidate = (tag[:config.CLAN_TAG_MAX - 1] + suffix).upper()
                if await self.repo.name_or_tag_taken("\x00", candidate) is None:
                    tag = candidate
                    break
            else:
                await ctx.send("❌ Couldn't find a free tag — try a different name.")
                return

        await self.repo.adjust_balance(
            ctx.author.id, -config.CLAN_CREATE_COST, "clan:create"
        )
        clan_id = await self.repo.create_clan(
            ctx.author.id, name, tag, config.CLAN_DEFAULT_EMOJI,
            ctx.guild.id if ctx.guild else None, config.CLAN_MAX_MEMBERS,
        )
        clan = await self.repo.get_clan(clan_id)
        await ctx.send(
            f"🛡️ **GUILD FOUNDED** — {clan.label()}\n"
            f"−{config.CLAN_CREATE_COST:,}GB $RAM. You're the leader.\n"
            f"`!guild tag <TAG>` · `!guild emoji <e>` · `!guild desc <text>` to customise."
        )

    @guild.command(name="join")
    async def guild_join(self, ctx, *, name: str):
        if await self.repo.get_membership(ctx.author.id):
            await ctx.send("❌ You're already in a guild — `!guild leave` first.")
            return

        clan = await self.repo.find_clan(name)
        if clan is None:
            await ctx.send(f"❌ No guild called **{name}**. Try `!guild list`.")
            return
        if not clan.is_open:
            await ctx.send(f"🔒 **{clan.name}** isn't accepting new members.")
            return
        if clan.member_count >= clan.max_members:
            await ctx.send(
                f"❌ **{clan.name}** is full ({clan.member_count}/{clan.max_members})."
            )
            return

        await self.repo.join_clan(ctx.author.id, clan.clan_id)
        await ctx.send(f"🎉 {ctx.author.mention} joined {clan.label()}!")

    @guild.command(name="leave")
    async def guild_leave(self, ctx):
        member, clan = await self._require_membership(ctx)
        if member is None:
            return

        if member.is_leader:
            await self._leader_departs(ctx, clan, member)
            return

        await self.repo.remove_from_clan(ctx.author.id)
        await ctx.send(f"👋 You left {clan.label()}.")

    async def _leader_departs(self, ctx, clan, member):
        """Succession: longest-tenured officer, else longest-tenured member, else disband."""
        others = [
            m for m in await self.repo.get_clan_members(clan.clan_id)
            if m.user_id != member.user_id
        ]
        if not others:
            await self.repo.disband_clan(clan.clan_id)
            await ctx.send(
                f"👋 You left, and {clan.label()} had no one else — it's been disbanded."
            )
            return

        officers = sorted(
            [m for m in others if m.role == "officer"], key=lambda m: m.joined_at
        )
        heir = officers[0] if officers else sorted(others, key=lambda m: m.joined_at)[0]

        await self.repo.transfer_leadership(clan.clan_id, member.user_id, heir.user_id)
        await self.repo.remove_from_clan(member.user_id)
        try:
            new_leader = await self.bot.fetch_user(heir.user_id)
            heir_name = new_leader.name
        except Exception:
            heir_name = f"User {heir.user_id}"
        await ctx.send(
            f"👑 You left {clan.label()}. **{heir_name}** is the new leader."
        )

    @guild.command(name="list")
    async def guild_list(self, ctx):
        clans = await self.repo.list_clans(config.CLAN_LIST_LIMIT)
        if not clans:
            await ctx.send(
                "No guilds exist yet. Be the first — `!guild create <name>`"
            )
            return
        embed = discord.Embed(title="🛡️ Guilds", color=discord.Color.blurple())
        embed.description = "\n".join(
            f"{c.label()} — **{c.member_count}/{c.max_members}** members"
            + ("" if c.is_open else " 🔒")
            for c in clans
        )
        embed.set_footer(text="!guild join <name> · !guild info <name>")
        await ctx.send(embed=embed)

    # ========================================================
    #  MODERATION (leader / officer)
    # ========================================================
    @guild.command(name="kick")
    async def guild_kick(self, ctx, member: discord.Member):
        actor, clan = await self._require_membership(ctx)
        if actor is None:
            return
        if not actor.can_kick:
            await ctx.send("❌ Only the leader or an officer can kick.")
            return
        if member.id == ctx.author.id:
            await ctx.send("❌ Use `!guild leave` to remove yourself.")
            return

        target = await self._resolve_target(ctx, member, clan)
        if target is None:
            return
        if target.is_leader:
            await ctx.send("❌ You can't kick the leader.")
            return
        if target.role == "officer" and not actor.is_leader:
            await ctx.send("❌ Officers can't kick other officers.")
            return

        await self.repo.remove_from_clan(member.id)
        await ctx.send(f"👢 {member.display_name} was removed from {clan.label()}.")

    @guild.command(name="promote")
    async def guild_promote(self, ctx, member: discord.Member):
        actor, clan = await self._require_membership(ctx)
        if actor is None:
            return
        if not actor.is_leader:
            await ctx.send("❌ Only the leader can promote.")
            return
        target = await self._resolve_target(ctx, member, clan)
        if target is None:
            return
        if target.role != "member":
            await ctx.send(f"❌ {member.display_name} is already an officer.")
            return
        await self.repo.set_clan_role(member.id, "officer")
        await ctx.send(f"⚔️ {member.display_name} is now an **officer**.")

    @guild.command(name="demote")
    async def guild_demote(self, ctx, member: discord.Member):
        actor, clan = await self._require_membership(ctx)
        if actor is None:
            return
        if not actor.is_leader:
            await ctx.send("❌ Only the leader can demote.")
            return
        target = await self._resolve_target(ctx, member, clan)
        if target is None:
            return
        if target.role != "officer":
            await ctx.send(f"❌ {member.display_name} isn't an officer.")
            return
        await self.repo.set_clan_role(member.id, "member")
        await ctx.send(f"· {member.display_name} is now a **member**.")

    @guild.command(name="transfer")
    async def guild_transfer(self, ctx, member: discord.Member):
        actor, clan = await self._require_membership(ctx)
        if actor is None:
            return
        if not actor.is_leader:
            await ctx.send("❌ Only the leader can transfer leadership.")
            return
        if member.id == ctx.author.id:
            await ctx.send("❌ You're already the leader.")
            return
        target = await self._resolve_target(ctx, member, clan)
        if target is None:
            return
        await self.repo.transfer_leadership(clan.clan_id, ctx.author.id, member.id)
        await ctx.send(
            f"👑 **{member.display_name}** now leads {clan.label()}. "
            f"You've been made an officer."
        )

    @guild.command(name="disband")
    async def guild_disband(self, ctx, confirm: str = None):
        actor, clan = await self._require_membership(ctx)
        if actor is None:
            return
        if not actor.is_leader:
            await ctx.send("❌ Only the leader can disband.")
            return

        if confirm is None or confirm.lower() != "confirm":
            self._disband_pending[ctx.author.id] = time.time()
            await ctx.send(
                f"⚠️ This disbands {clan.label()} and removes all "
                f"**{clan.member_count}** member(s). Contribution history is kept "
                f"but the guild is gone.\n"
                f"Type `!guild disband confirm` within 60s to go through with it."
            )
            return

        started = self._disband_pending.get(ctx.author.id)
        if started is None or time.time() - started > 60:
            self._disband_pending.pop(ctx.author.id, None)
            await ctx.send("⏳ That confirmation expired. Run `!guild disband` again.")
            return

        self._disband_pending.pop(ctx.author.id, None)
        removed = await self.repo.disband_clan(clan.clan_id)
        await ctx.send(f"💥 {clan.label()} disbanded. {removed} member(s) released.")

    # ========================================================
    #  COSMETICS (leader)
    # ========================================================
    async def _leader_only(self, ctx):
        actor, clan = await self._require_membership(ctx)
        if actor is None:
            return None, None
        if not actor.is_leader:
            await ctx.send("❌ Only the leader can change that.")
            return None, None
        return actor, clan

    @guild.command(name="tag")
    async def guild_tag(self, ctx, tag: str):
        actor, clan = await self._leader_only(ctx)
        if actor is None:
            return
        tag = tag.strip().upper()
        if not (config.CLAN_TAG_MIN <= len(tag) <= config.CLAN_TAG_MAX):
            await ctx.send(
                f"❌ Tag must be {config.CLAN_TAG_MIN}–{config.CLAN_TAG_MAX} characters."
            )
            return
        if not TAG_ALLOWED.match(tag):
            await ctx.send("❌ Tags are letters and numbers only.")
            return
        if contains_banned(tag, config.DEFAULT_BANNED):
            await ctx.send("❌ Pick a different tag.")
            return
        if await self.repo.name_or_tag_taken("\x00", tag):
            await ctx.send(f"❌ Tag **[{tag}]** is taken.")
            return
        await self.repo.update_clan_field(clan.clan_id, "tag", tag)
        await ctx.send(f"✅ Tag set to **[{tag}]**.")

    @guild.command(name="emoji")
    async def guild_emoji(self, ctx, emoji: str):
        actor, clan = await self._leader_only(ctx)
        if actor is None:
            return
        if len(emoji) > 8:
            await ctx.send("❌ One emoji, please.")
            return
        await self.repo.update_clan_field(clan.clan_id, "emoji", emoji)
        await ctx.send(f"✅ Guild emoji set to {emoji}")

    @guild.command(name="desc", aliases=["description"])
    async def guild_desc(self, ctx, *, text: str):
        actor, clan = await self._leader_only(ctx)
        if actor is None:
            return
        text = text.strip()[:config.CLAN_DESC_MAX]
        if contains_banned(text, config.DEFAULT_BANNED):
            await ctx.send("❌ Reword that one.")
            return
        await self.repo.update_clan_field(clan.clan_id, "description", text)
        await ctx.send("✅ Description updated.")

    @guild.command(name="open")
    async def guild_open(self, ctx):
        actor, clan = await self._leader_only(ctx)
        if actor is None:
            return
        await self.repo.update_clan_field(clan.clan_id, "is_open", 1)
        await ctx.send("✅ Guild is now **open** — anyone can join.")

    @guild.command(name="close")
    async def guild_close(self, ctx):
        actor, clan = await self._leader_only(ctx)
        if actor is None:
            return
        await self.repo.update_clan_field(clan.clan_id, "is_open", 0)
        await ctx.send("🔒 Guild is now **closed** to new members.")

    # ========================================================
    #  GUILD WAR
    # ========================================================
    @guild.command(name="war", aliases=["top", "leaderboard"])
    async def guild_war(self, ctx):
        weights = {
            "clear": config.CLAN_WAR_CLEAR_POINTS,
            "flawless": config.CLAN_WAR_FLAWLESS_POINTS,
            "duel_win": config.CLAN_WAR_DUEL_WIN_POINTS,
            "contribution_divisor": config.CLAN_WAR_CONTRIBUTION_DIVISOR,
        }
        scores = await self.repo.clan_war_scores(config.CLAN_WAR_WINDOW_DAYS, weights)
        if not scores:
            await ctx.send("No guilds are competing yet. `!guild create <name>`")
            return

        embed = discord.Embed(
            title="⚔️ Guild War Standings",
            description=f"Rolling {config.CLAN_WAR_WINDOW_DAYS}-day scoreboard.",
            color=discord.Color.gold(),
        )
        medals = ["🥇", "🥈", "🥉"]
        for i, s in enumerate(scores[:10]):
            rank = medals[i] if i < 3 else f"`{i+1}.`"
            embed.add_field(
                name=f"{rank} {s['emoji']} [{s['tag']}] {s['name']} — {s['score']:,} pts",
                value=(
                    f"{s['member_count']} members · {s['clears']} clears "
                    f"({s['flawless']} flawless) · {s['duel_wins']} duel wins · "
                    f"{s['contribution']:,}GB earned"
                ),
                inline=False,
            )
        embed.set_footer(
            text=(
                f"clear {weights['clear']} · flawless {weights['flawless']} · "
                f"duel win {weights['duel_win']} · "
                f"1 pt per {weights['contribution_divisor']:,}GB earned"
            )
        )
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Clans(bot))
