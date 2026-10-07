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

# Keep all jokes in very easy English: many players in the server don't speak English well.
EXCUSES = [
    "My internet was very bad.",
    "My cat walked on my keyboard.",
    "The sun was in my eyes. Yes, inside my house.",
    "My mouse had no battery.",
    "I tried a new plan. The plan was to lose.",
    "My team played like bots.",
    "The enemy was cheating. 100%.",
    "My screen was too dark. I saw nothing.",
    "My chair is not a gaming chair. What did you think?",
    "I let them win. I am very nice.",
    "A Discord sound made me scared.",
    "My little brother was playing. (I have no little brother.)",
    "I was eating with one hand.",
    "The game made me worse after the update.",
    "I sneezed. Two times.",
]

ROASTS = [
    "{name} shoots like a blind man on a boat.",
    "{name} dies more than they kill. Every game.",
    "{name} plays like their screen is off.",
    "{name} is the reason the respawn button exists.",
    "{name} lost a 1v1 to a player who was AFK.",
    "The enemy is not afraid of {name}. They are happy to see {name}.",
    "{name} only uses the white flag. 🏳️",
    "{name} gets carried so much, they should pay the team.",
    "Nobody knows what {name} was shooting at. Not even {name}.",
    "{name} has no brain in this game.",
    "If being bad was a rank, {name} would be the general.",
    "When {name} leaves, the team gets better.",
]

BLAME_REASONS = [
    "We have proof.",
    "No more questions.",
    "The judge says so. 🔨",
    "Don't say no. We know.",
    "Everybody saw it.",
    "The end.",
]

EIGHT_BALL = [
    "Yes!", "100% yes.", "Yes, for sure.", "Probably yes.", "I think yes.",
    "Ask me later.", "I won't tell you now. 🤫", "I don't know.",
    "I don't think so.", "No.", "Probably not.", "NO. Never. 💀",
]

# Keyword reactions (only active when FUN_KEYWORDS=true). Each entry: (pattern, reaction emoji or None, replies)
# Every keyword also has its Cyrillic (Russian) spelling.
KEYWORDS = [
    (r"\b(gg|гг)\b", "🫡", []),
    (r"\b(bruh|брух)\b", "💀", []),
    (r"\b(noob|нуб|нубас|bot|бот|mult|мульт)\b", "🍼", []),
    (r"\b(ez|изи)\b", None, [
        "ez? You died 40 times. 📉",
        "Easy for you, sad for them. 😢",
        "Calm down, it was only one game. 😌",
    ]),
    (r"\b(lag+(y|ging)?|лаг+(и+|ает|ают|ов|ал|ала|ало|нул)?)\b", None, [
        "📶 Lag. The number 1 excuse. Try `/excuse` for more.",
        "Lag? Or are you just bad? 🤔",
    ]),
    (r"\b(rigged|подстава|подкрутка|подкручено)\b", None, [
        "🤫 It is not rigged. Stop asking.",
        "It is fair. Very fair. For me. 🤖",
    ]),
    (r"\b(afk|афк)\b", None, [
        "💤 One more soldier is gone. Bye bye.",
    ]),
    (r"\b(who asked|кто спрашивал)\b", None, [
        "🔎 Looking for who asked… nobody found.",
    ]),
    (r"\b(pizdec+|pizdets|pzd|пиздец+|пзд)\b", None, [
        "PIZDEC ALREADY!",
    ]),
    (r"\b(suka+|сука+)\b", None, [
        "Suka?? More like Osuka!",
    ]),
    (r"\b(osaka|осака)\b", None, [
        "Osaka?? More like Osuka!",
    ]),
    (r"\b(d3s|д3с)\b", None, [
        "D3S control game, gg ez, no sweat, all easy!",
    ]),
    (r"\b(hard3st|хард3ст)\b", None, [
        "HARD3ST = MULT3ST... 👑",
    ]),
    (r"\b(tool|тул)\b", None, [
        "Toolek d3senko bystro bystro bystro",
    ]),
    (r"\b(blya+t+|blya+d|бля+ть|бля+дь|бля+)\b", None, [
        "🐻 Blyat! Quick, bring the vodka!",
        "Cyka blyat! Rush B, don't stop! 🏃",
        "Blyat? Calm down, take a deep breath. 🧘",
    ]),
    (r"\b(na ?[xh]uy|nakhuy|на ?хуй)\b", None, [
        "🚀 Naxuy! Fly fast, fly far!",
        "Naxuy? OK, you can go. No coming back. ✈️",
        "Going naxuy now. Bye bye! 👋",
    ]),
    (r"\b(yebat|ebat|[её]бать)\b", None, [
        "Yebat'! Call babushka, this is serious! 👵",
        "Yebat'… my computer is on fire. 🔥",
        "Yebat' kopat'! Calm down, it's only a game. 🫡",
    ]),
]
KEYWORD_PATTERNS = [(re.compile(p, re.IGNORECASE), emoji, replies) for p, emoji, replies in KEYWORDS]

# Reactions that are always added, even when the message also has a keyword from the list above.
ALWAYS_REACTIONS = [(re.compile(p, re.IGNORECASE), emoji) for p, emoji in [
    (r"\b(softik|софтик)\b", "🐱"),
    (r"\b(demonnuk|демоннук)\b", "🤡"),
]]


class Fun(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db: Database = bot.db
        self.last_keyword_reply = {}  # (channel_id, keyword pattern) -> timestamp

    @app_commands.command(name="excuse", description="Get an excuse for why you lost")
    async def excuse(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"🧾 {interaction.user.mention}'s excuse: *{random.choice(EXCUSES)}*",
            allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="roast", description="Say something mean (for fun) about someone")
    @app_commands.describe(member="Who to roast")
    @app_commands.checks.cooldown(2, 30, key=lambda i: (i.guild_id, i.user.id))
    async def roast(self, interaction: discord.Interaction, member: discord.Member):
        if member.id == self.bot.user.id:
            text = f"Nice try. {random.choice(ROASTS).format(name=interaction.user.mention)} 🔄"
        else:
            text = f"🔥 {random.choice(ROASTS).format(name=member.mention)}"
        await interaction.response.send_message(text)

    @app_commands.command(name="blame", description="Find out who made the team lose")
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
            f"🔍 Who made us lose?…\n📋 It was **<@{target}>**! "
            f"{random.choice(BLAME_REASONS)}",
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(target)]))

    @app_commands.command(name="bonk", description="Bonk someone")
    @app_commands.describe(member="Who to bonk")
    @app_commands.checks.cooldown(3, 60, key=lambda i: (i.guild_id, i.user.id))
    async def bonk(self, interaction: discord.Interaction, member: discord.Member):
        if member.id == self.bot.user.id:
            target, prefix = interaction.user, "You want to bonk me? No, I bonk you. "
        else:
            target, prefix = member, ""
        total = self.db.add_bonk(interaction.guild_id, target.id)
        await interaction.response.send_message(
            f"{prefix}🔨 **BONK!** {target.mention} got bonked. Total bonks: **{total}**")

    @app_commands.command(name="coinflip", description="Flip a coin")
    async def coinflip(self, interaction: discord.Interaction):
        if random.random() < 0.01:
            await interaction.response.send_message("🪙 The coin is standing on its **side**. Nobody wins. 😐")
            return
        await interaction.response.send_message(f"🪙 **{random.choice(['Heads', 'Tails'])}**!")

    @app_commands.command(name="8ball", description="Ask the magic 8-ball a question")
    @app_commands.describe(question="A question with a yes or no answer")
    async def eight_ball(self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 200]):
        await interaction.response.send_message(
            f"❓ *{question}*\n🎱 {random.choice(EIGHT_BALL)}",
            allowed_mentions=discord.AllowedMentions.none())

    async def cog_app_command_error(self, interaction: discord.Interaction,
                                    error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"Slow down 🧊 Try again in {error.retry_after:.0f} seconds.", ephemeral=True)
            return
        log.exception("Error in fun command", exc_info=error)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not config.FUN_KEYWORDS or message.author.bot or message.guild is None:
            return
        for pattern, emoji in ALWAYS_REACTIONS:
            if pattern.search(message.content):
                try:
                    await message.add_reaction(emoji)
                except discord.HTTPException:
                    pass
        for pattern, emoji, replies in KEYWORD_PATTERNS:
            if not pattern.search(message.content):
                continue
            try:
                if emoji:
                    await message.add_reaction(emoji)
                if replies:
                    now = time.monotonic()
                    key = (message.channel.id, pattern.pattern)
                    last = self.last_keyword_reply.get(key, 0)
                    if now - last >= config.KEYWORD_COOLDOWN_SECONDS:
                        self.last_keyword_reply[key] = now
                        await message.reply(random.choice(replies), mention_author=False)
            except discord.HTTPException:
                pass
            return  # one reaction per message is enough


async def setup(bot: commands.Bot):
    await bot.add_cog(Fun(bot))
