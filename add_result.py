"""Gives a captains game its result by hand, e.g. when the game got stuck and nobody could report it.

    python add_result.py 39          -> shows game 39: the teams and its status
    python add_result.py 39 red      -> red team (captain A) wins, blue team loses
    python add_result.py 39 blue     -> blue team (captain B) wins, red team loses
    python add_result.py 39 draw     -> everyone gets a draw

Works the same as a captain approving the result: the game becomes 'done' and wins, losses,
draws, ranked points and streaks are saved. To undo it: python remove_game.py 39
"""
import os
import sys

from database import Database

db = Database(os.getenv("DATABASE_PATH", "events.db"))

if len(sys.argv) < 2:
    sys.exit(__doc__)

lobby = db.get_lobby(int(sys.argv[1]))
if lobby is None:
    sys.exit(f"No game with number {sys.argv[1]}.")

players = [p for p in db.lobby_players(lobby.id) if p.team]
red = [p for p in players if p.team == "A"]
blue = [p for p in players if p.team == "B"]
print(f"Game {lobby.id} (status: {lobby.status})")
print("  red  (A): " + ", ".join(p.name for p in red))
print("  blue (B): " + ", ".join(p.name for p in blue))

if len(sys.argv) < 3:
    sys.exit("\nTo give the result: python add_result.py <number> red|blue|draw")

if lobby.status in ("done", "removed", "cancelled"):
    sys.exit(f"Game {lobby.id} is already '{lobby.status}'. Nothing changed.")
if not red or not blue:
    sys.exit(f"Game {lobby.id} has no full teams yet. Nothing changed.")

choice = sys.argv[2].lower()
if choice in ("red", "a"):
    winner, winners, losers = "A", red, blue
elif choice in ("blue", "b"):
    winner, winners, losers = "B", blue, red
elif choice == "draw":
    winner, winners, losers = "draw", [], []
else:
    sys.exit("Use red, blue or draw.")

db.update_lobby(lobby.id, status="done", winner=winner)
if winner == "draw":
    db.record_result(lobby.guild_id, [], [], draws=[p.user_id for p in players])
    print(f"\nDone. Game {lobby.id} is a draw: everyone +1 draw.")
else:
    db.record_result(lobby.guild_id, [p.user_id for p in winners], [p.user_id for p in losers])
    print(f"\nDone. Game {lobby.id}:")
    for p in winners:
        print(f"  {p.name}: +1 win")
    for p in losers:
        print(f"  {p.name}: +1 loss")
