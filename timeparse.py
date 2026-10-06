from datetime import datetime, timedelta, tzinfo

FULL_FORMATS = ["%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M"]
NO_YEAR_FORMATS = ["%d-%m %H:%M", "%d/%m %H:%M"]

HELP = (
    "I couldn't understand that time. Try one of these:\n"
    "• `20:00` (today, or tomorrow if that time has passed)\n"
    "• `tomorrow 20:00`\n"
    "• `10-10 20:00` (day-month)\n"
    "• `10-10-2026 20:00` or `2026-10-10 20:00`"
)


def parse_time(text: str, tz: tzinfo) -> datetime:
    """Parses a user-typed start time into a timezone-aware datetime in the future."""
    text = " ".join(text.strip().lower().split())
    now = datetime.now(tz)
    result = None

    for fmt in FULL_FORMATS:
        try:
            result = datetime.strptime(text, fmt).replace(tzinfo=tz)
            break
        except ValueError:
            pass

    if result is None:
        for fmt in NO_YEAR_FORMATS:
            try:
                parsed = datetime.strptime(f"{now.year} {text}", f"%Y {fmt}")
            except ValueError:
                continue
            result = parsed.replace(tzinfo=tz)
            if result < now:
                result = result.replace(year=now.year + 1)
            break

    if result is None:
        day_offset = None
        clock = text
        for prefix, offset in (("today ", 0), ("tomorrow ", 1)):
            if text.startswith(prefix):
                day_offset, clock = offset, text[len(prefix):]
        try:
            hm = datetime.strptime(clock, "%H:%M")
        except ValueError:
            raise ValueError(HELP)
        result = now.replace(hour=hm.hour, minute=hm.minute, second=0, microsecond=0)
        if day_offset is not None:
            result += timedelta(days=day_offset)
        elif result < now:
            result += timedelta(days=1)

    if result < now - timedelta(minutes=1):
        raise ValueError("That time is in the past.")
    return result
