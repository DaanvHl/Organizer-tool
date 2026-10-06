import logging

import discord
from discord.ext import commands

import config
from cogs.events import ChannelPollSelect, DraftSelect, EventButton, PollSelect
from database import Database

log = logging.getLogger("bot")


class EventBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.db = Database(config.DATABASE_PATH)

    async def setup_hook(self):
        self.add_dynamic_items(EventButton, PollSelect, ChannelPollSelect, DraftSelect)
        await self.load_extension("cogs.events")

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
    EventBot().run(config.DISCORD_TOKEN, root_logger=True)
