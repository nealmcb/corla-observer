"""A walkthrough: a past audit replayed step by step, in its real order, for people to follow on the web pages.

A walkthrough workspace (`init_walkthrough`) is an ordinary workspace with a
``walkthrough.json`` naming the election, the county followed and the
public mirror's checkout. Its timeline (`timeline`) lists what arrived when,
in the order it did, taken from the mirror's git history and its
``observerfiles/`` (the 2026 primary, `PRIMARY_2026_TIMELINE`, follows the
general election's sequence: the target list, the results, the manifests
and the upload status, the county's own files, the roll and the seed, then
CORLA's reports). `next_step` applies exactly the next step's arrival,
through the same code a live audit uses:

- ``fetch``: the audit center's files as the mirror held them at the
  step's commit (`corla_observer.rehearse.stage_fetch`), with the
  fetcher's alerts;
- ``results``: an election night results file from the mirror, recorded
  as fetched (`corla_observer.enr.record_results_file`);
- ``uploads``: the county's redacted CVR and manifest, received as a
  coordinator uploads them, redaction check included
  (`corla_observer.rehearse.stage_uploads`);
- ``seed``: the seed entered and the roll's time recorded
  (`corla_observer.rehearse.stage_seed`), then the county's extract built;
- ``note``: nothing arrives (something that happened elsewhere, such as a
  county's upload to CORLA, known only later).

then any data map overrides whose evidence arrives at that step
(`Step.overrides`: for the 2026 primary, the alias for Prowers's misspelled
county with the manifests, and the manifest adjustment only with CORLA's
final reports, the evidence for it), added to the workspace's overrides
file and applied, and then the normal pipeline
(`corla_observer.schedule.update_derived`: flags, estimates, prediction,
interim extracts). Every event of a step is marked ``simulated`` with the
step's time where the code supports it, and a ``walkthrough.step`` event
records each step (its number, title and simulated time). The web pages
show the simulated time prominently and say that this is a walkthrough
(`state`), and never fetch from the real sites.

Nothing known only later is applied, or told, before its step: a
walkthrough starts with no overrides, a county's audit board count is a
placeholder that says only that it is not yet known, the seed's source is
the roll's announcement, and a step whose title would tell what it brings
shows a plain one (`Step.upcoming`) until it is applied.

`init_walkthrough` with ``force`` starts again from nothing, keeping the
coordinator accounts. The mirror's path is kept only in
``walkthrough.json`` in the workspace, never in the event log.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from corla_observer.datamap import OVERRIDES_FILENAME, parse_time
from corla_observer.events import EventLog, JSONValue
from corla_observer.rehearse import (
    MIRROR_REPOSITORY,
    PRIMARY_2026_ALAMOSA_ADJUSTMENT,
    PRIMARY_2026_COUNTY_FILES,
    PRIMARY_2026_OVERRIDES_HEADER,
    PRIMARY_2026_PROWES_ALIAS,
    PRIMARY_2026_RECEIVED,
    PRIMARY_2026_ROLL_SOURCE,
    PRIMARY_2026_ROLL_TIME,
    PRIMARY_2026_SEED,
    RehearsalError,
    resolve_commit,
    stage_fetch,
    stage_overrides,
    stage_seed,
    stage_uploads,
)
from corla_observer.workspace import Workspace

STATE_FILENAME = "walkthrough.json"
ACTOR = "coordinator:walkthrough"
#: Files kept when a walkthrough is started again.
KEPT_ON_RESET = ("accounts",)


class WalkthroughError(RuntimeError):
    """A walkthrough cannot be started or advanced."""


@dataclass(frozen=True)
class Step:
    """One arrival in the timeline: when, what, and how to apply it."""

    time: str  # Colorado time with offset, as written
    title: str
    kind: str  # fetch, results, uploads, seed, note
    commit: str | None = None
    path: str | None = None
    detail: str = ""
    #: Data map overrides whose evidence arrives at this step, added to the
    #: overrides file and applied after it, and what they are, in words.
    overrides: str = ""
    overrides_note: str = ""
    #: The title shown before the step is applied, when ``title`` would tell what is found there.
    upcoming: str = ""

    @property
    def utc(self) -> str:
        return parse_time(self.time)

    @property
    def preview(self) -> str:
        """The title to show before the step is applied."""
        return self.upcoming or self.title


def primary_2026_timeline(county: str) -> list[Step]:
    """The 2026 primary's arrivals for ``county``, in the general election's order (see the module documentation)."""
    if county not in PRIMARY_2026_COUNTY_FILES:
        raise WalkthroughError(f"no county files for {county}; known: {', '.join(PRIMARY_2026_COUNTY_FILES)}")
    received, received_source = PRIMARY_2026_RECEIVED[county]
    # What happened in CORLA is known here only from its upload status, posted on 13 July: say no more until then.
    uploads_to_corla = {
        "Weld": ("2026-07-09T16:57:06-06:00", "Weld uploads its manifest and its CVR to CORLA (their times are known from CORLA's upload status, posted on 13 July)"),
        "La Plata": ("2026-07-09T17:00:00-06:00", "La Plata uploads its manifest and its CVR to CORLA (what the audit center records of them is known from CORLA's upload status, posted on 13 July)"),
        "Morgan": ("2026-07-09T17:00:00-06:00", "Morgan uploads its manifest and its CVR to CORLA (what the audit center records of them is known from CORLA's upload status, posted on 13 July)"),
    }
    corla_time, corla_note = uploads_to_corla[county]
    steps = [
        Step("2026-07-07T12:33:27-06:00", "The target contest list is posted", "fetch", "5108046",
             detail="files/targetContestSelections.xlsx (the mirror's commit 5108046)"),
        Step(corla_time, f"{county} uploads to CORLA", "note", detail=corla_note + ". Nothing arrives here."),
        Step("2026-07-10T17:00:00-06:00", "Election night results: the state's detail of 10 July", "results", "eac1014",
             "2026/primary/observerfiles/SOS_ENR_detail_20260710.xlsx",
             detail="the Secretary of State's ENR detail workbook as saved on 10 July (its time of day is not known: 17:00 is assumed)"),
        Step("2026-07-13T09:14:27-06:00", "61 county manifests are posted, before the roll", "fetch", "a4a2b93",
             detail="the mirror's commit a4a2b93", overrides=PRIMARY_2026_PROWES_ALIAS,
             overrides_note="the county alias 'Prowes' for Prowers: files/Prowers.csv, posted at this step, spells its county 'Prowes' on one row"),
        Step(received, f"{county}'s own files arrive", "uploads", detail=received_source),
        Step(PRIMARY_2026_ROLL_TIME, "The dice roll: the seed is entered", "seed", detail=PRIMARY_2026_ROLL_SOURCE + "; then the county's extract is built"),
        Step("2026-07-13T10:12:40-06:00", "More manifests are posted, after the roll", "fetch", "23af1c6"),
        Step("2026-07-13T10:33:59-06:00", "Two counties' manifests are replaced", "fetch", "f46c663", upcoming="Audit center update"),
        Step("2026-07-13T11:03:34-06:00", "CORLA's upload status is posted", "fetch", "c32f441"),
        Step("2026-07-13T16:15:36-06:00", "Audit center update: a county's manifest as CORLA imported it", "fetch", "43b6c78", upcoming="Audit center update"),
        Step("2026-07-16T16:13:28-06:00", "CORLA's final reports: the contest list, the selections, the comparison", "fetch", "ba18ba6",
             overrides=PRIMARY_2026_ALAMOSA_ADJUSTMENT,
             overrides_note="the manifest adjustment for Alamosa (batch 102-139, 11 cards): CORLA's manifest-to-CVR comparison and its contest list, both posted at this step, account for the batch"),
    ]  # fmt: skip
    return sorted(steps, key=lambda s: s.utc)


@dataclass(frozen=True)
class Replay:
    """What a walkthrough of one election needs: its timeline and the facts its steps enter."""

    timeline: Callable[[str], list[Step]]
    seed: str
    seed_source: str
    roll_time: str
    roll_source: str
    county_files: Mapping[str, Mapping[str, str]]
    received: Mapping[str, tuple[str, str]]
    #: The overrides file's first lines, written before the first step that brings overrides (`Step.overrides`).
    overrides: str | None = None
    uploads_commit: str = "ba18ba6874c5ed4be713b3745a5d32c261cc6083"
    #: The note on each county's placeholder audit board count: only what is known when its files arrive.
    audit_boards_note: str = "not yet known; a walkthrough placeholder"


#: The elections a walkthrough can replay.
TIMELINES: dict[str, Replay] = {
    "2026/primary": Replay(
        primary_2026_timeline, PRIMARY_2026_SEED, "announced at the public dice roll on 2026-07-13",
        PRIMARY_2026_ROLL_TIME, PRIMARY_2026_ROLL_SOURCE, PRIMARY_2026_COUNTY_FILES, PRIMARY_2026_RECEIVED,
        PRIMARY_2026_OVERRIDES_HEADER,
    )
}  # fmt: skip


@dataclass
class WalkthroughState:
    election: str
    county: str
    mirror: str
    steps: list[Step]
    done: int
    #: The simulated time now: the last step's, or None before the first.
    now: str | None

    @property
    def next(self) -> Step | None:
        return self.steps[self.done] if self.done < len(self.steps) else None


def is_walkthrough(workspace: Workspace) -> bool:
    return (workspace.root / STATE_FILENAME).is_file()


def walkthrough_county(workspace: Workspace) -> str | None:
    """The county a walkthrough follows, or None if the workspace is not a walkthrough."""
    path = workspace.root / STATE_FILENAME
    if not path.is_file():
        return None
    try:
        county = json.loads(path.read_text(encoding="utf-8")).get("county")
    except (OSError, ValueError):
        return None
    return county if isinstance(county, str) else None


def init_walkthrough(
    root: Path, *, election: str, county: str, mirror: Path, accounts_from: Path | None = None, force: bool = False
) -> Workspace:
    """Make (or, with ``force``, make again) a walkthrough workspace at ``root``; return it.

    Raises:
        WalkthroughError: if ``root`` holds something other than a walkthrough, or one exists without ``force``.
    """
    if election not in TIMELINES:
        raise WalkthroughError(f"no walkthrough for {election}; known: {', '.join(TIMELINES)}")
    steps = TIMELINES[election].timeline(county)
    for step in steps:
        if step.commit:
            try:
                resolve_commit(mirror, step.commit)
            except RehearsalError as exc:
                raise WalkthroughError(f"the mirror lacks commit {step.commit}: {exc}") from None
    if root.exists() and any(root.iterdir()):
        if not (root / STATE_FILENAME).is_file():
            raise WalkthroughError(f"{root} is not empty and is not a walkthrough: refusing to touch it")
        if not force:
            raise WalkthroughError(f"{root} is a walkthrough already: use force to start it again")
        for child in root.iterdir():
            if child.name in KEPT_ON_RESET:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    workspace = Workspace.open(root)
    if accounts_from is not None:
        copy_coordinator_accounts(accounts_from, root)
    (root / STATE_FILENAME).write_text(
        json.dumps({"election": election, "county": county, "mirror": str(mirror)}, indent=2) + "\n", encoding="utf-8"
    )
    with EventLog.open(root) as log:
        log.append("walkthrough.started", ACTOR, data={"election": election, "county": county, "steps": len(steps), "repository": MIRROR_REPOSITORY})
    return workspace


def copy_coordinator_accounts(source: Path, dest: Path) -> bool:
    """Copy a workspace's coordinator accounts (``accounts/coordinators.json`` only: not observer links); return whether there were any."""
    found = source / "accounts" / "coordinators.json"
    if not found.is_file():
        return False
    target = dest / "accounts"
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copy2(found, target / "coordinators.json")
    (target / "coordinators.json").chmod(0o600)
    return True


def state(workspace: Workspace, log: EventLog) -> WalkthroughState:
    """The walkthrough's timeline and how far it has got."""
    path = workspace.root / STATE_FILENAME
    if not path.is_file():
        raise WalkthroughError("not a walkthrough workspace")
    info = json.loads(path.read_text(encoding="utf-8"))
    steps = TIMELINES[info["election"]].timeline(info["county"])
    done, now = 0, None
    for event in log.iter_events():
        if event.kind == "walkthrough.step":
            done = int(str(event.data.get("index"))) + 1
            now = str(event.data.get("time"))
    return WalkthroughState(info["election"], info["county"], info["mirror"], steps, done, now)


def next_step(workspace: Workspace, log: EventLog, *, mirror: Path | None = None) -> tuple[Step, list[str]]:
    """Apply the next step and run the pipeline; return the step and notes on what happened.

    Raises:
        WalkthroughError: when every step has been applied, or the mirror is not at hand.
    """
    from corla_observer.enr import record_results_file
    from corla_observer.extract import ExtractError, build_extract
    from corla_observer.schedule import CycleReport, update_derived

    current = state(workspace, log)
    replay = TIMELINES[current.election]
    step = current.next
    if step is None:
        raise WalkthroughError("the walkthrough is over: start it again to replay it")
    repo = mirror or Path(current.mirror)
    if not (repo / ".git").exists():
        raise WalkthroughError(f"the mirror checkout is not at {repo}")
    notes: list[str] = []
    marker: dict[str, JSONValue] = {"source": "walkthrough", "repository": MIRROR_REPOSITORY, "time": step.utc}
    if step.kind == "fetch" and step.commit:
        report = stage_fetch(workspace, log, repo, step.commit, election=current.election)
        notes.append(", ".join(f"{report.count(o)} {o}" for o in ("new", "changed", "unchanged", "missing")) + f"; {len(report.alerts)} alerts")
    elif step.kind == "results" and step.commit and step.path:
        data = subprocess.run(["git", "-C", str(repo), "show", f"{resolve_commit(repo, step.commit)}:{step.path}"], capture_output=True, check=True).stdout
        record_results_file(
            workspace, log, data, url=f"{MIRROR_REPOSITORY}/blob/{step.commit}/{step.path}", scope="state", what="detail",
            election_id=None, version=None, simulated=marker, actor=ACTOR,
        )  # fmt: skip
        notes.append("the state's results recorded")
    elif step.kind == "uploads":
        uploads = stage_uploads(
            workspace, log, repo, commit=replay.uploads_commit, counties=[current.county], actor=ACTOR,
            election=current.election, county_files=replay.county_files, received=replay.received,
            audit_boards_note=replay.audit_boards_note,
        )  # fmt: skip
        notes.append("; ".join(f"{r.kind}: {'accepted' if r.accepted else 'REJECTED'}" for r in uploads.results))
    elif step.kind == "seed":
        stage_seed(log, seed=replay.seed, source=replay.seed_source, actor=ACTOR, roll_time=replay.roll_time, roll_source=replay.roll_source)
        notes.append(f"seed {replay.seed} entered; roll time recorded")
    if step.overrides:
        path = workspace.root / OVERRIDES_FILENAME
        before = path.read_text(encoding="utf-8") if path.exists() else (replay.overrides or "format = 1\n")
        path.write_text(before + step.overrides, encoding="utf-8")
        result = stage_overrides(workspace, log, actor=ACTOR)
        notes.append(f"data map overrides applied: {step.overrides_note}" if result.applied else f"error: the overrides were not applied: {'; '.join(result.errors)}")
    log.append("walkthrough.step", ACTOR, data={"index": current.done, "title": step.title, "kind": step.kind, "time": step.utc, "commit": step.commit})
    cycle = CycleReport()
    update_derived(workspace, log, None, cycle, actor="system", counties=[current.county])
    if step.kind == "seed" or (current.done > 0 and _has_extract(log, current.county)):
        try:
            built = build_extract(workspace, log, current.county, actor=ACTOR, skip_if_unchanged=True)
            notes.append(f"{current.county}'s extract: {built.extract['order']} order, {len({b['position'] for b in built.extract['ballots']})} ballots")
        except ExtractError as exc:
            notes.append(f"extract not built: {exc}")
    notes.extend(f"error: {e}" for e in cycle.errors)
    return step, notes


def _has_extract(log: EventLog, county: str) -> bool:
    return any(e.kind == "extract.built" and e.county == county for e in log.iter_events())


def steps_table(walk: WalkthroughState) -> Sequence[tuple[int, Step, bool]]:
    """Each step with its number (from 1) and whether it has been applied."""
    return [(n, step, n <= walk.done) for n, step in enumerate(walk.steps, start=1)]
