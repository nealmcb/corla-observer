"""Where the audit is, statewide: one line of stages for the public dashboard.

Shown at the top of the dashboard (docs/PLAN.md, feedback from the demos) so that a reader sees
where the audit stands before any problem: a missing seed before the roll is a stage not yet
reached, not an error. The stages, in order:

Each stage is named by the event that completes it, so that only a ``done`` mark says it happened.

1. **Target contest list posted**: the Secretary of State's target contest list (the contests the
   audit must confirm), or CORLA's contest list, is in the data map.
2. **Counties' files imported in CORLA**: how many counties in CORLA's audit have both their ballot
   manifest and their CVR recorded as ``IMPORTED`` in CORLA's upload status
   (`corla_observer.county_status.corla_uploads`); done when every one has.
3. **Seed entered**: the random seed is in the data map.
4. **Selections posted**: CORLA's selections (the ballots drawn) are in the data map.
5. **Audit rounds**: each county's round (`corla_observer.county_status.stage`), shown as a
   floor and a high marker: the round every county has reached and the furthest any has. This
   stage is never marked done: the audit center's files do not say when a county finished.

Each stage is ``done``, ``current`` (the first not done) or ``later``; a stage not done whose
milestone is still ahead says when it is due, and where no calendar is known (a workspace without
an election setup) says so. Everything comes from the dashboard's snapshot,
so the line can never disagree with the data map or the county statuses below it. Standard
library only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from corla_observer.county_status import CorlaUploads, CountyAuditStatus


@dataclass
class ProgressStep:
    """One stage: its label, its state, and a detail line."""

    label: str
    done: bool
    detail: str
    state: str = "later"  # "done", "current" or "later"

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "state": self.state, "detail": self.detail}


def _due(upcoming: Mapping[str, str], key: str, words: str, calendar_known: bool) -> str:
    """When a stage not reached is due ("due Fri 6 Nov 5:00 PM"), "no election calendar here", or "" once its time has passed."""
    if not calendar_known:
        return "no election calendar here"
    when = upcoming.get(key)
    return f"{words} {when}" if when else ""


def _sentence(text: str, due: str) -> str:
    return f"{text}; {due}" if due else text


def audit_progress(
    *, contests_posted: bool, corla: CorlaUploads, seed: bool, selections: bool, statuses: Mapping[str, CountyAuditStatus],
    upcoming: Mapping[str, str] | None = None, calendar_known: bool = True,
) -> list[ProgressStep]:
    """The stages (see the module documentation); ``upcoming`` is the due texts of the milestones not yet reached, by calendar key."""
    upcoming = upcoming or {}

    def due(key: str, words: str) -> str:
        return _due(upcoming, key, words, calendar_known)

    in_corla = [s for s in statuses.values() if s.in_corla]
    imported = sum(1 for s in in_corla if s.corla_manifest is not None and s.corla_manifest.imported and s.corla_cvr is not None and s.corla_cvr.imported)
    if corla.source is None:
        files = _sentence("CORLA's upload status is not yet posted", due("tabulation_deadline", "due"))
    else:
        files = f"{imported} of {len(in_corla)} counties have their ballot manifest and CVR imported"
    rounds = [s.round or 0 for s in in_corla]
    low, high = (min(rounds), max(rounds)) if rounds else (0, 0)
    if high == 0:
        round_text = "no county has started a round"
    elif low == high:
        round_text = f"every county in round {high}"
    else:
        round_text = f"every county in round {low} or later; the furthest in round {high}" if low else f"the furthest county in round {high}; some not yet started"
    steps = [
        ProgressStep("Target contest list posted", contests_posted, "the Secretary of State's target contest list is posted" if contests_posted else
                     _sentence("the Secretary of State's target contest list is not yet posted", due("targets_deadline", "due"))),
        ProgressStep("Counties' files imported in CORLA", bool(in_corla) and imported == len(in_corla), files),
        ProgressStep("Seed entered", seed, "the random seed is entered" if seed else
                     _sentence("the random seed is not yet entered", due("roll", "at the roll,"))),
        ProgressStep("Selections posted", selections, "CORLA's selections (the ballots drawn) are posted" if selections else
                     _sentence("CORLA's selections (the ballots drawn) are not yet posted", due("roll", "expected after the roll,"))),
        ProgressStep("Audit rounds", False, round_text),
    ]  # fmt: skip
    first = True
    for step in steps:
        if step.done:
            step.state = "done"
        elif first:
            step.state, first = "current", False
    return steps
