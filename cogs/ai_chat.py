"""AI chat: mention/reply responses, random chime-ins, and quiet-channel revival.

The Anthropic key comes from ANTHROPIC_API_KEY in .env. If it is absent the cog
loads with the client disabled and every AI path becomes a no-op, which is the
same guard the old `if not ai_client` checks provided.
"""

import random
import re
import time
import traceback

import anthropic
import discord
from discord.ext import commands, tasks

import config


class AIChat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.client = (
            anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
            if config.ANTHROPIC_API_KEY
            else None
        )
        if self.client is None:
            print("[ai] ANTHROPIC_API_KEY missing -- AI chat disabled")

        self.channel_memory: dict[int, list] = {}   # channel_id -> [{role, content}]
        self.last_activity: dict[int, float] = {}   # channel_id -> unix timestamp
        self._role_hint_sent: dict[int, float] = {}  # channel_id -> last nudge time
        self.quiet_check.start()

    def cog_unload(self):
        self.quiet_check.cancel()

    @property
    def repo(self):
        return self.bot.repo

    # ========================================================
    #  MEMORY
    # ========================================================
    def _trim(self, history):
        h = history[-config.MEMORY_LIMIT:]
        while h and h[0]["role"] != "user":   # API requires first msg = user
            h = h[1:]
        return h

    async def get_ai_reply(self, channel_id, user_name, user_message):
        if not self.client:
            return None
        history = self.channel_memory.get(channel_id, [])
        history.append({"role": "user", "content": f"{user_name}: {user_message}"})
        history = self._trim(history)
        try:
            resp = await self.client.messages.create(
                model=config.AI_MODEL,
                max_tokens=config.AI_MAX_TOKENS_REPLY,
                system=config.AI_SYSTEM_PROMPT,
                messages=history,
            )
            reply = resp.content[0].text.strip()
            history.append({"role": "assistant", "content": reply})
            self.channel_memory[channel_id] = self._trim(history)
            return reply
        except Exception as e:
            # Type name + traceback, not just str(e): several API exceptions
            # stringify to an empty message, which is how this looked like
            # "no output at all" while debugging the mention bug.
            print(f"AI error: {type(e).__name__}: {e}")
            traceback.print_exc()
            return None

    # ========================================================
    #  HOOKS CALLED BY THE on_message ORCHESTRATOR IN main.py
    # ========================================================
    def mark_activity(self, channel_id):
        self.last_activity[channel_id] = time.time()

    def mentions_own_role(self, message) -> bool:
        """True if the message pings this bot's auto-generated managed role.

        Discord creates a role carrying the bot's name when it joins with
        permissions. In the mention autocomplete it is visually identical to
        the bot's user account, so it gets picked by accident constantly --
        and it lands in role_mentions, never in message.mentions.

        Matching on tags.bot_id rather than role.managed keeps this narrow: it
        only fires for the integration role belonging to THIS bot, not for any
        other role the bot happens to wear.
        """
        if self.bot.user is None:
            return False
        for role in getattr(message, "role_mentions", ()):
            tags = getattr(role, "tags", None)
            if tags is not None and getattr(tags, "bot_id", None) == self.bot.user.id:
                return True
        return False

    async def nudge_role_mention(self, message):
        """Point someone at the right autocomplete entry instead of ignoring them."""
        now = time.time()
        if now - self._role_hint_sent.get(message.channel.id, 0) < (
            config.ROLE_MENTION_HINT_COOLDOWN
        ):
            return
        self._role_hint_sent[message.channel.id] = now
        try:
            hint = random.choice(config.ROLE_MENTION_HINTS)
            await message.channel.send(hint.format(name=self.bot.user.name))
        except Exception as e:
            print(f"role hint error: {type(e).__name__}: {e}")

    async def maybe_reply(self, message):
        if message.content.startswith("!") or not self.client:
            return

        cfg = await self.repo.get_guild_config(message.guild.id)
        if not cfg.ai_enabled:
            return

        is_mention = self.bot.user in message.mentions

        # Pinged the role instead of the account? Say so rather than going
        # quiet. Only when the user account was NOT also pinged -- if both are
        # in the message, the real mention wins and we answer normally.
        if not is_mention and self.mentions_own_role(message):
            await self.nudge_role_mention(message)
            return

        is_reply_to_bot = (
            message.reference
            and isinstance(message.reference.resolved, discord.Message)
            and message.reference.resolved.author.id == self.bot.user.id
        )
        should_chime = random.random() < config.CHIME_CHANCE

        if not (is_mention or is_reply_to_bot or should_chime):
            return

        clean = re.sub(r"<@!?\d+>", "", message.content).strip()
        try:
            async with message.channel.typing():
                reply = await self.get_ai_reply(
                    message.channel.id, message.author.display_name, clean or "hey"
                )
            if reply:
                await message.channel.send(reply)
        except Exception as e:
            print(f"AI send error: {type(e).__name__}: {e}")
            traceback.print_exc()

    # ========================================================
    #  PROACTIVE AI ("talk when it's quiet")
    # ========================================================
    @tasks.loop(minutes=10)
    async def quiet_check(self):
        if not self.client:
            return
        now = time.time()
        for channel_id, last_seen in list(self.last_activity.items()):
            if now - last_seen <= config.QUIET_MINUTES * 60:
                continue
            if random.random() >= config.PROACTIVE_CHANCE:
                continue
            channel = self.bot.get_channel(channel_id)
            if not channel:
                continue
            try:
                resp = await self.client.messages.create(
                    model=config.AI_MODEL,
                    max_tokens=config.AI_MAX_TOKENS_PROACTIVE,
                    system=config.AI_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": config.AI_PROACTIVE_PROMPT}],
                )
                await channel.send(resp.content[0].text.strip())
                self.last_activity[channel_id] = now   # reset so it doesn't spam
            except Exception as e:
                print(f"proactive error: {e}")

    @quiet_check.before_loop
    async def _before_quiet(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(AIChat(bot))
