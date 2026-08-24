"""Shared server-log helper.

Both cogs.moderation and cogs.events need to write to a guild's configured log
channel, so the helper lives here rather than in either cog. Same fire-and-
forget, never-raise behaviour as the Nexus 1.x _log().
"""


async def send_log(bot, guild, text: str) -> None:
    if guild is None:
        return
    try:
        cfg = await bot.repo.get_guild_config(guild.id)
        if not cfg.log_channel:
            return
        channel = guild.get_channel(cfg.log_channel)
        if channel:
            await channel.send(text)
    except Exception as e:
        print(f"log error: {e}")
