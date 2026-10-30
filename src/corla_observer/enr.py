"""Contest results from Colorado's election night reporting (ENR), for estimating the sample counts.

The Secretary of State publishes unofficial results through Clarity
Elections' ENR site, https://results.enr.clarityelections.com/CO/ (the 2026
primary was election 126592). Its machine-readable downloads, for a state
election ``ID`` at its current version ``VER``:

- ``CO/ID/current_ver.txt``: the current version number, a few bytes;
- ``CO/ID/VER/reports/detailxml.zip``: ``detail.xml``, every contest with
  each choice's votes by county, and each county's ballots cast
  (``ElectionVoterTurnout``);
- ``CO/ID/VER/json/en/electionsettings.json`` (often gzip-compressed): among
  other things ``participatingcounties``, ``County_Name|ID|VER|time|...``
  for each county's own ENR election, whose ``json/en/summary.json`` has
  the county's own contests (county commissioners, local questions) with
  each choice's total (a county's election has no ``detailxml.zip``);
- ``reports/detailxlsx.zip`` and ``reports/summary.zip``: the same in a
  workbook and in a summary CSV.

`gather` fetches these politely (a separate `corla_observer.fetch.Fetcher`
allowed only Clarity's host, spaced by the setup's ``min_interval``): the
version first, and the rest only when the version changed since the last
fetch recorded; county details only for the counties asked for, and only
when their version changed. Every body is stored in the workspace under its
SHA-256 and recorded in a ``results.fetched`` event. The state election's
ID comes from the setup, or else from the first Clarity link on the
Secretary of State's results page (``results.discovered``).

`parse_detail` reads ``detail.xml`` (in its zip or not), `parse_detail_xlsx`
the workbook form (as the observers saved it for the 2026 primary),
`parse_summary_csv` the state's ``summary.zip`` (totals only) and
`parse_summary_json` a county's summary, all into `Results`
(`read_results` tells them apart by content, and says when Clarity
generated the file where the file says: `results_file_time`).

**A file downloaded by hand.** When the automatic fetch is blocked or
behind, a coordinator uploads the file (`receive_results`, from
``/coord/upload`` or ``corla-observer results add``): it is identified by
content, refused if it is no results file or contradicts the scope given,
stored by hash and recorded as ``results.uploaded`` with the coordinator,
the source and a note. From then on it is used like a fetched one:
`load_results` takes, per scope, the newest file by the time Clarity
generated it (else when it was fetched or uploaded), a fetched one before an
upload of the same time, and for the state a detail (by county) before a
summary (`ranked`). `corla_observer.totals` checks every arrival's totals. `contest_margin` gives a contest's margin over a set of
counties as CORLA computes it: the votes of the last winner (``vote for``
N: the Nth highest) minus those of the first loser.

Events:

``results.fetched``
    ``url``, ``scope`` (``state`` or the county), ``what`` (``version``,
    ``detail``, ``settings``, ``summary``), ``election_id``, ``version``, ``status``,
    ``sha256``, ``size``, ``headers``; ``file_sha256`` the body's.
``results.fetch_failed``
    ``url``, ``scope``, ``what``, ``error``.
``results.discovered``
    ``election_id`` and the ``page`` it was found on.
``results.uploaded``
    A file uploaded by hand (``uploaded`` true): ``scope``, ``what``
    (``detail`` or ``summary``), ``form``, ``original_name``,
    ``source_url``, ``note``, ``sha256``, ``size``, ``generated`` (UTC) and
    ``generated_basis``, ``election_name``, ``contests``, ``ballots_cast``;
    ``file_sha256``; the county for a county's file.
``results.upload_rejected``
    A file refused and not stored: ``original_name``, ``scope``,
    ``reason``, ``sha256``, ``size``, ``form``.

A detail or summary in ``results.fetched`` also has its ``form`` and
``generated`` time.

XML is parsed with the standard library, refusing any document type
declaration, and zip members are size-limited.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
import zipfile
from datetime import UTC, datetime, timedelta, timezone
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from xml.etree import ElementTree as ET

from corla_observer.classify import XlsxReader, canonical_county
from corla_observer.datamap import DataMap, contest_key
from corla_observer.election import ElectionSetup
from corla_observer.events import Event, EventLog, JSONValue, check_actor, format_server_time
from corla_observer.fetch import FetchError, Fetcher, FetchPolicy
from corla_observer.localtime import colorado_offset
from corla_observer.sampling import county_key
from corla_observer.workspace import ObjectNotFoundError, Workspace

ACTOR = "system"
CLARITY_HOST = "results.enr.clarityelections.com"
#: Largest decompressed detail file read.
MAX_DETAIL_BYTES = 256 * 1024 * 1024

_CLARITY_LINK_RE = re.compile(r"results\.enr\.clarityelections\.com/CO/(\d{4,9})")
_VOTE_FOR_RE = re.compile(r"\s*\(vote\s*for\s*=?\s*(\d+)\)\s*$", re.IGNORECASE)
_CLARITY_TIME_RE = re.compile(
    r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AP]M)?\s*([A-Z]{2,4})?", re.IGNORECASE
)
#: Offsets from UTC of the zones Clarity's timestamps name.
_ZONES = {"MDT": -6, "MST": -7, "EDT": -4, "EST": -5, "CDT": -5, "CST": -6, "PDT": -7, "PST": -8, "UTC": 0, "GMT": 0}
#: Every form `read_results` reads, by content.
FORMS = ("detail.xml", "detailxml.zip", "detail.xlsx", "detailxlsx.zip", "summary.csv", "summary.zip", "summary.json")
#: Largest results file a coordinator may upload.
MAX_RESULTS_UPLOAD_BYTES = 64 * 1024 * 1024


class ResultsError(ValueError):
    """A results file cannot be read."""


@dataclass
class ContestResult:
    """One contest's results: its choices' votes in total and by area (county, or precinct in a county's file)."""

    name: str
    vote_for: int
    totals: dict[str, int] = field(default_factory=dict)
    #: area -> choice -> votes.
    by_area: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass
class Results:
    """A detail file's contents: the scope (``state`` or a county), its timestamp, ballots cast by county, the contests."""

    scope: str
    election_name: str | None
    timestamp: str | None
    ballots_cast: dict[str, int]
    contests: list[ContestResult]
    #: Whether ``by_area`` is by county (the state's file) or by precinct (a county's).
    areas_are_counties: bool = True
    #: The form read (`FORMS`), and when Clarity generated the file (UTC, from the file itself; None if it does not say), and how that was found.
    form: str = ""
    generated: str | None = None
    generated_basis: str = ""


# ---------------------------------------------------------------------------
# Reading


def _unzip(data: bytes, suffix: str) -> bytes:
    if not data.startswith(b"PK"):
        return data
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(suffix)]
        if not names:
            raise ResultsError(f"no {suffix} file in the zip")
        info = archive.getinfo(names[0])
        if info.file_size > MAX_DETAIL_BYTES:
            raise ResultsError(f"{names[0]} is larger than {MAX_DETAIL_BYTES} bytes")
        return archive.read(names[0])


def contest_label(name: str) -> tuple[str, int]:
    """A contest's name without its ``(Vote For N)``, and N (1 if absent)."""
    match = _VOTE_FOR_RE.search(name)
    if match:
        return name[: match.start()].strip(), int(match.group(1))
    return name.strip(), 1


def parse_detail(data: bytes, scope: str = "state") -> Results:
    """Read Clarity's ``detail.xml`` (or its ``detailxml.zip``).

    Raises:
        ResultsError: if it is not such a file.
    """
    xml = _unzip(data, ".xml")
    if b"<!DOCTYPE" in xml[:4096].upper():
        raise ResultsError("refusing an XML document type declaration")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ResultsError(f"not XML: {exc}") from None
    if root.tag != "ElectionResult":
        raise ResultsError(f"not a Clarity detail file (root {root.tag})")
    ballots: dict[str, int] = {}
    turnout = root.find("ElectionVoterTurnout")
    counties_by_area = False
    if turnout is not None:
        for county in turnout.iter("County"):
            name = canonical_county(county.get("name", "").replace("_", " "))
            if name and (county.get("ballotsCast") or "").isdigit():
                ballots[name] = int(county.get("ballotsCast") or 0)
                counties_by_area = True
        if not ballots and scope != "state" and (turnout.get("ballotsCast") or "").isdigit():
            ballots[canonical_county(scope) or scope] = int(turnout.get("ballotsCast") or 0)
    contests = []
    for element in root.iter("Contest"):
        name, _ = contest_label(element.get("text", ""))
        vote_for = int(element.get("voteFor") or 1)
        result = ContestResult(name, vote_for)
        for choice in element.iter("Choice"):
            label = choice.get("text", "")
            result.totals[label] = result.totals.get(label, 0) + int(choice.get("totalVotes") or 0)
            for vote_type in choice.iter("VoteType"):
                for area in list(vote_type):
                    area_name = area.get("name", "")
                    if area.tag == "County":
                        area_name = canonical_county(area_name.replace("_", " ")) or area_name
                    votes = int(area.get("votes") or 0)
                    per = result.by_area.setdefault(area_name, {})
                    per[label] = per.get(label, 0) + votes
        contests.append(result)
    name_el, time_el = root.find("ElectionName"), root.find("Timestamp")
    return Results(
        scope=scope,
        election_name=name_el.text if name_el is not None else None,
        timestamp=time_el.text if time_el is not None else None,
        ballots_cast=ballots,
        contests=contests,
        areas_are_counties=counties_by_area or scope == "state",
    )


def parse_detail_xlsx(data: bytes, scope: str = "state") -> Results:
    """Read Clarity's ``detail.xlsx`` (or its zip): a "Registered Voters" sheet and a sheet per contest.

    Each contest sheet has the contest's title (with "(Vote For N)") in its
    first row, the choices' names in the next, starting the columns of each
    choice's vote types, whose last is the choice's total, then a row of
    column names (County or Precinct, Registered Voters, ..., Total) and a
    row per area.

    Raises:
        ResultsError: if it is not such a workbook.
    """
    book = data
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if "xl/workbook.xml" not in archive.namelist():
            book = _unzip(data, ".xlsx")
    reader = XlsxReader(book)
    names = reader.sheet_names
    if "Registered Voters" not in names:
        raise ResultsError("not a Clarity detail workbook (no 'Registered Voters' sheet)")
    ballots: dict[str, int] = {}
    for row in reader.rows("Registered Voters"):
        county = canonical_county(row[0]) if row else None
        if county and len(row) > 2 and row[2].strip().isdigit():
            ballots[county] = int(row[2])
    contests = []
    for sheet in names:
        if sheet in ("Table of Contents", "Registered Voters"):
            continue
        rows = list(reader.rows(sheet))
        if len(rows) < 4 or not rows[0] or not rows[0][0].strip():
            continue
        name, vote_for = contest_label(rows[0][0])
        header = rows[2]
        total_col = max((i for i, c in enumerate(header) if c.strip() == "Total"), default=len(header))
        starts = [(i, c.strip()) for i, c in enumerate(rows[1]) if c.strip() and i < total_col]
        spans = [(label, (starts[k + 1][0] if k + 1 < len(starts) else total_col) - 1) for k, (_, label) in enumerate(starts)]
        result = ContestResult(name, vote_for)
        for row in rows[3:]:
            if not row or not row[0].strip() or row[0].strip().lower() == "total:":
                continue
            area = canonical_county(row[0]) or row[0].strip()
            for label, last in spans:
                cell = row[last].strip() if last < len(row) else ""
                if cell.isdigit():
                    per = result.by_area.setdefault(area, {})
                    per[label] = per.get(label, 0) + int(cell)
                    result.totals[label] = result.totals.get(label, 0) + int(cell)
        contests.append(result)
    return Results(scope, None, None, ballots, contests, areas_are_counties=True)


def parse_summary_json(data: bytes, scope: str) -> Results:
    """Read Clarity's ``json/en/summary.json`` (gzip or not): each contest's choices (``CH``), votes (``V``) and ``VF``.

    A county's own ENR election publishes this rather than a detail file;
    it has totals only, and no ballots cast.

    Raises:
        ResultsError: if it is not such a file.
    """
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    try:
        items = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ResultsError(f"unreadable summary: {exc}") from None
    if not isinstance(items, list):
        raise ResultsError("not a Clarity summary (not a list)")
    contests = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("C"), str):
            continue
        name, _ = contest_label(item["C"])
        choices, votes = item.get("CH") or [], item.get("V") or []
        raw_vote_for = item.get("VF")
        vote_for = raw_vote_for if isinstance(raw_vote_for, int) and raw_vote_for > 0 else 1
        result = ContestResult(name, vote_for)
        for choice, n in zip(choices, votes, strict=False):
            if isinstance(n, int):
                result.totals[str(choice)] = result.totals.get(str(choice), 0) + n
        contests.append(result)
    return Results(scope, None, None, {}, contests, areas_are_counties=False)


def parse_summary_csv(data: bytes, scope: str = "state") -> Results:
    """Read Clarity's ``summary.csv`` (or its ``summary.zip``): a row per choice, with its contest and total votes.

    It has totals only, no county breakdown and no ballots cast of its own
    (its "ballots cast" column is not the state's).

    Raises:
        ResultsError: if it is not such a file.
    """
    raw = _unzip(data, ".csv")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fields = {(f or "").strip().lower(): f for f in reader.fieldnames or []}
    if not {"contest name", "choice name", "total votes"} <= set(fields):
        raise ResultsError("not a Clarity summary.csv (no contest name, choice name and total votes columns)")
    contests: dict[str, ContestResult] = {}
    try:
        for row in reader:
            title = (row.get(fields["contest name"]) or "").strip()
            votes = (row.get(fields["total votes"]) or "").strip()
            if not title or not votes.isdigit():
                continue
            name, vote_for = contest_label(title)
            result = contests.setdefault(name, ContestResult(name, vote_for))
            choice = (row.get(fields["choice name"]) or "").strip()
            result.totals[choice] = result.totals.get(choice, 0) + int(votes)
    except csv.Error as exc:
        raise ResultsError(f"unreadable summary.csv: {exc}") from None
    return Results(scope, None, None, {}, list(contests.values()), areas_are_counties=False)


# ---------------------------------------------------------------------------
# When a file was generated
#
# Clarity's detail.xml says when it was generated (``<Timestamp>7/23/2026
# 4:21:16 PM MDT</Timestamp>``). Its detail.xlsx says so only in its
# document properties (``dcterms:created``), written by its spreadsheet
# library as Clarity's server's wall-clock time, US Eastern, though marked
# ``Z``: the 2026 primary's detail.xml of 23 July says 4:21:16 PM MDT, its
# detail.xlsx 18:21:16Z, and the zips' member dates 18:21 (Clarity's
# electionsettings.json says ``"timezoneoffset": -2`` from there). A
# summary.csv has no time of its own but its zip member's date (Eastern
# too, to the minute); a summary.json has none.


def _eastern_wall_to_utc(naive: datetime) -> str:
    """A US Eastern wall-clock time (Colorado's two hours later, with the same daylight rules) as a stored UTC time."""
    mountain = naive - timedelta(hours=2)
    return format_server_time(mountain.replace(tzinfo=colorado_offset(mountain)).astimezone(UTC))


def parse_clarity_time(text: str) -> str | None:
    """A Clarity time such as ``7/23/2026 4:21:16 PM MDT`` as a stored UTC time; Colorado time if it names no zone; None if not a time."""
    match = _CLARITY_TIME_RE.search(text or "")
    if not match:
        return None
    month, day, year, hour, minute, second, half, zone = match.groups()
    h = int(hour)
    if half:
        h = h % 12 + (12 if half.upper() == "PM" else 0)
    try:
        naive = datetime(int(year), int(month), int(day), h, int(minute), int(second or 0))
    except ValueError:
        return None
    offset = _ZONES.get((zone or "").upper())
    tz = timezone(timedelta(hours=offset)) if offset is not None else colorado_offset(naive)
    return format_server_time(naive.replace(tzinfo=tz).astimezone(UTC))


def _xlsx_created(book: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(book)) as archive:
            if "docProps/core.xml" not in archive.namelist():
                return None
            core = archive.read("docProps/core.xml")[:65536].decode("utf-8", errors="replace")
    except zipfile.BadZipFile:
        return None
    match = re.search(r"<dcterms:created[^>]*>(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", core)
    if not match:
        return None
    try:
        y, mo, d, h, mi, se = (int(g) for g in match.groups())
        return _eastern_wall_to_utc(datetime(y, mo, d, h, mi, se))
    except ValueError:
        return None


def results_file_time(data: bytes) -> tuple[str | None, str]:
    """When Clarity generated a results file, from the file itself (UTC), and how that was found; ``(None, "")`` if it does not say."""
    if data.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
                if "xl/workbook.xml" in names:
                    found = _xlsx_created(data)
                    return (found, "the workbook's creation time (Clarity's Eastern time)") if found else (None, "")
                for name in names:
                    info = archive.getinfo(name)
                    lower = name.lower()
                    if info.file_size > MAX_DETAIL_BYTES:
                        continue
                    if lower.endswith(".xml"):
                        with archive.open(name) as member:
                            return results_file_time(member.read(4096))
                    if lower.endswith(".xlsx"):
                        found = _xlsx_created(archive.read(name))
                        return (found, "the workbook's creation time (Clarity's Eastern time)") if found else (None, "")
                    if lower.endswith(".csv"):
                        return _eastern_wall_to_utc(datetime(*info.date_time)), "the zip's date for its summary.csv (Clarity's Eastern time, to the minute)"
        except (zipfile.BadZipFile, ValueError):
            return None, ""
        return None, ""
    match = re.search(rb"<Timestamp>([^<]{1,64})</Timestamp>", data[:4096])
    if match:
        found = parse_clarity_time(match.group(1).decode("utf-8", errors="replace"))
        if found:
            return found, f"the file's Timestamp ({match.group(1).decode('utf-8', errors='replace').strip()})"
    return None, ""


def _form(data: bytes) -> str:
    """The form of a results file, by content (`FORMS`), or "" if it is none of them."""
    if data.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = [n.lower() for n in archive.namelist()]
        except zipfile.BadZipFile:
            raise ResultsError("a damaged zip file") from None
        if "xl/workbook.xml" in names:
            return "detail.xlsx"
        if any(n.endswith(".xlsx") for n in names):
            return "detailxlsx.zip"
        if any(n.endswith(".xml") for n in names):
            return "detailxml.zip"
        if any(n.endswith(".csv") for n in names):
            return "summary.zip"
        return ""
    if data[:2] == b"\x1f\x8b" or data.lstrip()[:1] == b"[":
        return "summary.json"
    head = data.lstrip(b"\xef\xbb\xbf \t\r\n")
    if head[:1] == b"<":
        return "detail.xml"
    if head[:20].lower().replace(b'"', b"").startswith(b"line number"):
        return "summary.csv"
    return ""


def results_form(data: bytes) -> str:
    """The form of a results file by its content (`FORMS`), or "" if it is none of them (or a damaged zip)."""
    try:
        return _form(data)
    except ResultsError:
        return ""


_REGION_RE = re.compile(rb"<Region>([^<]{1,100})</Region>")
_ELECTION_NAME_RE = re.compile(rb"<ElectionName>([^<]{1,300})</ElectionName>")


def detail_header(data: bytes) -> dict[str, str]:
    """The ``Region`` and ``ElectionName`` a ``detail.xml`` (or its ``detailxml.zip``) gives in its first lines, where it does ("" otherwise).

    A county's own ENR election names the county in them; the state's says
    ``CO`` or ``Colorado``.
    """
    head = data
    if data.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = [n for n in archive.namelist() if n.lower().endswith(".xml")]
                if not names:
                    return {}
                with archive.open(names[0]) as member:
                    head = member.read(8192)
        except (zipfile.BadZipFile, OSError, ValueError):
            return {}
    found = {}
    for key, pattern in (("region", _REGION_RE), ("election_name", _ELECTION_NAME_RE)):
        match = pattern.search(head[:8192])
        if match:
            found[key] = match.group(1).decode("utf-8", "replace").strip()
    return found


def read_results(data: bytes, scope: str = "state") -> Results:
    """A results file in any of its forms (`FORMS`), by content: ``detail.xml``, ``detail.xlsx``, either zipped, ``summary.csv`` (or ``summary.zip``) and ``summary.json``.

    The result says its form and, where the file says, when it was generated.

    Raises:
        ResultsError: if it is none of these.
    """
    form = _form(data)
    if form in ("detail.xlsx", "detailxlsx.zip"):
        results = parse_detail_xlsx(data, scope)
    elif form in ("detail.xml", "detailxml.zip"):
        results = parse_detail(data, scope)
    elif form in ("summary.csv", "summary.zip"):
        results = parse_summary_csv(data, scope)
    elif form == "summary.json":
        results = parse_summary_json(data, scope)
    else:
        raise ResultsError(
            "not a Clarity results file: expected detail.xml, detail.xlsx (either may be zipped), summary.csv (or summary.zip) or summary.json"
        )
    results.form = form
    results.generated, results.generated_basis = results_file_time(data)
    return results


# ---------------------------------------------------------------------------
# Margins


def margin_of(votes: Mapping[str, int], vote_for: int) -> int:
    """CORLA's minimum margin: the last winner's votes minus the first loser's (0 for a loser that is absent)."""
    ordered = sorted(votes.values(), reverse=True)
    if len(ordered) < vote_for:
        return 0
    winner = ordered[vote_for - 1]
    loser = ordered[vote_for] if len(ordered) > vote_for else 0
    return winner - loser


def contest_votes(result: ContestResult, counties: Iterable[str] | None, areas_are_counties: bool) -> dict[str, int]:
    """A contest's votes per choice over ``counties`` (all of it with None, or in a county's own file)."""
    if counties is None or not areas_are_counties:
        return dict(result.totals)
    wanted = {county_key(c) for c in counties}
    votes: dict[str, int] = {}
    for area, per in result.by_area.items():
        if county_key(area) in wanted:
            for choice, n in per.items():
                votes[choice] = votes.get(choice, 0) + n
    return votes


def find_contest(results: Results, name: str, enr_name: str | None = None) -> ContestResult | None:
    """The contest named ``enr_name`` (exactly), else the one whose `contest_key` matches ``name``'s."""
    if enr_name:
        return next((c for c in results.contests if c.name == enr_name or contest_key(c.name) == contest_key(enr_name)), None)
    key = contest_key(name)
    return next((c for c in results.contests if contest_key(c.name) == key), None)


# ---------------------------------------------------------------------------
# Fetching


def results_policy(setup: ElectionSetup) -> FetchPolicy:
    """The fetch policy for the results site: Clarity's host only, requests spaced by the setup's ``min_interval``."""
    return FetchPolicy(allowed_hosts=frozenset({CLARITY_HOST}), min_interval=setup.results.min_interval)


def discover_election_id(page: bytes) -> str | None:
    """The first Clarity Colorado election ID a page links to, or None."""
    match = _CLARITY_LINK_RE.search(page.decode("utf-8", errors="replace"))
    return match.group(1) if match else None


def county_path(county: str) -> str:
    """Clarity's path segment for a county (``La_Plata``)."""
    return county.replace(" ", "_")


def parse_settings(data: bytes) -> dict[str, tuple[str, str]]:
    """Each county's own ENR election ``(ID, version)``, from ``electionsettings.json`` (gzip or not)."""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    try:
        settings = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ResultsError(f"unreadable election settings: {exc}") from None
    details = settings.get("settings", {}).get("electiondetails", {}) if isinstance(settings, dict) else {}
    found: dict[str, tuple[str, str]] = {}
    for item in details.get("participatingcounties") or []:
        parts = str(item).split("|")
        county = canonical_county(parts[0].replace("_", " ")) if parts else None
        if county and len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
            found[county] = (parts[1], parts[2])
    return found


@dataclass
class GatherReport:
    """What `gather` did."""

    election_id: str | None = None
    version: str | None = None
    changed: bool = False
    fetched: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FetchedResults:
    """A recorded results file's facts: a ``results.fetched`` event's, or a ``results.uploaded`` event's (``uploaded``)."""

    seq: int
    url: str
    scope: str
    what: str
    election_id: str | None
    version: str | None
    sha256: str
    #: When it was fetched or received (UTC; a rehearsal's simulated time).
    time: str
    uploaded: bool = False
    actor: str = ACTOR
    #: When Clarity generated it (UTC), from the file itself or, for an upload, as the coordinator gave it; None if not known.
    generated: str | None = None
    generated_basis: str = ""
    form: str = ""
    original_name: str = ""
    note: str = ""

    @property
    def rank_time(self) -> str:
        """The time it ranks by: when it was generated, else when it was fetched or received."""
        return self.generated or self.time

    @property
    def origin(self) -> str:
        """``fetched``, or ``uploaded by hand by <actor>``."""
        return f"uploaded by hand by {self.actor}" if self.uploaded else "fetched"


def fetched_results(log: EventLog) -> list[FetchedResults]:
    """Every ``results.fetched`` event, in log order (never an upload: `gather` decides what to fetch from these)."""
    found = []
    for event in log.iter_events():
        if event.kind == "results.fetched" and event.file_sha256:
            found.append(_fetched(event))
    return found


def _fetched(event: Event) -> FetchedResults:
    d = event.data
    simulated = d.get("simulated")
    moment = str(simulated["time"]) if isinstance(simulated, dict) and isinstance(simulated.get("time"), str) else event.server_time
    generated = d.get("generated")
    return FetchedResults(
        event.seq,
        str(d.get("url") or d.get("source_url") or ""),
        str(d.get("scope")),
        str(d.get("what")),
        str(d["election_id"]) if d.get("election_id") else None,
        str(d["version"]) if d.get("version") else None,
        str(event.file_sha256),
        moment,
        uploaded=event.kind == "results.uploaded",
        actor=event.actor,
        generated=generated if isinstance(generated, str) else None,
        generated_basis=str(d.get("generated_basis") or ""),
        form=str(d.get("form") or ""),
        original_name=str(d.get("original_name") or ""),
        note=str(d.get("note") or ""),
    )


_TIME_CACHE: dict[str, tuple[str | None, str]] = {}


def results_files(log: EventLog, workspace: Workspace | None = None) -> list[FetchedResults]:
    """Every results file recorded (detail or summary), fetched or uploaded, in log order.

    A file recorded before its generated time was kept in the event gets it
    from the file itself, with ``workspace``.
    """
    found = []
    for event in log.iter_events():
        if event.kind not in ("results.fetched", "results.uploaded") or not event.file_sha256:
            continue
        item = _fetched(event)
        if item.what not in ("detail", "summary"):
            continue
        if item.generated is None and "generated" not in event.data and workspace is not None:
            if item.sha256 not in _TIME_CACHE:
                try:
                    _TIME_CACHE[item.sha256] = results_file_time(workspace.get_bytes(item.sha256))
                except (ObjectNotFoundError, ValueError):
                    _TIME_CACHE[item.sha256] = (None, "")
            when, basis = _TIME_CACHE[item.sha256]
            item = replace(item, generated=when, generated_basis=basis)
        found.append(item)
    return found


def ranked(items: Iterable[FetchedResults]) -> list[FetchedResults]:
    """Results files of one scope, the one used first: a file with a county breakdown (a detail) before a
    summary, then the newest by `FetchedResults.rank_time`, a fetched one before an upload of the same
    time, and then the later recorded."""
    return sorted(items, key=lambda i: (i.what == "detail", i.rank_time, not i.uploaded, i.seq), reverse=True)


def results_by_scope(log: EventLog, workspace: Workspace | None = None) -> dict[str, list[FetchedResults]]:
    """Each scope's results files, `ranked`, each content once (its best-ranked record)."""
    scopes: dict[str, list[FetchedResults]] = {}
    for item in results_files(log, workspace):
        scopes.setdefault(item.scope, []).append(item)
    out = {}
    for scope, items in scopes.items():
        seen: set[str] = set()
        out[scope] = [i for i in ranked(items) if not (i.sha256 in seen or seen.add(i.sha256))]  # type: ignore[func-returns-value]
    return out


def latest_details(log: EventLog, workspace: Workspace | None = None) -> dict[str, FetchedResults]:
    """The results file used for each scope (the first `ranked`)."""
    return {scope: items[0] for scope, items in results_by_scope(log, workspace).items() if items}


def load_results(workspace: Workspace, log: EventLog) -> dict[str, tuple[Results, FetchedResults]]:
    """The results used per scope, read, with the record they came from: the first readable file `ranked`."""
    loaded = {}
    for scope, items in results_by_scope(log, workspace).items():
        for item in items:
            try:
                loaded[scope] = (read_results(workspace.get_bytes(item.sha256), scope), item)
                break
            except (ResultsError, ObjectNotFoundError, ValueError):
                continue
    return loaded


def record_results_file(
    workspace: Workspace,
    log: EventLog,
    data: bytes,
    *,
    url: str,
    scope: str,
    what: str,
    election_id: str | None,
    version: str | None,
    status: int = 200,
    headers: Mapping[str, str] | None = None,
    simulated: Mapping[str, JSONValue] | None = None,
    actor: str = ACTOR,
) -> int:
    """Store a results file and record a ``results.fetched`` event; return its seq.

    A detail or summary is recorded with its form and, if it says, when it
    was generated (`results_file_time`).
    """
    stored = workspace.put_bytes(data)
    payload: dict[str, JSONValue] = {
        "url": url,
        "scope": scope,
        "what": what,
        "election_id": election_id,
        "version": version,
        "status": status,
        "sha256": stored.sha256,
        "size": stored.size,
        "headers": dict(headers or {}),
    }
    if what in ("detail", "summary"):
        try:
            payload["form"] = _form(data)
        except ResultsError:
            payload["form"] = ""
        payload["generated"], payload["generated_basis"] = results_file_time(data)
    if simulated is not None:
        payload["simulated"] = dict(simulated)
    return log.append("results.fetched", actor, data=payload, file_sha256=stored.sha256).seq


@dataclass(frozen=True)
class ResultsUpload:
    """What happened to a results file uploaded by hand."""

    accepted: bool
    scope: str
    original_name: str
    sha256: str | None
    size: int
    form: str = ""
    reason: str | None = None
    generated: str | None = None
    generated_basis: str = ""
    contests: int = 0
    ballots_cast: int = 0
    seq: int | None = None
    notes: tuple[str, ...] = ()


def _upload_scope(scope: str | None) -> str | None:
    if scope is None or scope.strip() == "":
        return None
    if scope.strip().lower() == "state":
        return "state"
    county = canonical_county(scope)
    if county is None:
        raise ResultsError(f"the scope must be 'state' or a Colorado county, not {scope!r}")
    return county


def receive_results(
    workspace: Workspace,
    log: EventLog,
    data: bytes,
    original_name: str,
    actor: str,
    *,
    scope: str | None = None,
    source_url: str = "",
    note: str = "",
    file_time: str | None = None,
    simulated: Mapping[str, JSONValue] | None = None,
) -> ResultsUpload:
    """Receive a results file downloaded by hand: identify it by content, store it by hash and record ``results.uploaded``.

    ``scope`` is ``state`` or the county whose results it is; a detail file
    is the state's by default, and a summary (which does not say whose it
    is) needs it. ``source_url`` or ``note`` says where it came from (one
    is required). ``file_time`` is when Clarity generated it, used only for a
    file that does not say so itself (a summary.json), as Colorado time
    (``2026-11-04 21:40``) or with an offset. Anything that is not a results
    file, or whose content contradicts the scope, is refused, recorded as
    ``results.upload_rejected`` (with its hash and size) and not stored.

    Raises:
        ResultsError: for an invalid actor or scope, or neither a source URL nor a note, before anything is read.
    """
    from corla_observer.localtime import parse_instant

    try:
        check_actor(actor)
    except ValueError as exc:
        raise ResultsError(str(exc)) from None
    wanted = _upload_scope(scope)
    source_url, note = source_url.strip()[:500], note.strip()[:1000]
    if not source_url and not note:
        raise ResultsError("say where the file came from: a source URL or a note")
    given_time = None
    if file_time and file_time.strip():
        text = file_time.strip()
        moment = parse_instant(text.replace(" ", "T", 1)) if re.search(r"(Z|[+-]\d\d:?\d\d)$", text) else None
        if moment is not None:
            given_time = format_server_time(moment.astimezone(UTC))
        else:
            try:
                naive = datetime.fromisoformat(text)
            except ValueError:
                given_time = parse_clarity_time(text)
            else:
                given_time = format_server_time(naive.replace(tzinfo=colorado_offset(naive)).astimezone(UTC))
        if given_time is None:
            raise ResultsError(f"not a time: {file_time!r} (give it as 2026-11-04 21:40, Colorado time)")
    name = "".join(ch for ch in original_name.replace("\\", "/").rsplit("/", 1)[-1] if ch.isprintable()).strip()[:255] or "(unnamed)"
    sha256 = hashlib.sha256(data).hexdigest()

    def reject(reason: str, form: str = "") -> ResultsUpload:
        event = log.append(
            "results.upload_rejected", actor, county=wanted if wanted not in (None, "state") else None,
            data={"original_name": name, "scope": wanted, "reason": reason, "sha256": sha256, "size": len(data), "form": form}
            | ({"simulated": dict(simulated)} if simulated is not None else {}),
        )  # fmt: skip
        return ResultsUpload(False, wanted or "", name, sha256, len(data), form, reason, seq=event.seq)

    if len(data) > MAX_RESULTS_UPLOAD_BYTES:
        return reject(f"larger than {MAX_RESULTS_UPLOAD_BYTES} bytes")
    try:
        form = _form(data)
    except ResultsError as exc:
        return reject(str(exc))
    if not form:
        return reject("not a Clarity results file: expected detail.xml, detail.xlsx (either may be zipped), summary.csv (or summary.zip) or summary.json")
    if wanted is None:
        if form.startswith("summary"):
            return reject(f"a {form} does not say whose results it is: choose the state or the county", form)
        wanted = "state"
    try:
        results = read_results(data, wanted)
    except (ResultsError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        return reject(f"not readable as {form}: {exc}", form)
    if not results.contests:
        return reject(f"no contests found in the {form}", form)
    counties = len(results.ballots_cast)
    if wanted != "state" and form.startswith("detail") and counties > 1:
        return reject(f"this file has results for {counties} counties: it is the state's, not {wanted}'s", form)
    if wanted == "state" and form.startswith("detail") and not results.areas_are_counties:
        return reject("this file's results are by precinct, not by county: it is a county's; choose its county", form)
    notes = []
    generated, basis = results.generated, results.generated_basis
    if generated is None and given_time is not None:
        generated, basis = given_time, f"as given by {actor} (the file does not say)"
    elif generated is not None and given_time is not None and given_time != generated:
        notes.append("the time given was not used: the file says when it was generated")
    if generated is None:
        notes.append("the file does not say when it was generated: it ranks by when it was uploaded")
    stored = workspace.put_bytes(data)
    payload: dict[str, JSONValue] = {
        "uploaded": True,
        "scope": wanted,
        "what": "detail" if form.startswith("detail") else "summary",
        "form": form,
        "original_name": name,
        "source_url": source_url,
        "note": note,
        "sha256": stored.sha256,
        "size": stored.size,
        "generated": generated,
        "generated_basis": basis,
        "election_name": results.election_name,
        "contests": len(results.contests),
        "ballots_cast": sum(results.ballots_cast.values()),
    }
    if stored.already_present:
        payload["already_present"] = True
    if simulated is not None:
        payload["simulated"] = dict(simulated)
    county = wanted if wanted != "state" else None
    seq = log.append("results.uploaded", actor, county=county, data=payload, file_sha256=stored.sha256).seq
    return ResultsUpload(
        True, wanted, name, stored.sha256, stored.size, form, None, generated, basis,
        len(results.contests), sum(results.ballots_cast.values()), seq, tuple(notes),
    )  # fmt: skip


def gather(
    workspace: Workspace,
    log: EventLog,
    setup: ElectionSetup,
    *,
    counties: Sequence[str] = (),
    fetcher: Fetcher | None = None,
    discover_fetcher: Fetcher | None = None,
) -> GatherReport:
    """Fetch the state's results, and ``counties``' own, when their versions have changed; see the module documentation."""
    report = GatherReport()
    fetcher = fetcher or Fetcher(results_policy(setup))
    base = setup.results.base_url
    election_id = setup.results.election_id or _discovered(log)
    if not election_id:
        discover = discover_fetcher or Fetcher()
        for page in setup.results.discover_from:
            try:
                result = discover.fetch(page)
            except FetchError as exc:
                report.notes.append(f"could not fetch {page}: {exc}")
                continue
            found = discover_election_id(result.body) if result.ok else None
            if found:
                election_id = found
                log.append("results.discovered", ACTOR, data={"election_id": found, "page": page})
                break
        if not election_id:
            report.notes.append("the results site's election ID is not known yet: set [results] election_id")
            return report
    report.election_id = election_id
    previous = fetched_results(log)

    def get(url: str, scope: str, what: str, version: str | None) -> bytes | None:
        try:
            result = fetcher.fetch(url)
        except FetchError as exc:
            log.append("results.fetch_failed", ACTOR, data={"url": url, "scope": scope, "what": what, "error": str(exc)[:500]})
            report.failed.append(url)
            return None
        if not result.ok:
            log.append("results.fetch_failed", ACTOR, data={"url": url, "scope": scope, "what": what, "error": f"HTTP {result.status}"})
            report.failed.append(url)
            return None
        record_results_file(
            workspace, log, result.body, url=url, scope=scope, what=what,
            election_id=election_id, version=version, status=result.status, headers=result.headers,
        )  # fmt: skip
        report.fetched.append(url)
        return result.body

    body = get(f"{base}{election_id}/current_ver.txt", "state", "version", None)
    if body is None:
        return report
    version = body.decode("ascii", errors="replace").strip()
    if not version.isdigit():
        report.notes.append(f"unexpected version text {version[:40]!r}")
        return report
    report.version = version
    have = {(p.scope, p.what): p.version for p in previous if p.election_id == election_id}
    if have.get(("state", "detail")) != version:
        report.changed = True
        get(f"{base}{election_id}/{version}/reports/detailxml.zip", "state", "detail", version)
    county_versions: dict[str, tuple[str, str]] = {}
    if counties:
        settings_body = None
        if have.get(("state", "settings")) != version:
            settings_body = get(f"{base}{election_id}/{version}/json/en/electionsettings.json", "state", "settings", version)
        else:
            last = next(p for p in reversed(previous) if p.scope == "state" and p.what == "settings" and p.version == version)
            settings_body = workspace.get_bytes(last.sha256)
        if settings_body is not None:
            try:
                county_versions = parse_settings(settings_body)
            except ResultsError as exc:
                report.notes.append(str(exc))
    for county in counties:
        name = canonical_county(county) or county
        if name not in county_versions:
            if counties and county_versions:
                report.notes.append(f"{name} has no results of its own on the results site")
            continue
        county_id, county_version = county_versions[name]
        if have.get((name, "summary")) == county_version:
            continue
        report.changed = True
        get(f"{base}{county_path(name)}/{county_id}/{county_version}/json/en/summary.json", name, "summary", county_version)
    return report


def results_counties(datamap: DataMap, setup: ElectionSetup) -> list[str]:
    """The counties whose own results `gather` fetches: those of one-county targeted contests, the pilot counties' only by default."""
    mode = setup.results.county_contests
    if mode == "none":
        return []
    counties = {spec.counties[0] for spec in datamap.contests if len(spec.counties) == 1 and not spec.statewide}
    if mode == "pilots":
        counties &= set(setup.pilot_counties)
    return sorted(counties)


def _discovered(log: EventLog) -> str | None:
    found = None
    for event in log.iter_events():
        if event.kind == "results.discovered" and isinstance(event.data.get("election_id"), str):
            found = str(event.data["election_id"])
    return found
