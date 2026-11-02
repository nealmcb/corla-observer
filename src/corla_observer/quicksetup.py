"""Populate test data: a county with files and something to observe in one step, for testing the observer pages.

Testing mode only: the web server offers it (``POST /coord/populate``) only where it also offers
Reset (``serve --reset-command``), never on a real deployment.

`populate` does, for one test county, what a coordinator would otherwise do on several pages, through
the same functions and so into the same event log:

1. **Refuses a county that already has a redacted CVR**, so nobody's own uploads or configuration
   are disturbed; for such a county the targeted ballots and Links pages do the rest by hand.
2. **Uploads the county's files**, through the normal upload (`uploads.receive`, with its redaction
   check): for Testland (`corla_observer.testcounty`), its generated manifest and CVR; for a
   demonstration county ("Mesa (2020 demo)", `corla_observer.classify.DEMO_COUNTIES`), the
   county's manifest and CVR from the test data bundle (`serve --test-data DIR`, made by
   scripts/make_test_bundle.py: ``DIR/<County>/manifest.csv`` and ``cvr.csv``). A CVR that fails
   the redaction check stops here: the result says so, which is itself a useful test.
3. **Draws** `SIZE` ballots from any ballot with votes (the empty pattern, `targets.ANY_BALLOT`),
   seed `SEED`, and records the draw.
4. **Sends** the working list to the county as its next targeted list, for one audit board.
5. **Makes links** for board 1, named after the coordinator (``coordinator:neal`` makes
   ``observer:neal`` and ``reader:neal``): an observer link, and a practice reader link.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from corla_observer import targets as tg
from corla_observer import testcounty
from corla_observer.classify import DEMO_COUNTIES, is_test_county, known_county
from corla_observer.events import EventLog
from corla_observer.extract import county_manifest
from corla_observer.practice import LinkStore
from corla_observer.redaction import RedactionReport
from corla_observer.uploads import receive, status
from corla_observer.workspace import Workspace

#: Ballots drawn.
SIZE = 10
#: The draw's seed, in words.
SEED = "populate test data"
DEMO_SUFFIX = " (2020 demo)"


class PopulateError(ValueError):
    """The county cannot be populated: not a test county, no files for it, or files already there."""


@dataclass(frozen=True)
class Populated:
    county: str
    #: Files uploaded, by name.
    uploaded: tuple[str, ...]
    #: The CVR's redaction report when the check rejected it; then nothing after the upload was done.
    rejected: RedactionReport | None = None
    ids: tuple[str, ...] = ()
    list_name: str = ""
    list_key: str = ""
    observer_token: str = ""
    reader_token: str = ""


def bundle_counties(test_data: Path | None) -> list[str]:
    """The demonstration counties the test data bundle has files for, by their demonstration names."""
    if test_data is None or not test_data.is_dir():
        return []
    names = []
    for folder in sorted(test_data.iterdir()):
        demo = f"{folder.name}{DEMO_SUFFIX}"
        if demo in DEMO_COUNTIES and (folder / "manifest.csv").is_file() and (folder / "cvr.csv").is_file():
            names.append(demo)
    return names


def choices(test_data: Path | None) -> list[str]:
    """What the button offers: Testland, then the bundle's demonstration counties."""
    return [testcounty.COUNTY, *bundle_counties(test_data)]


def has_cvr(log: EventLog, county: str) -> bool:
    entry = status(log, [county]).get(county)
    return bool(entry and "redacted_cvr" in entry.latest)


def _files(county: str, test_data: Path | None, tmp: Path) -> list[tuple[str, str, Path]]:
    """``(kind, original name, path)`` of the county's manifest and CVR, manifest first."""
    if county == testcounty.COUNTY:
        made = testcounty.generate(tmp)
        return [("manifest", testcounty.MANIFEST_NAME, made[testcounty.MANIFEST_NAME]), ("redacted_cvr", testcounty.CVR_NAME, made[testcounty.CVR_NAME])]
    if county not in bundle_counties(test_data):
        raise PopulateError(f"the test data bundle has no files for {county}")
    assert test_data is not None
    folder = test_data / county.removesuffix(DEMO_SUFFIX)
    real = folder.name.replace(" ", "")
    return [("manifest", f"manifest-{real}.csv", folder / "manifest.csv"), ("redacted_cvr", f"{real} 2020 CVR.csv", folder / "cvr.csv")]


def populate(
    workspace: Workspace, log: EventLog, county: str, actor: str, *, test_data: Path | None = None, session_id: str | None = None
) -> Populated:
    """Upload a test county's files, draw and send a targeted list, and make links (see the module documentation).

    Raises:
        PopulateError: for a county that is not a test county, has no files, or already has a redacted CVR.
        corla_observer.targets.DrawError, corla_observer.practice.PracticeError: as the steps raise them.
    """
    name = known_county(county)
    if name is None or not is_test_county(name):
        raise PopulateError(f"{county!r} is not a test county: only Testland and the 2020 demonstration counties can be populated")
    if has_cvr(log, name):
        raise PopulateError(
            f"{name} already has a redacted CVR here, so it is left as it is: draw a list on the targeted ballots page and make links on the Links page"
        )
    uploaded: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for kind, original, path in _files(name, test_data, Path(tmp)):
            result = receive(workspace, log, name, kind, path, original, actor)
            uploaded.append(original)
            if not result.accepted:
                if result.report is not None:
                    return Populated(name, tuple(uploaded), rejected=result.report)
                raise PopulateError(f"{original} was not accepted: {result.reason}")
    sha = status(log, [name])[name].latest["redacted_cvr"].sha256
    manifest = county_manifest(workspace, log, name)
    votes = tg.cached_votes(workspace, sha)
    pattern = tg.pattern_of(name, [])
    found = tg.sources(workspace, log, name)
    record = tg.draw(
        votes, county=name, cvr_sha256=sha, pattern=pattern, seed=SEED, seed_source="entered for this draw",
        number=tg.draw_count(log, name) + 1, locations=manifest.locations, manifest_sha256=manifest.sha256, actor=actor,
        size=SIZE, exclude=tg.holders(workspace, log, name, found),
    )  # fmt: skip
    record, _ = tg.record_draw(workspace, log, record, session_id=session_id)
    sent = tg.send(workspace, log, name, actor, votes=votes, locations=manifest.locations, sizes=manifest.sizes, boards=1, session_id=session_id)
    person = actor.split(":", 1)[-1]
    links = LinkStore(workspace.root)
    observer_token, _ = links.create("observer", person, name, 1, log, actor=actor)
    reader_token, _ = links.create("reader", person, name, 1, log, actor=actor)
    ids = tuple(str(b["imprinted_id"]) for b in record.ballots)
    return Populated(name, tuple(uploaded), None, ids, sent.name, sent.key, observer_token, reader_token)
