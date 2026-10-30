"""Colorado's local time (America/Denver), computed with the standard library only.

Event times are stored in UTC (docs/EVENT_LOG.md); everything shown to a
person, on the web pages, on the command line and in printouts, is Colorado
time with its zone abbreviation, ``MDT`` or ``MST``, by `colorado_time`.

Colorado follows the United States rules in force since 2007: Mountain
Daylight Time (UTC-6) from 2:00 MST on the second Sunday of March (09:00 UTC)
to 2:00 MDT on the first Sunday of November (08:00 UTC), Mountain Standard
Time (UTC-7) otherwise. `colorado_offset` reads a wall-clock time (the hour
repeated when daylight time ends is taken as MDT); `colorado_offset_utc`
judges an instant, which is never ambiguous.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone

#: Colorado's offsets from UTC: Mountain Standard and Mountain Daylight Time.
MST = timezone(timedelta(hours=-7), "MST")
MDT = timezone(timedelta(hours=-6), "MDT")


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def colorado_offset(local: datetime) -> timezone:
    """The UTC offset of a Colorado wall-clock time: MDT from 2:00 on the second Sunday of March
    to 2:00 on the first Sunday of November (the US rules in force since 2007), MST otherwise.

    The repeated hour when daylight time ends is taken as MDT.
    """
    naive = local.replace(tzinfo=None)
    start = datetime.combine(_nth_sunday(naive.year, 3, 2), time(2))
    end = datetime.combine(_nth_sunday(naive.year, 11, 1), time(2))
    return MDT if start <= naive < end else MST


def colorado_offset_utc(moment: datetime) -> timezone:
    """Colorado's offset at an instant (a naive ``moment`` is taken as UTC)."""
    utc = (moment if moment.tzinfo is None else moment.astimezone(UTC)).replace(tzinfo=None)
    start = datetime.combine(_nth_sunday(utc.year, 3, 2), time(9))  # 2:00 MST
    end = datetime.combine(_nth_sunday(utc.year, 11, 1), time(8))  # 2:00 MDT
    return MDT if start <= utc < end else MST


def to_colorado(moment: datetime) -> datetime:
    """An instant as Colorado wall-clock time, with the MDT or MST offset (a naive ``moment`` is UTC)."""
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(colorado_offset_utc(aware))


def parse_instant(text: str) -> datetime | None:
    """A stored time (``2026-07-13T16:10:00.000Z``, or any ISO 8601 with an offset) as an aware datetime.

    A time without an offset is taken as UTC. None if the text is not a time.
    """
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def colorado_wall_time(text: str) -> str:
    """A Colorado wall-clock time as written without an offset (CORLA's upload status, ``2026-07-13 11:46:37.5``),
    shown with its zone (``2026-07-13 11:46:37 MDT``); text that is not a time unchanged.

    A time with an offset is converted to Colorado time.
    """
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError:
        return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=colorado_offset(moment))
    return colorado_time(moment)


def colorado_time(value: str | datetime | None, *, seconds: bool = True) -> str:
    """A time as shown to people: Colorado time with its zone, such as ``2026-07-13 10:10:00 MDT``.

    ``value`` is a UTC time as stored (``2026-07-13T16:10:00.000Z``), any ISO
    8601 time with an offset, or a datetime (naive ones are UTC). ``""`` for
    None or empty; text that is not a time is returned unchanged.
    """
    if value is None or value == "":
        return ""
    moment = value if isinstance(value, datetime) else parse_instant(value)
    if moment is None:
        return str(value)
    local = to_colorado(moment)
    return local.strftime("%Y-%m-%d %H:%M:%S %Z" if seconds else "%Y-%m-%d %H:%M %Z")
