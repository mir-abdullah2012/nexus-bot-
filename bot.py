"""Nexus 2.0 -- entry point.

Slim on purpose. This file builds the bot, opens the database, loads the cogs,
and owns the one piece of logic that cannot live in a cog: the on_message
ordering.

Why on_message is here and not split across cogs
------------------------------------------------
Nexus 1.x ran a single on_message with early returns, and that ordering was
load-bearing: a banned word stopped XP, buying, AI and command processing dead.
Independent Cog listeners all fire in parallel and cannot stop one another, so
splitting it up would silently change behaviour. Instead the cogs expose plain
methods and this orchestrator calls them in the original order.
"""

import asyncio
import traceback

import discord
from discord.ext import commands

import config
from core.database import Database
from core.repository import Repository

EXTENSIONS = (
    "cogs.economy",
    "cogs.games",
    "cogs.moderation",
    "cogs.roles",
    "cogs.utility",
    "cogs.ai_chat",
    "cogs.admin",
    "cogs.events",
    "cogs.rpg",
    "cogs.dungeon",
    "cogs.clans",
    "cogs.duel",
)


class NexusBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True   # read messages (REQUIRED, enable in Dev Portal)
        intents.members = True           # welcome msgs + join/leave logging (Dev Portal)
        intents.presences = False

        super().__init__(
            command_prefix=config.COMMAND_PREFIX,
            intents=intents,
            chunk_guilds_at_startup=False,
            help_command=None,
        )

        self.db = Database(config.DB_PATH)
        self.repo: Repository | None = None

    async def setup_hook(self):
        await self.db.connect()
        self.repo = Repository(self.db)
        for extension in EXTENSIONS:
            try:
                await self.load_extension(extension)
                print(f"[cogs] loaded {extension}")
            except Exception as e:
                print(f"[cogs] FAILED {extension}: {e}")

    async def close(self):
        await self.db.close()
        await super().close()

    # ========================================================
    #  THE on_message ORCHESTRATOR
    # ========================================================
    async def on_message(self, message):
        try:
            if message.author.bot or not message.guild:
                return

            economy = self.get_cog("Economy")
            moderation = self.get_cog("Moderation")
            ai_chat = self.get_cog("AIChat")

            # 0) activity tracking, before anything can return early
            if ai_chat:
                ai_chat.mark_activity(message.channel.id)

            # 1) word filter -- deletes + auto-warns, then stops everything
            if moderation and await moderation.screen(message):
                return

            # 2) XP for every message, commands included
            if economy:
                await economy.grant_xp(message)

                # 3) prefix-less "buy <model>" shortcut, stops everything
                if await economy.try_buy(message):
                    return

            # 4) AI chat -- mention, reply-to-bot, or random chime
            if ai_chat:
                await ai_chat.maybe_reply(message)

            await self.process_commands(message)
        except Exception as e:
            # Keep the traceback. A bare one-line print is what made the
            # mention bug take as long as it did to pin down.
            print(f"on_message error: {type(e).__name__}: {e}")
            traceback.print_exc()


async def main():
    if not config.DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN missing. Add it to your .env file.")
        return

    bot = NexusBot()
    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")
