import logging

import discord
from discord.ext import commands

import config
from cogs.captains import DraftSelect, LobbyButton, MapVoteButton, TiebreakerBanButton
from cogs.events import ChannelPollSelect, EventButton, PollSelect
from database import Database

log = logging.getLogger("bot")


class ChannelLimitedTree(discord.app_commands.CommandTree):
    """Every slash command only works in ALLOWED_CHANNEL_IDS (for everyone, admins too).
    The only exception: the temporary event channels the bot creates, so events can be managed there."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not config.ALLOWED_CHANNEL_IDS or interaction.channel_id in config.ALLOWED_CHANNEL_IDS:
            return True
        if self.client.db.get_event_by_channel(interaction.channel_id) is not None:
            return True
        if interaction.type == discord.InteractionType.application_command:
            channels = ", ".join(f"<#{cid}>" for cid in sorted(config.ALLOWED_CHANNEL_IDS))
            await interaction.response.send_message(
                f"You can't use bot commands here. Use them in {channels}.", ephemeral=True)
        return False  # also no suggestions (autocomplete) outside the allowed channels


class EventBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        # Reading message text is only needed for the keyword reactions (privileged intent).
        intents.message_content = config.FUN_KEYWORDS
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, tree_cls=ChannelLimitedTree)
        self.db = Database(config.DATABASE_PATH)

    async def setup_hook(self):
        self.add_dynamic_items(EventButton, PollSelect, ChannelPollSelect, DraftSelect, LobbyButton,
                               MapVoteButton, TiebreakerBanButton)
        await self.load_extension("cogs.events")
        await self.load_extension("cogs.fun")
        await self.load_extension("cogs.captains")

        if config.GUILD_IDS:
            for guild_id in config.GUILD_IDS:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %d slash command(s) to server %s", len(synced), guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d slash command(s) globally", len(synced))

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
