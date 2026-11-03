import logging
import random
import re
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs.events import truncate_field
from cogs.fun import LeaderboardView, rank_label
from database import Database, Lobby, LobbyPlayer

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
    "report": ("Report win", "🏆", discord.ButtonStyle.success),
    "approve": ("Approve", "✅", discord.ButtonStyle.success),
    "deny": ("Deny", "❌", discord.ButtonStyle.danger),
}


def draft_turn(pick_index: int) -> str:
    """Team that makes the given (0-based) pick in a snake draft: A B B A A B B A …"""
    return "A" if pick_index % 2 == (pick_index // 2) % 2 else "B"


def other_team(team: str) -> str:
    return "B" if team == "A" else "A"


def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.manage_guild


def safe(name: str) -> str:
    return discord.utils.escape_markdown(name)


def team_name(lobby: Lobby, players: List[LobbyPlayer], team: str) -> str:
    names = {p.user_id: p.name for p in players}
    return f"Team {names.get(lobby.captain_of(team), team)}"


def team_members(players: List[LobbyPlayer], team: str) -> List[LobbyPlayer]:
    return sorted((p for p in players if p.team == team), key=lambda p: p.pick_no or 0)


class LobbyButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"lobby:(?P<action>join|leave|start|cancel|claim|unclaim|lock|report|approve|deny)"
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
        lines = [f"👑 {safe(p.name)}" if p.pick_no == 0 else f"{p.pick_no}. {safe(p.name)}"
                 for p in team_members(players, team)]
        embed.add_field(name=f"{crown}{TEAM_EMOJI[team]} {team_name(lobby, players, team)}"[:256],
                        value=truncate_field("\n".join(lines)), inline=True)
    available = [p for p in players if p.team is None]
    if available:
        embed.add_field(name=f"Available ({len(available)})",
                        value=truncate_field(", ".join(safe(p.name) for p in available)), inline=False)

    if lobby.status == "drafting":
        picks_done = sum(1 for p in players if p.pick_no)
        captain = lobby.captain_of(draft_turn(picks_done))
        embed.description = f"Now picking: <@{captain}> (pick {picks_done + 1} of {len(players) - 2})"
        embed.set_footer(text="Snake order: X O O X X O O X … · Only the captain whose turn it is can pick.")
    elif lobby.status == "playing":
        embed.color = discord.Color.green()
        embed.description = "✅ Draft complete! After the game, the winning captain clicks **Report win**."
    elif lobby.status == "reported":
        embed.color = discord.Color.gold()
        loser = lobby.captain_of(other_team(lobby.winner))
        embed.description = (f"🏆 <@{lobby.reported_by}> says **{team_name(lobby, players, lobby.winner)}** "
                             f"won.\n<@{loser}>, is this true? Click **Approve** or **Deny**.")
    elif lobby.status == "done":
        embed.color = discord.Color.gold()
        embed.description = f"🏆 **{team_name(lobby, players, lobby.winner)}** won!"
        embed.set_footer(text="Winners got +1 win, losers +1 loss. See /leaderboard")
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
    elif lobby.status == "reported":
        view.add_item(LobbyButton("approve", lobby.id))
        view.add_item(LobbyButton("deny", lobby.id))
    else:
        return None
    return view


def build_leaderboard_embed(rows: list, offset: int, page: int, pages: int) -> discord.Embed:
    """rows: (name, wins, losses) for one page. Plain names, so nobody gets pinged."""
    lines = []
    for rank, (name, wins, losses) in enumerate(rows, start=offset + 1):
        rate = round(100 * wins / (wins + losses))
        lines.append(f"{rank_label(rank)} {safe(name)} · **{wins}** W · **{losses}** L · {rate}%")
    embed = discord.Embed(title="🏆 Captains leaderboard", description="\n".join(lines),
                          color=discord.Color.gold())
    embed.set_footer(text=f"Page {page + 1} of {pages} · W = wins, L = losses")
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
        self.db.update_lobby(lobby.id, status="drafting", captain_a=first, captain_b=second)
        self.db.assign_pick(lobby.id, first, "A", 0)
        self.db.assign_pick(lobby.id, second, "B", 0)
        await self.show_game(interaction, lobby)

    async def handle_pick(self, interaction: discord.Interaction, lobby_id: int, user_id: int):
        lobby = self.db.get_lobby(lobby_id)
        if lobby is None or lobby.status != "drafting":
            await interaction.response.send_message("This draft is over.", ephemeral=True)
            return

        # No awaits between reading the turn and saving the pick, so two quick clicks can't both count.
        players = self.db.lobby_players(lobby.id)
        picks_done = sum(1 for p in players if p.pick_no)
        team = draft_turn(picks_done)
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
            last, last_team = remaining[0], draft_turn(picks_done + 1)
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
            return await self.refuse(interaction, "A win was already reported.")
        team = lobby.team_of_captain(interaction.user.id)
        if team is None:
            return await self.refuse(interaction, "Only the captain of the winning team can report the win.")
        self.db.update_lobby(lobby.id, status="reported", winner=team, reported_by=interaction.user.id)
        await self.show_game(interaction, lobby)
        other = lobby.captain_of(other_team(team))
        await interaction.followup.send(
            f"<@{other}>, please approve or deny the win on the message above.",
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(other)]))

    async def on_approve(self, interaction: discord.Interaction, lobby: Lobby, players: List[LobbyPlayer]):
        if lobby.status != "reported":
            return await self.refuse(interaction, "There is nothing to approve.")
        if interaction.user.id == lobby.reported_by:
            other = lobby.captain_of(other_team(lobby.winner))
            return await self.refuse(interaction, f"You can't approve your own report. Wait for <@{other}>.")
        if interaction.user.id not in (lobby.captain_a, lobby.captain_b) and not is_admin(interaction):
            return await self.refuse(interaction, "Only the other captain can approve.")

        winners = [p.user_id for p in players if p.team == lobby.winner]
        losers = [p.user_id for p in players if p.team == other_team(lobby.winner)]
        self.db.update_lobby(lobby.id, status="done")
        self.db.record_result(interaction.guild_id, winners, losers)
        await self.show_game(interaction, lobby)

    async def on_deny(self, interaction: discord.Interaction, lobby: Lobby, players: List[LobbyPlayer]):
        if lobby.status != "reported":
            return await self.refuse(interaction, "There is nothing to deny.")
        if interaction.user.id not in (lobby.captain_a, lobby.captain_b) and not is_admin(interaction):
            return await self.refuse(interaction, "Only the captains can deny.")
        self.db.update_lobby(lobby.id, status="playing", winner=None, reported_by=None)
        await self.show_game(interaction, lobby)

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
    @app_commands.command(name="stats", description="See how many captains games someone won and lost")
    @app_commands.describe(member="The player to check (empty = you)")
    async def stats(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        member = member or interaction.user
        wins, losses = self.db.player_stats(interaction.guild_id, member.id)
        if wins + losses == 0:
            text = f"{member.mention} has not played a captains game yet."
        else:
            rate = round(100 * wins / (wins + losses))
            text = (f"📊 {member.mention}: **{wins}** wins · **{losses}** losses · "
                    f"{wins + losses} games · {rate}% won")
        await interaction.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.guild_only()
    @app_commands.command(name="leaderboard", description="See the wins and losses of all captains players")
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
