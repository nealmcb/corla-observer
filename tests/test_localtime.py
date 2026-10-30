"""Fast tests for corla_observer.localtime: Colorado time with its zone, across daylight-saving changes."""

from __future__ import annotations

from datetime import UTC, datetime

from corla_observer.localtime import colorado_offset_utc, colorado_time, colorado_wall_time


def test_colorado_time_from_stored_utc() -> None:
    assert colorado_time("2026-07-13T16:10:00.000Z") == "2026-07-13 10:10:00 MDT"
    assert colorado_time("2026-11-16T17:00:00.000Z") == "2026-11-16 10:00:00 MST"
    assert colorado_time("2026-11-16T17:00:00Z", seconds=False) == "2026-11-16 10:00 MST"
    assert colorado_time(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)) == "2026-01-01 20:04:05 MST"
    assert colorado_time(None) == "" and colorado_time("") == ""
    assert colorado_time("not a time") == "not a time"


def test_daylight_saving_changes() -> None:
    # 2026: daylight time from 2026-03-08 09:00 UTC to 2026-11-01 08:00 UTC.
    assert colorado_time("2026-03-08T08:59:59.000Z") == "2026-03-08 01:59:59 MST"
    assert colorado_time("2026-03-08T09:00:00.000Z") == "2026-03-08 03:00:00 MDT"
    assert colorado_time("2026-11-01T07:59:59.000Z") == "2026-11-01 01:59:59 MDT"
    assert colorado_time("2026-11-01T08:00:00.000Z") == "2026-11-01 01:00:00 MST"
    assert colorado_offset_utc(datetime(2027, 3, 14, 9, 0)).tzname(None) == "MDT"
    assert colorado_offset_utc(datetime(2027, 3, 14, 8, 59)).tzname(None) == "MST"


def test_colorado_wall_time() -> None:
    assert colorado_wall_time("2026-07-13 11:46:37.543509") == "2026-07-13 11:46:37 MDT"
    assert colorado_wall_time("2026-11-16 09:59:00") == "2026-11-16 09:59:00 MST"
    assert colorado_wall_time("2026-07-13T16:10:00Z") == "2026-07-13 10:10:00 MDT"
    assert colorado_wall_time("yesterday") == "yesterday"
