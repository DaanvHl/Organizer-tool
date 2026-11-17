"""Removes a wrongly reported captains game from the stats. Referees can also use /game remove in Discord.

    python remove_game.py        -> shows the last 15 finished games with their numbers
    python remove_game.py 12     -> removes game 12: winners lose 1 win, losers lose 1 loss
                                    (or everyone loses 1 draw), and ranked points are recalculated
"""
import os
import sys

from database import Database

db = Database(os.getenv("DATABASE_PATH", "events.db"))

if len(sys.argv) < 2:
    for guild_id in [r[0] for r in db.conn.execute("SELECT DISTINCT guild_id FROM lobbies")]:
        names = db.player_names(guild_id)
        for lobby_id, winner, teams in db.finished_games(guild_id, limit=15):
            lobby = db.get_lobby(lobby_id)
            red, blue = names.get(lobby.captain_a, "?"), names.get(lobby.captain_b, "?")
            result = {"A": f"red ({red}) won", "B": f"blue ({blue}) won"}.get(winner, "draw")
            print(f"Game {lobby_id}: red = {red} vs blue = {blue} -> {result}")
    print("\nTo remove one: python remove_game.py <number>")
    sys.exit()

changes = db.remove_game(int(sys.argv[1]))
if not changes:
    sys.exit(f"No finished game with number {sys.argv[1]}. Nothing changed.")
for name, label in changes:
    print(f"  {name}: -1 {label}")
print(f"Done. Game {sys.argv[1]} removed and ranked points recalculated.")
