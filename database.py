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

-- Polls started inside an event channel with /event poll
CREATE TABLE IF NOT EXISTS polls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL,
    message_id INTEGER,
    question   TEXT    NOT NULL,
    options    TEXT    NOT NULL  -- JSON list of option labels
);

CREATE TABLE IF NOT EXISTS poll_votes (
    poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    choice  INTEGER NOT NULL,
    PRIMARY KEY (poll_id, user_id)
);

-- Captains drafts started with /event captains
CREATE TABLE IF NOT EXISTS drafts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL,
    message_id INTEGER,
    captain_a  INTEGER NOT NULL,
    captain_b  INTEGER NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'active'  -- active, done, replaced
);

CREATE TABLE IF NOT EXISTS draft_players (
    draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL,
    name     TEXT    NOT NULL,
    team     TEXT,              -- 'A', 'B' or NULL while still available
    pick_no  INTEGER,           -- 0 for captains, 1.. for picks
    PRIMARY KEY (draft_id, user_id)
);
"""

# Columns added after the first release; added to existing databases on startup.
MIGRATIONS = [
    ("events", "poll_question", "TEXT"),
    ("events", "poll_options", "TEXT"),  # JSON list of option labels
    ("participants", "choice", "INTEGER"),  # index into poll_options
    ("events", "signups_closed", "INTEGER NOT NULL DEFAULT 0"),
    ("events", "autoclosed", "INTEGER NOT NULL DEFAULT 0"),  # sign-ups were closed at start time
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
    signups_closed: bool = False
    autoclosed: bool = False

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


@dataclass
class Poll:
    id: int
    event_id: int
    channel_id: int
    message_id: Optional[int]
    question: str
    options_json: str

    @property
    def options(self) -> List[str]:
        return json.loads(self.options_json)


@dataclass
class Draft:
    id: int
    event_id: int
    channel_id: int
    message_id: Optional[int]
    captain_a: int
    captain_b: int
    status: str


@dataclass
class DraftPlayer:
    user_id: int
    name: str
    team: Optional[str]
    pick_no: Optional[int]


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
        for flag in ("reminded", "signups_closed", "autoclosed"):
            data[flag] = bool(data[flag])
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
            "UPDATE events SET start_ts = ?, reminded = ?, autoclosed = 0 WHERE id = ?",
            (start_ts, int(reminded), event_id),
        )
        self.conn.commit()

    def update_description(self, event_id: int, description: Optional[str]) -> None:
        self.conn.execute("UPDATE events SET description = ? WHERE id = ?", (description, event_id))
        self.conn.commit()

    def mark_reminded(self, event_id: int) -> None:
        self.conn.execute("UPDATE events SET reminded = 1 WHERE id = ?", (event_id,))
        self.conn.commit()

    def set_signups_closed(self, event_id: int, closed: bool, auto: bool = False) -> None:
        """Opens or closes sign-ups. auto=True marks the automatic close at start time as done."""
        if auto:
            self.conn.execute("UPDATE events SET signups_closed = ?, autoclosed = 1 WHERE id = ?",
                              (int(closed), event_id))
        else:
            self.conn.execute("UPDATE events SET signups_closed = ? WHERE id = ?", (int(closed), event_id))
        self.conn.commit()

    def is_participant(self, event_id: int, user_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM participants WHERE event_id = ? AND user_id = ?", (event_id, user_id)
        ).fetchone()
        return row is not None

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

    # --- channel polls ------------------------------------------------------

    def create_poll(self, event_id: int, channel_id: int, question: str, options: List[str]) -> int:
        cur = self.conn.execute(
            "INSERT INTO polls (event_id, channel_id, question, options) VALUES (?, ?, ?, ?)",
            (event_id, channel_id, question, json.dumps(options)),
        )
        self.conn.commit()
        return cur.lastrowid

    def set_poll_message(self, poll_id: int, message_id: int) -> None:
        self.conn.execute("UPDATE polls SET message_id = ? WHERE id = ?", (message_id, poll_id))
        self.conn.commit()

    def get_poll(self, poll_id: int) -> Optional[Poll]:
        row = self.conn.execute(
            "SELECT id, event_id, channel_id, message_id, question, options AS options_json "
            "FROM polls WHERE id = ?", (poll_id,)
        ).fetchone()
        return Poll(**dict(row)) if row else None

    def set_vote(self, poll_id: int, user_id: int, choice: int) -> None:
        self.conn.execute(
            "INSERT INTO poll_votes (poll_id, user_id, choice) VALUES (?, ?, ?) "
            "ON CONFLICT (poll_id, user_id) DO UPDATE SET choice = excluded.choice",
            (poll_id, user_id, choice),
        )
        self.conn.commit()

    def votes(self, poll_id: int) -> List[Participant]:
        rows = self.conn.execute(
            "SELECT user_id, choice FROM poll_votes WHERE poll_id = ? ORDER BY rowid", (poll_id,)
        ).fetchall()
        return [Participant(r["user_id"], r["choice"]) for r in rows]

    # --- drafts -------------------------------------------------------------

    def create_draft(self, event_id: int, channel_id: int, captain_a: int, captain_b: int,
                     players: List[tuple]) -> int:
        """players: (user_id, name) for everyone in the draft, captains included."""
        cur = self.conn.execute(
            "INSERT INTO drafts (event_id, channel_id, captain_a, captain_b) VALUES (?, ?, ?, ?)",
            (event_id, channel_id, captain_a, captain_b),
        )
        draft_id = cur.lastrowid
        for user_id, name in players:
            team = "A" if user_id == captain_a else "B" if user_id == captain_b else None
            self.conn.execute(
                "INSERT INTO draft_players (draft_id, user_id, name, team, pick_no) VALUES (?, ?, ?, ?, ?)",
                (draft_id, user_id, name, team, 0 if team else None),
            )
        self.conn.commit()
        return draft_id

    def set_draft_message(self, draft_id: int, message_id: int) -> None:
        self.conn.execute("UPDATE drafts SET message_id = ? WHERE id = ?", (message_id, draft_id))
        self.conn.commit()

    def set_draft_status(self, draft_id: int, status: str) -> None:
        self.conn.execute("UPDATE drafts SET status = ? WHERE id = ?", (status, draft_id))
        self.conn.commit()

    def get_draft(self, draft_id: int) -> Optional[Draft]:
        row = self.conn.execute(
            "SELECT id, event_id, channel_id, message_id, captain_a, captain_b, status "
            "FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        return Draft(**dict(row)) if row else None

    def active_draft(self, event_id: int) -> Optional[Draft]:
        row = self.conn.execute(
            "SELECT id FROM drafts WHERE event_id = ? AND status = 'active'", (event_id,)
        ).fetchone()
        return self.get_draft(row["id"]) if row else None

    def draft_players(self, draft_id: int) -> List[DraftPlayer]:
        rows = self.conn.execute(
            "SELECT user_id, name, team, pick_no FROM draft_players WHERE draft_id = ? ORDER BY rowid",
            (draft_id,),
        ).fetchall()
        return [DraftPlayer(r["user_id"], r["name"], r["team"], r["pick_no"]) for r in rows]

    def assign_pick(self, draft_id: int, user_id: int, team: str, pick_no: int) -> None:
        self.conn.execute(
            "UPDATE draft_players SET team = ?, pick_no = ? WHERE draft_id = ? AND user_id = ?",
            (team, pick_no, draft_id, user_id),
        )
        self.conn.commit()

    # --- participants -------------------------------------------------------

    def participants(self, event_id: int) -> List[Participant]:
        rows = self.conn.execute(
            "SELECT user_id, choice FROM participants WHERE event_id = ? ORDER BY joined_ts, rowid",
            (event_id,),
        ).fetchall()
        return [Participant(r["user_id"], r["choice"]) for r in rows]
