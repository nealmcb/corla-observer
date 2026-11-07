"""The post proposed before the seed is rolled (`corla_observer.post`)."""

from __future__ import annotations

from datetime import UTC, datetime

from corla_observer.post import X_LIMIT, propose_post, roll_phrase, x_length

HASH = "c" * 64
# 2026-11-16 10:00 MST, the 2026 general's roll.
ROLL = "2026-11-16T17:00:00.000Z"


def test_links_count_as_23_characters() -> None:
    assert x_length("see https://example.com/a/very/long/path/indeed.html now") == len(
        "see "
    ) + 23 + len(" now")


def test_the_roll_time_in_colorado_words() -> None:
    assert roll_phrase(ROLL, datetime(2026, 11, 16, 15, 0, tzinfo=UTC)) == "at 10 AM"
    assert (
        roll_phrase(ROLL, datetime(2026, 11, 15, 15, 0, tzinfo=UTC))
        == "Monday at 10 AM"
    )
    assert (
        roll_phrase("2026-11-16T17:30:00Z", datetime(2026, 11, 16, 15, 0, tzinfo=UTC))
        == "at 10:30 AM"
    )
    assert roll_phrase(None, datetime(2026, 11, 16, tzinfo=UTC)) == "soon"


def test_the_post_gives_the_hash_and_fits() -> None:
    post = propose_post(HASH, ROLL, datetime(2026, 11, 16, 15, 0, tzinfo=UTC))
    lines = post.text.splitlines()
    assert lines[0] == (
        "Watch Colorado's risk-limiting audits! Seed dice roll at 10 AM: "
        "https://www.coloradosos.gov/pubs/elections/auditCenter.html"
    )
    assert lines[1] == f"Secure timestamps of data to be audited via SHA-256: {HASH}"
    assert lines[2:] == [
        "https://github.com/nealmcb/auditcenter/blob/main/observerfiles/evidence-timestamping.md",
        "@COSecofState",
        "#RiskLimitingAudits",
    ]
    assert post.length == x_length(post.text) and not post.too_long
    # The longest roll phrase still fits.
    assert (
        not propose_post(
            HASH, "2026-11-18T17:30:00Z", datetime(2026, 11, 16, tzinfo=UTC)
        ).length
        > X_LIMIT
    )
