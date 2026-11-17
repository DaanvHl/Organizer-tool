import logging
import random
import re
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs.events import truncate_field
from cogs.fun import LeaderboardView, rank_label
from config import REFEREE_ROLE_IDS
from database import LOSS_POINTS, WIN_POINTS, Database, Lobby, LobbyPlayer

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
                  template=r"lobby:(?P<action>join|leave|start|cancel|claim|unclaim|lock|snake|normal|report|draw|approve|deny)"
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

def build_game_embed(lobby: Lobby, players: List[LobbyPlayer]) -> discord.Embed:
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

    embed = discord.Embed(title="⚔️ Captains draft", color=discord.Color.red())
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
            embed.set_footer(text=f"Winners got +{WIN_POINTS} points, losers -{LOSS_POINTS} points. "
                                  "See /leaderboard")
        if lobby.reported_by and lobby.team_of_captain(lobby.reported_by) is None:
            embed.description += f"\nReported by referee <@{lobby.reported_by}>."
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


def build_approval_view(lobby: Lobby) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(LobbyButton("approve", lobby.id))
    view.add_item(LobbyButton("deny", lobby.id))
    return view


NAME_LENGTH = 18  # longer names are cut off with "…", so a name never wraps onto a 2nd line


def short_name(name: str) -> str:
    name = " ".join(name.split())
    return name if len(name) <= NAME_LENGTH else name[:NAME_LENGTH - 1].rstrip() + "…"


def build_leaderboard_embed(rows: list, offset: int, page: int, pages: int) -> discord.Embed:
    """rows: (name, points, wins, losses, draws) for one page. Plain names, so nobody gets pinged.
    Three columns side by side (player, points, record), so the numbers line up."""
    players, points_col, records = [], [], []
    for rank, (name, points, wins, losses, draws) in enumerate(rows, start=offset + 1):
        rate = round(100 * wins / (wins + losses + draws))
        players.append(f"{rank_label(rank)} {safe(short_name(name))}")
        points_col.append(f"**{points}**")
        records.append(f"{wins} W · {losses} L · {draws} D · {rate}%")
    embed = discord.Embed(title="🏆 Captains leaderboard", color=discord.Color.gold())
    embed.add_field(name="Player", value="\n".join(players), inline=True)
    embed.add_field(name="Points", value="\n".join(points_col), inline=True)
    embed.add_field(name="Record", value="\n".join(records), inline=True)
    embed.set_footer(text=f"Page {page + 1} of {pages} · Win +{WIN_POINTS} points, loss -{LOSS_POINTS} points · "
                          "W = wins, L = losses, D = draws")
    return embed


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
        await interaction.response.edit_message(content=None, embed=build_game_embed(lobby, players),
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
            self.db.update_lobby(lobby.id, status="playing")

        await self.show_game(interaction, lobby)
        if not remaining:
            await self.make_event_team_channels(interaction, self.db.get_lobby(lobby.id),
                                                self.db.lobby_players(lobby.id))

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
                content=None, embed=build_game_embed(lobby, players), view=build_game_view(lobby, players))
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
    @app_commands.command(name="stats", description="See someone's ranked points, wins, losses and draws")
    @app_commands.describe(member="The player to check (empty = you)")
    async def stats(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        member = member or interaction.user
        points, wins, losses, draws = self.db.player_stats(interaction.guild_id, member.id)
        games = wins + losses + draws
        if games == 0:
            text = f"{member.mention} has not played a captains game yet."
        else:
            rate = round(100 * wins / games)
            text = (f"📊 {member.mention}: **{points}** points · **{wins}** wins · **{losses}** losses · "
                    f"**{draws}** draws · {games} games · {rate}% won")
        await interaction.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())

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
