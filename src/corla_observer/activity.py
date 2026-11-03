"""The latest events of the log in plain lines, for the coordinators' Activity page (``GET /coord/activity``).

A coordinator watching a fetch, a sprint, uploads or observers at work wants to see what is
happening now, without a shell on the server. Every action the tool takes is an event
(`corla_observer.events`), so the page lists the latest events, newest first: the time in
Colorado, the kind, who did it, the county and board, and a one-line summary of the event's
data (`summary`): its plain values (text, numbers, true or false) as ``key value``, lists by
their length, nested objects left out, and signatures, payloads and hashes shortened or left
out, since the full event is in the published log. ``group`` puts each kind in a family
(fetch, schedule, upload, observation, ...) for filtering.

Standard library only.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from corla_observer.events import Event

#: Longest summary shown.
SUMMARY_CHARS = 220
#: Data keys never summarized: large, or signatures and payloads the log already holds.
SKIPPED = frozenset({"payload", "signature", "public_key", "inputs", "pdfs", "pages", "software", "details", "log"})


@dataclass(frozen=True)
class ActivityLine:
    """One event as the page shows it."""

    seq: int
    time: str
    kind: str
    group: str
    actor: str
    where: str
    summary: str


def group(kind: str) -> str:
    """The family of an event kind: the part before the first dot (``fetch``, ``schedule``, ``observation``, ...)."""
    return kind.split(".", 1)[0]


def _value(key: str, value: Any) -> str | None:
    if isinstance(value, bool):
        return key if value else None
    if isinstance(value, int | float):
        return f"{key} {value}"
    if isinstance(value, str):
        if not value:
            return None
        if key.endswith("sha256") or key in ("sha256", "hash"):
            return f"{key} {value[:12]}"
        return f"{key} {value[:80]}"
    if isinstance(value, list):
        return f"{key} ({len(value)})" if value else None
    return None


def summary(event: Event) -> str:
    """The event's data in one line (see the module documentation)."""
    parts = [p for k, v in event.data.items() if k not in SKIPPED for p in [_value(k, v)] if p]
    if event.file_sha256 and event.file_sha256 not in event.data.values():
        parts.append(f"file {event.file_sha256[:12]}")
    text = "; ".join(parts)
    return text if len(text) <= SUMMARY_CHARS else text[: SUMMARY_CHARS - 1] + "…"


def latest(events: Iterable[Event], *, limit: int = 200, groups: Sequence[str] = ()) -> list[ActivityLine]:
    """The latest ``limit`` events (of the ``groups`` given, if any), newest first."""
    kept = [e for e in events if not groups or group(e.kind) in groups]
    lines = []
    for e in reversed(kept[-limit:]):
        where = " ".join(x for x in (e.county or "", f"board {e.board}" if e.board else "") if x)
        lines.append(ActivityLine(e.seq, e.server_time, e.kind, group(e.kind), e.actor, where, summary(e)))
    return lines
