"""Each election's setup: the Audit Center's likely pages, the results source, the calendar and the estimates' settings.

One small, coordinator-editable TOML file in the workspace,
``election.toml`` (`SETUP_FILENAME`), says what the scheduled crawls
(`corla_observer.schedule`), the results gatherer (`corla_observer.enr`)
and the estimates (`corla_observer.estimate`) need to know about one
election, so that a correction on the day is an edit, not a code change:

- ``election``: the audit center's name for it (``2026/general``, as in
  its URLs), and a title;
- ``audit_center``: the Audit Center page the crawl starts from;
- ``crawl``: false to never crawl it (for a past election replayed in a
  demonstration, whose files come from the public mirror);
- ``probe_urls``: audit center URLs fetched on every crawl even when no
  page links them yet. The Audit Center page lists its coming files in an
  HTML comment, without addresses, until they are posted; these are the
  addresses the 2026 primary's files had, with ``primary`` replaced by
  ``general``. A probe that is not there yet is recorded once as missing,
  and costs one request per crawl;
- ``pilot_counties``: the counties observed;
- ``[calendar]``: the Secretary of State's deadlines, as Colorado times;
- ``[results]``: the election night reporting (ENR) source: Clarity's
  address for Colorado and the state election's ID there (found from the
  Secretary of State's results page when empty), the least time between
  two requests to it, and when results count as final;
- ``[estimates]``: how the low estimate of each sample count is made
  (`corla_observer.estimate`);
- ``[cards_per_ballot]``: per county, the ballot cards per ballot, with
  its source, to turn ballots cast into ballot cards before the manifests
  arrive. The defaults come from the 2024 general election (each county's
  manifest total over its ballots cast); a coordinator corrects them from
  the target list's remarks ("2 ballot cards per ballot") or the county.

`init_setup` writes the default for an election (`GENERAL_2026_SETUP` for
``2026/general``, `GENERAL_2020_SETUP` for ``2020/general``, a stand-in for
it in demonstrations). `load_setup` reads and checks the file (`SetupError`
lists every problem). `record_setup` stores the file in the workspace and
records it in an ``election.setup_recorded`` event (its SHA-256 and text)
whenever it differs from the last one recorded, so every estimate and
crawl can be traced to the setup in force.

Standard library only.
"""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from corla_observer.classify import COLORADO_COUNTIES, canonical_county
from corla_observer.datamap import parse_time
from corla_observer.events import EventLog, JSONValue, check_actor
from corla_observer.workspace import Workspace

SETUP_FILENAME = "election.toml"
SETUP_FORMAT = 1

#: The Secretary of State's Audit Center page.
AUDIT_CENTER_URL = "https://www.coloradosos.gov/pubs/elections/auditCenter.html"
#: Where the audit center keeps an election's files: ``RLA/<year>/<election>/``.
RLA_URL = "https://www.coloradosos.gov/pubs/elections/RLA/"
#: Clarity Elections' election night reporting for Colorado.
CLARITY_CO_URL = "https://results.enr.clarityelections.com/CO/"
#: The Secretary of State's results page, which links the current election's ENR.
RESULTS_PAGE_URL = "https://www.coloradosos.gov/pubs/elections/resultsData.html"

#: The files the 2026 primary's audit center posted, by path under the
#: election's directory (the mirror's ``2026/primary/``), in the order they
#: appeared; the 2026 general's are expected under the same names.
PRIMARY_2026_LAYOUT = (
    "riskLimit.html",
    "files/targetContestSelections.xlsx",
    "countyManifest.html",
    "files/uploadStatus.csv",
    "finalReports/ContestsListRound1.csv",
    "finalReports/ContestMarginAndContestCVR-IDs-Round1.csv",
    "finalReports/ContestIDsByCounty.csv",
    "finalReports/CountyBallotManifestToCVRcomparison.csv",
    "finalReports/CanonicalListOfContestsAndChoices.csv",
    "finalReports/CandidateVoteTotalsSummary.csv",
    "finalReports/CandidateVoteTotalsByCounty.csv",
    "finalReports/CVRtoAuditBoardInterpretationComparison.csv",
    "finalReports/StateAuditReport.xlsx",
)

#: Further likely addresses: the same files under ``files/`` while the
#: audit runs, and the round files of earlier elections' layouts.
LIKELY_EXTRA_PATHS = (
    "files/ContestsListRound1.csv",
    "files/ContestMarginAndContestCVR-IDs-Round1.csv",
    "files/ContestIDsByCounty.csv",
    "files/seed.csv",
    "seed.csv",
    "finalReports/ContestsListRound2.csv",
    "finalReports/ContestMarginAndContestCVR-IDs-Round2.csv",
)

#: Ballot cards per ballot in the 2024 general election, per county: its
#: manifest total (the mirror's ``2024/general/<County>BallotManifest.csv``)
#: over its ballots cast (the Secretary of State's
#: ``Results/2024/2024GeneralPrecinctVoterTurnout.xlsx``), rounded to two
#: places. Counties not listed had one card (a ratio of 0.99 to 1.01).
CARDS_PER_BALLOT_2024_GENERAL: dict[str, tuple[float, int, int]] = {
    "Adams": (1.98, 468858, 236899),
    "Alamosa": (1.98, 15216, 7671),
    "Baca": (1.06, 2165, 2034),
    "Boulder": (1.99, 396121, 198886),
    "Denver": (2.99, 1104271, 369436),
    "Dolores": (1.02, 1453, 1431),
    "Eagle": (1.03, 28988, 28225),
    "Grand": (1.02, 10236, 10064),
    "Kiowa": (1.22, 1060, 870),
    "Kit Carson": (1.02, 3807, 3739),
    "Larimer": (2.0, 458224, 229068),
    "Park": (1.09, 13392, 12231),
    "Pueblo": (1.98, 170790, 86126),
    "Routt": (2.0, 33261, 16632),
    "San Miguel": (1.03, 5008, 4870),
    "Teller": (1.03, 17112, 16583),
}


def _cards_table() -> str:
    lines = []
    for county in COLORADO_COUNTIES:
        if county == "San Juan":
            continue
        if county in CARDS_PER_BALLOT_2024_GENERAL:
            value, cards, ballots = CARDS_PER_BALLOT_2024_GENERAL[county]
            source = f"2024 general: {cards} ballot cards in its manifest over {ballots} ballots cast"
        else:
            value, source = 1.0, "2024 general: one card per ballot"
        lines.append(f'"{county}" = {{ value = {value}, source = "{source}" }}')
    return "\n".join(lines)


def _probe_list(election: str) -> str:
    paths = PRIMARY_2026_LAYOUT + LIKELY_EXTRA_PATHS
    return "\n".join(f'  "{RLA_URL}{election}/{path}",' for path in paths)


GENERAL_2026_SETUP = f"""\
# Election setup for corla-observer: the 2026 general election (3 November 2026).
# Edit it in place; the next crawl records the new version in the event log.
format = {SETUP_FORMAT}
election = "2026/general"
title = "2026 General Election"
audit_center = "{AUDIT_CENTER_URL}"
pilot_counties = ["La Plata", "Weld", "Morgan"]

# Fetched on every crawl even before the Audit Center page links them: the
# 2026 primary's file names with "primary" replaced by "general". A file not
# there yet is recorded once as missing. Add or correct addresses here.
probe_urls = [
{_probe_list("2026/general")}
]

# The Secretary of State's 2026 election calendar (updated 31 July 2026),
# in Colorado time.
[calendar]
targets_deadline = "2026-11-06 17:00"      # deadline to select the targeted contests
seed_notice_deadline = "2026-11-09 23:59"  # notice of the public seed meeting
cure_deadline = "2026-11-11 23:59"
provisionals_done = "2026-11-12 23:59"     # provisional and military/overseas ballots done
tabulation_deadline = "2026-11-13 14:00"   # counties finish tabulating; hashed manifest and CVR in CORLA (10th day)
roll = "2026-11-16 10:00"                  # seed rolled (13th day; past notices said 10 AM)
selections_notified = "2026-11-16 23:59"   # counties notified of their selections
audit_reports_due = "2026-11-24 17:00"
canvass_deadline = "2026-11-25 23:59"

[results]
source = "clarity"
base_url = "{CLARITY_CO_URL}"
# The state election's ID on Clarity (126592 was the 2026 primary's). When
# empty, it is read from the first Clarity link on these pages.
election_id = ""
discover_from = ["{RESULTS_PAGE_URL}"]
# Least seconds between two requests to the results site.
min_interval = 1.0
# Results count as final from this time (the counties' tabulation deadline).
final_after = "2026-11-13 14:00"
# Whose own results (for county contests, which the state's results do not
# carry) are fetched: "pilots" (the pilot counties), "all" (every county
# with a targeted county contest: about 60 requests whenever results
# change) or "none".
county_contests = "pilots"

[estimates]
# The low estimate assumes the diluted margin may be this much wider than
# the central estimate's, and one card per ballot until the manifests are in.
low_margin_factor_preliminary = 1.5
low_margin_factor_final = 1.1
low_cards_per_ballot = 1.0
default_cards_per_ballot = 1.0

# Ballot cards per ballot, per county, until its manifest arrives.
[cards_per_ballot]
{_cards_table()}
"""

#: The 2020 general's audit center files (paths under ``2020/general/`` in the
#: public mirror) posted at or after the roll of 16 November 2020, or after the
#: audit: the seed's notice, the round files, CORLA's reports and the final
#: reports.
GENERAL_2020_AFTER_ROLL = (
    "20201116NoticeOfRandomSeed.pdf",
    "round_1/contest.csv",
    "round_1/contest_selection.csv",
    "round_1/contestComparison.csv",
    "round_1/activityReport.xlsx",
    "round_1/resultsReport.xlsx",
    "round_1/stateReport.xlsx",
    "round_2/",
    "round_3/",
    "finalReport",
    "DiscrepancyReport.pdf",
    "gunnison/",
    "batch_count_comparison.csv",
    "tabulate",
    "canonicalTitleCase.csv",
    "contests_by_county.csv",
)


def _quoted(paths: Sequence[str]) -> str:
    return "\n".join(f'  "{p}",' for p in paths)


#: The 2020 general election (3 November 2020), replayed as a stand-in for the
#: 2026 general in demonstrations: its election day falls on the same date, so
#: its calendar is the 2026 general's in 2020 (the roll at 9 AM, as the
#: Secretary of State's notice of 6 November 2020 said). It never crawls the
#: live Audit Center, which holds another election's files: its audit center
#: files come from the public mirror, one update per fetch requested
#: (``[audit_center_mirror]``). Its results, Clarity's election 105975 (still
#: online in October 2026), are not fetched unless its ID is set.
GENERAL_2020_SETUP = f"""\
# Election setup for corla-observer: the 2020 general election (3 November 2020),
# replayed as a stand-in for the 2026 general in a demonstration.
# Edit it in place; the next cycle records the new version in the event log.
format = {SETUP_FORMAT}
election = "2020/general"
title = "2020 General Election (a demonstration)"
audit_center = "{AUDIT_CENTER_URL}"
# The live Audit Center holds another election's files: never crawl it. Each
# "Fetch now" applies the next update below from the public mirror instead
# (its local checkout is named in audit-center-mirror.json).
crawl = false
pilot_counties = ["Delta"]
probe_urls = []

[audit_center_mirror]
election = "2020/general"
repository = "https://github.com/nealmcb/auditcenter"

# The mirror holds the 2020 general in one snapshot taken after the audit, so
# what was posted at or after the roll (16 November 2020) is left out by name.
[[audit_center_mirror.updates]]
title = "Before the roll: the target contest list, the 63 county manifests, CORLA's upload status (13 November 2020)"
commit = "382a035fe0ff90291382a6dfb33bf583032d1605"
exclude = [
{_quoted(GENERAL_2020_AFTER_ROLL)}
]

# The 2026 general's calendar rules applied to 2020 (election day was 3 November
# in both); the roll is the Secretary of State's notice of 6 November 2020.
[calendar]
targets_deadline = "2020-11-06 17:00"
seed_notice_deadline = "2020-11-09 23:59"
tabulation_deadline = "2020-11-13 14:00"
roll = "2020-11-16 09:00"
audit_reports_due = "2020-11-24 17:00"
canvass_deadline = "2020-11-25 23:59"

[results]
source = "clarity"
base_url = "{CLARITY_CO_URL}"
# Not fetched in the demonstration. Clarity's ID for Colorado's 2020 general
# election is 105975: put it here to fetch its results.
election_id = ""
discover_from = []
min_interval = 1.0
final_after = "2020-11-13 14:00"
county_contests = "pilots"

[estimates]
low_margin_factor_preliminary = 1.5
low_margin_factor_final = 1.1
low_cards_per_ballot = 1.0
default_cards_per_ballot = 1.0
"""

#: Default setups by election.
DEFAULT_SETUPS = {"2026/general": GENERAL_2026_SETUP, "2020/general": GENERAL_2020_SETUP}


class SetupError(ValueError):
    """An election setup file does not check out; ``errors`` lists every problem."""

    def __init__(self, errors: Sequence[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = list(errors)


@dataclass(frozen=True)
class Milestone:
    """One calendar deadline: its key, its time (UTC) and the time as written."""

    key: str
    time: str
    given: str


@dataclass(frozen=True)
class ResultsSetup:
    source: str = "clarity"
    base_url: str = CLARITY_CO_URL
    election_id: str = ""
    discover_from: tuple[str, ...] = (RESULTS_PAGE_URL,)
    min_interval: float = 1.0
    final_after: str | None = None
    county_contests: str = "pilots"


@dataclass(frozen=True)
class MirrorUpdate:
    """One state of the audit center replayed from the public mirror: its files at ``commit``, less ``exclude``."""

    title: str
    commit: str
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class MirrorSetup:
    """A past election's audit center, replayed from the public mirror in place of crawling the live one.

    Each request to fetch the audit center applies the next of ``updates``
    (`corla_observer.rehearse.mirror_update`), and after the last fetches it
    again. The mirror's local checkout is named in the workspace's
    ``audit-center-mirror.json``, kept out of the event log.
    """

    election: str
    repository: str
    updates: tuple[MirrorUpdate, ...]


@dataclass(frozen=True)
class EstimatesSetup:
    low_margin_factor_preliminary: float = 1.5
    low_margin_factor_final: float = 1.1
    low_cards_per_ballot: float = 1.0
    default_cards_per_ballot: float = 1.0


@dataclass(frozen=True)
class ElectionSetup:
    """An election's setup; see the module documentation."""

    election: str
    title: str = ""
    audit_center: str = AUDIT_CENTER_URL
    #: Whether scheduled and requested cycles crawl the Audit Center (false for a past election replayed).
    crawl: bool = True
    #: A past election's audit center replayed from the public mirror, one update per fetch requested.
    mirror: MirrorSetup | None = None
    probe_urls: tuple[str, ...] = ()
    pilot_counties: tuple[str, ...] = ()
    calendar: tuple[Milestone, ...] = ()
    results: ResultsSetup = field(default_factory=ResultsSetup)
    estimates: EstimatesSetup = field(default_factory=EstimatesSetup)
    #: Per county: (cards per ballot, source).
    cards_per_ballot: Mapping[str, tuple[float, str]] = field(default_factory=dict)
    sha256: str | None = None
    text: str | None = None

    def milestone(self, key: str) -> Milestone | None:
        return next((m for m in self.calendar if m.key == key), None)

    def cards_for(self, county: str) -> tuple[float, str]:
        """The county's ballot cards per ballot and its source (the default if the county is not listed)."""
        name = canonical_county(county) or county
        if name in self.cards_per_ballot:
            return self.cards_per_ballot[name]
        return self.estimates.default_cards_per_ballot, "the setup's default_cards_per_ballot"

    def results_final(self, moment: str) -> bool:
        """Whether results obtained at ``moment`` (UTC, the event log's format) count as final."""
        return self.results.final_after is not None and moment >= self.results.final_after


def _float(value: Any, name: str, errors: list[str], minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= minimum:
        errors.append(f"{name} must be a number above {minimum}")
        return 1.0
    return float(value)


def _time(value: Any, name: str, errors: list[str]) -> str | None:
    if not isinstance(value, str):
        errors.append(f"{name} must be a date and time as text, such as \"2026-11-13 14:00\"")
        return None
    try:
        return parse_time(value)
    except ValueError as exc:
        errors.append(f"{name}: {exc}")
        return None


def _county_contests(value: Any, errors: list[str]) -> str:
    if value is True:
        return "all"
    if value is False:
        return "none"
    if value not in ("pilots", "all", "none"):
        errors.append('results.county_contests must be "pilots", "all" or "none"')
        return "pilots"
    return str(value)


def _mirror_setup(raw: Any, errors: list[str]) -> MirrorSetup | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errors.append("[audit_center_mirror] must be a table")
        return None
    election, repository = raw.get("election"), raw.get("repository", "https://github.com/nealmcb/auditcenter")
    if not isinstance(election, str) or not election.strip():
        errors.append('audit_center_mirror.election must be the election\'s directory in the mirror, such as "2020/general"')
    if not isinstance(repository, str) or not repository.startswith("https://"):
        errors.append("audit_center_mirror.repository must be an https address")
    updates = []
    for n, item in enumerate(raw.get("updates", []), start=1):
        if not isinstance(item, dict) or not isinstance(item.get("commit"), str) or not str(item["commit"]).strip():
            errors.append(f"audit_center_mirror.updates {n}: needs a commit")
            continue
        exclude = item.get("exclude", [])
        if not isinstance(exclude, list) or not all(isinstance(x, str) and x for x in exclude):
            errors.append(f"audit_center_mirror.updates {n}: exclude must be a list of paths")
            exclude = []
        updates.append(MirrorUpdate(str(item.get("title") or f"update {n}"), str(item["commit"]).strip(), tuple(exclude)))
    if not updates:
        errors.append("[audit_center_mirror] needs at least one [[audit_center_mirror.updates]]")
    return MirrorSetup(str(election or ""), str(repository), tuple(updates))


def parse_setup(text: str) -> ElectionSetup:
    """Read and check an election setup file's text.

    Raises:
        SetupError: listing every problem found.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SetupError([f"not valid TOML: {exc}"]) from None
    errors: list[str] = []
    if data.get("format") != SETUP_FORMAT:
        errors.append(f"format must be {SETUP_FORMAT}")
    election = data.get("election")
    if not isinstance(election, str) or not election.strip():
        errors.append('election must be the audit center\'s name for it, such as "2026/general"')
        election = ""
    crawl = data.get("crawl", True)
    if not isinstance(crawl, bool):
        errors.append("crawl must be true or false")
        crawl = True
    mirror = _mirror_setup(data.get("audit_center_mirror"), errors)
    probes = data.get("probe_urls", [])
    if not isinstance(probes, list) or not all(isinstance(u, str) and u.startswith(RLA_URL) for u in probes):
        errors.append(f"probe_urls must be a list of addresses under {RLA_URL}")
        probes = []
    pilots = []
    for name in data.get("pilot_counties", []):
        county = canonical_county(str(name))
        if county is None:
            errors.append(f"pilot_counties: {name!r} is not a Colorado county")
        else:
            pilots.append(county)
    calendar = []
    raw_calendar = data.get("calendar", {})
    if not isinstance(raw_calendar, dict):
        errors.append("[calendar] must be a table")
        raw_calendar = {}
    for key, value in raw_calendar.items():
        moment = _time(value, f"calendar.{key}", errors)
        if moment:
            calendar.append(Milestone(key, moment, str(value)))
    raw_results = data.get("results", {})
    results = ResultsSetup()
    if isinstance(raw_results, dict):
        if raw_results.get("source", "clarity") != "clarity":
            errors.append('results.source: only "clarity" is supported')
        base = raw_results.get("base_url", CLARITY_CO_URL)
        if not isinstance(base, str) or not base.startswith("https://") or not base.endswith("/"):
            errors.append("results.base_url must be an https address ending in /")
            base = CLARITY_CO_URL
        election_id = str(raw_results.get("election_id", "")).strip()
        if election_id and not election_id.isdigit():
            errors.append("results.election_id must be digits, or empty")
            election_id = ""
        discover = raw_results.get("discover_from", [RESULTS_PAGE_URL])
        if not isinstance(discover, list) or not all(isinstance(u, str) and u.startswith("https://") for u in discover):
            errors.append("results.discover_from must be a list of https addresses")
            discover = []
        final_after = _time(raw_results["final_after"], "results.final_after", errors) if "final_after" in raw_results else None
        results = ResultsSetup(
            base_url=base,
            election_id=election_id,
            discover_from=tuple(discover),
            min_interval=_float(raw_results.get("min_interval", 1.0), "results.min_interval", errors, 0.09),
            final_after=final_after,
            county_contests=_county_contests(raw_results.get("county_contests", "pilots"), errors),
        )
    else:
        errors.append("[results] must be a table")
    raw_estimates = data.get("estimates", {})
    estimates = EstimatesSetup()
    if isinstance(raw_estimates, dict):
        estimates = EstimatesSetup(
            low_margin_factor_preliminary=_float(raw_estimates.get("low_margin_factor_preliminary", 1.5), "estimates.low_margin_factor_preliminary", errors, 0.99),
            low_margin_factor_final=_float(raw_estimates.get("low_margin_factor_final", 1.1), "estimates.low_margin_factor_final", errors, 0.99),
            low_cards_per_ballot=_float(raw_estimates.get("low_cards_per_ballot", 1.0), "estimates.low_cards_per_ballot", errors, 0.99),
            default_cards_per_ballot=_float(raw_estimates.get("default_cards_per_ballot", 1.0), "estimates.default_cards_per_ballot", errors, 0.99),
        )
    else:
        errors.append("[estimates] must be a table")
    cards: dict[str, tuple[float, str]] = {}
    raw_cards = data.get("cards_per_ballot", {})
    if not isinstance(raw_cards, dict):
        errors.append("[cards_per_ballot] must be a table")
        raw_cards = {}
    for name, value in raw_cards.items():
        county = canonical_county(name)
        if county is None:
            errors.append(f"cards_per_ballot: {name!r} is not a Colorado county")
            continue
        if isinstance(value, dict):
            number = _float(value.get("value"), f"cards_per_ballot.{name}.value", errors, 0.99)
            source = str(value.get("source") or "the election setup")
        else:
            number, source = _float(value, f"cards_per_ballot.{name}", errors, 0.99), "the election setup"
        cards[county] = (number, source)
    if errors:
        raise SetupError(errors)
    return ElectionSetup(
        election=str(election),
        title=str(data.get("title", "")),
        audit_center=str(data.get("audit_center", AUDIT_CENTER_URL)),
        crawl=crawl,
        mirror=mirror,
        probe_urls=tuple(dict.fromkeys(probes)),
        pilot_counties=tuple(pilots),
        calendar=tuple(calendar),
        results=results,
        estimates=estimates,
        cards_per_ballot=cards,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        text=text,
    )


def setup_path(workspace: Workspace) -> Path:
    return workspace.root / SETUP_FILENAME


def load_setup(workspace: Workspace) -> ElectionSetup | None:
    """The workspace's election setup, or None if it has no ``election.toml``.

    Raises:
        SetupError: if the file does not check out.
    """
    path = setup_path(workspace)
    if not path.is_file():
        return None
    return parse_setup(path.read_text(encoding="utf-8"))


def init_setup(workspace: Workspace, election: str, *, force: bool = False) -> Path:
    """Write the default setup for ``election`` to the workspace; never over an existing file unless ``force``.

    Raises:
        SetupError: if there is no default for ``election``, or a file exists.
    """
    text = DEFAULT_SETUPS.get(election)
    if text is None:
        raise SetupError([f"no default setup for {election!r}; known: {', '.join(DEFAULT_SETUPS)}"])
    path = setup_path(workspace)
    if path.exists() and not force:
        raise SetupError([f"{SETUP_FILENAME} exists already in the workspace"])
    parse_setup(text)
    path.write_text(text, encoding="utf-8")
    return path


def last_recorded_setup(log: EventLog) -> tuple[int, str] | None:
    """The seq and SHA-256 of the last ``election.setup_recorded`` event, or None."""
    found = None
    for event in log.iter_events():
        if event.kind == "election.setup_recorded" and event.file_sha256:
            found = (event.seq, event.file_sha256)
    return found


def record_setup(workspace: Workspace, log: EventLog, setup: ElectionSetup, actor: str = "system") -> int | None:
    """Store the setup file and record it, if it differs from the last recorded; return the event's seq, or None."""
    check_actor(actor)
    if setup.text is None or setup.sha256 is None:
        return None
    last = last_recorded_setup(log)
    if last is not None and last[1] == setup.sha256:
        return None
    stored = workspace.put_bytes(setup.text.encode("utf-8"))
    data: dict[str, JSONValue] = {
        "sha256": stored.sha256,
        "size": stored.size,
        "election": setup.election,
        "text": setup.text if len(setup.text) <= 32_000 else None,
    }
    return log.append("election.setup_recorded", actor, data=data, file_sha256=stored.sha256).seq


def now_utc() -> str:
    """The current time in the event log's format."""
    from corla_observer.events import format_server_time

    return format_server_time(datetime.now(UTC))
