import logging
import random
import re
import time
from datetime import datetime
from typing import Awaitable, Callable, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from database import Database, Draft, DraftPlayer, Event, Participant, Poll
from timeparse import parse_time

log = logging.getLogger(__name__)

STATUS_FOOTERS = {
    "ended": "This event has ended.",
    "cancelled": "This event was cancelled.",
    "expired": "This event was closed automatically.",
}


MAX_POLL_OPTIONS = 25  # Discord's limit for a select menu


LAST_PICK_LINES = [
    "Last pick, but first in our hearts 💔",
    "Mr. Irrelevant has entered the chat.",
    "Someone had to be last. Today it's them.",
    "Picked last, plays like first. Probably. Maybe.",
    "Not picked, just... included. 🫂",
]

MAX_DRAFT_POOL = 125  # 5 menus of 25 players; a message holds at most 5 rows


def truncate_field(value: str, limit: int = 1024) -> str:
    if len(value) > limit:
        value = value[:limit - 24].rsplit("\n", 1)[0] + "\n…"
    return value


def build_embed(event: Event, participants: List[Participant]) -> discord.Embed:
    if event.active:
        title = f"🎯 {event.name}"
        color = discord.Color.orange()
    else:
        title = f"🎯 {event.name} [{event.status.capitalize()}]"
        color = discord.Color.dark_grey()

    embed = discord.Embed(title=title, description=event.description or None, color=color)
    embed.add_field(name="When", value=f"<t:{event.start_ts}:F>\n<t:{event.start_ts}:R>", inline=True)
    embed.add_field(name="Organizer", value=f"<@{event.organizer_id}>", inline=True)

    options = event.options
    if options:
        counts = [0] * len(options)
        for p in participants:
            if p.choice is not None and 0 <= p.choice < len(options):
                counts[p.choice] += 1
        results = "\n".join(f"**{label}**: {count}" for label, count in zip(options, counts))
        embed.add_field(name=f"📊 {event.poll_question}"[:256], value=truncate_field(results), inline=False)

    lines = []
    for i, p in enumerate(participants, start=1):
        line = f"{i}. <@{p.user_id}>"
        if options:
            valid = p.choice is not None and 0 <= p.choice < len(options)
            line += f" · {options[p.choice]}" if valid else " · *no choice yet*"
        lines.append(line)
    embed.add_field(name=f"Players ({len(participants)})",
                    value=truncate_field("\n".join(lines) or "Nobody yet"), inline=False)

    if event.active and event.signups_closed:
        embed.set_footer(text="🔒 Sign-ups are closed.")
    elif event.active and options:
        embed.set_footer(text="Click Join and pick an option to get access to the event channel.")
    elif event.active:
        embed.set_footer(text="Click Join to get access to the event channel.")
    else:
        embed.set_footer(text=STATUS_FOOTERS.get(event.status, "This event is closed."))
    return embed


class EventButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"event:(?P<action>join|leave):(?P<id>\d+)"):
    """Join/Leave button. The event ID lives in the custom_id, so buttons keep working after a restart."""

    def __init__(self, action: str, event_id: int):
        if action == "join":
            button = discord.ui.Button(label="Join", emoji="✅", style=discord.ButtonStyle.success,
                                       custom_id=f"event:join:{event_id}")
        else:
            button = discord.ui.Button(label="Leave", emoji="🚪", style=discord.ButtonStyle.secondary,
                                       custom_id=f"event:leave:{event_id}")
        super().__init__(button)
        self.action = action
        self.event_id = event_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                             match: re.Match[str], /):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        cog: "Events" = interaction.client.get_cog("Events")
        if self.action == "join":
            await cog.handle_join(interaction, self.event_id)
        else:
            await cog.handle_leave(interaction, self.event_id)


class PollSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"event:pick:(?P<id>\d+)"):
    """Select menu a player uses to pick a poll option when joining an event."""

    def __init__(self, event_id: int, options: List[str], current: Optional[int] = None):
        select = discord.ui.Select(
            custom_id=f"event:pick:{event_id}",
            placeholder="Choose an option…",
            options=[discord.SelectOption(label=label, value=str(i), default=(i == current))
                     for i, label in enumerate(options)],
        )
        super().__init__(select)
        self.event_id = event_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select,
                             match: re.Match[str], /):
        return cls(int(match["id"]), [o.label for o in item.options])

    async def callback(self, interaction: discord.Interaction):
        cog: "Events" = interaction.client.get_cog("Events")
        await cog.handle_pick(interaction, self.event_id, int(interaction.data["values"][0]))


class PollModal(discord.ui.Modal, title="Poll"):
    """Form to enter a poll question and options. Calls on_poll(interaction, question, options)."""

    question = discord.ui.TextInput(
        label="Question", max_length=200,
        placeholder="e.g. Which role do you want?")
    options = discord.ui.TextInput(
        label="Options (one per line)", style=discord.TextStyle.paragraph, max_length=2000,
        placeholder="Option 1\nOption 2\nOption 3")

    def __init__(self, on_poll: Callable[[discord.Interaction, str, List[str]], Awaitable[None]]):
        super().__init__(timeout=900)
        self.on_poll = on_poll

    async def on_submit(self, interaction: discord.Interaction):
        options = []
        for line in self.options.value.splitlines():
            line = line.strip()
            if line and line not in options:
                options.append(line)
        error = None
        if len(options) < 2:
            error = "A poll needs at least 2 different options (one per line)."
        elif len(options) > MAX_POLL_OPTIONS:
            error = f"A poll can have at most {MAX_POLL_OPTIONS} options."
        elif any(len(o) > 100 for o in options):
            error = "Each option can be at most 100 characters."
        if error:
            await interaction.response.send_message(
                f"{error} Please try again.\n\nYour options were:\n```\n{self.options.value}\n```",
                ephemeral=True)
            return
        await self.on_poll(interaction, self.question.value.strip(), options)


class ChannelPollSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"poll:vote:(?P<id>\d+)"):
    """Voting menu of a poll started with /event poll inside an event channel."""

    def __init__(self, poll_id: int, options: List[str]):
        select = discord.ui.Select(
            custom_id=f"poll:vote:{poll_id}",
            placeholder="Vote…",
            options=[discord.SelectOption(label=label, value=str(i)) for i, label in enumerate(options)],
        )
        super().__init__(select)
        self.poll_id = poll_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select,
                             match: re.Match[str], /):
        return cls(int(match["id"]), [o.label for o in item.options])

    async def callback(self, interaction: discord.Interaction):
        cog: "Events" = interaction.client.get_cog("Events")
        await cog.handle_vote(interaction, self.poll_id, int(interaction.data["values"][0]))


def build_channel_poll_embed(poll: Poll, votes: List[Participant]) -> discord.Embed:
    options = poll.options
    voters = [[] for _ in options]
    for vote in votes:
        if 0 <= vote.choice < len(options):
            voters[vote.choice].append(vote.user_id)
    lines = []
    for label, user_ids in zip(options, voters):
        line = f"**{label}**: {len(user_ids)}"
        if user_ids:
            line += " · " + ", ".join(f"<@{uid}>" for uid in user_ids)
        lines.append(line)
    embed = discord.Embed(title=f"📊 {poll.question}"[:256],
                          description=truncate_field("\n".join(lines), 4096),
                          color=discord.Color.blurple())
    embed.set_footer(text=f"{len(votes)} vote(s) · Pick an option below. You can change your vote.")
    return embed


def draft_turn(pick_index: int) -> str:
    """Team that makes the given (0-based) pick in a snake draft: A B B A A B B A …"""
    return "A" if pick_index % 2 == (pick_index // 2) % 2 else "B"


class DraftSelect(discord.ui.DynamicItem[discord.ui.Select],
                  template=r"draft:pick:(?P<id>\d+):(?P<chunk>\d+)"):
    """Menu a captain uses to pick a player. Large pools are split over several menus of 25."""

    def __init__(self, draft_id: int, chunk: int, players: List[tuple], placeholder: str):
        select = discord.ui.Select(
            custom_id=f"draft:pick:{draft_id}:{chunk}",
            placeholder=placeholder,
            options=[discord.SelectOption(label=name[:100], value=str(uid)) for uid, name in players],
        )
        super().__init__(select)
        self.draft_id = draft_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select,
                             match: re.Match[str], /):
        players = [(int(o.value), o.label) for o in item.options]
        return cls(int(match["id"]), int(match["chunk"]), players, item.placeholder or "")

    async def callback(self, interaction: discord.Interaction):
        cog: "Events" = interaction.client.get_cog("Events")
        await cog.handle_draft_pick(interaction, self.draft_id, int(interaction.data["values"][0]))


def build_draft_embed(draft: Draft, players: List[DraftPlayer]) -> discord.Embed:
    names = {p.user_id: p.name for p in players}
    available = [p for p in players if p.team is None]
    picks_done = sum(1 for p in players if p.pick_no)
    total_picks = len(players) - 2

    def team_lines(team: str, captain_id: int) -> str:
        picked = sorted((p for p in players if p.team == team and p.pick_no), key=lambda p: p.pick_no)
        lines = [f"👑 <@{captain_id}>"] + [f"{i}. <@{p.user_id}>" for i, p in enumerate(picked, start=1)]
        return truncate_field("\n".join(lines))

    embed = discord.Embed(title="⚔️ Captains draft", color=discord.Color.red())
    embed.add_field(name=f"Team {names.get(draft.captain_a, 'A')}"[:256],
                    value=team_lines("A", draft.captain_a), inline=True)
    embed.add_field(name=f"Team {names.get(draft.captain_b, 'B')}"[:256],
                    value=team_lines("B", draft.captain_b), inline=True)
    if available:
        embed.add_field(name=f"Available ({len(available)})",
                        value=truncate_field(", ".join(f"<@{p.user_id}>" for p in available)),
                        inline=False)

    if draft.status == "replaced":
        embed.color = discord.Color.dark_grey()
        embed.description = "This draft was replaced by a new one."
    elif draft.status == "done" or not available:
        embed.color = discord.Color.green()
        embed.description = "✅ Draft complete!"
    else:
        team = draft_turn(picks_done)
        captain = draft.captain_a if team == "A" else draft.captain_b
        embed.description = f"Now picking: <@{captain}> (pick {picks_done + 1} of {total_picks})"
        embed.set_footer(text="Snake order: X O O X X O O X … · Only the captain whose turn it is can pick.")
    return embed


def build_draft_view(draft: Draft, players: List[DraftPlayer]) -> Optional[discord.ui.View]:
    available = [(p.user_id, p.name) for p in players if p.team is None]
    if draft.status != "active" or not available:
        return None
    view = discord.ui.View(timeout=None)
    chunks = [available[i:i + 25] for i in range(0, len(available), 25)]
    for index, chunk in enumerate(chunks):
        placeholder = "Pick a player…"
        if len(chunks) > 1:
            placeholder = f"Pick a player ({index * 25 + 1}–{index * 25 + len(chunk)})…"
        view.add_item(DraftSelect(draft.id, index, chunk, placeholder))
    return view


def event_view(event_id: int, signups_open: bool = True) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if signups_open:
        view.add_item(EventButton("join", event_id))
    view.add_item(EventButton("leave", event_id))
    return view


def reminder_already_due(start_ts: int) -> bool:
    """True if the reminder moment has already passed, so we don't ping right after creating/editing."""
    if config.REMINDER_MINUTES <= 0:
        return True
    return time.time() >= start_ts - config.REMINDER_MINUTES * 60


@app_commands.guild_only()
class Events(commands.GroupCog, group_name="event", group_description="Create and manage events"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db: Database = bot.db
        self.housekeeping.start()

    def cog_unload(self):
        self.housekeeping.cancel()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only allow commands in the configured channels, and always in event channels."""
        if not config.ALLOWED_CHANNEL_IDS or interaction.channel_id in config.ALLOWED_CHANNEL_IDS:
            return True
        if self.db.get_event_by_channel(interaction.channel_id) is not None:
            return True
        channels = ", ".join(f"<#{cid}>" for cid in sorted(config.ALLOWED_CHANNEL_IDS))
        await interaction.response.send_message(
            f"Event commands can't be used here. Use them in {channels}.", ephemeral=True)
        return False

    # --- helpers ------------------------------------------------------------

    async def refresh_announcement(self, event: Event) -> None:
        """Updates the announcement's embed and buttons to match the event's current state."""
        if not event.announce_channel_id or not event.announce_message_id:
            return
        channel = self.bot.get_channel(event.announce_channel_id)
        if channel is None:
            return
        message = channel.get_partial_message(event.announce_message_id)
        embed = build_embed(event, self.db.participants(event.id))
        view = event_view(event.id, not event.signups_closed) if event.active else None
        try:
            await message.edit(embed=embed, view=view)
        except discord.HTTPException as e:
            log.warning("Could not update announcement for event %s: %s", event.id, e)

    async def get_category(self, guild: discord.Guild) -> discord.CategoryChannel:
        category = discord.utils.get(guild.categories, name=config.EVENTS_CATEGORY)
        if category is None:
            category = await guild.create_category(config.EVENTS_CATEGORY, reason="Event bot category")
        return category

    async def finish_event(self, event: Event, status: str) -> None:
        """Marks the event as closed and deletes its channel and role."""
        self.db.set_status(event.id, status)
        event = self.db.get_event(event.id)
        await self.refresh_announcement(event)

        guild = self.bot.get_guild(event.guild_id)
        if guild is None:
            return
        for draft in self.db.drafts_with_teams(event.id):
            await self.delete_team_channels(guild, draft)
        reason = f"Event '{event.name}' {status}"
        channel = guild.get_channel(event.channel_id) if event.channel_id else None
        if channel is not None:
            try:
                await channel.delete(reason=reason)
            except discord.HTTPException as e:
                log.warning("Could not delete channel for event %s: %s", event.id, e)
        role = guild.get_role(event.role_id) if event.role_id else None
        if role is not None:
            try:
                await role.delete(reason=reason)
            except discord.HTTPException as e:
                log.warning("Could not delete role for event %s: %s", event.id, e)

    async def event_for_organizer(self, interaction: discord.Interaction) -> Optional[Event]:
        """Finds the event belonging to the current channel and checks the user may manage it."""
        event = self.db.get_event_by_channel(interaction.channel_id)
        if event is None:
            await interaction.response.send_message(
                "Run this command inside the event's channel.", ephemeral=True)
            return None
        is_admin = interaction.user.guild_permissions.manage_guild
        if interaction.user.id != event.organizer_id and not is_admin:
            await interaction.response.send_message(
                "Only the organizer or an admin can do that.", ephemeral=True)
            return None
        return event

    # --- buttons ------------------------------------------------------------

    async def handle_join(self, interaction: discord.Interaction, event_id: int):
        event = self.db.get_event(event_id)
        if event is None or not event.active:
            await interaction.response.send_message("This event is no longer open.", ephemeral=True)
            return
        if event.signups_closed and not self.db.is_participant(event.id, interaction.user.id):
            await interaction.response.send_message("🔒 Sign-ups for this event are closed.", ephemeral=True)
            return

        if event.has_poll:
            # Joining happens once the player picks an option (see handle_pick).
            current = self.db.get_choice(event.id, interaction.user.id)
            if current is not None and 0 <= current < len(event.options):
                intro = (f"You're already in **{event.name}** with **{event.options[current]}**. "
                         "Pick another option to change your choice.")
            else:
                intro = f"Pick an option to join **{event.name}**."
            view = discord.ui.View(timeout=None)
            view.add_item(PollSelect(event.id, event.options, current))
            await interaction.response.send_message(
                f"{intro}\n📊 **{event.poll_question}**", view=view, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.join_event(interaction, event, choice=None)
        await interaction.followup.send(result, ephemeral=True)

    async def handle_pick(self, interaction: discord.Interaction, event_id: int, choice: int):
        await interaction.response.defer()
        event = self.db.get_event(event_id)
        if event is None or not event.active:
            message = "This event is no longer open."
        elif event.signups_closed and not self.db.is_participant(event.id, interaction.user.id):
            message = "🔒 Sign-ups for this event are closed."
        elif not 0 <= choice < len(event.options):
            message = "That option doesn't exist anymore. Click Join again."
        else:
            message = await self.join_event(interaction, event, choice)
        await interaction.edit_original_response(content=message, view=None)

    async def join_event(self, interaction: discord.Interaction, event: Event,
                         choice: Optional[int]) -> str:
        """Adds the user to the event (or updates their poll choice) and returns a reply text."""
        role = interaction.guild.get_role(event.role_id)
        if role is None:
            return "The event role is missing. Ask an admin."

        newly_joined = self.db.add_participant(event.id, interaction.user.id, choice)
        try:
            await interaction.user.add_roles(role, reason=f"Joined event '{event.name}'")
        except discord.HTTPException:
            if newly_joined:
                self.db.remove_participant(event.id, interaction.user.id)
            return ("I couldn't give you the event role. Make sure my role is above the event roles "
                    "and that I have **Manage Roles**.")

        if not newly_joined and choice is None:
            return f"You're already in this event: <#{event.channel_id}>"
        if not newly_joined:
            self.db.set_choice(event.id, interaction.user.id, choice)
        await self.refresh_announcement(event)

        picked = f" with **{event.options[choice]}**" if choice is not None else ""
        if newly_joined:
            return f"You joined **{event.name}**{picked}! Head over to <#{event.channel_id}>."
        return f"Your choice is now **{event.options[choice]}**. See you in <#{event.channel_id}>."

    async def handle_leave(self, interaction: discord.Interaction, event_id: int):
        await interaction.response.defer(ephemeral=True, thinking=True)
        event = self.db.get_event(event_id)
        if event is None or not event.active:
            await interaction.followup.send("This event is no longer open.", ephemeral=True)
            return
        if interaction.user.id == event.organizer_id:
            await interaction.followup.send(
                "You're the organizer. Use `/event cancel` in the event channel if it's not happening.",
                ephemeral=True)
            return
        if not self.db.remove_participant(event.id, interaction.user.id):
            await interaction.followup.send("You weren't in this event.", ephemeral=True)
            return

        role = interaction.guild.get_role(event.role_id)
        if role is not None:
            try:
                await interaction.user.remove_roles(role, reason=f"Left event '{event.name}'")
            except discord.HTTPException as e:
                log.warning("Could not remove role for event %s: %s", event.id, e)
        await interaction.followup.send(f"You left **{event.name}**.", ephemeral=True)
        await self.refresh_announcement(event)

    async def handle_vote(self, interaction: discord.Interaction, poll_id: int, choice: int):
        poll = self.db.get_poll(poll_id)
        event = self.db.get_event(poll.event_id) if poll else None
        if poll is None or event is None or not event.active:
            await interaction.response.send_message("This poll is closed.", ephemeral=True)
            return
        if not 0 <= choice < len(poll.options):
            await interaction.response.send_message("That option doesn't exist.", ephemeral=True)
            return
        self.db.set_vote(poll.id, interaction.user.id, choice)
        await interaction.response.edit_message(embed=build_channel_poll_embed(poll, self.db.votes(poll.id)))
        await interaction.followup.send(f"You voted **{poll.options[choice]}**.", ephemeral=True)

    async def handle_draft_pick(self, interaction: discord.Interaction, draft_id: int, user_id: int):
        draft = self.db.get_draft(draft_id)
        event = self.db.get_event(draft.event_id) if draft else None
        if draft is None or draft.status != "active" or event is None or not event.active:
            await interaction.response.send_message("This draft is over.", ephemeral=True)
            return

        # No awaits between reading the turn and saving the pick, so two quick clicks can't both count.
        players = self.db.draft_players(draft.id)
        picks_done = sum(1 for p in players if p.pick_no)
        team = draft_turn(picks_done)
        captain = draft.captain_a if team == "A" else draft.captain_b
        if interaction.user.id != captain:
            if interaction.user.id in (draft.captain_a, draft.captain_b):
                text = f"It's not your turn. Waiting for <@{captain}> to pick."
            else:
                text = f"Only the captains can pick. It's <@{captain}>'s turn."
            await interaction.response.send_message(text, ephemeral=True)
            return
        target = next((p for p in players if p.user_id == user_id), None)
        if target is None or target.team is not None:
            await interaction.response.send_message("That player was already picked.", ephemeral=True)
            return

        self.db.assign_pick(draft.id, user_id, team, picks_done + 1)
        log_lines = [f"<@{captain}> picked <@{user_id}>."]
        remaining = [p for p in players if p.team is None and p.user_id != user_id]
        if len(remaining) == 1:
            last, last_team = remaining[0], draft_turn(picks_done + 1)
            self.db.assign_pick(draft.id, last.user_id, last_team, picks_done + 2)
            last_captain = draft.captain_a if last_team == "A" else draft.captain_b
            log_lines.append(f"<@{last.user_id}> is the last player and joins <@{last_captain}>'s team.")
            remaining = []
        if not remaining:
            self.db.set_draft_status(draft.id, "done")

        draft = self.db.get_draft(draft.id)
        players = self.db.draft_players(draft.id)
        await interaction.response.edit_message(
            content=None, embed=build_draft_embed(draft, players), view=build_draft_view(draft, players))

        if remaining:
            next_team = draft_turn(picks_done + 1)
            next_captain = draft.captain_a if next_team == "A" else draft.captain_b
            log_lines.append(f"<@{next_captain}>, your turn to pick!")
            mentions = discord.AllowedMentions(users=[discord.Object(next_captain)])
        else:
            for team_key, team_captain in (("A", draft.captain_a), ("B", draft.captain_b)):
                members = [f"<@{p.user_id}>" for p in sorted(players, key=lambda p: p.pick_no)
                           if p.team == team_key]
                log_lines.append(f"**Team <@{team_captain}>**: " + ", ".join(members))
            log_lines.insert(0, "✅ **Draft complete!**")
            if len(players) > 2:
                log_lines.append(f"*{random.choice(LAST_PICK_LINES)}*")
            mentions = discord.AllowedMentions.none()
        await interaction.channel.send("\n".join(log_lines), allowed_mentions=mentions)

        if not remaining:
            await self.create_team_channels(interaction.guild, interaction.channel, event, draft, players)

    # --- team voice channels ---------------------------------------------------

    async def create_team_channels(self, guild: discord.Guild, channel: discord.abc.Messageable,
                                   event: Event, draft: Draft, players: List[DraftPlayer]) -> None:
        """Creates a role and private voice channel per team, then moves players who are in voice."""
        names = {p.user_id: p.name for p in players}
        created = {}
        try:
            category = await self.get_category(guild)
            for team, captain, emoji in (("A", draft.captain_a, "🔴"), ("B", draft.captain_b, "🔵")):
                team_name = f"Team {names.get(captain, team)}"
                role = await guild.create_role(name=f"{team_name} ({event.name})"[:100],
                                               reason=f"Draft teams for event '{event.name}'")
                created[f"{team}_role"] = role
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    role: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True),
                    guild.me: discord.PermissionOverwrite(view_channel=True, connect=True),
                }
                voice = await guild.create_voice_channel(
                    f"{emoji} {team_name}"[:100], category=category, overwrites=overwrites,
                    reason=f"Draft teams for event '{event.name}'")
                created[f"{team}_voice"] = voice
        except discord.HTTPException as e:
            log.warning("Could not create team channels for draft %s: %s", draft.id, e)
            await channel.send("⚠️ I couldn't create the team voice channels. "
                               "I need **Manage Roles** and **Manage Channels**.")
        finally:
            ids = {key: obj.id for key, obj in created.items()}
            self.db.set_draft_teams(draft.id, ids.get("A_role"), ids.get("B_role"),
                                    ids.get("A_voice"), ids.get("B_voice"))
        if len(created) < 4:
            return

        for p in players:
            role = created[f"{p.team}_role"]
            member = await self.get_member(guild, p.user_id)
            if member is not None:
                try:
                    await member.add_roles(role, reason="Draft team")
                except discord.HTTPException as e:
                    log.warning("Could not give team role to %s: %s", p.user_id, e)

        draft = self.db.get_draft(draft.id)
        moved, not_in_voice, failed = await self.move_to_team_channels(guild, draft, players)
        lines = [f"🔊 Team voice channels are ready: <#{draft.team_a_voice}> and <#{draft.team_b_voice}>."]
        if moved:
            lines.append(f"Moved **{moved}** player(s) to their team channel.")
        if not_in_voice:
            lines.append("Not in voice yet: " + ", ".join(f"<@{uid}>" for uid in not_in_voice)
                         + ". Join your team channel, or ask the organizer to run `/event move`.")
        if failed:
            lines.append("⚠️ I couldn't move some players. I need the **Move Members** permission.")
        await channel.send("\n".join(lines), allowed_mentions=discord.AllowedMentions(users=True))

    async def get_member(self, guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                return None
        return member

    async def move_to_team_channels(self, guild: discord.Guild, draft: Draft,
                                    players: List[DraftPlayer]) -> tuple:
        """Moves every team member who is in a voice channel. Returns (moved, not_in_voice ids, failed)."""
        moved, not_in_voice, failed = 0, [], 0
        voices = {"A": guild.get_channel(draft.team_a_voice), "B": guild.get_channel(draft.team_b_voice)}
        for p in players:
            target = voices.get(p.team)
            member = await self.get_member(guild, p.user_id)
            if target is None or member is None:
                continue
            if member.voice is None or member.voice.channel is None:
                not_in_voice.append(p.user_id)
                continue
            if member.voice.channel.id == target.id:
                continue
            try:
                await member.move_to(target, reason="Draft team")
                moved += 1
            except discord.HTTPException as e:
                log.warning("Could not move %s: %s", p.user_id, e)
                failed += 1
        return moved, not_in_voice, failed

    async def delete_team_channels(self, guild: discord.Guild, draft: Draft) -> None:
        for channel_id in (draft.team_a_voice, draft.team_b_voice):
            channel = guild.get_channel(channel_id) if channel_id else None
            if channel is not None:
                try:
                    await channel.delete(reason="Draft teams removed")
                except discord.HTTPException as e:
                    log.warning("Could not delete team channel %s: %s", channel_id, e)
        for role_id in (draft.team_a_role, draft.team_b_role):
            role = guild.get_role(role_id) if role_id else None
            if role is not None:
                try:
                    await role.delete(reason="Draft teams removed")
                except discord.HTTPException as e:
                    log.warning("Could not delete team role %s: %s", role_id, e)
        self.db.set_draft_teams(draft.id, None, None, None, None)

    async def member_names(self, guild: discord.Guild, user_ids: List[int]) -> List[tuple]:
        """(user_id, display name) for each user, fetching members that aren't cached."""
        result = []
        for user_id in user_ids:
            member = await self.get_member(guild, user_id)
            result.append((user_id, member.display_name if member else f"User {user_id}"))
        return result

    # --- commands -----------------------------------------------------------

    @app_commands.command(name="create", description="Create an event with a join poll and a private channel")
    @app_commands.describe(
        name="Name of the event, e.g. Friday game night",
        time="Start time, e.g. 20:00, tomorrow 20:00 or 10-10 20:00",
        description="Optional details: what, where, rules, ...",
        poll="Let players pick an option when they join (opens a form to enter the options)",
    )
    async def create(self, interaction: discord.Interaction,
                     name: app_commands.Range[str, 1, 80],
                     time: str,
                     description: Optional[app_commands.Range[str, 1, 1000]] = None,
                     poll: bool = False):
        try:
            start = parse_time(time, config.TIMEZONE)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        if poll:
            await interaction.response.send_modal(PollModal(
                lambda i, question, options: self.create_event(i, name, start, description,
                                                               question, options)))
            return
        await self.create_event(interaction, name, start, description)

    async def create_event(self, interaction: discord.Interaction, name: str, start: datetime,
                           description: Optional[str], poll_question: Optional[str] = None,
                           poll_options: Optional[List[str]] = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        organizer = interaction.user
        start_ts = int(start.timestamp())
        event_id = self.db.create_event(guild.id, name, description, organizer.id, start_ts,
                                        reminded=reminder_already_due(start_ts),
                                        poll_question=poll_question, poll_options=poll_options)

        role = None
        try:
            role = await guild.create_role(name=f"Event: {name}", mentionable=True,
                                           reason=f"Event created by {organizer}")
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                role: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                  read_message_history=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                      read_message_history=True, embed_links=True),
                organizer: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                       read_message_history=True),
            }
            channel = await guild.create_text_channel(
                name=name, category=await self.get_category(guild), overwrites=overwrites,
                topic=f"Event by {organizer.display_name}. Starts {start:%d-%m %H:%M}.",
                reason=f"Event created by {organizer}")
        except discord.Forbidden:
            if role is not None:
                await role.delete(reason="Event creation failed")
            self.db.delete_event(event_id)
            await interaction.followup.send(
                "I'm missing permissions. I need **Manage Roles** and **Manage Channels**.",
                ephemeral=True)
            return

        self.db.set_resources(event_id, channel.id, role.id)
        self.db.add_participant(event_id, organizer.id)
        try:
            await organizer.add_roles(role, reason="Event organizer")
        except discord.HTTPException as e:
            log.warning("Could not give organizer the event role: %s", e)

        event = self.db.get_event(event_id)
        try:
            announcement = await interaction.channel.send(
                embed=build_embed(event, self.db.participants(event_id)), view=event_view(event_id))
        except discord.HTTPException:
            await self.finish_event(event, "cancelled")
            await interaction.followup.send(
                "I can't post messages in this channel, so I cancelled the event.", ephemeral=True)
            return
        self.db.set_announcement(event_id, announcement.channel.id, announcement.id)

        poll_note = ""
        if poll_options:
            poll_note = (f"📊 Poll: **{poll_question}**. {organizer.mention}, you're in already. "
                         "Click Join on the announcement to pick your own option too.\n\n")
        await channel.send(
            f"Welcome to **{name}**, organized by {organizer.mention}! "
            f"Starts <t:{start_ts}:F> (<t:{start_ts}:R>).\n\n{poll_note}"
            "**Organizer commands** (use them in this channel):\n"
            "• `/event edit` to change the time or description\n"
            "• `/event kick` to remove a player\n"
            "• `/event close` to stop new sign-ups (happens automatically at the start time)\n"
            "• `/event reopen` to allow sign-ups again\n"
            "• `/event poll` to start a poll in this channel\n"
            "• `/event captains` to let two captains draft teams (after sign-ups are closed)\n"
            "• `/event move` to move drafted players to their team voice channel\n"
            "• `/event regroup` to pull everyone into your voice channel\n"
            "• `/event end` to close the event and delete this channel\n"
            "• `/event cancel` to cancel the event",
            allowed_mentions=discord.AllowedMentions.none())
        await interaction.followup.send(f"Event created! Channel: {channel.mention}", ephemeral=True)

    @app_commands.command(name="edit", description="Change the time or description of this event")
    @app_commands.describe(time="New start time, e.g. 21:00 or 10-10 20:00",
                           description="New description")
    async def edit(self, interaction: discord.Interaction, time: Optional[str] = None,
                   description: Optional[app_commands.Range[str, 1, 1000]] = None):
        event = await self.event_for_organizer(interaction)
        if event is None:
            return
        if time is None and description is None:
            await interaction.response.send_message("Give a new time and/or description.", ephemeral=True)
            return

        if time is not None:
            try:
                start = parse_time(time, config.TIMEZONE)
            except ValueError as e:
                await interaction.response.send_message(str(e), ephemeral=True)
                return
            start_ts = int(start.timestamp())
            self.db.update_time(event.id, start_ts, reminded=reminder_already_due(start_ts))
        if description is not None:
            self.db.update_description(event.id, description)

        event = self.db.get_event(event.id)
        changes = []
        if time is not None:
            changes.append(f"New start time: <t:{event.start_ts}:F> (<t:{event.start_ts}:R>)")
        if description is not None:
            changes.append("Description updated.")
        if time is not None and event.signups_closed:
            changes.append("Sign-ups are still closed. Use `/event reopen` to let more people join.")
        await interaction.response.send_message(
            f"<@&{event.role_id}> 📝 Event updated!\n" + "\n".join(changes),
            allowed_mentions=discord.AllowedMentions(roles=time is not None))
        await self.refresh_announcement(event)

    @app_commands.command(name="kick", description="Remove a player from this event")
    @app_commands.describe(member="The player to remove")
    async def kick(self, interaction: discord.Interaction, member: discord.Member):
        event = await self.event_for_organizer(interaction)
        if event is None:
            return
        if member.id == event.organizer_id:
            await interaction.response.send_message("You can't kick the organizer.", ephemeral=True)
            return
        if not self.db.remove_participant(event.id, member.id):
            await interaction.response.send_message(f"{member.mention} isn't in this event.", ephemeral=True)
            return

        role = interaction.guild.get_role(event.role_id)
        if role is not None:
            try:
                await member.remove_roles(role, reason=f"Kicked from event by {interaction.user}")
            except discord.HTTPException as e:
                log.warning("Could not remove role from kicked member: %s", e)
        await interaction.response.send_message(
            f"{member.mention} was removed from the event.",
            allowed_mentions=discord.AllowedMentions.none())
        await self.refresh_announcement(event)

    @app_commands.command(name="close", description="Close sign-ups so nobody new can join this event")
    async def close(self, interaction: discord.Interaction):
        event = await self.event_for_organizer(interaction)
        if event is None:
            return
        if event.signups_closed:
            await interaction.response.send_message("Sign-ups are already closed.", ephemeral=True)
            return
        self.db.set_signups_closed(event.id, True)
        count = len(self.db.participants(event.id))
        await interaction.response.send_message(
            f"🔒 Sign-ups are closed by {interaction.user.mention}. Nobody new can join. "
            f"Final player count: **{count}**.",
            allowed_mentions=discord.AllowedMentions.none())
        await self.refresh_announcement(self.db.get_event(event.id))

    @app_commands.command(name="reopen", description="Open sign-ups again for this event")
    async def reopen(self, interaction: discord.Interaction):
        event = await self.event_for_organizer(interaction)
        if event is None:
            return
        if not event.signups_closed:
            await interaction.response.send_message("Sign-ups are already open.", ephemeral=True)
            return
        self.db.set_signups_closed(event.id, False)
        await interaction.response.send_message(
            f"🔓 Sign-ups are open again, reopened by {interaction.user.mention}.",
            allowed_mentions=discord.AllowedMentions.none())
        await self.refresh_announcement(self.db.get_event(event.id))

    @app_commands.command(name="poll", description="Start a poll in this event channel")
    async def channel_poll(self, interaction: discord.Interaction):
        event = await self.event_for_organizer(interaction)
        if event is None:
            return

        async def post_poll(modal_interaction: discord.Interaction, question: str, options: List[str]):
            poll_id = self.db.create_poll(event.id, modal_interaction.channel_id, question, options)
            poll = self.db.get_poll(poll_id)
            view = discord.ui.View(timeout=None)
            view.add_item(ChannelPollSelect(poll.id, poll.options))
            await modal_interaction.response.send_message(
                f"📊 New poll by {modal_interaction.user.mention}",
                embed=build_channel_poll_embed(poll, []), view=view,
                allowed_mentions=discord.AllowedMentions.none())
            message = await modal_interaction.original_response()
            self.db.set_poll_message(poll.id, message.id)

        await interaction.response.send_modal(PollModal(post_poll))

    @app_commands.command(name="captains", description="Start a snake draft where two captains pick their teams")
    @app_commands.describe(captain1="Captain who picks first", captain2="Captain who picks second")
    async def captains(self, interaction: discord.Interaction,
                       captain1: discord.Member, captain2: discord.Member):
        event = await self.event_for_organizer(interaction)
        if event is None:
            return

        async def refuse(text: str):
            await interaction.response.send_message(text, ephemeral=True)

        participant_ids = [p.user_id for p in self.db.participants(event.id)]
        count = len(participant_ids)
        if not event.signups_closed:
            return await refuse(
                "🔒 Sign-ups are still open. Close them first with `/event close`, "
                "so nobody can join while the teams are being picked.")
        if count <= 3:
            return await refuse(
                f"A draft needs at least **4 players** (2 captains + 2 to pick). "
                f"This event has only **{count}**.")
        if count % 2:
            return await refuse(
                f"This event has **{count} players**, so the teams would be uneven. "
                "Make it an even number first: remove someone with `/event kick`, "
                "or use `/event reopen` to let one more player join.")
        if count - 2 > MAX_DRAFT_POOL:
            return await refuse(f"A draft can have at most {MAX_DRAFT_POOL + 2} players.")
        if captain1.id == captain2.id:
            return await refuse("Pick two different captains.")
        not_in_event = [c.mention for c in (captain1, captain2) if c.id not in participant_ids]
        if not_in_event:
            return await refuse(f"{' and '.join(not_in_event)} isn't in this event. "
                                "Captains must be players of the event.")

        await interaction.response.defer()
        previous = self.db.active_draft(event.id)
        if previous is not None:
            self.db.set_draft_status(previous.id, "replaced")
            if previous.message_id:
                previous = self.db.get_draft(previous.id)
                try:
                    await interaction.channel.get_partial_message(previous.message_id).edit(
                        content=None, embed=build_draft_embed(previous, self.db.draft_players(previous.id)),
                        view=None)
                except discord.HTTPException as e:
                    log.warning("Could not update replaced draft %s: %s", previous.id, e)
        for old in self.db.drafts_with_teams(event.id):
            await self.delete_team_channels(interaction.guild, old)

        players = await self.member_names(interaction.guild, participant_ids)
        draft_id = self.db.create_draft(event.id, interaction.channel_id, captain1.id, captain2.id, players)
        draft = self.db.get_draft(draft_id)
        draft_players = self.db.draft_players(draft_id)
        message = await interaction.followup.send(
            f"⚔️ Draft started! {captain1.mention} vs {captain2.mention}. "
            f"{captain1.mention}, you pick first.",
            embed=build_draft_embed(draft, draft_players), view=build_draft_view(draft, draft_players),
            allowed_mentions=discord.AllowedMentions(users=[captain1, captain2]), wait=True)
        self.db.set_draft_message(draft_id, message.id)

    @app_commands.command(name="move", description="Move all drafted players who are in voice to their team channel")
    async def move(self, interaction: discord.Interaction):
        event = await self.event_for_organizer(interaction)
        if event is None:
            return
        draft = self.db.latest_finished_draft(event.id)
        if draft is None or not draft.team_a_voice:
            await interaction.response.send_message(
                "There are no team channels yet. Finish a draft with `/event captains` first.", ephemeral=True)
            return
        await interaction.response.defer()
        moved, not_in_voice, failed = await self.move_to_team_channels(
            interaction.guild, draft, self.db.draft_players(draft.id))
        lines = [f"🔊 Moved **{moved}** player(s) to <#{draft.team_a_voice}> and <#{draft.team_b_voice}>."]
        if not_in_voice:
            lines.append("Not in voice: " + ", ".join(f"<@{uid}>" for uid in not_in_voice))
        if failed:
            lines.append("⚠️ I couldn't move some players. I need the **Move Members** permission.")
        await interaction.followup.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="regroup", description="Pull all event players in voice into your voice channel")
    async def regroup(self, interaction: discord.Interaction):
        event = await self.event_for_organizer(interaction)
        if event is None:
            return
        voice_state = interaction.user.voice
        if voice_state is None or voice_state.channel is None:
            await interaction.response.send_message(
                "Join a voice channel first. I'll pull everyone into the channel you're in.", ephemeral=True)
            return
        target = voice_state.channel
        await interaction.response.defer()
        moved, failed = 0, 0
        for p in self.db.participants(event.id):
            member = await self.get_member(interaction.guild, p.user_id)
            if member is None or member.voice is None or member.voice.channel is None:
                continue
            if member.voice.channel.id == target.id:
                continue
            try:
                await member.move_to(target, reason="Regroup")
                moved += 1
            except discord.HTTPException:
                failed += 1
        text = f"📣 Regrouped **{moved}** player(s) into {target.mention}."
        if failed:
            text += " ⚠️ Some players couldn't be moved (I need **Move Members**)."
        await interaction.followup.send(text, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="end", description="End this event and delete its channel and role")
    async def end(self, interaction: discord.Interaction):
        event = await self.event_for_organizer(interaction)
        if event is None:
            return
        await interaction.response.send_message("Ending the event and cleaning up…")
        await self.finish_event(event, "ended")

    @app_commands.command(name="cancel", description="Cancel this event and delete its channel and role")
    async def cancel(self, interaction: discord.Interaction):
        event = await self.event_for_organizer(interaction)
        if event is None:
            return
        await interaction.response.send_message("Cancelling the event and cleaning up…")
        await self.finish_event(event, "cancelled")

    @app_commands.command(name="list", description="Show all upcoming events")
    async def list_events(self, interaction: discord.Interaction):
        events = self.db.active_events(interaction.guild_id)
        if not events:
            await interaction.response.send_message("There are no active events.", ephemeral=True)
            return
        lines = []
        for event in events:
            count = len(self.db.participants(event.id))
            link = (f"https://discord.com/channels/{event.guild_id}/"
                    f"{event.announce_channel_id}/{event.announce_message_id}")
            closed = " · 🔒 sign-ups closed" if event.signups_closed else ""
            lines.append(f"• **[{event.name}]({link})** · <t:{event.start_ts}:F> · {count} players{closed}")
        embed = discord.Embed(title="Upcoming events", description="\n".join(lines),
                              color=discord.Color.orange())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction,
                                    error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            return  # interaction_check already told the user why
        log.exception("Error in event command", exc_info=error)
        message = "Something went wrong. Check the bot's console for details."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    # --- reminders & automatic cleanup -------------------------------------

    @tasks.loop(seconds=30)
    async def housekeeping(self):
        now = time.time()
        for event in self.db.active_events():
            try:
                if now >= event.start_ts + config.AUTO_CLEANUP_HOURS * 3600:
                    log.info("Auto-closing event %s (%s)", event.id, event.name)
                    await self.finish_event(event, "expired")
                    continue

                if not event.autoclosed and now >= event.start_ts:
                    already_closed = event.signups_closed
                    self.db.set_signups_closed(event.id, True, auto=True)
                    if not already_closed:
                        log.info("Closing sign-ups for event %s (%s)", event.id, event.name)
                        await self.refresh_announcement(self.db.get_event(event.id))
                        channel = self.bot.get_channel(event.channel_id)
                        if channel is not None:
                            count = len(self.db.participants(event.id))
                            await channel.send(
                                f"🔒 **{event.name}** has started, so sign-ups are now closed. "
                                f"Final player count: **{count}**. The organizer can use "
                                "`/event reopen` to let more people join.")

                if not event.reminded and now >= event.start_ts - config.REMINDER_MINUTES * 60:
                    self.db.mark_reminded(event.id)
                    channel = self.bot.get_channel(event.channel_id)
                    if channel is not None:
                        when = (f"starts <t:{event.start_ts}:R>" if now < event.start_ts
                                else "is starting now")
                        await channel.send(
                            f"<@&{event.role_id}> ⏰ **{event.name}** {when}! Get ready.",
                            allowed_mentions=discord.AllowedMentions(roles=True))
            except Exception:
                log.exception("Housekeeping failed for event %s", event.id)

    @housekeeping.before_loop
    async def before_housekeeping(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Events(bot))
