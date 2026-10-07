import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")

# Optional: your server IDs, separated by commas. Makes slash commands show up instantly in
# those servers instead of after ~1 hour. Empty registers the commands globally.
GUILD_IDS = [int(part) for part in os.getenv("GUILD_ID", "").replace(" ", ",").split(",") if part.strip()]

# Channels where /event commands may be used, separated by commas. Empty means everywhere.
# Event channels created by the bot always allow commands, so events can be managed there.
ALLOWED_CHANNEL_IDS = {
    int(part) for part in os.getenv("ALLOWED_CHANNEL_IDS", "").replace(" ", ",").split(",") if part.strip()
}

# Timezone used to interpret times typed in /event create, e.g. "20:00".
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Amsterdam"))

# How many minutes before the start the event role gets pinged. 0 disables reminders.
REMINDER_MINUTES = int(os.getenv("REMINDER_MINUTES", "15"))

# If nobody runs /event end, the channel and role are removed this many hours after the start.
AUTO_CLEANUP_HOURS = float(os.getenv("AUTO_CLEANUP_HOURS", "3"))

# Category the temporary event channels are created in (created automatically if missing).
EVENTS_CATEGORY = os.getenv("EVENTS_CATEGORY", "Events")

# Funny reactions to words like "gg", "ez" and "lag". Needs the Message Content Intent to be
# switched on in the Discord developer portal, otherwise the bot can't start.
FUN_KEYWORDS = os.getenv("FUN_KEYWORDS", "false").strip().lower() in ("1", "true", "yes", "on")

# Minimum seconds between replies to the same keyword in the same channel (emoji reactions are not limited).
KEYWORD_COOLDOWN_SECONDS = int(os.getenv("KEYWORD_COOLDOWN_SECONDS", "60"))

# Users whose every message gets a ♿ reaction, separated by commas (right-click user -> Copy User ID).
WHEELCHAIR_USER_IDS = {
    int(part) for part in os.getenv("WHEELCHAIR_USER_IDS", "").replace(" ", ",").split(",") if part.strip()
}

DATABASE_PATH =os.getenv("DATABASE_PATH", "events.db")
