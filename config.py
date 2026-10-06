import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")

# Optional: your server ID. Makes slash commands show up instantly instead of after ~1 hour.
GUILD_ID = int(os.getenv("GUILD_ID") or 0) or None

# Timezone used to interpret times typed in /event create, e.g. "20:00".
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Amsterdam"))

# How many minutes before the start the event role gets pinged. 0 disables reminders.
REMINDER_MINUTES = int(os.getenv("REMINDER_MINUTES", "15"))

# If nobody runs /event end, the channel and role are removed this many hours after the start.
AUTO_CLEANUP_HOURS = float(os.getenv("AUTO_CLEANUP_HOURS", "3"))

# Category the temporary event channels are created in (created automatically if missing).
EVENTS_CATEGORY = os.getenv("EVENTS_CATEGORY", "Events")

DATABASE_PATH = os.getenv("DATABASE_PATH", "events.db")
