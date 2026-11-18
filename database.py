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

-- Captains games started with /captains: queue -> captains -> choosing -> drafting -> mapvote -> playing -> reported -> done
CREATE TABLE IF NOT EXISTS lobbies (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    channel_id       INTEGER NOT NULL,
    creator_id       INTEGER NOT NULL,
    event_id         INTEGER REFERENCES events(id) ON DELETE SET NULL,  -- set when started in an event channel
    queue_message_id INTEGER,
    draft_message_id INTEGER,
    status           TEXT    NOT NULL DEFAULT 'queue',  -- queue, captains, choosing, drafting, mapvote, playing, reported, done, cancelled, removed
    captain_a        INTEGER,
    captain_b        INTEGER,
    winner           TEXT,     -- 'A', 'B' or 'draw' once a result is reported
    reported_by      INTEGER,
    team_a_role      INTEGER,
    team_b_role      INTEGER,
    team_a_voice     INTEGER,
    team_b_voice     INTEGER,
    draft_type       TEXT    NOT NULL DEFAULT 'snake',  -- 'snake' (A B B A ...) or 'normal' (A B A B ...)
    map_options      TEXT,     -- JSON list of the maps in the map vote
    map_name         TEXT,     -- the map that won the vote
    map_vote_ends    INTEGER,  -- unix time when the map vote ends by itself
    map_rerolled     INTEGER NOT NULL DEFAULT 0  -- 1 once a captain used the re-roll button
);

CREATE TABLE IF NOT EXISTS lobby_players (
    lobby_id INTEGER NOT NULL REFERENCES lobbies(id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL,
    name     TEXT    NOT NULL,
    team     TEXT,              -- 'A', 'B' or NULL while still available
    pick_no  INTEGER,           -- 0 for captains, 1.. for picks
    PRIMARY KEY (lobby_id, user_id)
);

-- Wins, losses, draws and ranked points from approved /captains games
CREATE TABLE IF NOT EXISTS player_stats (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    wins     INTEGER NOT NULL DEFAULT 0,
    losses   INTEGER NOT NULL DEFAULT 0,
    draws    INTEGER NOT NULL DEFAULT 0,
    points   INTEGER NOT NULL DEFAULT 0,  -- ranked points: +WIN_POINTS per win, -LOSS_POINTS per loss, never below 0
    streak   INTEGER NOT NULL DEFAULT 0,  -- wins in a row; a loss sets it to 0, a draw keeps it
    PRIMARY KEY (guild_id, user_id)
);

-- Map votes after the captains draft: one vote per player, choice = index into lobbies.map_options
CREATE TABLE IF NOT EXISTS map_votes (
    lobby_id INTEGER NOT NULL REFERENCES lobbies(id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL,
    choice   INTEGER NOT NULL,
    PRIMARY KEY (lobby_id, user_id)
);

-- /tiebreaker: 3 maps, each captain bans 1 (ban_a / ban_b = index into maps)
CREATE TABLE IF NOT EXISTS tiebreakers (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    lobby_id INTEGER NOT NULL REFERENCES lobbies(id) ON DELETE CASCADE,
    maps     TEXT    NOT NULL,  -- JSON list of map names
    ban_a    INTEGER,
    ban_b    INTEGER
);

-- /bonk counter
CREATE TABLE IF NOT EXISTS bonks (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
"""

# Columns added after the first release; added to existing databases on startup.
MIGRATIONS = [
    ("events", "poll_question", "TEXT"),
    ("events", "poll_options", "TEXT"),  # JSON list of option labels
    ("participants", "choice", "INTEGER"),  # index into poll_options
    ("events", "signups_closed", "INTEGER NOT NULL DEFAULT 0"),
    ("events", "autoclosed", "INTEGER NOT NULL DEFAULT 0"),  # sign-ups were closed at start time
    ("lobbies", "draft_type", "TEXT NOT NULL DEFAULT 'snake'"),  # 'snake' or 'normal'
    ("player_stats", "draws", "INTEGER NOT NULL DEFAULT 0"),
    ("player_stats", "points", "INTEGER NOT NULL DEFAULT 0"),  # filled from past games by recalculate_points()
    ("player_stats", "streak", "INTEGER NOT NULL DEFAULT 0"),  # filled from past games by recalculate_points()
    ("lobbies", "map_options", "TEXT"),  # JSON list of the maps in the map vote
    ("lobbies", "map_name", "TEXT"),     # the map that won the vote
    ("lobbies", "map_vote_ends", "INTEGER"),  # unix time when the map vote ends by itself
    ("lobbies", "map_rerolled", "INTEGER NOT NULL DEFAULT 0"),  # 1 once the maps were re-rolled
]

WIN_POINTS = 50    # ranked points a player gets for a win
LOSS_POINTS = 30   # ranked points a player loses for a loss (points never go below 0)
STREAK_MIN = 2     # wins in a row needed for a win streak
STREAK_BONUS = 10  # extra points for every win while on a win streak (the STREAK_MIN-th win in a row and later)


def win_points(streak_after_win: int) -> int:
    """Points for a win, given the number of wins in a row including this one."""
    return WIN_POINTS + (STREAK_BONUS if streak_after_win >= STREAK_MIN else 0)


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
class Lobby:
    id: int
    guild_id: int
    channel_id: int
    creator_id: int
    event_id: Optional[int]
    queue_message_id: Optional[int]
    draft_message_id: Optional[int]
    status: str
    captain_a: Optional[int]
    captain_b: Optional[int]
    winner: Optional[str]
    reported_by: Optional[int]
    team_a_role: Optional[int] = None
    team_b_role: Optional[int] = None
    team_a_voice: Optional[int] = None
    team_b_voice: Optional[int] = None
    draft_type: str = "snake"
    map_options: Optional[str] = None
    map_name: Optional[str] = None
    map_vote_ends: Optional[int] = None
    map_rerolled: int = 0

    @property
    def maps(self) -> List[str]:
        """The maps in the map vote."""
        return json.loads(self.map_options) if self.map_options else []

    def captain_of(self, team: str) -> Optional[int]:
        return self.captain_a if team == "A" else self.captain_b

    def team_of_captain(self, user_id: int) -> Optional[str]:
        return "A" if user_id == self.captain_a else "B" if user_id == self.captain_b else None


@dataclass
class Tiebreaker:
    id: int
    lobby_id: int
    maps_json: str
    ban_a: Optional[int]  # index of the map captain A banned
    ban_b: Optional[int]  # index of the map captain B banned

    @property
    def maps(self) -> List[str]:
        return json.loads(self.maps_json)

    @property
    def banned(self) -> List[int]:
        return [ban for ban in (self.ban_a, self.ban_b) if ban is not None]

    @property
    def final_map(self) -> Optional[str]:
        """The map that is left once both captains banned one, else None."""
        if self.ban_a is None or self.ban_b is None:
            return None
        return next(m for i, m in enumerate(self.maps) if i not in self.banned)


@dataclass
class LobbyPlayer:
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
        added = set()
        for table, column, column_type in MIGRATIONS:
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
                added.add((table, column))
        if ("player_stats", "points") in added or ("player_stats", "streak") in added:
            self.recalculate_points()
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

    def set_organizer(self, event_id: int, user_id: int) -> None:
        self.conn.execute("UPDATE events SET organizer_id = ? WHERE id = ?", (user_id, event_id))
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

    # --- captains lobbies ---------------------------------------------------

    def create_lobby(self, guild_id: int, channel_id: int, creator_id: int, event_id: Optional[int],
                     draft_type: str = "snake") -> int:
        cur = self.conn.execute(
            "INSERT INTO lobbies (guild_id, channel_id, creator_id, event_id, draft_type) VALUES (?, ?, ?, ?, ?)",
            (guild_id, channel_id, creator_id, event_id, draft_type),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_lobby(self, lobby_id: int) -> Optional[Lobby]:
        row = self.conn.execute(
            "SELECT id, guild_id, channel_id, creator_id, event_id, queue_message_id, draft_message_id, "
            "status, captain_a, captain_b, winner, reported_by, "
            "team_a_role, team_b_role, team_a_voice, team_b_voice, draft_type, map_options, map_name, "
            "map_vote_ends, map_rerolled "
            "FROM lobbies WHERE id = ?", (lobby_id,)
        ).fetchone()
        return Lobby(**dict(row)) if row else None

    def update_lobby(self, lobby_id: int, **fields) -> None:
        """Sets the given lobby columns, e.g. update_lobby(1, status="playing", winner=None)."""
        columns = ", ".join(f"{name} = ?" for name in fields)
        self.conn.execute(f"UPDATE lobbies SET {columns} WHERE id = ?", (*fields.values(), lobby_id))
        self.conn.commit()

    def set_lobby_teams(self, lobby_id: int, team_a_role: Optional[int], team_b_role: Optional[int],
                        team_a_voice: Optional[int], team_b_voice: Optional[int]) -> None:
        self.update_lobby(lobby_id, team_a_role=team_a_role, team_b_role=team_b_role,
                          team_a_voice=team_a_voice, team_b_voice=team_b_voice)

    def latest_lobby_with_teams(self, event_id: int) -> Optional[Lobby]:
        row = self.conn.execute(
            "SELECT id FROM lobbies WHERE event_id = ? AND team_a_voice IS NOT NULL ORDER BY id DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        return self.get_lobby(row["id"]) if row else None

    def lobbies_with_teams(self, event_id: int) -> List[Lobby]:
        rows = self.conn.execute(
            "SELECT id FROM lobbies WHERE event_id = ? AND (team_a_role IS NOT NULL OR team_b_role IS NOT NULL "
            "OR team_a_voice IS NOT NULL OR team_b_voice IS NOT NULL)", (event_id,)
        ).fetchall()
        return [self.get_lobby(r["id"]) for r in rows]

    def add_lobby_player(self, lobby_id: int, user_id: int, name: str) -> bool:
        """Returns False if the user was already in the lobby."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO lobby_players (lobby_id, user_id, name) VALUES (?, ?, ?)",
            (lobby_id, user_id, name),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def remove_lobby_player(self, lobby_id: int, user_id: int) -> bool:
        """Returns False if the user wasn't in the lobby."""
        cur = self.conn.execute(
            "DELETE FROM lobby_players WHERE lobby_id = ? AND user_id = ?", (lobby_id, user_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def lobby_players(self, lobby_id: int) -> List[LobbyPlayer]:
        rows = self.conn.execute(
            "SELECT user_id, name, team, pick_no FROM lobby_players WHERE lobby_id = ? ORDER BY rowid",
            (lobby_id,),
        ).fetchall()
        return [LobbyPlayer(r["user_id"], r["name"], r["team"], r["pick_no"]) for r in rows]

    def assign_pick(self, lobby_id: int, user_id: int, team: str, pick_no: int) -> None:
        self.conn.execute(
            "UPDATE lobby_players SET team = ?, pick_no = ? WHERE lobby_id = ? AND user_id = ?",
            (team, pick_no, lobby_id, user_id),
        )
        self.conn.commit()

    def latest_captain_lobby(self, guild_id: int, user_id: int) -> Optional[Lobby]:
        """The newest drafted game (map chosen or later) in this server where the user is a captain."""
        row = self.conn.execute(
            "SELECT id FROM lobbies WHERE guild_id = ? AND (captain_a = ? OR captain_b = ?) "
            "AND status IN ('playing', 'reported', 'done') ORDER BY id DESC LIMIT 1",
            (guild_id, user_id, user_id),
        ).fetchone()
        return self.get_lobby(row["id"]) if row else None

    # --- map votes and tiebreakers ------------------------------------------

    def set_map_vote(self, lobby_id: int, user_id: int, choice: int) -> None:
        self.conn.execute(
            "INSERT INTO map_votes (lobby_id, user_id, choice) VALUES (?, ?, ?) "
            "ON CONFLICT (lobby_id, user_id) DO UPDATE SET choice = excluded.choice",
            (lobby_id, user_id, choice),
        )
        self.conn.commit()

    def clear_map_votes(self, lobby_id: int) -> None:
        self.conn.execute("DELETE FROM map_votes WHERE lobby_id = ?", (lobby_id,))
        self.conn.commit()

    def lobbies_with_status(self, status: str) -> List[int]:
        rows = self.conn.execute("SELECT id FROM lobbies WHERE status = ?", (status,)).fetchall()
        return [r["id"] for r in rows]

    def map_votes(self, lobby_id: int) -> dict:
        """user_id -> index of the map they voted for."""
        rows = self.conn.execute("SELECT user_id, choice FROM map_votes WHERE lobby_id = ?", (lobby_id,)).fetchall()
        return {r["user_id"]: r["choice"] for r in rows}

    def create_tiebreaker(self, lobby_id: int, maps: List[str]) -> int:
        cur = self.conn.execute("INSERT INTO tiebreakers (lobby_id, maps) VALUES (?, ?)",
                                (lobby_id, json.dumps(maps)))
        self.conn.commit()
        return cur.lastrowid

    def get_tiebreaker(self, tiebreaker_id: int) -> Optional[Tiebreaker]:
        row = self.conn.execute(
            "SELECT id, lobby_id, maps AS maps_json, ban_a, ban_b FROM tiebreakers WHERE id = ?", (tiebreaker_id,)
        ).fetchone()
        return Tiebreaker(**dict(row)) if row else None

    def set_tiebreaker_ban(self, tiebreaker_id: int, team: str, index: int) -> None:
        column = "ban_a" if team == "A" else "ban_b"
        self.conn.execute(f"UPDATE tiebreakers SET {column} = ? WHERE id = ?", (index, tiebreaker_id))
        self.conn.commit()

    # --- wins, losses, draws and ranked points ---------------------------------------------

    def record_result(self, guild_id: int, winners: List[int], losers: List[int],
                      draws: List[int] = ()) -> None:
        """Counts a finished game. Winners get +WIN_POINTS (+STREAK_BONUS while on a win streak),
        losers -LOSS_POINTS (never below 0) and lose their streak, draws change nothing but the draw count."""
        for user_id in [*winners, *losers, *draws]:
            self.conn.execute("INSERT OR IGNORE INTO player_stats (guild_id, user_id) VALUES (?, ?)",
                              (guild_id, user_id))
        for user_id in winners:
            # In an UPDATE, "streak" is still the old value, so the streak after this win is streak + 1.
            self.conn.execute(
                "UPDATE player_stats SET wins = wins + 1, streak = streak + 1, "
                "points = points + ? + CASE WHEN streak + 1 >= ? THEN ? ELSE 0 END "
                "WHERE guild_id = ? AND user_id = ?",
                (WIN_POINTS, STREAK_MIN, STREAK_BONUS, guild_id, user_id))
        for user_id in losers:
            self.conn.execute(
                "UPDATE player_stats SET losses = losses + 1, streak = 0, points = MAX(0, points - ?) "
                "WHERE guild_id = ? AND user_id = ?", (LOSS_POINTS, guild_id, user_id))
        for user_id in draws:
            self.conn.execute("UPDATE player_stats SET draws = draws + 1 WHERE guild_id = ? AND user_id = ?",
                              (guild_id, user_id))
        self.conn.commit()

    def recalculate_points(self) -> None:
        """Gives everyone the ranked points and win streak they should have, by replaying all approved
        games in order. Runs when the points/streak columns are added and after a game is removed.
        Players whose saved wins/losses don't match the game history (e.g. changed by hand) get
        max(0, wins * WIN_POINTS - losses * LOSS_POINTS) and no streak."""
        points, streak, wins, losses = {}, {}, {}, {}
        lobbies = self.conn.execute(
            "SELECT id, guild_id, winner FROM lobbies WHERE status = 'done' AND winner IN ('A', 'B') ORDER BY id"
        ).fetchall()
        for lobby in lobbies:
            for player in self.conn.execute("SELECT user_id, team FROM lobby_players WHERE lobby_id = ? "
                                            "AND team IS NOT NULL", (lobby["id"],)):
                key = (lobby["guild_id"], player["user_id"])
                if player["team"] == lobby["winner"]:
                    streak[key] = streak.get(key, 0) + 1
                    points[key] = points.get(key, 0) + win_points(streak[key])
                    wins[key] = wins.get(key, 0) + 1
                else:
                    streak[key] = 0
                    points[key] = max(0, points.get(key, 0) - LOSS_POINTS)
                    losses[key] = losses.get(key, 0) + 1
        for row in self.conn.execute("SELECT guild_id, user_id, wins, losses FROM player_stats").fetchall():
            key = (row["guild_id"], row["user_id"])
            if (wins.get(key, 0), losses.get(key, 0)) == (row["wins"], row["losses"]):
                values = (points.get(key, 0), streak.get(key, 0))
            else:
                values = (max(0, row["wins"] * WIN_POINTS - row["losses"] * LOSS_POINTS), 0)
            self.conn.execute("UPDATE player_stats SET points = ?, streak = ? WHERE guild_id = ? AND user_id = ?",
                              (*values, *key))

    def remove_game(self, lobby_id: int) -> List[tuple]:
        """Undoes a finished game: winners lose 1 win, losers 1 loss (or everyone 1 draw), the game gets
        status 'removed' and all points and streaks are recalculated. Returns (name, 'win'/'loss'/'draw')
        per player. Returns [] if the game is not finished (e.g. already removed)."""
        lobby = self.get_lobby(lobby_id)
        if lobby is None or lobby.status != "done":
            return []
        changes = []
        for player in self.lobby_players(lobby_id):
            if player.team is None:
                continue
            if lobby.winner not in ("A", "B"):
                column, label = "draws", "draw"
            elif player.team == lobby.winner:
                column, label = "wins", "win"
            else:
                column, label = "losses", "loss"
            self.conn.execute(f"UPDATE player_stats SET {column} = MAX(0, {column} - 1) "
                              "WHERE guild_id = ? AND user_id = ?", (lobby.guild_id, player.user_id))
            changes.append((player.name, label))
        self.conn.execute("UPDATE lobbies SET status = 'removed' WHERE id = ?", (lobby_id,))
        self.recalculate_points()
        self.conn.commit()
        return changes

    def finished_games(self, guild_id: int, limit: Optional[int] = None) -> List[tuple]:
        """Approved games, newest first: (lobby_id, winner, {user_id: team}). winner is 'A', 'B' or 'draw'."""
        query = "SELECT id, winner FROM lobbies WHERE guild_id = ? AND status = 'done' ORDER BY id DESC"
        if limit:
            query += f" LIMIT {int(limit)}"
        games = []
        for lobby in self.conn.execute(query, (guild_id,)).fetchall():
            teams = {r["user_id"]: r["team"] for r in self.conn.execute(
                "SELECT user_id, team FROM lobby_players WHERE lobby_id = ? AND team IS NOT NULL", (lobby["id"],))}
            games.append((lobby["id"], lobby["winner"], teams))
        return games

    def player_names(self, guild_id: int) -> dict:
        """user_id -> the name they used in their latest captains game."""
        rows = self.conn.execute(
            "SELECT p.user_id, p.name FROM lobby_players p JOIN lobbies l ON l.id = p.lobby_id "
            "WHERE l.guild_id = ? ORDER BY l.id", (guild_id,)).fetchall()
        return {r["user_id"]: r["name"] for r in rows}

    def player_stats(self, guild_id: int, user_id: int) -> tuple:
        """(points, wins, losses, draws, streak) of the user in this server."""
        row = self.conn.execute(
            "SELECT points, wins, losses, draws, streak FROM player_stats WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id)
        ).fetchone()
        return (row["points"], row["wins"], row["losses"], row["draws"], row["streak"]) if row else (0, 0, 0, 0, 0)

    def stats_leaderboard(self, guild_id: int, limit: int, offset: int = 0) -> List[tuple]:
        """(user_id, points, wins, losses, draws, streak) for everyone who played, most ranked points first
        (most wins, then fewest losses on a tie)."""
        rows = self.conn.execute(
            "SELECT user_id, points, wins, losses, draws, streak FROM player_stats WHERE guild_id = ? "
            "AND wins + losses + draws > 0 ORDER BY points DESC, wins DESC, losses, user_id LIMIT ? OFFSET ?",
            (guild_id, limit, offset),
        ).fetchall()
        return [(r["user_id"], r["points"], r["wins"], r["losses"], r["draws"], r["streak"]) for r in rows]

    def stats_player_count(self, guild_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM player_stats WHERE guild_id = ? AND wins + losses + draws > 0", (guild_id,)
        ).fetchone()
        return row["n"]

    # --- bonks --------------------------------------------------------------

    def add_bonk(self, guild_id: int, user_id: int) -> int:
        """Adds a bonk and returns the user's new total."""
        self.conn.execute(
            "INSERT INTO bonks (guild_id, user_id, count) VALUES (?, ?, 1) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET count = count + 1",
            (guild_id, user_id),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT count FROM bonks WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()
        return row["count"]

    def bonk_leaderboard(self, guild_id: int, limit: int, offset: int = 0) -> List[tuple]:
        """(user_id, count) for the most bonked users in the server, most bonks first."""
        rows = self.conn.execute(
            "SELECT user_id, count FROM bonks WHERE guild_id = ? AND count > 0 "
            "ORDER BY count DESC, user_id LIMIT ? OFFSET ?",
            (guild_id, limit, offset),
        ).fetchall()
        return [(r["user_id"], r["count"]) for r in rows]

    def bonked_user_count(self, guild_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM bonks WHERE guild_id = ? AND count > 0", (guild_id,)
        ).fetchone()
        return row["n"]

    # --- participants -------------------------------------------------------

    def participants(self, event_id: int) -> List[Participant]:
        rows = self.conn.execute(
            "SELECT user_id, choice FROM participants WHERE event_id = ? ORDER BY joined_ts, rowid",
            (event_id,),
        ).fetchall()
        return [Participant(r["user_id"], r["choice"]) for r in rows]
