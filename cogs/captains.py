import json
import logging
import random
import re
import unicodedata
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs.events import truncate_field
from cogs.fun import LeaderboardView
from config import REFEREE_ROLE_IDS
from database import LOSS_POINTS, STREAK_BONUS, STREAK_MIN, WIN_POINTS, Database, Lobby, LobbyPlayer, Tiebreaker
from maps import MAP_VOTE_OPTIONS, TIEBREAKER_OPTIONS, game_format, maps_for

log = logging.getLogger(__name__)

# Keep all texts in very easy English: many players in the server don't speak English well.

MIN_PLAYERS = 4  # 2 captains + 2 to pick
MAX_PLAYERS = 127  # 2 captains + 5 menus of 25 players; a message holds at most 5 rows

TEAM_EMOJI = {"A": "🔴", "B": "🔵"}

BUTTONS = {
    # action: (label, emoji, style)
    "join": ("Join", "✅", discord.ButtonStyle.success),
    "leave": ("Leave", "🚪", discord.ButtonStyle.secondary),
    "start": ("Start", "▶️", discord.ButtonStyle.primary),
    "cancel": ("Cancel", "✖️", discord.ButtonStyle.danger),
    "claim": ("Be captain", "👑", discord.ButtonStyle.primary),
    "unclaim": ("Step down", "↩️", discord.ButtonStyle.secondary),
    "lock": ("Lock in", "🔒", discord.ButtonStyle.success),
    "snake": ("Snake draft", "🐍", discord.ButtonStyle.primary),
    "normal": ("Normal draft", "🔁", discord.ButtonStyle.primary),
    "report": ("Report win", "🏆", discord.ButtonStyle.success),
    "draw": ("Report draw", "🤝", discord.ButtonStyle.secondary),
    "approve": ("Approve", "✅", discord.ButtonStyle.success),
    "deny": ("Deny", "❌", discord.ButtonStyle.danger),
    "mapdone": ("Continue", "⏩", discord.ButtonStyle.success),
}


DRAW = "draw"  # value of lobby.winner when the game ended in a draw

DRAFT_TYPES = {
    # value: (name, order shown to players)
    "snake": ("Snake draft", "1 2 2 1 1 2 2 1 …"),
    "normal": ("Normal draft", "1 2 1 2 1 2 …"),
}


def draft_turn(pick_index: int, draft_type: str = "snake") -> str:
    """Team that makes the given (0-based) pick.
    Snake: A B B A A B B A … · Normal: A B A B …"""
    if draft_type == "normal":
        return "A" if pick_index % 2 == 0 else "B"
    return "A" if pick_index % 2 == (pick_index // 2) % 2 else "B"


def draft_label_of(draft_type: str) -> str:
    name, order = DRAFT_TYPES.get(draft_type, DRAFT_TYPES["snake"])
    return f"{name} ({order})"


def draft_label(lobby: Lobby) -> str:
    return draft_label_of(lobby.draft_type)


def other_team(team: str) -> str:
    return "B" if team == "A" else "A"


def other_captain(lobby: Lobby, user_id: int) -> Optional[int]:
    """The captain who is not user_id."""
    return lobby.captain_b if user_id == lobby.captain_a else lobby.captain_a


def confirmers(lobby: Lobby) -> List[int]:
    """Captains who can confirm the reported result: the other captain, or both if a referee reported it."""
    if lobby.team_of_captain(lobby.reported_by) is not None:
        return [other_captain(lobby, lobby.reported_by)]
    return [lobby.captain_a, lobby.captain_b]


def mention_list(user_ids: List[int]) -> str:
    return " or ".join(f"<@{user_id}>" for user_id in user_ids)


def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.manage_guild


def is_referee(interaction: discord.Interaction) -> bool:
    """Has the REFEREE_ROLE_ID role: can report and approve results without being a captain."""
    roles = getattr(interaction.user, "roles", [])
    return any(role.id in REFEREE_ROLE_IDS for role in roles)


def safe(name: str) -> str:
    return discord.utils.escape_markdown(name)


def team_name(lobby: Lobby, players: List[LobbyPlayer], team: str) -> str:
    names = {p.user_id: p.name for p in players}
    return f"Team {names.get(lobby.captain_of(team), team)}"


def team_members(players: List[LobbyPlayer], team: str) -> List[LobbyPlayer]:
    return sorted((p for p in players if p.team == team), key=lambda p: p.pick_no or 0)


class LobbyButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"lobby:(?P<action>join|leave|start|cancel|claim|unclaim|lock|snake|normal|report|draw|approve|deny|mapdone)"
                           r":(?P<id>\d+)"):
    """All buttons of a captains game. The lobby ID lives in the custom_id, so they keep working after a restart."""

    def __init__(self, action: str, lobby_id: int, disabled: bool = False):
        label, emoji, style = BUTTONS[action]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, disabled=disabled,
                                           custom_id=f"lobby:{action}:{lobby_id}"))
        self.action = action
        self.lobby_id = lobby_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                             match: re.Match[str], /):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        cog: "Captains" = interaction.client.get_cog("Captains")
        await cog.handle_button(interaction, self.action, self.lobby_id)


class DraftSelect(discord.ui.DynamicItem[discord.ui.Select],
                  template=r"lobby:pick:(?P<id>\d+):(?P<chunk>\d+)"):
    """Menu a captain uses to pick a player. Large pools are split over several menus of 25."""

    def __init__(self, lobby_id: int, chunk: int, players: List[tuple], placeholder: str):
        select = discord.ui.Select(
            custom_id=f"lobby:pick:{lobby_id}:{chunk}",
            placeholder=placeholder,
            options=[discord.SelectOption(label=name[:100], value=str(uid)) for uid, name in players],
        )
        super().__init__(select)
        self.lobby_id = lobby_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select,
                             match: re.Match[str], /):
        players = [(int(o.value), o.label) for o in item.options]
        return cls(int(match["id"]), int(match["chunk"]), players, item.placeholder or "")

    async def callback(self, interaction: discord.Interaction):
        cog: "Captains" = interaction.client.get_cog("Captains")
        await cog.handle_pick(interaction, self.lobby_id, int(interaction.data["values"][0]))


class MapVoteButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"lobby:vote:(?P<id>\d+):(?P<index>\d+)"):
    """A map in the map vote after the draft. Every player in the game can click one."""

    def __init__(self, lobby_id: int, index: int, map_name: str):
        super().__init__(discord.ui.Button(label=map_name[:80], emoji="🗺️", style=discord.ButtonStyle.secondary,
                                           custom_id=f"lobby:vote:{lobby_id}:{index}"))
        self.lobby_id = lobby_id
        self.index = index

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                             match: re.Match[str], /):
        return cls(int(match["id"]), int(match["index"]), item.label or "Map")

    async def callback(self, interaction: discord.Interaction):
        cog: "Captains" = interaction.client.get_cog("Captains")
        await cog.handle_map_vote(interaction, self.lobby_id, self.index)


class TiebreakerBanButton(discord.ui.DynamicItem[discord.ui.Button],
                          template=r"tiebreaker:ban:(?P<id>\d+):(?P<index>\d+)"):
    """'Ban <map>' on a /tiebreaker message. Each captain bans 1 map."""

    def __init__(self, tiebreaker_id: int, index: int, map_name: str, disabled: bool = False):
        super().__init__(discord.ui.Button(label=f"Ban {map_name}"[:80], emoji="🚫", style=discord.ButtonStyle.danger,
                                           disabled=disabled, custom_id=f"tiebreaker:ban:{tiebreaker_id}:{index}"))
        self.tiebreaker_id = tiebreaker_id
        self.index = index

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                             match: re.Match[str], /):
        return cls(int(match["id"]), int(match["index"]), (item.label or "").removeprefix("Ban "))

    async def callback(self, interaction: discord.Interaction):
        cog: "Captains" = interaction.client.get_cog("Captains")
        await cog.handle_ban(interaction, self.tiebreaker_id, self.index)


class RefereeWinnerView(discord.ui.View):
    """Private buttons for a referee who is not a captain: which team won?"""

    def __init__(self, cog: "Captains", lobby: Lobby, players: List[LobbyPlayer]):
        super().__init__(timeout=300)
        for team, style in (("A", discord.ButtonStyle.danger), ("B", discord.ButtonStyle.primary)):
            button = discord.ui.Button(label=f"{team_name(lobby, players, team)} won"[:80],
                                       emoji=TEAM_EMOJI[team], style=style)
            button.callback = self.make_callback(cog, lobby.id, team)
            self.add_item(button)

    @staticmethod
    def make_callback(cog: "Captains", lobby_id: int, team: str):
        async def callback(interaction: discord.Interaction):
            await cog.referee_pick_winner(interaction, lobby_id, team)
        return callback


# --- queue message -----------------------------------------------------------

def build_queue_embed(lobby: Lobby, players: List[LobbyPlayer]) -> discord.Embed:
    embed = discord.Embed(title="⚔️ Captains queue", color=discord.Color.blurple())
    lines = [f"{i}. {safe(p.name)}" for i, p in enumerate(players, start=1)]
    embed.add_field(name=f"Players ({len(players)})",
                    value=truncate_field("\n".join(lines) or "Nobody yet"), inline=False)
    if lobby.status == "queue":
        embed.description = (f"Click **Join** to play. Host: <@{lobby.creator_id}>\n"
                             "When the number of players is even, the host clicks **Start**.")
        if len(players) < MIN_PLAYERS:
            embed.set_footer(text=f"Need at least {MIN_PLAYERS} players to start.")
        elif len(players) % 2:
            embed.set_footer(text="Odd number of players. Need 1 more (or 1 less) to start.")
        else:
            embed.set_footer(text="Ready! The host can start now.")
    elif lobby.status == "cancelled":
        embed.color = discord.Color.dark_grey()
        embed.description = "This game was cancelled."
    else:
        embed.color = discord.Color.green()
        embed.description = "✅ Started! The game is in the message below."
    return embed


def build_queue_view(lobby: Lobby, players: List[LobbyPlayer]) -> Optional[discord.ui.View]:
    if lobby.status != "queue":
        return None
    can_start = len(players) >= MIN_PLAYERS and len(players) % 2 == 0
    view = discord.ui.View(timeout=None)
    view.add_item(LobbyButton("join", lobby.id))
    view.add_item(LobbyButton("leave", lobby.id))
    view.add_item(LobbyButton("start", lobby.id, disabled=not can_start))
    view.add_item(LobbyButton("cancel", lobby.id))
    return view


# --- game message (captains, draft, result) ------------------------------------

def vote_counts(options: List[str], votes: dict) -> List[int]:
    """Number of votes per map, in the same order as options."""
    counts = [0] * len(options)
    for choice in votes.values():
        if 0 <= choice < len(options):
            counts[choice] += 1
    return counts


def pick_map(options: List[str], votes: dict) -> tuple:
    """(winning map, True if it was a tie and chosen at random). No votes at all is a tie between all maps."""
    counts = vote_counts(options, votes)
    best = [m for m, c in zip(options, counts) if c == max(counts)]
    return random.choice(best), len(best) > 1


def build_game_embed(lobby: Lobby, players: List[LobbyPlayer], votes: Optional[dict] = None) -> discord.Embed:
    names = {p.user_id: p.name for p in players}

    if lobby.status == "captains" or (lobby.status == "cancelled" and not any(p.team for p in players)):
        embed = discord.Embed(title="👑 Choose captains", color=discord.Color.gold())
        for number, captain in ((1, lobby.captain_a), (2, lobby.captain_b)):
            embed.add_field(name=f"Captain {number}",
                            value=safe(names[captain]) if captain in names else "*free*", inline=True)
        others = [safe(p.name) for p in players if p.user_id not in (lobby.captain_a, lobby.captain_b)]
        embed.add_field(name=f"Players ({len(players)})", value=truncate_field(", ".join(others) or "-"),
                        inline=False)
        if lobby.status == "cancelled":
            embed.color = discord.Color.dark_grey()
            embed.description = "This game was cancelled."
        else:
            embed.description = ("Who wants to be captain? Click **Be captain**.\n"
                                 "When there are 2 captains, a captain clicks **Lock in** to start the draft.")
            embed.set_footer(text="A coin flip decides which captain picks first.")
        return embed

    embed = discord.Embed(title=f"⚔️ Captains draft · Game {lobby.id}", color=discord.Color.red())
    for team in ("A", "B"):
        crown = "🏆 " if lobby.status == "done" and lobby.winner == team else ""
        # Number each team on its own: the captain is 1, then 2, 3, 4 …
        lines = [f"{number}. {'👑 ' if p.pick_no == 0 else ''}{safe(p.name)}"
                 for number, p in enumerate(team_members(players, team), start=1)]
        embed.add_field(name=f"{crown}{TEAM_EMOJI[team]} {team_name(lobby, players, team)}"[:256],
                        value=truncate_field("\n".join(lines)), inline=True)
    available = [p for p in players if p.team is None]
    if available:
        embed.add_field(name=f"Available ({len(available)})",
                        value=truncate_field(", ".join(safe(p.name) for p in available)), inline=False)
    if lobby.map_name and lobby.status != "mapvote":
        embed.add_field(name="🗺️ Map", value=f"**{safe(lobby.map_name)}**", inline=False)

    if lobby.status == "choosing":
        embed.description = ("A captain chooses the draft type:\n"
                             f"🐍 **{draft_label_of('snake')}**: the order goes back and forth.\n"
                             f"🔁 **{draft_label_of('normal')}**: the teams take turns.")
        embed.set_footer(text=f"Coin flip: {team_name(lobby, players, 'A')} picks first.")
    elif lobby.status == "drafting":
        picks_done = sum(1 for p in players if p.pick_no)
        captain = lobby.captain_of(draft_turn(picks_done, lobby.draft_type))
        embed.description = f"Now picking: <@{captain}> (pick {picks_done + 1} of {len(players) - 2})"
        embed.set_footer(text=f"{draft_label(lobby)} · Only the captain whose turn it is can pick.")
    elif lobby.status == "mapvote":
        votes = votes or {}
        embed.color = discord.Color.teal()
        embed.description = ("🗺️ **Vote for the map!** Everyone in the game: click a map. You can change your vote.\n"
                             "The vote ends when everyone voted, or when a captain clicks **Continue**.")
        counts = vote_counts(lobby.maps, votes)
        lines = [f"**{safe(m)}** · {c} vote{'' if c == 1 else 's'}" for m, c in zip(lobby.maps, counts)]
        voters = [p for p in players if p.team]
        embed.add_field(name=f"Maps ({sum(1 for p in voters if p.user_id in votes)} of {len(voters)} voted)",
                        value="\n".join(lines), inline=False)
        waiting = [safe(p.name) for p in voters if p.user_id not in votes]
        if waiting:
            embed.add_field(name="Did not vote yet", value=truncate_field(", ".join(waiting)), inline=False)
        embed.set_footer(text="Same number of votes? The bot picks one of them at random.")
    elif lobby.status == "playing":
        embed.color = discord.Color.green()
        embed.description = ("✅ Draft complete! After the game, the winning captain clicks **Report win**.\n"
                             "Nobody won? A captain clicks **Report draw**.")
        if REFEREE_ROLE_IDS:
            embed.description += "\nA referee can also report the result."
    elif lobby.status == "reported":
        embed.color = discord.Color.gold()
        embed.description = (f"{claim_text(lobby, players)}\n"
                             f"⏳ Waiting for {mention_list(confirmers(lobby))} to approve. See the message below.")
    elif lobby.status == "done":
        embed.color = discord.Color.gold()
        if lobby.winner == DRAW:
            embed.description = "🤝 The game was a **draw**!"
            embed.set_footer(text="Everyone got +1 draw and 0 points. See /leaderboard")
        else:
            embed.description = f"🏆 **{team_name(lobby, players, lobby.winner)}** won!"
            embed.set_footer(text=f"Winners got +{WIN_POINTS} points (+{STREAK_BONUS} on a win streak), "
                                  f"losers -{LOSS_POINTS} points. See /leaderboard")
        if lobby.reported_by and lobby.team_of_captain(lobby.reported_by) is None:
            embed.description += f"\nReported by referee <@{lobby.reported_by}>."
    elif lobby.status == "removed":
        embed.color = discord.Color.dark_grey()
        embed.description = "🗑️ A referee removed this game. It does not count."
    else:
        embed.color = discord.Color.dark_grey()
        embed.description = "This game was cancelled."
    return embed


def build_game_view(lobby: Lobby, players: List[LobbyPlayer]) -> Optional[discord.ui.View]:
    view = discord.ui.View(timeout=None)
    if lobby.status == "captains":
        both = lobby.captain_a is not None and lobby.captain_b is not None
        view.add_item(LobbyButton("claim", lobby.id, disabled=both))
        view.add_item(LobbyButton("unclaim", lobby.id))
        view.add_item(LobbyButton("lock", lobby.id, disabled=not both))
        view.add_item(LobbyButton("cancel", lobby.id))
    elif lobby.status == "choosing":
        view.add_item(LobbyButton("snake", lobby.id))
        view.add_item(LobbyButton("normal", lobby.id))
    elif lobby.status == "drafting":
        available = [(p.user_id, p.name) for p in players if p.team is None]
        chunks = [available[i:i + 25] for i in range(0, len(available), 25)]
        for index, chunk in enumerate(chunks):
            placeholder = "Pick a player…"
            if len(chunks) > 1:
                placeholder = f"Pick a player ({index * 25 + 1}–{index * 25 + len(chunk)})…"
            view.add_item(DraftSelect(lobby.id, index, chunk, placeholder))
    elif lobby.status == "mapvote":
        for index, map_name in enumerate(lobby.maps):
            view.add_item(MapVoteButton(lobby.id, index, map_name))
        view.add_item(LobbyButton("mapdone", lobby.id))
    elif lobby.status == "playing":
        view.add_item(LobbyButton("report", lobby.id))
        view.add_item(LobbyButton("draw", lobby.id))
    else:  # reported: Approve / Deny are on a separate message, see build_approval_view
        return None
    return view


def result_text(lobby: Lobby, players: List[LobbyPlayer]) -> str:
    """'the game was a **draw**' or '**Team X** won'."""
    if lobby.winner == DRAW:
        return "the game was a **draw**"
    return f"**{team_name(lobby, players, lobby.winner)}** won"


def claim_text(lobby: Lobby, players: List[LobbyPlayer]) -> str:
    """What the reporter said, e.g. '🏆 @Bob says **Team Bob** won.'"""
    who = f"<@{lobby.reported_by}>"
    if lobby.team_of_captain(lobby.reported_by) is None:
        who = f"Referee {who}"
    return f"{'🤝' if lobby.winner == DRAW else '🏆'} {who} says {result_text(lobby, players)}."


def build_tiebreaker_embed(tiebreaker: Tiebreaker, lobby: Lobby, players: List[LobbyPlayer]) -> discord.Embed:
    names = {p.user_id: p.name for p in players}
    banned_by = {tiebreaker.ban_a: lobby.captain_a, tiebreaker.ban_b: lobby.captain_b}
    lines = []
    for index, map_name in enumerate(tiebreaker.maps):
        if index in tiebreaker.banned:
            lines.append(f"🚫 ~~{safe(map_name)}~~ · banned by {safe(names.get(banned_by[index], 'a captain'))}")
        elif tiebreaker.final_map:
            lines.append(f"✅ **{safe(map_name)}**")
        else:
            lines.append(f"🗺️ **{safe(map_name)}**")
    embed = discord.Embed(title=f"⚔️ Tiebreaker · Game {lobby.id}", description="\n".join(lines),
                          color=discord.Color.orange())
    if tiebreaker.final_map:
        embed.color = discord.Color.green()
        embed.add_field(name="🗺️ Tiebreaker map", value=f"**{safe(tiebreaker.final_map)}**! Good luck! 🍀",
                        inline=False)
    else:
        waiting = [captain for captain, ban in ((lobby.captain_a, tiebreaker.ban_a),
                                                (lobby.captain_b, tiebreaker.ban_b)) if ban is None]
        embed.add_field(name="How it works",
                        value=("Each captain bans 1 map. The last map is the tiebreaker map.\n"
                               f"Waiting for: {' and '.join(f'<@{c}>' for c in waiting)}"), inline=False)
    return embed


def build_tiebreaker_view(tiebreaker: Tiebreaker) -> Optional[discord.ui.View]:
    if tiebreaker.final_map:
        return None
    view = discord.ui.View(timeout=None)
    for index, map_name in enumerate(tiebreaker.maps):
        view.add_item(TiebreakerBanButton(tiebreaker.id, index, map_name, disabled=index in tiebreaker.banned))
    return view


def build_approval_view(lobby: Lobby) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(LobbyButton("approve", lobby.id))
    view.add_item(LobbyButton("deny", lobby.id))
    return view


NAME_WIDTH = 15  # leaderboard name column; a whole row is 32 characters, so it fits on a phone


def char_width(char: str) -> int:
    """Columns a character takes in a monospace font: 2 for wide (e.g. Chinese) characters, 0 for accents."""
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def fit(text: str, width: int) -> str:
    """Cuts text to the given number of columns ("…" when cut) and pads it with spaces to exactly that width."""
    text = "".join(c for c in text if unicodedata.category(c) not in ("Cc", "Cf", "So", "Cs"))  # no emoji
    text = " ".join(text.split()).replace("`", "'")  # a backtick would end the monospace tag
    if sum(map(char_width, text)) > width:
        cut, used = "", 0
        for char in text:
            if used + char_width(char) > width - 1:  # keep 1 column for "…"
                break
            cut += char
            used += char_width(char)
        text = cut.rstrip() + "…"
    return text + " " * (width - sum(map(char_width, text)))


# Rank tiers by ranked points, highest first: (minimum points, name, emoji)
TIERS = [
    (800, "Master", "👑"),
    (550, "Diamond", "💎"),
    (350, "Platinum", "💠"),
    (200, "Gold", "🥇"),
    (100, "Silver", "🥈"),
    (0, "Bronze", "🥉"),
]


def tier_of(points: int) -> tuple:
    """(name, emoji) of the tier for these points."""
    return next((name, emoji) for minimum, name, emoji in TIERS if points >= minimum)


def next_tier(points: int) -> Optional[tuple]:
    """(points still needed, name, emoji) of the next tier, or None at the top tier."""
    higher = [(minimum - points, name, emoji) for minimum, name, emoji in TIERS if minimum > points]
    return higher[-1] if higher else None


def streak_text(streak: int) -> str:
    """'🔥3' while on a win streak, else ''."""
    return f"🔥{streak}" if streak >= STREAK_MIN else ""


def build_leaderboard_embed(rows: list, offset: int, page: int, pages: int) -> discord.Embed:
    """rows: (name, points, wins, losses, draws, streak) for one page. Plain names, so nobody gets pinged.
    One row per player in a small monospace tag, so the columns line up. The tier emoji and win streak
    come after the tag (emoji have no fixed width). A row is short enough to not wrap on a phone."""
    lines = [f"` #  {fit('Player', NAME_WIDTH)}  Pts  W  L`"]
    for rank, (name, points, wins, losses, draws, streak) in enumerate(rows, start=offset + 1):
        extra = " ".join(part for part in (tier_of(points)[1], streak_text(streak)) if part)
        lines.append(f"`{rank:>2}  {fit(name, NAME_WIDTH)} {points:>4} {wins:>2} {losses:>2}` {extra}")
    embed = discord.Embed(title="🏆 Captains leaderboard", description="\n".join(lines),
                          color=discord.Color.gold())
    tiers = " · ".join(f"{emoji} {minimum}" for minimum, name, emoji in reversed(TIERS))
    embed.set_footer(text=f"Page {page + 1} of {pages} · Pts = points · W = wins · L = losses · "
                          f"🔥 = wins in a row (+{STREAK_BONUS} per win)\nTiers: {tiers}")
    return embed


# --- duos and head-to-head ------------------------------------------------------

DUO_MIN_GAMES = 3  # games together needed to show up in /duos
DUO_WIDTH = 17     # /duos name column; a whole row is 32 characters, so it fits on a phone

VS_NEVER = [
    "{a} and {b} never played against each other. Time for a fight! ⚔️",
    "No games against each other yet. Who is better? Nobody knows! 🤔",
    "{a} vs {b}? This fight never happened. Make it happen! 🥊",
]
VS_TIE = [
    "Exactly the same! Nobody is the boss here. 🤝",
    "Equal! The next game decides who is the boss. 👀",
    "Same number of wins. Very close fight! ⚖️",
]
VS_LEAD = [
    "{leader} is the boss here! 👑",
    "{loser} needs revenge! 😤",
    "{leader} wins more. {loser}, train more! 🏋️",
    "{leader} is happy. {loser} is not. 😅",
    "Next game, {loser}! You can do it! 💪",
]
VS_CRUSH = [
    "{loser} never won against {leader}. Ouch! 💀",
    "{leader} owns {loser}! 😎",
    "{loser}, maybe play on the same team as {leader}… 😂",
]

DUO_NEVER = [
    "{a} and {b} never played in the same team. Try it! 🤝",
    "No games together yet. Maybe you are a dream team? Find out! ✨",
]
DUO_GREAT = [
    "Dream team! 🔥",
    "Best friends, best players! 💪",
    "Together you are very strong! 🦾",
    "The other team should be scared of you two! 😱",
]
DUO_OK = [
    "Not bad together! 👍",
    "A good team. Keep going! 🙂",
    "Sometimes you win, sometimes you lose. Normal! 🤷",
]
DUO_BAD = [
    "Maybe play on different teams… 😅",
    "This duo needs more training! 🏋️",
    "Together you lose a lot. But you have fun, right? 😂",
    "Bad luck or bad teamwork? 🤔",
]


def head_to_head(games: list, a: int, b: int) -> tuple:
    """(wins of a, wins of b, draws) in games where a and b were on different teams."""
    a_wins = b_wins = draws = 0
    for _, winner, teams in games:
        if a not in teams or b not in teams or teams[a] == teams[b]:
            continue
        if winner == teams[a]:
            a_wins += 1
        elif winner == teams[b]:
            b_wins += 1
        else:
            draws += 1
    return a_wins, b_wins, draws


def duo_records(games: list) -> dict:
    """(user_a, user_b) with user_a < user_b -> [wins, losses, draws] when they were on the same team."""
    records = {}
    for _, winner, teams in games:
        for team in ("A", "B"):
            members = sorted(user_id for user_id, t in teams.items() if t == team)
            outcome = 0 if winner == team else 2 if winner not in ("A", "B") else 1
            for i, first in enumerate(members):
                for second in members[i + 1:]:
                    records.setdefault((first, second), [0, 0, 0])[outcome] += 1
    return records


def short_player(name: str) -> str:
    """'Danek / Danila / King RU' -> 'Danek': the first part of a 'name / nickname' name."""
    return name.split(" / ")[0].strip() or name


class RemoveGameView(discord.ui.View):
    """Private Yes / No buttons after /game remove."""

    def __init__(self, cog: "Captains", lobby_id: int):
        super().__init__(timeout=120)
        self.cog = cog
        self.lobby_id = lobby_id

    @discord.ui.button(label="Yes, remove it", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.confirm_remove(interaction, self.lobby_id)

    @discord.ui.button(label="No, keep it", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="OK, nothing changed. The game stays.", embed=None, view=None)


def describe_game(lobby: Lobby, players: List[LobbyPlayer]) -> str:
    """'Team Bob vs Team Ann → Team Bob won' for lists and confirmations."""
    result = "draw" if lobby.winner == DRAW else f"{team_name(lobby, players, lobby.winner)} won"
    return f"{team_name(lobby, players, 'A')} vs {team_name(lobby, players, 'B')} → {result}"


class Captains(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db: Database = bot.db

    # --- buttons ------------------------------------------------------------

    async def handle_button(self, interaction: discord.Interaction, action: str, lobby_id: int):
        lobby = self.db.get_lobby(lobby_id)
        if lobby is None:
            await interaction.response.send_message("This game does not exist anymore.", ephemeral=True)
            return
        handler = getattr(self, f"on_{action}")
        await handler(interaction, lobby, self.db.lobby_players(lobby.id))

    async def refuse(self, interaction: discord.Interaction, text: str) -> None:
        await interaction.response.send_message(text, ephemeral=True)

    async def show_queue(self, interaction: discord.Interaction, lobby: Lobby) -> None:
        """Updates the queue message the button was clicked on."""
        lobby = self.db.get_lobby(lobby.id)
        players = self.db.lobby_players(lobby.id)
        await interaction.response.edit_message(embed=build_queue_embed(lobby, players),
                                                view=build_queue_view(lobby, players))

    async def show_game(self, interaction: discord.Interaction, lobby: Lobby) -> None:
        """Updates the game message the button was clicked on."""
        lobby = self.db.get_lobby(lobby.id)
        players = self.db.lobby_players(lobby.id)
        await interaction.response.edit_message(content=None,
                                                embed=build_game_embed(lobby, players, self.db.map_votes(lobby.id)),
                                                view=build_game_view(lobby, players))

    async def on_join(self, interaction: discord.Interaction, lobby: Lobby, players: List[LobbyPlayer]):
        if lobby.status != "queue":
            return await self.refuse(interaction, "This queue is closed. The game already started.")
        if any(p.user_id == interaction.user.id for p in players):
            return await self.refuse(interaction, "You are already in the queue.")
        if len(players) >= MAX_PLAYERS:
            return await self.refuse(interaction, f"The queue is full ({MAX_PLAYERS} players).")
        self.db.add_lobby_player(lobby.id, interaction.user.id, interaction.user.display_name)
        await self.show_queue(interaction, lobby)

    async def on_leave(self, interaction: discord.Interaction, lobby: Lobby, players: List[LobbyPlayer]):
        if lobby.status != "queue":
            return await self.refuse(interaction, "The game already started. You can't leave now.")
        if not self.db.remove_lobby_player(lobby.id, interaction.user.id):
            return await self.refuse(interaction, "You are not in the queue.")
        await self.show_queue(interaction, lobby)

    async def on_start(self, interaction: discord.Interaction, lobby: Lobby, players: List[LobbyPlayer]):
        if lobby.status != "queue":
            return await self.refuse(interaction, "This game already started.")
        if interaction.user.id != lobby.creator_id and not is_admin(interaction):
            return await self.refuse(interaction, f"Only the host (<@{lobby.creator_id}>) can start.")
        if len(players) < MIN_PLAYERS:
            return await self.refuse(interaction, f"You need at least **{MIN_PLAYERS} players** to start.")
        if len(players) % 2:
            return await self.refuse(
                interaction, f"There are **{len(players)} players**. The teams must be even. "
                             "Wait for 1 more player, or 1 player must leave.")

        self.db.update_lobby(lobby.id, status="captains")
        await self.show_queue(interaction, lobby)
        lobby = self.db.get_lobby(lobby.id)
        message = await interaction.followup.send(
            embed=build_game_embed(lobby, players), view=build_game_view(lobby, players), wait=True)
        self.db.update_lobby(lobby.id, draft_message_id=message.id)

    async def on_cancel(self, interaction: discord.Interaction, lobby: Lobby, players: List[LobbyPlayer]):
        if lobby.status not in ("queue", "captains"):
            return await self.refuse(interaction, "This game can't be cancelled now.")
        if interaction.user.id != lobby.creator_id and not is_admin(interaction):
            return await self.refuse(interaction, f"Only the host (<@{lobby.creator_id}>) can cancel.")
        was_queue = lobby.status == "queue"
        self.db.update_lobby(lobby.id, status="cancelled")
        if was_queue:
            await self.show_queue(interaction, lobby)
            return
        await self.show_game(interaction, lobby)
        if lobby.queue_message_id:
            lobby = self.db.get_lobby(lobby.id)
            try:
                await interaction.channel.get_partial_message(lobby.queue_message_id).edit(
                    embed=build_queue_embed(lobby, players), view=None)
            except discord.HTTPException as e:
                log.warning("Could not update queue message of lobby %s: %s", lobby.id, e)

    async def on_claim(self, interaction: discord.Interaction, lobby: Lobby, players: List[LobbyPlayer]):
        if lobby.status != "captains":
            return await self.refuse(interaction, "Captains are already chosen.")
        if not any(p.user_id == interaction.user.id for p in players):
            return await self.refuse(interaction, "Only players in this game can be captain.")
        if interaction.user.id in (lobby.captain_a, lobby.captain_b):
            return await self.refuse(interaction, "You are already a captain.")
        if lobby.captain_a is None:
            self.db.update_lobby(lobby.id, captain_a=interaction.user.id)
        elif lobby.captain_b is None:
            self.db.update_lobby(lobby.id, captain_b=interaction.user.id)
        else:
            return await self.refuse(interaction, "There are already 2 captains.")
        await self.show_game(interaction, lobby)

    async def on_unclaim(self, interaction: discord.Interaction, lobby: Lobby, players: List[LobbyPlayer]):
        if lobby.status != "captains":
            return await self.refuse(interaction, "The draft already started. You can't step down now.")
        if interaction.user.id == lobby.captain_a:
            self.db.update_lobby(lobby.id, captain_a=None)
        elif interaction.user.id == lobby.captain_b:
            self.db.update_lobby(lobby.id, captain_b=None)
        else:
            return await self.refuse(interaction, "You are not a captain.")
        await self.show_game(interaction, lobby)

    async def on_lock(self, interaction: discord.Interaction, lobby: Lobby, players: List[LobbyPlayer]):
        if lobby.status != "captains":
            return await self.refuse(interaction, "The draft already started.")
        if lobby.captain_a is None or lobby.captain_b is None:
            return await self.refuse(interaction, "You need 2 captains first.")
        if interaction.user.id not in (lobby.captain_a, lobby.captain_b) and not is_admin(interaction):
            return await self.refuse(interaction, "Only a captain can lock in.")

        first, second = random.sample([lobby.captain_a, lobby.captain_b], 2)
        self.db.update_lobby(lobby.id, status="choosing", captain_a=first, captain_b=second)
        self.db.assign_pick(lobby.id, first, "A", 0)
        self.db.assign_pick(lobby.id, second, "B", 0)
        await self.show_game(interaction, lobby)

    async def choose_draft(self, interaction: discord.Interaction, lobby: Lobby, draft_type: str):
        if lobby.status != "choosing":
            return await self.refuse(interaction, "The draft type is already chosen.")
        if interaction.user.id not in (lobby.captain_a, lobby.captain_b) and not is_admin(interaction):
            return await self.refuse(interaction, "Only a captain can choose the draft type.")
        self.db.update_lobby(lobby.id, status="drafting", draft_type=draft_type)
        await self.show_game(interaction, lobby)

    async def on_snake(self, interaction: discord.Interaction, lobby: Lobby, players: List[LobbyPlayer]):
        await self.choose_draft(interaction, lobby, "snake")

    async def on_normal(self, interaction: discord.Interaction, lobby: Lobby, players: List[LobbyPlayer]):
        await self.choose_draft(interaction, lobby, "normal")

    async def handle_pick(self, interaction: discord.Interaction, lobby_id: int, user_id: int):
        lobby = self.db.get_lobby(lobby_id)
        if lobby is None or lobby.status != "drafting":
            await interaction.response.send_message("This draft is over.", ephemeral=True)
            return

        # No awaits between reading the turn and saving the pick, so two quick clicks can't both count.
        players = self.db.lobby_players(lobby.id)
        picks_done = sum(1 for p in players if p.pick_no)
        team = draft_turn(picks_done, lobby.draft_type)
        captain = lobby.captain_of(team)
        if interaction.user.id != captain:
            if interaction.user.id in (lobby.captain_a, lobby.captain_b):
                text = f"It's not your turn. Waiting for <@{captain}> to pick."
            else:
                text = f"Only the captains can pick. It's <@{captain}>'s turn."
            await interaction.response.send_message(text, ephemeral=True)
            return
        target = next((p for p in players if p.user_id == user_id), None)
        if target is None or target.team is not None:
            await interaction.response.send_message("That player was already picked.", ephemeral=True)
            return

        self.db.assign_pick(lobby.id, user_id, team, picks_done + 1)
        remaining = [p for p in players if p.team is None and p.user_id != user_id]
        if len(remaining) == 1:
            last, last_team = remaining[0], draft_turn(picks_done + 1, lobby.draft_type)
            self.db.assign_pick(lobby.id, last.user_id, last_team, picks_done + 2)
            remaining = []
        if not remaining:
            choices = maps_for(len(players))
            if choices:
                options = random.sample(choices, min(MAP_VOTE_OPTIONS, len(choices)))
                self.db.update_lobby(lobby.id, status="mapvote", map_options=json.dumps(options))
            else:  # no map list for this format (e.g. 11v11): skip the vote
                self.db.update_lobby(lobby.id, status="playing")

        await self.show_game(interaction, lobby)
        if not remaining:
            await self.make_event_team_channels(interaction, self.db.get_lobby(lobby.id),
                                                self.db.lobby_players(lobby.id))

    async def handle_map_vote(self, interaction: discord.Interaction, lobby_id: int, index: int):
        lobby = self.db.get_lobby(lobby_id)
        if lobby is None or lobby.status != "mapvote":
            await interaction.response.send_message("The map vote is over.", ephemeral=True)
            return
        players = [p for p in self.db.lobby_players(lobby.id) if p.team]
        if not any(p.user_id == interaction.user.id for p in players):
            return await self.refuse(interaction, "Only players in this game can vote for the map.")
        if not 0 <= index < len(lobby.maps):
            return await self.refuse(interaction, "This map is not in the vote.")
        # No awaits between saving the vote and checking if everyone voted, so the vote can't end twice.
        self.db.set_map_vote(lobby.id, interaction.user.id, index)
        votes = self.db.map_votes(lobby.id)
        if all(p.user_id in votes for p in players):
            return await self.finish_map_vote(interaction, lobby, votes)
        await self.show_game(interaction, lobby)

    async def on_mapdone(self, interaction: discord.Interaction, lobby: Lobby, players: List[LobbyPlayer]):
        if lobby.status != "mapvote":
            return await self.refuse(interaction, "The map vote is already over.")
        if interaction.user.id not in (lobby.captain_a, lobby.captain_b) and not is_admin(interaction):
            return await self.refuse(interaction, "Only a captain can end the map vote.")
        await self.finish_map_vote(interaction, lobby, self.db.map_votes(lobby.id))

    async def finish_map_vote(self, interaction: discord.Interaction, lobby: Lobby, votes: dict):
        """Saves the map with the most votes (random on a tie) and starts the game."""
        chosen, was_tie = pick_map(lobby.maps, votes)
        self.db.update_lobby(lobby.id, status="playing", map_name=chosen)
        await self.show_game(interaction, lobby)
        text = f"🗺️ Game {lobby.id}: the map is **{safe(chosen)}**! Good luck and have fun! 🍀"
        if was_tie:
            text += "\n-# Same number of votes, so the bot picked one at random. 🎲"
        await interaction.followup.send(text)

    async def make_event_team_channels(self, interaction: discord.Interaction, lobby: Lobby,
                                       players: List[LobbyPlayer]) -> None:
        """In an event channel, each team gets a role and voice channel (removed when the event ends)."""
        events = self.bot.get_cog("Events")
        event = self.db.get_event(lobby.event_id) if lobby.event_id else None
        if events is None or event is None or not event.active:
            return
        for old in self.db.lobbies_with_teams(event.id):
            await events.delete_team_channels(interaction.guild, old)
        await events.create_team_channels(interaction.guild, interaction.channel, event, lobby, players)

    async def on_report(self, interaction: discord.Interaction, lobby: Lobby, players: List[LobbyPlayer]):
        if lobby.status != "playing":
            return await self.refuse(interaction, "A result was already reported.")
        team = lobby.team_of_captain(interaction.user.id)
        if team is not None:
            return await self.report_result(interaction, lobby, team)
        if not is_referee(interaction):
            return await self.refuse(interaction, "Only the captain of the winning team can report the win.")
        # A referee who is not a captain chooses the winning team with private buttons.
        await interaction.response.send_message("Which team won?", view=RefereeWinnerView(self, lobby, players),
                                                ephemeral=True)

    async def referee_pick_winner(self, interaction: discord.Interaction, lobby_id: int, team: str):
        """A referee clicked a team on their private 'Which team won?' message. A captain must still confirm."""
        lobby = self.db.get_lobby(lobby_id)
        if lobby is None or lobby.status != "playing":
            await interaction.response.edit_message(content="Too late: a result was already reported.", view=None)
            return
        players = self.db.lobby_players(lobby.id)
        self.db.update_lobby(lobby.id, status="reported", winner=team, reported_by=interaction.user.id)
        await interaction.response.edit_message(
            content=f"✅ Sent: **{safe(team_name(lobby, players, team))}** won. A captain must approve it.",
            view=None)
        await self.refresh_game_message(interaction, lobby.id)
        await self.ping_confirmers(interaction, lobby.id)

    async def on_draw(self, interaction: discord.Interaction, lobby: Lobby, players: List[LobbyPlayer]):
        if lobby.status != "playing":
            return await self.refuse(interaction, "A result was already reported.")
        if lobby.team_of_captain(interaction.user.id) is None and not is_referee(interaction):
            return await self.refuse(interaction, "Only a captain can report a draw.")
        await self.report_result(interaction, lobby, DRAW)

    def finish_game(self, interaction: discord.Interaction, lobby: Lobby, players: List[LobbyPlayer],
                    winner: str) -> None:
        """Ends the game and saves wins, losses, draws and ranked points. No awaits, so it can't count twice."""
        self.db.update_lobby(lobby.id, status="done", winner=winner)
        if winner == DRAW:
            self.db.record_result(interaction.guild_id, [], [], draws=[p.user_id for p in players if p.team])
        else:
            winners = [p.user_id for p in players if p.team == winner]
            losers = [p.user_id for p in players if p.team == other_team(winner)]
            self.db.record_result(interaction.guild_id, winners, losers)

    async def refresh_game_message(self, interaction: discord.Interaction, lobby_id: int) -> None:
        """Updates the game message when the click came from another message (e.g. the private referee one)."""
        lobby = self.db.get_lobby(lobby_id)
        if not lobby.draft_message_id:
            return
        players = self.db.lobby_players(lobby.id)
        try:
            await interaction.channel.get_partial_message(lobby.draft_message_id).edit(
                content=None, embed=build_game_embed(lobby, players, self.db.map_votes(lobby.id)),
                view=build_game_view(lobby, players))
        except discord.HTTPException as e:
            log.warning("Could not update game message of lobby %s: %s", lobby.id, e)

    async def report_result(self, interaction: discord.Interaction, lobby: Lobby, winner: str):
        """winner: 'A', 'B' or DRAW. A captain who did not report it (or a referee) must approve it."""
        self.db.update_lobby(lobby.id, status="reported", winner=winner, reported_by=interaction.user.id)
        await self.show_game(interaction, lobby)
        await self.ping_confirmers(interaction, lobby.id)

    async def ping_confirmers(self, interaction: discord.Interaction, lobby_id: int) -> None:
        """Posts a new message with Approve / Deny and pings who must confirm the result."""
        lobby = self.db.get_lobby(lobby_id)
        players = self.db.lobby_players(lobby.id)
        targets = confirmers(lobby)
        text = (f"{claim_text(lobby, players)}\n"
                f"{mention_list(targets)}, is this true? Click **Approve** or **Deny**.")
        if REFEREE_ROLE_IDS:
            text += "\n-# A referee can also approve or deny."
        await interaction.followup.send(
            text, view=build_approval_view(lobby),
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(t) for t in targets]))

    async def show_decision(self, interaction: discord.Interaction, lobby: Lobby, text: str) -> None:
        """After Approve / Deny: the approval message says who decided, and the game message is updated."""
        if interaction.message is not None and interaction.message.id == lobby.draft_message_id:
            return await self.show_game(interaction, lobby)  # buttons on the game message (games from before)
        await interaction.response.edit_message(content=text, view=None,
                                                allowed_mentions=discord.AllowedMentions.none())
        await self.refresh_game_message(interaction, lobby.id)

    async def on_approve(self, interaction: discord.Interaction, lobby: Lobby, players: List[LobbyPlayer]):
        if lobby.status != "reported":
            return await self.refuse(interaction, "There is nothing to approve.")
        if interaction.user.id == lobby.reported_by:
            return await self.refuse(interaction, f"You can't approve your own report. "
                                                  f"Wait for {mention_list(confirmers(lobby))}.")
        if (interaction.user.id not in (lobby.captain_a, lobby.captain_b)
                and not is_admin(interaction) and not is_referee(interaction)):
            return await self.refuse(interaction, "Only the other captain or a referee can approve.")

        self.finish_game(interaction, lobby, players, lobby.winner)
        await self.show_decision(interaction, lobby, f"{claim_text(lobby, players)}\n"
                                                     f"✅ {interaction.user.mention} approved it.")

    async def on_deny(self, interaction: discord.Interaction, lobby: Lobby, players: List[LobbyPlayer]):
        if lobby.status != "reported":
            return await self.refuse(interaction, "There is nothing to deny.")
        if (interaction.user.id not in (lobby.captain_a, lobby.captain_b)
                and not is_admin(interaction) and not is_referee(interaction)):
            return await self.refuse(interaction, "Only the captains or a referee can deny.")
        claim = claim_text(lobby, players)
        self.db.update_lobby(lobby.id, status="playing", winner=None, reported_by=None)
        await self.show_decision(interaction, lobby, f"~~{claim}~~\n❌ {interaction.user.mention} denied it. "
                                                     "The game goes on. Report again when it is done.")

    # --- commands -----------------------------------------------------------

    @app_commands.guild_only()
    @app_commands.command(name="captains", description="Start a queue. Two captains pick the teams.")
    async def captains(self, interaction: discord.Interaction):
        event = self.db.get_event_by_channel(interaction.channel_id)
        lobby_id = self.db.create_lobby(interaction.guild_id, interaction.channel_id, interaction.user.id,
                                        event.id if event else None)
        self.db.add_lobby_player(lobby_id, interaction.user.id, interaction.user.display_name)
        lobby = self.db.get_lobby(lobby_id)
        players = self.db.lobby_players(lobby_id)
        await interaction.response.send_message(
            embed=build_queue_embed(lobby, players), view=build_queue_view(lobby, players))
        message = await interaction.original_response()
        self.db.update_lobby(lobby_id, queue_message_id=message.id)

    @app_commands.guild_only()
    @app_commands.command(name="tiebreaker", description="Captains: get 3 maps for a tiebreaker. Each captain bans 1.")
    async def tiebreaker(self, interaction: discord.Interaction):
        lobby = self.db.latest_captain_lobby(interaction.guild_id, interaction.user.id)
        if lobby is None:
            await interaction.response.send_message(
                "Only a captain can use this, after the map vote of their game.", ephemeral=True)
            return
        players = self.db.lobby_players(lobby.id)
        in_teams = sum(1 for p in players if p.team)
        choices = [m for m in maps_for(in_teams) if m != lobby.map_name]
        if len(choices) < TIEBREAKER_OPTIONS:
            await interaction.response.send_message(
                f"There are not enough {game_format(in_teams)} maps for a tiebreaker.", ephemeral=True)
            return
        tiebreaker = self.db.get_tiebreaker(
            self.db.create_tiebreaker(lobby.id, random.sample(choices, TIEBREAKER_OPTIONS)))
        captains = [lobby.captain_a, lobby.captain_b]
        await interaction.response.send_message(
            f"⚔️ {' '.join(f'<@{c}>' for c in captains)}: tiebreaker! Each of you bans 1 map.",
            embed=build_tiebreaker_embed(tiebreaker, lobby, players), view=build_tiebreaker_view(tiebreaker),
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(c) for c in captains]))

    async def handle_ban(self, interaction: discord.Interaction, tiebreaker_id: int, index: int):
        tiebreaker = self.db.get_tiebreaker(tiebreaker_id)
        if tiebreaker is None:
            return await self.refuse(interaction, "This tiebreaker does not exist anymore.")
        if tiebreaker.final_map:
            return await self.refuse(interaction, f"The bans are done. The map is **{safe(tiebreaker.final_map)}**.")
        lobby = self.db.get_lobby(tiebreaker.lobby_id)
        team = lobby.team_of_captain(interaction.user.id)
        if team is None:
            return await self.refuse(interaction, "Only the 2 captains can ban maps.")
        if (tiebreaker.ban_a if team == "A" else tiebreaker.ban_b) is not None:
            return await self.refuse(interaction, "You already banned a map. Now the other captain bans one.")
        if index in tiebreaker.banned or not 0 <= index < len(tiebreaker.maps):
            return await self.refuse(interaction, "This map is already banned. Pick another one.")
        self.db.set_tiebreaker_ban(tiebreaker.id, team, index)
        tiebreaker = self.db.get_tiebreaker(tiebreaker.id)
        await interaction.response.edit_message(
            embed=build_tiebreaker_embed(tiebreaker, lobby, self.db.lobby_players(lobby.id)),
            view=build_tiebreaker_view(tiebreaker))
        if tiebreaker.final_map:
            await interaction.followup.send(
                f"🗺️ Tiebreaker map for game {lobby.id}: **{safe(tiebreaker.final_map)}**! Good luck! 🍀")

    @app_commands.guild_only()
    @app_commands.command(name="stats", description="See someone's ranked points, wins, losses and draws")
    @app_commands.describe(member="The player to check (empty = you)")
    async def stats(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        member = member or interaction.user
        points, wins, losses, draws, streak = self.db.player_stats(interaction.guild_id, member.id)
        games = wins + losses + draws
        if games == 0:
            text = f"{member.mention} has not played a captains game yet."
        else:
            rate = round(100 * wins / games)
            tier, emoji = tier_of(points)
            text = f"📊 {member.mention}\n{emoji} **{tier}** · **{points}** points"
            upcoming = next_tier(points)
            if upcoming:
                needed, next_name, next_emoji = upcoming
                text += f" · {needed} more for {next_emoji} {next_name}"
            text += (f"\n**{wins}** wins · **{losses}** losses · **{draws}** draws · "
                     f"{games} games · {rate}% won")
            if streak >= STREAK_MIN:
                text += f"\n🔥 **{streak} wins in a row!** Every win now gives +{STREAK_BONUS} extra points."
            elif streak == STREAK_MIN - 1:
                text += f"\n🔥 Win the next game for a win streak (+{STREAK_BONUS} extra points per win)!"
        await interaction.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())

    def display_name(self, guild: discord.Guild, user_id: int, saved: dict) -> str:
        member = guild.get_member(user_id)
        return member.display_name if member else saved.get(user_id, "Unknown player")

    @app_commands.guild_only()
    @app_commands.command(name="vs", description="Who wins more when two players are on different teams?")
    @app_commands.describe(player="The player to compare with", player2="The other player (empty = you)")
    async def vs(self, interaction: discord.Interaction, player: discord.Member,
                 player2: Optional[discord.Member] = None):
        first = player2 or interaction.user
        if first.id == player.id:
            await interaction.response.send_message("Pick two different players. 🙂", ephemeral=True)
            return
        a_wins, b_wins, draws = head_to_head(self.db.finished_games(interaction.guild_id), first.id, player.id)
        a, b = safe(first.display_name), safe(player.display_name)
        text = f"⚔️ **{a}** vs **{b}**\n"
        if a_wins + b_wins + draws == 0:
            text += random.choice(VS_NEVER).format(a=a, b=b)
        else:
            text += f"**{a}**: {a_wins} wins · **{b}**: {b_wins} wins · Draws: {draws}\n"
            if a_wins == b_wins:
                text += random.choice(VS_TIE)
            else:
                leader, loser = (a, b) if a_wins > b_wins else (b, a)
                crushed = min(a_wins, b_wins) == 0 and max(a_wins, b_wins) >= 3
                text += random.choice(VS_CRUSH if crushed else VS_LEAD).format(leader=leader, loser=loser)
        await interaction.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.guild_only()
    @app_commands.command(name="duo", description="How good are two players together in the same team?")
    @app_commands.describe(player="Your teammate", player2="The other player (empty = you)")
    async def duo(self, interaction: discord.Interaction, player: discord.Member,
                  player2: Optional[discord.Member] = None):
        first = player2 or interaction.user
        if first.id == player.id:
            await interaction.response.send_message("Pick two different players. 🙂", ephemeral=True)
            return
        records = duo_records(self.db.finished_games(interaction.guild_id))
        wins, losses, draws = records.get(tuple(sorted((first.id, player.id))), (0, 0, 0))
        games = wins + losses + draws
        a, b = safe(first.display_name), safe(player.display_name)
        text = f"🤝 **{a}** + **{b}**\n"
        if games == 0:
            text += random.choice(DUO_NEVER).format(a=a, b=b)
        else:
            rate = round(100 * wins / games)
            text += f"{games} games together · **{wins}** wins · **{losses}** losses · **{draws}** draws · {rate}% won\n"
            text += random.choice(DUO_GREAT if rate >= 60 else DUO_OK if rate >= 40 else DUO_BAD)
        await interaction.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.guild_only()
    @app_commands.command(name="duos", description="The best duos: players who win the most together")
    async def duos(self, interaction: discord.Interaction):
        records = duo_records(self.db.finished_games(interaction.guild_id))
        ranked = []
        for pair, (wins, losses, draws) in records.items():
            games = wins + losses + draws
            if games >= DUO_MIN_GAMES:
                ranked.append((wins / games, wins, games, pair))
        if not ranked:
            await interaction.response.send_message(
                f"No duos yet. Two players need at least {DUO_MIN_GAMES} games in the same team. ⚔️", ephemeral=True)
            return
        ranked.sort(key=lambda r: (-r[0], -r[1], -r[2]))
        saved = self.db.player_names(interaction.guild_id)
        lines = [f"` #  {fit('Duo', DUO_WIDTH)}  W/G   Win`"]
        for rank, (rate, wins, games, (first, second)) in enumerate(ranked[:10], start=1):
            names = [fit(short_player(self.display_name(interaction.guild, user_id, saved)), 8).rstrip()
                     for user_id in (first, second)]
            lines.append(f"`{rank:>2}  {fit(' + '.join(names), DUO_WIDTH)} {wins:>2}/{games:<2} {round(100 * rate):>3}%`")
        embed = discord.Embed(title="🤝 Best duos", description="\n".join(lines), color=discord.Color.gold())
        embed.set_footer(text=f"W/G = wins / games together · Only duos with {DUO_MIN_GAMES}+ games together · "
                              "See one duo with /duo")
        await interaction.response.send_message(embed=embed)

    # --- /game remove (referees only) ------------------------------------------

    game = app_commands.Group(name="game", description="Manage captains games (referees only)", guild_only=True)

    @game.command(name="remove", description="Remove a wrong game from the stats (referees only)")
    @app_commands.describe(number="The game number. You see it in the title of the game message.")
    async def game_remove(self, interaction: discord.Interaction, number: int):
        if not is_referee(interaction):
            await interaction.response.send_message("Only referees can remove games.", ephemeral=True)
            return
        lobby = self.db.get_lobby(number)
        if lobby is None or lobby.guild_id != interaction.guild_id or lobby.status != "done":
            await interaction.response.send_message(
                f"Game {number} is not a finished game. Only finished games can be removed.", ephemeral=True)
            return
        players = self.db.lobby_players(lobby.id)
        embed = discord.Embed(title=f"🗑️ Remove game {lobby.id}?", description=describe_game(lobby, players),
                              color=discord.Color.red())
        for team in ("A", "B"):
            change = "-1 draw" if lobby.winner == DRAW else "-1 win" if lobby.winner == team else "-1 loss"
            names = "\n".join(safe(p.name) for p in team_members(players, team))
            embed.add_field(name=f"{TEAM_EMOJI[team]} {team_name(lobby, players, team)}: {change}"[:256],
                            value=truncate_field(names or "-"), inline=True)
        embed.set_footer(text="Everyone's points and win streaks are calculated again without this game.")
        await interaction.response.send_message("Are you sure?", embed=embed, view=RemoveGameView(self, lobby.id),
                                                ephemeral=True)

    @game_remove.autocomplete("number")
    async def game_number_autocomplete(self, interaction: discord.Interaction,
                                       current: str) -> List[app_commands.Choice[int]]:
        choices = []
        for lobby_id, _, _ in self.db.finished_games(interaction.guild_id, limit=50):
            if current and not str(lobby_id).startswith(current.strip()):
                continue
            lobby = self.db.get_lobby(lobby_id)
            label = f"Game {lobby_id}: {describe_game(lobby, self.db.lobby_players(lobby_id))}"
            choices.append(app_commands.Choice(name=label[:100], value=lobby_id))
            if len(choices) == 25:
                break
        return choices

    async def confirm_remove(self, interaction: discord.Interaction, lobby_id: int) -> None:
        if not is_referee(interaction):
            await interaction.response.send_message("Only referees can remove games.", ephemeral=True)
            return
        if not self.db.remove_game(lobby_id):
            await interaction.response.edit_message(
                content=f"Game {lobby_id} was already removed. Nothing changed.", embed=None, view=None)
            return
        await interaction.response.edit_message(
            content=f"✅ Game {lobby_id} is removed. Points and win streaks are calculated again.",
            embed=None, view=None)
        lobby = self.db.get_lobby(lobby_id)
        channel = self.bot.get_channel(lobby.channel_id)
        if channel is not None and lobby.draft_message_id:
            try:
                await channel.get_partial_message(lobby.draft_message_id).edit(
                    embed=build_game_embed(lobby, self.db.lobby_players(lobby_id)), view=None)
            except discord.HTTPException as e:
                log.warning("Could not update game message of removed lobby %s: %s", lobby_id, e)

    @app_commands.guild_only()
    @app_commands.command(name="leaderboard", description="See the ranking of all captains players (by ranked points)")
    async def leaderboard(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        if self.db.stats_player_count(guild_id) == 0:
            await interaction.response.send_message(
                "Nobody played a captains game yet. Use `/captains` to start one! ⚔️", ephemeral=True)
            return
        await interaction.response.defer()  # looking up names can take a moment
        view = LeaderboardView(interaction.guild, lambda: self.db.stats_player_count(guild_id),
                               lambda limit, offset: self.db.stats_leaderboard(guild_id, limit, offset),
                               build_leaderboard_embed)
        await view.send(interaction)

    async def cog_app_command_error(self, interaction: discord.Interaction,
                                    error: app_commands.AppCommandError):
        log.exception("Error in captains command", exc_info=error)
        message = "Something went wrong. Check the bot's console for details."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Captains(bot))
