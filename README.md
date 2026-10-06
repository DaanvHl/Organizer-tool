# Event Bot

Discord bot for hosting events (e.g. ProTanki clan wars / fun nights).

`/event create` posts a poll with **Join** / **Leave** buttons and creates a temporary role and a
private channel. Everyone who clicks Join gets the role and can see the channel. When the event is
over, the channel and role are deleted.

## Commands

| Command | Where | Who |
|---|---|---|
| `/event create name time [description] [poll]` | Any channel (the announcement is posted there) | Everyone |
| `/event edit [time] [description]` | Inside the event channel | Organizer / admins |
| `/event kick member` | Inside the event channel | Organizer / admins |
| `/event close` | Inside the event channel | Organizer / admins |
| `/event reopen` | Inside the event channel | Organizer / admins |
| `/event poll` | Inside the event channel | Organizer / admins |
| `/event captains captain1 captain2` | Inside the event channel | Organizer / admins |
| `/event move` | Inside the event channel | Organizer / admins |
| `/event regroup` | Inside the event channel | Organizer / admins |
| `/event end` | Inside the event channel | Organizer / admins |
| `/event cancel` | Inside the event channel | Organizer / admins |
| `/event list` | Anywhere | Everyone |

Times can be written as `20:00`, `tomorrow 20:00`, `10-10 20:00` or `10-10-2026 20:00`
(interpreted in the `TIMEZONE` from `.env`). Discord shows them to each member in their own timezone.

**Allowed channels:** set `ALLOWED_CHANNEL_IDS` (comma-separated, e.g. `123456789,987654321`) to
only allow `/event` commands in those channels. Event channels created by the bot always allow
commands, so organizers can still edit, end or cancel their event there.

**Poll:** set `poll: True` on `/event create` to get a form where you enter a question and the
options (one per line, 2–25). Players who click Join must pick an option before they get access,
and can click Join again later to change their choice. The announcement shows the results and
each player's choice.

**Closing sign-ups:** `/event close` stops new people from joining. The Join button disappears,
but the channel stays and players who already joined can still leave. Sign-ups also close
automatically at the start time. `/event reopen` opens them again.

**Polls in the event channel:** `/event poll` opens the same form as at creation and posts a poll
in the event channel. Everyone in the channel can vote and change their vote; the results update
live. This works whether sign-ups are open or closed.

**Captains draft:** `/event captains captain1 captain2` lets two captains pick teams in snake order
(X O O X X O O X …; captain1 picks first). Only works when sign-ups are closed, the event has at
least 4 players and an even number of players, and both captains are in the event. Only the captain
whose turn it is can pick; the last player is assigned automatically. Running it again replaces
the current draft.

**Team voice channels:** when a draft completes, each team gets a role and a private voice channel
(🔴 / 🔵). Players who are in voice are moved automatically. `/event move` moves everyone to their
team channel again, `/event regroup` pulls all players into the voice channel you're in. Team
channels and roles are deleted when the event ends or a new draft starts.

## Fun commands

Usable anywhere: `/excuse`, `/roast member`, `/blame` (picks a random culprit: event players in an
event channel, otherwise recent chatters), `/bonk member` (with lifetime bonk counter), `/coinflip`
and `/8ball question`.

**Keyword reactions** (`FUN_KEYWORDS=true`): the bot reacts to words like `gg` 🫡, `bruh` 💀 and
`noob` 🍼, and replies to `ez`, `lag`, `rigged`, `afk` and `who asked` (at most once per
`KEYWORD_COOLDOWN_SECONDS` per channel). This needs **Message Content Intent** enabled in the
developer portal (*Bot* page) — enable it *before* setting `FUN_KEYWORDS=true`, or the bot won't start.

Automatic behaviour:
- The event role is pinged `REMINDER_MINUTES` (default 15) before the start.
- Sign-ups close at the start time.
- If nobody runs `/event end`, the event is closed `AUTO_CLEANUP_HOURS` (default 3) after the start.

## Setup

1. **Create the bot** at <https://discord.com/developers/applications> → New Application → *Bot* →
   *Reset Token* and copy the token. No privileged intents are needed.
2. **Invite it**: *OAuth2 → URL Generator*, scopes `bot` + `applications.commands`, permissions
   **Manage Roles**, **Manage Channels**, **Move Members**, **View Channels**, **Send Messages**,
   **Embed Links**, **Read Message History**, **Add Reactions**. Open the generated URL and add the bot to your server.
3. **Role order**: in *Server Settings → Roles*, drag the bot's role near the top. It can only
   manage roles that are *below* its own role.
4. **Configure**: copy `.env.example` to `.env` and fill in `DISCORD_TOKEN` and `GUILD_ID`.
5. **Run**:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   python bot.py
   ```

The bot stores events in `events.db` (SQLite), so Join buttons keep working after a restart.

## Deploying to Railway

Railway deploys automatically on every push to `main` once the repo is connected:

1. Railway project → *New* → *GitHub Repo* → select this repo.
2. *Variables*: add `DISCORD_TOKEN`, `GUILD_ID` and `DATABASE_PATH=/data/events.db`
   (plus any other setting from `.env.example` you want to change).
3. Right-click the service → *Attach Volume* → mount path `/data`. Without a volume the database
   is wiped on every deploy, and old Join buttons stop working.
4. *Settings → Source*: make sure the branch is `main` (auto-deploy is on by default).

The start command and restart policy come from `railway.json`. Keep it at one replica,
otherwise every command would be handled twice.
