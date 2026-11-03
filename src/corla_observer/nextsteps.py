"""What a coordinator might do next for a county, along two paths: the targeted sample and the RLA sample.

Shown on the coordinator home page (docs/PLAN.md, feedback item 1). Before the seed is rolled a
coordinator can already explore the **targeted sample** path: upload the county's redacted CVR
and manifest, draw from patterns of votes (docs/TARGETING.md), make a draft or a list, make
observer links, and send a list to the county. The **RLA sample** path waits for the seed: then
the county's pull list (or CORLA's posted selections, or the prediction) gives the RLA extract
observers follow. Both paths may be followed at once.

Each step is ``done``, ``next`` (the first step of its path not done) or ``later``, judged only
from the event log and the link store: nothing here changes anything, and a step marked done
says only that the log shows it, not that it went well (the data map and the flags say that).
`next_steps` is a pure function of those facts (`Facts`), so the page and the tests share it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

from corla_observer.events import EventLog
from corla_observer.workspace import Workspace


@dataclass
class Facts:
    """What the log and the link store show for one county."""

    cvr: bool = False
    manifest: bool = False
    pull_list: bool = False
    sources: bool = False
    drafts: int = 0
    sent: int = 0
    observer_links: int = 0
    seed: bool = False
    rla_extract: bool = False


@dataclass
class Step:
    """One step: what to do, its state, the page to do it on, and a detail line."""

    text: str
    done: bool
    path: str
    detail: str = ""
    state: str = "later"  # "done", "next" or "later"


@dataclass
class Path:
    """A path's name, what it is for, and its steps in order."""

    name: str
    purpose: str
    steps: list[Step] = field(default_factory=list)

    @property
    def finished(self) -> bool:
        return all(s.done for s in self.steps)


def _mark(steps: list[Step]) -> list[Step]:
    first = True
    for step in steps:
        if step.done:
            step.state = "done"
        elif first:
            step.state, first = "next", False
        else:
            step.state = "later"
    return steps


def next_steps(county: str, facts: Facts) -> list[Path]:
    """The two paths for ``county``, each step marked (see the module documentation)."""
    q = "?county=" + quote(county)
    links = Step(
        "Make observer links for the county's boards", facts.observer_links > 0, "/coord/links",
        f"{facts.observer_links} observer link{'' if facts.observer_links == 1 else 's'} so far" if facts.observer_links else
        "one per observer and board; a link shows every list the county has",
    )  # fmt: skip
    cvr = Step("Upload the county's redacted CVR", facts.cvr, "/coord/upload", "the county's published cast vote record")
    targeted = Path(
        "Targeted sample",
        "Ballots chosen by patterns of votes or by imprinted ID: possible before the seed.",
        [
            cvr,
            Step("Upload the ballot manifest", facts.manifest, "/coord/upload", "gives each ballot's storage location for the pull list"),
            Step("Draw from a pattern of votes, or enter imprinted IDs", facts.sources, f"/coord/targets{q}", "each draw or entry joins the working list"),
            Step(
                "Make a draft, to print and check before deciding", facts.drafts > 0 or facts.sent > 0, f"/coord/targets{q}#list",
                f"{facts.drafts} open draft{'' if facts.drafts == 1 else 's'}" if facts.drafts else "optional: or send the working list at once",
            ),  # fmt: skip
            Step(links.text, links.done, links.path, links.detail),
            Step(
                "Send a list to the county", facts.sent > 0, f"/coord/targets{q}#{'drafts' if facts.drafts else 'list'}",
                f"{facts.sent} targeted list{'' if facts.sent == 1 else 's'} sent" if facts.sent else
                "press Send when you send it; observers see a list once it is sent",
            ),  # fmt: skip
        ],
    )
    rla = Path(
        "RLA sample",
        "The ballots CORLA selects from the seed: after the seed ceremony.",
        [
            Step(cvr.text, cvr.done, cvr.path, cvr.detail),
            Step("The seed is rolled", facts.seed, "/coord/seed", "at the Secretary of State's seed ceremony; recorded here when published"),
            Step(
                "The county's pull list, or CORLA's posted selections", facts.pull_list or facts.rla_extract, "/coord/upload",
                "upload the county's pull list; without one, the extract uses CORLA's selections or the prediction from the seed",
            ),  # fmt: skip
            Step("Build the RLA extract", facts.rla_extract, "/coord/extract", "the ballots, in pull order, that observers follow and print"),
            Step(links.text, links.done, links.path, links.detail),
        ],
    )
    for p in (targeted, rla):
        _mark(p.steps)
    return [targeted, rla]


def facts_for(workspace: Workspace, log: EventLog, county: str, *, observer_links: int) -> Facts:
    """The `Facts` for ``county`` from the log and the workspace; ``observer_links`` from the link store."""
    from corla_observer import pulllists
    from corla_observer import targets as tg
    from corla_observer.extract import latest_extract
    from corla_observer.seeds import seed_state
    from corla_observer.uploads import status

    entry = status(log).get(county)
    latest = entry.latest if entry is not None else {}
    lists = pulllists.county_lists(log.iter_events(), county, retired=True, drafts=True)
    return Facts(
        cvr="redacted_cvr" in latest,
        manifest="manifest" in latest,
        pull_list="pull_list" in latest,
        sources=bool(tg.sources(None, log, county)),
        drafts=sum(1 for x in lists if x.draft and not x.closed),
        sent=sum(1 for x in lists if x.targeted and not x.draft),
        observer_links=observer_links,
        seed=seed_state(log.iter_events()).seed is not None,
        rla_extract=latest_extract(workspace, log, county) is not None,
    )
