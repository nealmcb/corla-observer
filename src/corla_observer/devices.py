"""One observer on several devices: each device's view of each ballot, and where they disagree.

An observer may open their link on more than one device (a phone and a laptop, or the same phone
after clearing its data): each device signs its own entries, and the server keeps them all. The
observer's record of a ballot (``data.json``'s ``entries``) is the latest entry from any device,
so a device that entered a ballot differently, earlier or later, would otherwise be outvoted
without a word. `disagreements` finds those ballots, for the observer page to warn about.

A device's view of a ballot is its latest two-tap entry (`practice.SETTLING_KINDS`: matched,
different with a note, or skipped as another board's), or its contest-by-contest record
(`contests.fold` of its own events) once done: matched if every contest was confirmed, else
different, with what was heard. Two devices disagree on a ballot when their verdicts differ, or
when both have a final outcome for the same contest and the outcomes differ. Devices that agree,
and ballots only one device entered, are not reported. Nothing is chosen between disagreeing
entries: all stay in the event log, and the observer sees the conflict.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from corla_observer.contests import fold as fold_contests
from corla_observer.events import Event
from corla_observer.practice import SETTLING_KINDS, Link


@dataclass
class DeviceView:
    """One device's view of one ballot."""

    device_id: str
    verdict: str = ""  # "match", "different" or "other board"; "" while a contest-by-contest record is not done
    says: str = ""
    seq: int = 0
    #: Contest-by-contest final outcomes, by row: (outcome, heard).
    rows: dict[int, tuple[str, str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"device": short_device(self.device_id), "verdict": self.verdict, "says": self.says, "seq": self.seq}


def short_device(device_id: str) -> str:
    """A device ID short enough to show: ``d-`` and its first 6 hex digits."""
    return device_id[:8]


def _views(events: Sequence[Event], link: Link) -> dict[int, dict[str, DeviceView]]:
    mine = [e for e in events if e.actor == link.actor and e.county == link.county and e.board == str(link.board) and e.device_id]
    views: dict[int, dict[str, DeviceView]] = {}
    for device in sorted({e.device_id for e in mine if e.device_id}):
        own = [e for e in mine if e.device_id == device]
        for e in own:
            position = e.data.get("position")
            if e.kind not in SETTLING_KINDS or not isinstance(position, int):
                continue
            view = views.setdefault(position, {}).setdefault(device, DeviceView(device))
            if e.seq >= view.seq:
                if e.kind == "observation.other_board":
                    view.verdict, view.says = "other board", "skipped: another board's ballot"
                elif e.data.get("match"):
                    view.verdict, view.says = "match", "matched"
                else:
                    view.verdict, view.says = "different", f"different: {e.data.get('note') or '(no note)'}"
                view.seq = e.seq
        for (_, _, _), ballots in fold_contests(own).items():
            for position, rows in ballots.items():
                view = views.setdefault(position, {}).setdefault(device, DeviceView(device))
                view.rows = {n: (r.final.outcome, r.final.heard) for n, r in rows.rows.items() if r.final is not None}
                if rows.done and rows.last_seq >= view.seq:
                    view.verdict = "match" if rows.match else "different"
                    view.says = "matched, contest by contest" if rows.match else f"different: {rows.note()}"
                    view.seq = rows.last_seq
    return views


def _disagree(a: DeviceView, b: DeviceView) -> bool:
    if a.verdict and b.verdict and a.verdict != b.verdict:
        return True
    return any(n in b.rows and a.rows[n] != b.rows[n] for n in a.rows)


def disagreements(events: Iterable[Event], link: Link) -> dict[int, list[DeviceView]]:
    """The ballots on which this observer's devices disagree, each with every device's view (see the module documentation)."""
    found: dict[int, list[DeviceView]] = {}
    for position, by_device in _views(list(events), link).items():
        views = [v for v in by_device.values() if v.verdict or v.rows]
        if any(_disagree(a, b) for i, a in enumerate(views) for b in views[i + 1 :]):
            found[position] = sorted(views, key=lambda v: v.seq)
    return found
