import logging

import discord
from discord.ext import commands

import config
from cogs.events import ChannelPollSelect, DraftSelect, EventButton, PollSelect
from database import Database

log = logging.getLogger("bot")


class EventBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        # Reading message text is only needed for the keyword reactions (privileged intent).
        intents.message_content = config.FUN_KEYWORDS
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.db = Database(config.DATABASE_PATH)

    async def setup_hook(self):
        self.add_dynamic_items(EventButton, PollSelect, ChannelPollSelect, DraftSelect)
        await self.load_extension("cogs.events")
        await self.load_extension("cogs.fun")

        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("Synced %d slash command(s)", len(synced))

    async def on_ready(self):
        log.info("Logged in as %s (ID %s)", self.user, self.user.id)


if __name__ == "__main__":
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    discord.utils.setup_logging(root=True)
    try:
        EventBot().run(config.DISCORD_TOKEN, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        if not config.FUN_KEYWORDS:
            raise
        # Don't crash-loop: run without keyword reactions until the intent is enabled.
        log.warning("Message Content Intent is not enabled in the Discord developer portal. "
                    "Starting without keyword reactions. Enable it under Bot -> Privileged Gateway "
                    "Intents, or set FUN_KEYWORDS=false to hide this warning.")
        config.FUN_KEYWORDS = False
        EventBot().run(config.DISCORD_TOKEN, log_handler=None)
