# Event Bot

Discord bot for hosting events (e.g. ProTanki clan wars / fun nights).

`/event create` posts a poll with **Join** / **Leave** buttons and creates a temporary role and a
private channel. Everyone who clicks Join gets the role and can see the channel. When the event is
over, the channel and role are deleted.

## Commands

| Command | Where | Who |
|---|---|---|
| `/event create name time [description]` | Any channel (the poll is posted there) | Everyone |
| `/event edit [time] [description]` | Inside the event channel | Organizer / admins |
| `/event kick member` | Inside the event channel | Organizer / admins |
| `/event end` | Inside the event channel | Organizer / admins |
| `/event cancel` | Inside the event channel | Organizer / admins |
| `/event list` | Anywhere | Everyone |

Times can be written as `20:00`, `tomorrow 20:00`, `10-10 20:00` or `10-10-2026 20:00`
(interpreted in the `TIMEZONE` from `.env`). Discord shows them to each member in their own timezone.

Automatic behaviour:
- The event role is pinged `REMINDER_MINUTES` (default 15) before the start.
- If nobody runs `/event end`, the event is closed `AUTO_CLEANUP_HOURS` (default 3) after the start.

## Setup

1. **Create the bot** at <https://discord.com/developers/applications> → New Application → *Bot* →
   *Reset Token* and copy the token. No privileged intents are needed.
2. **Invite it**: *OAuth2 → URL Generator*, scopes `bot` + `applications.commands`, permissions
   **Manage Roles**, **Manage Channels**, **View Channels**, **Send Messages**, **Embed Links**,
   **Read Message History**. Open the generated URL and add the bot to your server.
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
