# Maps that can be played per format. The format comes from the number of players:
# 10 players in a captains game = 5v5. Edit these lists to add or remove maps.
MAPS = {
    "2v2": [
        "Boombox", "Cross", "Duality", "Garder", "Sandal",
        "Sandbox", "Station", "Valley", "Zone", "Magadan",
        "Short Bridge",
    ],
    "3v3": [
        "Boombox", "Cross", "Duality", "Garder", "Sandal",
        "Sandbox", "Station", "Valley", "Zone", "Magadan",
        "Short Bridge",
    ],
    "4v4": [
        "Sandbox", "Sandal", "Pass", "Farm", "Boombox", "Magadan",
        "Valley", "Short Bridge",
    ],
    "5v5": [
        "Cross", "Fort Knox", "Garder", "Sandal",
        "Station", "Valley", "Zone", "Pass",
    ],
    "6v6": [
        "Sandal", "Garder", "Zone", "Fort Knox", "Iran",
        "Cross", "Forest", "Red Alert", "Courage",
    ],
    "7v7": [
        "Station", "Red Alert", "Fort Knox",
        "Future", "Noise", "Iran", "Siege", "Forest", "Courage",
    ],
    "8v8": [
        "Noise", "Station", "Molotov", "Red Alert", "Forest",
        "Future", "Polygon", "Iran", "Courage",
    ],
    "9v9": [
        "Silence", "Red Alert", "Future", "Iran",
        "Molotov", "Industrial Zone",
    ],
    "10v10": [
        "Silence", "Red Alert", "Future", "Iran",
        "Bobruisk", "Molotov", "Industrial Zone",
    ],
}

MAP_VOTE_OPTIONS = 5    # maps in the vote after the draft
TIEBREAKER_OPTIONS = 3  # maps in /tiebreaker; each captain bans 1


def game_format(player_count: int) -> str:
    """'5v5' for 10 players."""
    return f"{player_count // 2}v{player_count // 2}"


def maps_for(player_count: int) -> list:
    """The maps for this number of players, or [] when there is no list for the format."""
    return list(MAPS.get(game_format(player_count), []))
