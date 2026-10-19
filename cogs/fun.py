import logging
import random
import re
import time
from pathlib import Path
from typing import Optional

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
    (r"\b(unleashed ?beast|beast|бист)\b", None, [
        "Oooo UnleashedBeast is here chat!! PUSH LIKE DEMONS!!!!!",
    ]),
    (r"\b(keitech|кейтек|кейтеч)\b", None, [
        "https://tenor.com/view/fv4005-wot-wot-blitz-world-of-tanks-shrek-rizz-gif-1282590711689902998",
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
    (r"\b(demonnuk|демоннук)\b", "♿"),
]]

# Words that make the bot reply with a sound file from the sounds folder (same cooldown as replies).
SOUNDS_DIR = Path(__file__).resolve().parent.parent / "sounds"
SOUND_KEYWORDS = [(re.compile(p, re.IGNORECASE), SOUNDS_DIR / name) for p, name in [
    (r"\b(simple|симпл)\b", "simple-dimple.mp3"),
]]


BONKBOARD_PAGE_SIZE = 10
MEDALS = ["🥇", "🥈", "🥉"]


def build_bonkboard_embed(rows: list, offset: int, page: int, pages: int) -> discord.Embed:
    """rows: (user_id, count) for one page; offset: how many users come before this page."""
    lines = []
    for rank, (user_id, count) in enumerate(rows, start=offset + 1):
        place = MEDALS[rank - 1] if rank <= len(MEDALS) else f"**{rank}.**"
        lines.append(f"{place} <@{user_id}> · **{count}** bonk{'' if count == 1 else 's'}")
    embed = discord.Embed(title="🔨 Bonk leaderboard", description="\n".join(lines),
                          color=discord.Color.orange())
    embed.set_footer(text=f"Page {page + 1} of {pages}")
    return embed


class BonkboardView(discord.ui.View):
    """Previous/Next buttons for the bonk leaderboard. Reads the database again on every page."""

    def __init__(self, db: Database, guild_id: int):
        super().__init__(timeout=300)
        self.db = db
        self.guild_id = guild_id
        self.page = 0
        self.pages = 1
        self.message: Optional[discord.Message] = None

    def render(self) -> discord.Embed:
        total = self.db.bonked_user_count(self.guild_id)
        self.pages = max(1, -(-total // BONKBOARD_PAGE_SIZE))
        self.page = max(0, min(self.page, self.pages - 1))
        offset = self.page * BONKBOARD_PAGE_SIZE
        rows = self.db.bonk_leaderboard(self.guild_id, BONKBOARD_PAGE_SIZE, offset)
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page >= self.pages - 1
        return build_bonkboard_embed(rows, offset, self.page, self.pages)

    @discord.ui.button(label="Previous", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(label="Next", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await interaction.response.edit_message(embed=self.render(), view=self)

    async def on_timeout(self):
        if self.message is None:
            return
        for item in self.children:
            item.disabled = True
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass


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

    @app_commands.command(name="bonkboard", description="See who got bonked the most")
    async def bonkboard(self, interaction: discord.Interaction):
        view = BonkboardView(self.db, interaction.guild_id)
        embed = view.render()
        if not embed.description:
            await interaction.response.send_message(
                "Nobody got bonked yet. Use `/bonk` to be the first! 🔨", ephemeral=True)
            return
        if view.pages == 1:
            await interaction.response.send_message(embed=embed)
            return
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

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

    def keyword_ready(self, channel_id: int, pattern: re.Pattern) -> bool:
        """True (and starts the cooldown) if this keyword may reply in this channel again."""
        now = time.monotonic()
        key = (channel_id, pattern.pattern)
        if now - self.last_keyword_reply.get(key, 0) < config.KEYWORD_COOLDOWN_SECONDS:
            return False
        self.last_keyword_reply[key] = now
        return True

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        if message.author.id in config.WHEELCHAIR_USER_IDS:
            try:
                await message.add_reaction("♿")
            except discord.HTTPException:
                pass
        if not config.FUN_KEYWORDS:
            return
        for pattern, emoji in ALWAYS_REACTIONS:
            if pattern.search(message.content):
                try:
                    await message.add_reaction(emoji)
                except discord.HTTPException:
                    pass
        for pattern, sound in SOUND_KEYWORDS:
            if pattern.search(message.content) and self.keyword_ready(message.channel.id, pattern):
                try:
                    await message.reply(file=discord.File(sound), mention_author=False)
                except discord.HTTPException:
                    log.warning("Could not send %s in channel %s (missing Attach Files permission?)",
                                sound.name, message.channel.id)
        for pattern, emoji, replies in KEYWORD_PATTERNS:
            if not pattern.search(message.content):
                continue
            try:
                if emoji:
                    await message.add_reaction(emoji)
                if replies and self.keyword_ready(message.channel.id, pattern):
                    await message.reply(random.choice(replies), mention_author=False)
            except discord.HTTPException:
                pass
            return  # one reaction per message is enough


async def setup(bot: commands.Bot):
    await bot.add_cog(Fun(bot))
