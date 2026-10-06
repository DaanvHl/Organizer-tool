import logging
import re
import time
from datetime import datetime
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from database import Database, Event, Participant
from timeparse import parse_time

log = logging.getLogger(__name__)

STATUS_FOOTERS = {
    "ended": "This event has ended.",
    "cancelled": "This event was cancelled.",
    "expired": "This event was closed automatically.",
}


MAX_POLL_OPTIONS = 25  # Discord's limit for a select menu


def truncate_field(value: str) -> str:
    if len(value) > 1024:
        value = value[:1000].rsplit("\n", 1)[0] + "\n…"
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

    if event.active and options:
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


class PollModal(discord.ui.Modal, title="Event poll"):
    question = discord.ui.TextInput(
        label="Question", max_length=200,
        placeholder="e.g. Which role do you want?")
    options = discord.ui.TextInput(
        label="Options (one per line)", style=discord.TextStyle.paragraph, max_length=2000,
        placeholder="Option 1\nOption 2\nOption 3")

    def __init__(self, cog: "Events", name: str, start: datetime, description: Optional[str]):
        super().__init__(timeout=900)
        self.cog = cog
        self.name = name
        self.start = start
        self.description = description

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
                f"{error} Run `/event create` again.\n\nYour options were:\n```\n{self.options.value}\n```",
                ephemeral=True)
            return
        await self.cog.create_event(interaction, self.name, self.start, self.description,
                                    self.question.value.strip(), options)


def event_view(event_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
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

    async def refresh_announcement(self, event: Event, remove_buttons: bool = False) -> None:
        if not event.announce_channel_id or not event.announce_message_id:
            return
        channel = self.bot.get_channel(event.announce_channel_id)
        if channel is None:
            return
        message = channel.get_partial_message(event.announce_message_id)
        embed = build_embed(event, self.db.participants(event.id))
        try:
            if remove_buttons:
                await message.edit(embed=embed, view=None)
            else:
                await message.edit(embed=embed)
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
        await self.refresh_announcement(event, remove_buttons=True)

        guild = self.bot.get_guild(event.guild_id)
        if guild is None:
            return
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
            await interaction.response.send_modal(PollModal(self, name, start, description))
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
            lines.append(f"• **[{event.name}]({link})** · <t:{event.start_ts}:F> · {count} players")
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
