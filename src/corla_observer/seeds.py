"""Seed entry by hand: one coordinator's entry takes effect; a second entry by another coordinator is optional.

The random seed is announced at the public dice roll as 20 digits. A
coordinator enters it with a note of where it came from (`enter_seed`), and
it takes effect at once: the data map uses the latest entry
(`corla_observer.datamap.build_map`). Every later entry, by the same or a
different coordinator, is a re-entry that takes effect in turn; one that
changes the value is recorded as a change and raises a high-severity flag
(`corla_observer.flags`), since the seed should never change once rolled.

A second entry is optional: another coordinator may enter the seed again,
independently (`confirm_seed`), and it is compared with the entry in effect.
A mismatch raises a high-severity flag, and the data map reports a problem
until a new entry resolves it. The same coordinator entering it this way is
recorded as a re-entry, not a second entry. The roll time is taken from the
first entry (`corla_observer.datamap`), since that is when the seed became
known.

Once the seed appears on the audit center (a seed file, or the State Audit
Report's "Audit Random Seed"), the data map compares it with the seed in
effect, and `corla_observer.flags` raises a high-severity flag on any
disagreement.

Events (``data`` always JSON; every event carries ``rehearsal: true`` when
entered by a rehearsal's synthetic coordinators):

``seed.entered``
    An entry, in effect from now on: ``seed``, ``source`` (free text: where
    it was announced or read), ``entry`` (``"first"`` or ``"re-entry"``),
    ``effective`` (``true``); a re-entry also has ``previous_seq`` (the
    entry it replaces), ``previous_seed`` and ``changed`` (whether the value
    differs).
``seed.second_entry``
    An optional second entry by another coordinator, compared with the entry
    in effect: ``seed``, ``source``, ``entered_seq`` and ``entered_by`` (the
    entry in effect), ``match`` (``true`` or ``false``) and, on a mismatch,
    ``entered_seed``.

Standard library only.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from corla_observer.events import Event, EventLog, JSONValue, check_actor
from corla_observer.localtime import colorado_time

#: Digits in a CORLA seed.
SEED_DIGITS = 20
MAX_SOURCE = 500
SEED_EVENT_KINDS = frozenset({"seed.entered", "seed.second_entry"})


class SeedError(ValueError):
    """A seed entry is refused: not 20 digits, or a second entry with no seed entered."""


def check_seed(seed: str) -> str:
    """The seed with spaces removed, if it is exactly `SEED_DIGITS` digits."""
    value = "".join(seed.split())
    if not (value.isascii() and value.isdigit() and len(value) == SEED_DIGITS):
        raise SeedError(f"a seed is {SEED_DIGITS} digits, got {seed.strip()[:40]!r}")
    return value


@dataclass(frozen=True)
class SeedEntry:
    seq: int
    actor: str
    time: str
    seed: str
    source: str
    rehearsal: bool = False


@dataclass(frozen=True)
class SeedOutcome:
    """A later entry compared with the entry in effect before it: ``matched`` or not.

    ``first`` is the entry in effect, ``second`` the new entry and ``seq``
    its event's seq. ``reentry`` marks the same coordinator entering the
    seed again, which is recorded as a new entry (``seed.entered``), in
    effect, rather than as a second entry.
    """

    matched: bool
    first: SeedEntry
    second: SeedEntry
    seq: int
    reentry: bool = False


@dataclass(frozen=True)
class SeedChange:
    """A re-entry whose value differs from the entry it replaced."""

    previous: SeedEntry
    entry: SeedEntry


@dataclass
class SeedState:
    """Where seed entry stands, from the event log."""

    #: The entry in effect: the latest ``seed.entered``.
    current: SeedEntry | None = None
    #: Every entry, in order.
    entries: list[SeedEntry] = field(default_factory=list)
    #: Every second entry compared with the entry then in effect, in order.
    outcomes: list[SeedOutcome] = field(default_factory=list)
    #: Every re-entry that changed the value, in order.
    changes: list[SeedChange] = field(default_factory=list)

    @property
    def seed(self) -> str | None:
        return self.current.seed if self.current else None

    @property
    def mismatches(self) -> list[SeedOutcome]:
        return [o for o in self.outcomes if not o.matched]

    @property
    def current_outcomes(self) -> list[SeedOutcome]:
        """The second entries compared with the entry in effect."""
        return [o for o in self.outcomes if self.current is not None and o.first.seq == self.current.seq]

    @property
    def confirmation(self) -> SeedOutcome | None:
        """The latest matching second entry of the entry in effect, if any."""
        return next((o for o in reversed(self.current_outcomes) if o.matched), None)

    @property
    def unresolved_mismatch(self) -> SeedOutcome | None:
        """The latest second entry that did not match the entry in effect, until a new entry is made."""
        return next((o for o in reversed(self.current_outcomes) if not o.matched), None)


def _entry(event: Event) -> SeedEntry | None:
    seed = event.data.get("seed")
    if not isinstance(seed, str):
        return None
    source = event.data.get("source")
    return SeedEntry(
        event.seq, event.actor, event.server_time, seed, source if isinstance(source, str) else "", bool(event.data.get("rehearsal"))
    )


def seed_state(events: Iterable[Event]) -> SeedState:
    """The seed entries, second entries and changes recorded in ``events``."""
    state = SeedState()
    by_seq: dict[int, SeedEntry] = {}
    for event in events:
        if event.kind == "seed.entered":
            entry = _entry(event)
            if entry is None:
                continue
            if state.current is not None and state.current.seed != entry.seed:
                state.changes.append(SeedChange(state.current, entry))
            by_seq[entry.seq] = entry
            state.entries.append(entry)
            state.current = entry
        elif event.kind == "seed.second_entry":
            second = _entry(event)
            entered_seq = event.data.get("entered_seq")
            first = by_seq.get(entered_seq) if isinstance(entered_seq, int) else None
            if second is not None and first is not None:
                state.outcomes.append(SeedOutcome(second.seed == first.seed, first, second, second.seq))
    return state


def enter_seed(log: EventLog, seed: str, actor: str, source: str, *, rehearsal: bool = False) -> int:
    """Record an entry of the seed (``seed.entered``), in effect at once; return its seq.

    An entry after an earlier one is a re-entry, recorded with the entry it
    replaces and whether the value changed.

    Raises:
        SeedError: if the seed is not 20 digits.
        ValueError: for an invalid actor.
    """
    check_actor(actor)
    value = check_seed(seed)
    previous = seed_state(log.iter_events()).current
    data: dict[str, JSONValue] = {
        "seed": value,
        "source": source.strip()[:MAX_SOURCE],
        "entry": "first" if previous is None else "re-entry",
        "effective": True,
    }
    if previous is not None:
        data |= {"previous_seq": previous.seq, "previous_seed": previous.seed, "changed": previous.seed != value}
    if rehearsal:
        data["rehearsal"] = True
    return log.append("seed.entered", actor, data=data).seq


def confirm_seed(log: EventLog, seed: str, actor: str, source: str = "", *, rehearsal: bool = False) -> SeedOutcome:
    """Record an optional second entry of the seed and compare it with the entry in effect.

    Records ``seed.second_entry`` with ``match`` true or false. If ``actor``
    made the entry in effect, the entry is recorded as a re-entry
    (`enter_seed`) instead, and the outcome has ``reentry`` set.

    Raises:
        SeedError: if the seed is not 20 digits, or no seed has been entered.
        ValueError: for an invalid actor.
    """
    check_actor(actor)
    value = check_seed(seed)
    current = seed_state(log.iter_events()).current
    if current is None:
        raise SeedError("no seed has been entered: a coordinator enters it first with `seed set`")
    clean_source = source.strip()[:MAX_SOURCE]
    if current.actor == actor:
        seq = enter_seed(log, value, actor, clean_source, rehearsal=rehearsal)
        entry = next(e for e in seed_state(log.iter_events()).entries if e.seq == seq)
        return SeedOutcome(value == current.seed, current, entry, seq, reentry=True)
    data: dict[str, JSONValue] = {
        "seed": value,
        "source": clean_source,
        "entered_seq": current.seq,
        "entered_by": current.actor,
        "match": value == current.seed,
    }
    if value != current.seed:
        data["entered_seed"] = current.seed
    if rehearsal:
        data["rehearsal"] = True
    event = log.append("seed.second_entry", actor, data=data)
    second = SeedEntry(event.seq, actor, event.server_time, value, clean_source, rehearsal)
    return SeedOutcome(value == current.seed, current, second, event.seq)


def enter_and_confirm(
    log: EventLog, seed: str, actor: str, source: str, *, second_actor: str | None = None, rehearsal: bool = False
) -> SeedOutcome:
    """An entry and a second entry at once, by ``actor`` and ``second_actor`` (``actor`` with ``-second`` appended by default).

    For tests only.
    """
    second = second_actor or f"{actor}-second"
    enter_seed(log, seed, actor, source, rehearsal=rehearsal)
    return confirm_seed(log, seed, second, source, rehearsal=rehearsal)


def describe_state(state: SeedState) -> list[str]:
    """Lines describing the seed's state, for the command line."""
    lines: list[str] = []
    c = state.current
    if c is None:
        return ["no seed entered"]
    lines.append(
        f"seed in effect {c.seed}: entered by {c.actor} at {colorado_time(c.time)} (event {c.seq}){' [rehearsal]' if c.rehearsal else ''}"
    )
    if c.source:
        lines.append(f"  source: {c.source}")
    confirmation = state.confirmation
    if confirmation is not None:
        lines.append(f"  second entry by {confirmation.second.actor} (event {confirmation.seq}) matches")
    else:
        lines.append("  no matching second entry (optional)")
    for o in state.mismatches:
        resolved = "" if o.first.seq == c.seq else " (resolved by a later entry)"
        lines.append(f"MISMATCH (event {o.seq}): {o.first.actor} entered {o.first.seed}, {o.second.actor} entered {o.second.seed}{resolved}")
    for change in state.changes:
        lines.append(
            f"CHANGED (event {change.entry.seq}): {change.entry.actor} entered {change.entry.seed}, replacing "
            f"{change.previous.seed} entered by {change.previous.actor} (event {change.previous.seq})"
        )
    return lines
