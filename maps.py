# Maps that can be played per format. The format comes from the number of players:
# 10 players in a captains game = 5v5. Edit these lists to add or remove maps.
MAPS = {
    "2v2": [
        "Boombox", "Cross", "Duality", "Garder", "Sandal",
        "Sandbox", "Station", "Valley", "Zone", "Magadan",
        "Short Bridge",
    ],
    "3v3": [
        "Valley", "Zone", "Garder", "Sandal", "Station",
        "Cross", "Pass", "Atra", "Sandbox", "Small Bridge",
    ],
    "4v4": [
        "Sandbox", "Sandal", "Pass", "Farm", "Boombox", "Magadan",
        "Valley", "Short Bridge",
    ],
    "5v5": [
        "Siege", "Gubakha", "Pass", "Iran", "Sandal",
        "Red Alert", "Polygon", "Molotov", "Station",
    ],
    "6v6": [
        "Red Alert", "Kungur", "Barda", "Gubakha", "Parma", "Solikamsk",
        "Forest", "Tribute", "Siege", "Future", "Cologne",
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

# Fair maps with no side advantage, only used for /tiebreaker. Formats that are not here
# (2v2, 7v7 and bigger) use the normal MAPS list above.
TIEBREAKER_MAPS = {
    "3v3": ["Farm", "Valley", "Garder", "Sandbox", "Duality"],
    "4v4": ["Farm", "Valley", "Pass", "Garder", "Sandbox", "Duality"],
    "5v5": ["Valley", "Pass", "Garder", "Cross", "Zone"],
    "6v6": ["Garder", "Cross", "Zone"],
}

MAP_VOTE_OPTIONS = 5    # maps in the vote after the draft
MAP_VOTE_SECONDS = 60   # the map vote ends by itself after this time
TIEBREAKER_OPTIONS = 3  # maps in /tiebreaker; each captain bans 1


def game_format(player_count: int) -> str:
    """'5v5' for 10 players."""
    return f"{player_count // 2}v{player_count // 2}"


def maps_for(player_count: int) -> list:
    """The maps for this number of players, or [] when there is no list for the format."""
    return list(MAPS.get(game_format(player_count), []))


def tiebreaker_maps_for(player_count: int) -> list:
    """The fair tiebreaker maps for this number of players, or the normal maps when there is no fair list."""
    fmt = game_format(player_count)
    return list(TIEBREAKER_MAPS[fmt]) if fmt in TIEBREAKER_MAPS else maps_for(player_count)
