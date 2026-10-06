import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id            INTEGER NOT NULL,
    name                TEXT    NOT NULL,
    description         TEXT,
    organizer_id        INTEGER NOT NULL,
    start_ts            INTEGER NOT NULL,
    channel_id          INTEGER,
    role_id             INTEGER,
    announce_channel_id INTEGER,
    announce_message_id INTEGER,
    reminded            INTEGER NOT NULL DEFAULT 0,
    status              TEXT    NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS participants (
    event_id  INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    user_id   INTEGER NOT NULL,
    joined_ts INTEGER NOT NULL,
    PRIMARY KEY (event_id, user_id)
);
"""

# Columns added after the first release; added to existing databases on startup.
MIGRATIONS = [
    ("events", "poll_question", "TEXT"),
    ("events", "poll_options", "TEXT"),  # JSON list of option labels
    ("participants", "choice", "INTEGER"),  # index into poll_options
]


@dataclass
class Event:
    id: int
    guild_id: int
    name: str
    description: Optional[str]
    organizer_id: int
    start_ts: int
    channel_id: Optional[int]
    role_id: Optional[int]
    announce_channel_id: Optional[int]
    announce_message_id: Optional[int]
    reminded: bool
    status: str  # active, ended, cancelled, expired
    poll_question: Optional[str] = None
    poll_options: Optional[str] = None

    @property
    def active(self) -> bool:
        return self.status == "active"

    @property
    def options(self) -> List[str]:
        return json.loads(self.poll_options) if self.poll_options else []

    @property
    def has_poll(self) -> bool:
        return bool(self.options)


@dataclass
class Participant:
    user_id: int
    choice: Optional[int]


class Database:
    def __init__(self, path: str):
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        for table, column, column_type in MIGRATIONS:
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
        self.conn.commit()

    @staticmethod
    def _to_event(row: Optional[sqlite3.Row]) -> Optional[Event]:
        if row is None:
            return None
        data = dict(row)
        data["reminded"] = bool(data["reminded"])
        return Event(**data)

    # --- events -------------------------------------------------------------

    def create_event(self, guild_id: int, name: str, description: Optional[str],
                     organizer_id: int, start_ts: int, reminded: bool,
                     poll_question: Optional[str] = None,
                     poll_options: Optional[List[str]] = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO events (guild_id, name, description, organizer_id, start_ts, reminded, "
            "poll_question, poll_options) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, name, description, organizer_id, start_ts, int(reminded),
             poll_question, json.dumps(poll_options) if poll_options else None),
        )
        self.conn.commit()
        return cur.lastrowid

    def delete_event(self, event_id: int) -> None:
        self.conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        self.conn.commit()

    def get_event(self, event_id: int) -> Optional[Event]:
        row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._to_event(row)

    def get_event_by_channel(self, channel_id: int) -> Optional[Event]:
        row = self.conn.execute(
            "SELECT * FROM events WHERE channel_id = ? AND status = 'active'", (channel_id,)
        ).fetchone()
        return self._to_event(row)

    def active_events(self, guild_id: Optional[int] = None) -> List[Event]:
        if guild_id is None:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE status = 'active' ORDER BY start_ts"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE status = 'active' AND guild_id = ? ORDER BY start_ts",
                (guild_id,),
            ).fetchall()
        return [self._to_event(r) for r in rows]

    def set_resources(self, event_id: int, channel_id: int, role_id: int) -> None:
        self.conn.execute(
            "UPDATE events SET channel_id = ?, role_id = ? WHERE id = ?",
            (channel_id, role_id, event_id),
        )
        self.conn.commit()

    def set_announcement(self, event_id: int, channel_id: int, message_id: int) -> None:
        self.conn.execute(
            "UPDATE events SET announce_channel_id = ?, announce_message_id = ? WHERE id = ?",
            (channel_id, message_id, event_id),
        )
        self.conn.commit()

    def update_time(self, event_id: int, start_ts: int, reminded: bool) -> None:
        self.conn.execute(
            "UPDATE events SET start_ts = ?, reminded = ? WHERE id = ?",
            (start_ts, int(reminded), event_id),
        )
        self.conn.commit()

    def update_description(self, event_id: int, description: Optional[str]) -> None:
        self.conn.execute("UPDATE events SET description = ? WHERE id = ?", (description, event_id))
        self.conn.commit()

    def mark_reminded(self, event_id: int) -> None:
        self.conn.execute("UPDATE events SET reminded = 1 WHERE id = ?", (event_id,))
        self.conn.commit()

    def set_status(self, event_id: int, status: str) -> None:
        self.conn.execute("UPDATE events SET status = ? WHERE id = ?", (status, event_id))
        self.conn.commit()

    # --- participants -------------------------------------------------------

    def add_participant(self, event_id: int, user_id: int, choice: Optional[int] = None) -> bool:
        """Returns False if the user had already joined."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO participants (event_id, user_id, joined_ts, choice) "
            "VALUES (?, ?, ?, ?)",
            (event_id, user_id, int(time.time()), choice),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def set_choice(self, event_id: int, user_id: int, choice: int) -> None:
        self.conn.execute(
            "UPDATE participants SET choice = ? WHERE event_id = ? AND user_id = ?",
            (choice, event_id, user_id),
        )
        self.conn.commit()

    def get_choice(self, event_id: int, user_id: int) -> Optional[int]:
        row = self.conn.execute(
            "SELECT choice FROM participants WHERE event_id = ? AND user_id = ?", (event_id, user_id)
        ).fetchone()
        return row["choice"] if row else None

    def remove_participant(self, event_id: int, user_id: int) -> bool:
        """Returns False if the user wasn't in the event."""
        cur = self.conn.execute(
            "DELETE FROM participants WHERE event_id = ? AND user_id = ?", (event_id, user_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def participants(self, event_id: int) -> List[Participant]:
        rows = self.conn.execute(
            "SELECT user_id, choice FROM participants WHERE event_id = ? ORDER BY joined_ts, rowid",
            (event_id,),
        ).fetchall()
        return [Participant(r["user_id"], r["choice"]) for r in rows]
