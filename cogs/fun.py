import logging
import random
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
from database import Database

log = logging.getLogger(__name__)

EXCUSES = [
    "My ping spiked to 999 exactly when it mattered.",
    "My cat walked over my keyboard.",
    "The sun was in my eyes. Yes, indoors.",
    "My mouse ran out of battery mid-fight.",
    "I was testing a new strategy. It's called losing.",
    "My teammates were clearly bots.",
    "The enemy was hacking. Probably. Definitely.",
    "I was playing with my monitor brightness at 2%.",
    "My chair isn't a gaming chair, so what did you expect?",
    "I let them win. Out of kindness.",
    "A Discord notification scared me.",
    "My little brother was playing. (I don't have a little brother.)",
    "I was eating with one hand.",
    "The game updated my skill level downwards.",
    "I sneezed. Twice.",
]

ROASTS = [
    "{name} has the aim of a stormtrooper with the hiccups.",
    "{name}'s K/D ratio is a cry for help.",
    "{name} plays like their monitor is turned off.",
    "{name} is the reason the respawn button exists.",
    "{name} once lost a 1v1 against an AFK player.",
    "Enemies don't fear {name}. They farm them.",
    "{name} thinks a flag is something you wave when you surrender.",
    "{name} gets carried so often they should pay the team a delivery fee.",
    "Scientists are still trying to figure out what {name} was aiming at.",
    "{name}'s game sense left the server years ago.",
    "If being bad was a rank, {name} would be Generalissimo.",
    "{name} brings so little to the team that their absence counts as a buff.",
]

BLAME_REASONS = [
    "The evidence is overwhelming.",
    "No further questions.",
    "The court has spoken. 🔨",
    "Don't even try to deny it.",
    "Witnesses saw everything.",
    "Case closed.",
]

EIGHT_BALL = [
    "It is certain.", "Without a doubt.", "Yes, definitely.", "Most likely.", "Signs point to yes.",
    "Ask again later.", "Better not tell you now.", "Cannot predict now.",
    "Don't count on it.", "My sources say no.", "Very doubtful.", "Absolutely not. 💀",
]

# Keyword reactions (only active when FUN_KEYWORDS=true). Each entry: (pattern, reaction emoji or None, replies)
KEYWORDS = [
    (r"\bgg\b", "🫡", []),
    (r"\bbruh\b", "💀", []),
    (r"\bnoob\b", "🍼", []),
    (r"\bez\b", None, [
        "ez? Bold words from someone who died 40 times. 📉",
        "Ez for you, emotional damage for them.",
        "Calm down, it was one game. 😌",
    ]),
    (r"\blag+(y|ging)?\b", None, [
        "📶 Ah yes, the lag. Excuse #1 in the official excuse book. Try `/excuse` for more.",
        "Lag? Or skill issue? 🤔",
    ]),
    (r"\brigged\b", None, [
        "🤫 Nothing is rigged. Please stop asking.",
        "The system is perfectly fair. Especially for me. 🤖",
    ]),
    (r"\bafk\b", None, [
        "💤 Another soldier has left the battlefield. Rest in peace.",
    ]),
    (r"\bwho asked\b", None, [
        "🔎 Searching for who asked… 0 results found.",
    ]),
]
KEYWORD_PATTERNS = [(re.compile(p, re.IGNORECASE), emoji, replies) for p, emoji, replies in KEYWORDS]


class Fun(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db: Database = bot.db
        self.last_keyword_reply = {}  # channel_id -> timestamp

    @app_commands.command(name="excuse", description="Get an official excuse for why you lost")
    async def excuse(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"🧾 {interaction.user.mention}'s official excuse: *{random.choice(EXCUSES)}*",
            allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="roast", description="Roast someone (with love)")
    @app_commands.describe(member="Who to roast")
    @app_commands.checks.cooldown(2, 30, key=lambda i: (i.guild_id, i.user.id))
    async def roast(self, interaction: discord.Interaction, member: discord.Member):
        if member.id == self.bot.user.id:
            text = f"Nice try. {random.choice(ROASTS).format(name=interaction.user.mention)} 🔄"
        else:
            text = f"🔥 {random.choice(ROASTS).format(name=member.mention)}"
        await interaction.response.send_message(text)

    @app_commands.command(name="blame", description="Find out whose fault it really was")
    @app_commands.checks.cooldown(2, 30, key=lambda i: (i.guild_id, i.user.id))
    async def blame(self, interaction: discord.Interaction):
        await interaction.response.defer()
        candidates = set()
        event = self.db.get_event_by_channel(interaction.channel_id)
        if event is not None:
            candidates = {p.user_id for p in self.db.participants(event.id)}
        else:
            try:
                async for message in interaction.channel.history(limit=100):
                    if not message.author.bot:
                        candidates.add(message.author.id)
            except discord.HTTPException:
                pass
        target = random.choice(sorted(candidates)) if candidates else interaction.user.id
        await interaction.followup.send(
            f"🔍 Investigating the loss…\n📋 Verdict: it was **<@{target}>**'s fault. "
            f"{random.choice(BLAME_REASONS)}",
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(target)]))

    @app_commands.command(name="bonk", description="Bonk someone")
    @app_commands.describe(member="Who deserves a bonk")
    @app_commands.checks.cooldown(3, 60, key=lambda i: (i.guild_id, i.user.id))
    async def bonk(self, interaction: discord.Interaction, member: discord.Member):
        if member.id == self.bot.user.id:
            target, prefix = interaction.user, "You tried to bonk me? Reverse card. "
        else:
            target, prefix = member, ""
        total = self.db.add_bonk(interaction.guild_id, target.id)
        await interaction.response.send_message(
            f"{prefix}🔨 **BONK!** {target.mention} has been bonked. Lifetime bonks: **{total}**")

    @app_commands.command(name="coinflip", description="Flip a coin")
    async def coinflip(self, interaction: discord.Interaction):
        if random.random() < 0.01:
            await interaction.response.send_message("🪙 The coin landed on its **side**. Nobody wins. 😐")
            return
        await interaction.response.send_message(f"🪙 **{random.choice(['Heads', 'Tails'])}**!")

    @app_commands.command(name="8ball", description="Ask the magic 8-ball a question")
    @app_commands.describe(question="Your yes/no question")
    async def eight_ball(self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 200]):
        await interaction.response.send_message(
            f"❓ *{question}*\n🎱 {random.choice(EIGHT_BALL)}",
            allowed_mentions=discord.AllowedMentions.none())

    async def cog_app_command_error(self, interaction: discord.Interaction,
                                    error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"Chill 🧊 Try again in {error.retry_after:.0f}s.", ephemeral=True)
            return
        log.exception("Error in fun command", exc_info=error)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not config.FUN_KEYWORDS or message.author.bot or message.guild is None:
            return
        for pattern, emoji, replies in KEYWORD_PATTERNS:
            if not pattern.search(message.content):
                continue
            try:
                if emoji:
                    await message.add_reaction(emoji)
                if replies:
                    now = time.monotonic()
                    last = self.last_keyword_reply.get(message.channel.id, 0)
                    if now - last >= config.KEYWORD_COOLDOWN_SECONDS:
                        self.last_keyword_reply[message.channel.id] = now
                        await message.reply(random.choice(replies), mention_author=False)
            except discord.HTTPException:
                pass
            return  # one reaction per message is enough


async def setup(bot: commands.Bot):
    await bot.add_cog(Fun(bot))
