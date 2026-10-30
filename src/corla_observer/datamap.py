"""Each election's data map: which file is which, and every adjustment made to it, in one place.

The audit center's layout, names and formats change from election to
election, and a file can turn out at the last minute to be wrong, missing or
in an unexpected shape. The data map puts everything a sampling run needs in
one deterministic structure built from the event log, and lets a coordinator
(or an AI assistant helping one) adjust it through one small, reviewable
file, without changing code.

**What goes in.** `build_map` reads the workspace's event log:

- every fetched audit center file (``fetch.file``, ``fetch.classified``,
  ``fetch.missing``), by URL, with the election its URL names;
- every county upload (``file.received``, ``file.classified``), the latest
  of each kind per county, and the audit board counts;
- seeds entered by hand by coordinators (``seed.entered``, and optional
  second entries, ``seed.second_entry``; see `corla_observer.seeds`), of
  which the latest entry is used;
- the overrides last applied (``map.overrides_applied``), with the files
  they supplied (``map.file_supplied``) and the adapter outputs they produced
  (``map.adapter_ran``).

Each file becomes a `MapEntry` with its kind, county and election, as the
classifier saw them, then as the overrides set them, with the reason for
every change.

**What comes out.** A `DataMap`: the entries; the manifest chosen for each
county; the seed; the county-ID order that combines counties into
multi-county domains; the targeted contests with their counties and sample
counts; the manifest adjustments and county-name aliases; and the problems
found, each with a hint about what to do. `DataMap.check` lists what is still
missing for a sampling run. Each value records where it came from.

How the map decides, before overrides:

- **Election.** The ``election`` override, else the election most fetched
  data files' URLs name (``/RLA/2026/primary/...`` is ``2026/primary``).
  Files whose URL names another election are not used.
- **Manifests.** Every entry of kind ``county_manifest`` is loaded with
  `corla_observer.sampling.load_county_manifest`. Identical content is used
  once. Two different files for one county that list the same batches (as
  when a county's own copy differs from the audit center's only in quoting)
  are used together, the audit center's copy being preferred. When they list
  different batches (``conflicting_manifests``, a warning), the one
  byte-identical to the manifest CORLA recorded importing is used if there is
  one, else the one obtained most recently (fetched, uploaded or supplied:
  `MapEntry.obtained`), and the problem says which. A fetched file whose
  name suggests one county and whose content names another is reported.
- **Seed.** The latest seed a coordinator entered (``seed.entered``,
  `corla_observer.seeds`), else a seed file, else a State or county audit
  report. An optional second entry that did not match the entry in effect
  is a problem until a new entry is made. Different values are a conflict.
- **Roll time.** When the seed was rolled, for flagging files that change
  afterwards (`corla_observer.flags`): the earliest known of the first
  ``seed.entered`` event, a roll time a coordinator recorded
  (``audit.time_recorded``, see `record_audit_time`) and the ``[roll_time]``
  override (a scheduled roll time). The first round's start is recorded the
  same way, or by the ``[round_start]`` override. Times without a UTC offset
  are Colorado time (`parse_time`).
- **County-ID order.** A ``contest_ids_by_county`` file, else Colorado's
  standard county numbering (`DEFAULT_COUNTY_IDS`), which CORLA's files have
  followed in every election surveyed.
- **Contests.** CORLA's contest lists (``contests_list``: the contests with
  a county-wide or statewide audit reason), with their counties from
  ``contest_ids_by_county``; else the target contest list
  (``target_contests``), whose sample counts are only estimates (see
  `corla_observer.estimate`). Each contest list's round comes from its
  URL or, for a list with nothing audited yet, is round one
  (`contest_list_round`); each gives its round's cumulative draws,
  ``max(optimistic + 1, audited)`` (`round_target`), so lists may arrive
  in any order or several at once. The sample count is round one's. The
  target list's denominator and cards per ballot (from its remarks) are
  kept on each contest for the estimates.

**Overrides.** The workspace's ``overrides.toml`` (`OVERRIDES_FILENAME`),
specified in `OVERRIDES_FORMAT_HELP`: reassign a file's kind or county,
ignore a file, supply a missing file (by path or HTTPS URL, with its
SHA-256), run a named adapter, set the seed or the county-ID order, add
manifest adjustments with their evidence, county-name aliases, and add,
change or exclude contests. Every item needs a ``reason``. `apply_overrides`
checks the whole file, stores it in the workspace, and records it (its text,
its SHA-256, who applied it and every item's reason) in one
``map.overrides_applied`` event; nothing is applied otherwise. The map is
always built from the overrides last *applied*, so it can be recomputed by
anyone holding the log and the workspace. Editing the file changes nothing
until it is applied; `build_map` with ``preview`` shows the effect first.

**Adapters.** A small Python module in the workspace's ``adapters/``
directory (`ADAPTERS_DIRNAME`), named by an ``[[adapter]]`` override, with a
function ``convert(data: bytes, context: dict) -> bytes | str`` that turns
one file's content into another, such as an unexpected manifest layout into
the usual one. Only modules named in applied overrides are ever run, only
from that directory, and only by `apply_overrides`. Each run is recorded
(``map.adapter_ran``) with the SHA-256 of the module's source, of its input
and of its output; the source and the output are stored in the workspace.
Adapters run in this process with no sandbox: they are code the coordinator
wrote or reviewed, and are kept as small as possible.

Events written here (``data`` always JSON):

``map.overrides_applied``
    ``sha256``, ``size``, ``text`` (if short enough), ``items`` (section,
    index and reason of each), ``supplied`` and ``adapters`` (their names and
    hashes). ``file_sha256`` is the overrides file's.
``map.overrides_rejected``
    ``sha256`` and ``errors``: an overrides file that did not check out.
``map.file_supplied``
    ``name``, ``sha256``, ``size``, ``source`` (``path`` or ``url``),
    ``url`` or ``path_name`` (the file's name only, never a local path),
    ``reason``.
``map.adapter_ran``
    ``module``, ``function``, ``source_sha256``, ``input``,
    ``input_sha256``, ``output_name``, ``output_sha256``, ``output_size``.
``seed.entered`` and the other seed events
    See `corla_observer.seeds`.
``audit.time_recorded``
    ``which`` (``roll`` or ``round_start``), ``time`` (UTC), ``given`` (as
    entered) and ``source``.

Standard library only.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import re
import tomllib
import urllib.parse
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from corla_observer.classify import (
    COLORADO_COUNTIES,
    KINDS,
    Classification,
    XlsxError,
    XlsxReader,
    canonical_county,
    classify_bytes,
    county_from_filename,
    decode_text,
    is_test_county,
    normalize_header,
)
from corla_observer.events import Event, EventLog, JSONValue, check_actor, format_server_time
from corla_observer.localtime import colorado_offset, colorado_time, colorado_wall_time
from corla_observer.sampling import (
    ManifestAdjustment,
    ManifestBatch,
    apply_manifest_adjustment,
    county_key,
    parse_county_manifest,
)
from corla_observer.seeds import SEED_EVENT_KINDS, SeedState, enter_seed, seed_state
from corla_observer.workspace import ObjectNotFoundError, Workspace

if TYPE_CHECKING:
    from corla_observer.due import ProblemView
    from corla_observer.flags import Flag

OVERRIDES_FILENAME = "overrides.toml"
ADAPTERS_DIRNAME = "adapters"
#: The overrides file format this module reads (its ``format`` key).
OVERRIDES_FORMAT = 1

#: Colorado's standard county numbering, which CORLA uses as county IDs:
#: alphabetical by name ignoring spaces (so Elbert 20 before El Paso 21, and
#: Lake 33 before La Plata 34), with Broomfield, the newest county, 64.
#: CORLA's ContestIDsByCounty.csv and its predecessors agree with it in the
#: 2019, 2022 and 2026 files surveyed (docs/AUDITCENTER_FORMATS.md).
DEFAULT_COUNTY_IDS: dict[str, int] = {
    name: index
    for index, name in enumerate(
        sorted((c for c in COLORADO_COUNTIES if c != "Broomfield"), key=county_key), start=1
    )
} | {"Broomfield": 64}

#: CORLA audit reasons of the contests it draws a sample for.
TARGETED_REASONS = frozenset({"county_wide_contest", "state_wide_contest"})

#: Source priority among different manifests for one county that agree.
_SOURCE_PRIORITY = {"fetch": 0, "supplied": 1, "adapter": 2, "upload": 3}

#: Kinds whose single-file data the map uses, and the kinds an upload maps to.
_UPLOAD_KIND = {"redacted_cvr": "cvr", "manifest": "county_manifest", "pull_list": "pull_list"}

_ELECTION_RE = re.compile(
    r"/RLA/(?:files/)?(?P<year>\d{4}[A-Za-z-]*)(?:/(?P<name>[A-Za-z][A-Za-z0-9-]*))?/", re.IGNORECASE
)
_NOT_ELECTION_NAMES = re.compile(
    r"^(files|round_?\d+|finalreports?|ballotmanifests|ballotpollingaudit|cvrs|boulder)$", re.IGNORECASE
)
_MODULE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA_PREFIX_RE = re.compile(r"^[0-9a-f]{8,64}$")

#: Longest overrides text kept in the ``map.overrides_applied`` event itself
#: (it is always stored in the workspace too).
_MAX_TEXT_IN_EVENT = 32_000
_MAX_REASON = 1000


class OverridesError(ValueError):
    """An overrides file does not check out; ``errors`` lists every problem found."""

    def __init__(self, errors: Sequence[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = list(errors)


def election_from_url(url: str) -> str | None:
    """The election an audit center URL's path names (``2026/primary``), or None.

    Handles both layouts the audit center has used: ``/RLA/2026/primary/files/...``
    and ``/RLA/files/2022/general/round_1/...``. A year with no election name
    under it (``/RLA/2025/files/...``) is just the year.
    """
    match = _ELECTION_RE.search(urllib.parse.urlsplit(url).path)
    if match is None:
        return None
    name = match.group("name")
    if name and not _NOT_ELECTION_NAMES.match(name):
        return f"{match.group('year')}/{name}"
    return match.group("year")


# ---------------------------------------------------------------------------
# Rounds

_ROUND_DIR_RE = re.compile(r"^round_?(\d{1,3})$", re.IGNORECASE)
#: "round" as a word, or "Round" in a CamelCase name (``ContestsListRound1``), then its number.
_ROUND_IN_NAME_RE = re.compile(r"(?:(?<![A-Za-z])(?:round|Round|ROUND)|(?<=[a-z])Round)[_ -]?(\d{1,2})(?!\d)")


def round_of(ref: str) -> int | None:
    """The audit round a file's URL or path names, or None.

    A ``round_2`` or ``round2`` directory, or a name such as
    ``ContestsListRound1.csv`` or ``...-Round 2.csv``; a directory wins over
    the name.
    """
    segments = urllib.parse.unquote(urllib.parse.urlsplit(ref).path).split("/")
    found = None
    for segment in segments[:-1]:
        match = _ROUND_DIR_RE.match(segment)
        if match:
            found = int(match.group(1))
    if found is None and segments:
        match = _ROUND_IN_NAME_RE.search(segments[-1])
        if match:
            found = int(match.group(1))
    return found


# ---------------------------------------------------------------------------
# Times

#: The audit times `record_audit_time` records.
AUDIT_TIMES = ("roll", "round_start")


def parse_time(text: str) -> str:
    """A date and time as UTC in the event log's format (``2026-07-13T16:10:00.000Z``).

    Accepts ISO 8601 with a UTC offset or ``Z`` (``2026-07-13T10:10:00-06:00``),
    or without one, which is then Colorado time (``2026-07-13 10:10``, as in
    CORLA's upload status). Raises ValueError for anything else.
    """
    value = text.strip()
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"not a date and time: {text!r} (use 2026-07-13 10:10 or ISO 8601)") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=colorado_offset(moment))
    return format_server_time(moment.astimezone(UTC))


# ---------------------------------------------------------------------------
# The map


@dataclass(frozen=True)
class Problem:
    """Something wrong or missing in the data map, with a hint about what to do.

    ``severity`` is ``error`` (blocks a sampling run that needs it),
    ``warning`` (needs a person to look) or ``info``.
    """

    severity: str
    code: str
    message: str
    refs: tuple[str, ...] = ()
    hint: str = ""
    county: str | None = None

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "refs": list(self.refs),
            "hint": self.hint,
            "county": self.county,
        }


@dataclass
class MapEntry:
    """One file in the data map.

    ``ref`` identifies it: the URL of a fetched file, ``upload:<County>:<kind>``
    for a county upload, ``supplied:<name>`` or ``adapter:<name>`` for a file
    an override supplied or an adapter produced. ``kind`` and ``county`` are
    as used, after overrides; ``classified_kind`` and ``classified_county``
    as the classifier saw them. ``notes`` says why, in order.
    """

    ref: str
    source: str
    name: str
    sha256: str
    size: int | None
    kind: str
    county: str | None
    election: str | None
    classified_kind: str
    classified_county: str | None
    evidence: dict[str, JSONValue] = field(default_factory=dict)
    ignored: bool = False
    notes: list[str] = field(default_factory=list)
    seq: int | None = None
    simulated: dict[str, JSONValue] | None = None
    #: When this content was obtained at this ref (UTC): fetched (when it last
    #: changed), uploaded, supplied or produced; a rehearsal's simulated time.
    obtained: str | None = None

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "ref": self.ref,
            "source": self.source,
            "name": self.name,
            "sha256": self.sha256,
            "size": self.size,
            "kind": self.kind,
            "county": self.county,
            "election": self.election,
            "classified_kind": self.classified_kind,
            "classified_county": self.classified_county,
            "ignored": self.ignored,
            "notes": list(self.notes),
            "seq": self.seq,
            "obtained": self.obtained,
        }


@dataclass(frozen=True)
class Sourced:
    """A value and where it came from."""

    value: Any
    source: str


@dataclass
class ContestSpec:
    """A targeted contest: its counties, sample count and where they came from.

    ``sample_count`` is the number of draws; ``count_source`` says where it
    came from. For a contest from the target contest list, it is None until
    `corla_observer.predict` estimates it from ``margin`` and ``risk_limit``.
    """

    name: str
    counties: list[str]
    statewide: bool
    sample_count: int | None
    count_source: str
    source: str
    audit_reason: str | None = None
    ballot_card_count: int | None = None
    margin: int | None = None
    risk_limit: str | None = None
    notes: list[str] = field(default_factory=list)
    #: The target list's denominator (its margin over its diluted margin, rounded) and cards per ballot, if it says.
    target_denominator: int | None = None
    target_cards_per_ballot: int | None = None
    #: CORLA's cumulative draws after each round, from each round's contest list (`round_target`).
    round_targets: dict[int, int] = field(default_factory=dict)
    #: ``audited_sample_count`` in the contest list this spec was read from (draws made before its round).
    audited_before: int | None = None

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "name": self.name,
            "counties": list(self.counties),
            "statewide": self.statewide,
            "sample_count": self.sample_count,
            "count_source": self.count_source,
            "source": self.source,
            "audit_reason": self.audit_reason,
            "ballot_card_count": self.ballot_card_count,
            "margin": self.margin,
            "risk_limit": self.risk_limit,
            "notes": list(self.notes),
            "target_denominator": self.target_denominator,
            "target_cards_per_ballot": self.target_cards_per_ballot,
            "round_targets": {str(k): v for k, v in sorted(self.round_targets.items())},
        }


@dataclass
class ChosenManifest:
    """The manifest a county's domain is built from."""

    county: str
    ref: str
    sha256: str
    batches: list[ManifestBatch]
    agrees_with: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ballot_cards(self) -> int:
        return sum(b.ballot_count for b in self.batches)


@dataclass
class DataMap:
    """An election's data map; see the module documentation."""

    election: str | None
    election_source: str
    entries: list[MapEntry]
    manifests: dict[str, ChosenManifest]
    seed: Sourced | None
    county_order: Sourced
    contests: list[ContestSpec]
    adjustments: list[ManifestAdjustment]
    county_aliases: dict[str, str]
    audit_boards: dict[str, Sourced]
    problems: list[Problem]
    #: The applied overrides: their SHA-256, the event seq, and the text.
    overrides_sha256: str | None = None
    overrides_seq: int | None = None
    overrides_text: str | None = None
    #: Whether this map previews an unapplied overrides file.
    preview: bool = False
    #: When the seed was rolled, and when the first round started (UTC), with their sources.
    roll_time: Sourced | None = None
    round_start: Sourced | None = None
    #: Per county, the ballot cards per ballot in the target list's remarks, with the remark; and that list's ref.
    target_cards: dict[str, tuple[int, str]] = field(default_factory=dict)
    target_cards_ref: str | None = None
    #: The applied overrides' estimate inputs supplied by hand (``[[estimate]]`` and ``[[county_estimate]]``).
    estimate_items: list[OverrideItem] = field(default_factory=list)
    #: Each county's uploaded manifest and redacted CVR compared with the audit center's and CORLA's hashes.
    upload_hashes: list[UploadHashCheck] = field(default_factory=list)

    def entry(self, ref: str) -> MapEntry | None:
        return next((e for e in self.entries if e.ref == ref), None)

    def used(self, kind: str) -> list[MapEntry]:
        """The entries of ``kind`` that are not ignored."""
        return [e for e in self.entries if e.kind == kind and not e.ignored]

    def check(self, counties: Iterable[str] = ()) -> list[Problem]:
        """Every problem that blocks or affects a sampling run, errors first.

        With ``counties``, only problems about those counties, the contests
        involving them, and the election as a whole.
        """
        wanted = {county_key(c) for c in counties}
        contests = self.contests_for(counties) if wanted else self.contests
        relevant_counties = {county_key(c) for spec in contests for c in spec.counties} | wanted
        found = []
        for problem in self.problems:
            if wanted and problem.county is not None and county_key(problem.county) not in relevant_counties:
                continue
            found.append(problem)
        return sorted(found, key=lambda p: ({"error": 0, "warning": 1}.get(p.severity, 2), p.code))

    def contests_for(self, counties: Iterable[str]) -> list[ContestSpec]:
        """The statewide contests, and the contests entirely within ``counties``."""
        wanted = {county_key(c) for c in counties}
        return [
            spec
            for spec in self.contests
            if spec.statewide or (spec.counties and {county_key(c) for c in spec.counties} <= wanted)
        ]

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "election": self.election,
            "election_source": self.election_source,
            "preview": self.preview,
            "overrides_sha256": self.overrides_sha256,
            "overrides_seq": self.overrides_seq,
            "seed": None if self.seed is None else {"value": self.seed.value, "source": self.seed.source},
            "roll_time": None if self.roll_time is None else {"value": self.roll_time.value, "source": self.roll_time.source},
            "round_start": (
                None if self.round_start is None else {"value": self.round_start.value, "source": self.round_start.source}
            ),
            "county_order": {"source": self.county_order.source, "ids": dict(self.county_order.value)},
            "manifests": {
                county: {
                    "ref": m.ref,
                    "sha256": m.sha256,
                    "batches": len(m.batches),
                    "ballot_cards": m.ballot_cards,
                    "agrees_with": list(m.agrees_with),
                    "notes": list(m.notes),
                }
                for county, m in sorted(self.manifests.items())
            },
            "contests": [c.to_dict() for c in self.contests],
            "adjustments": [adjustment_to_dict(a) for a in self.adjustments],
            "county_aliases": dict(self.county_aliases),
            "audit_boards": {c: {"count": s.value, "source": s.source} for c, s in sorted(self.audit_boards.items())},
            "entries": [e.to_dict() for e in self.entries],
            "problems": [p.to_dict() for p in self.problems],
            "upload_hashes": [c.to_dict() for c in self.upload_hashes],
        }


def adjustment_to_dict(adjustment: ManifestAdjustment) -> dict[str, JSONValue]:
    def batch(b: ManifestBatch) -> dict[str, JSONValue]:
        return {"tabulator": b.tabulator_id, "batch": b.batch_number, "count": b.ballot_count, "location": b.location}

    return {
        "county": adjustment.county,
        "reason": adjustment.reason,
        "remove": [list(key) for key in adjustment.remove],
        "insert_after": [{"after": list(anchor), **batch(b)} for anchor, b in adjustment.insert_after],
        "append": [batch(b) for b in adjustment.append],
    }


# ---------------------------------------------------------------------------
# Overrides: parsing and checking

#: The overrides file format, for people and AI assistants (also printed by
#: ``corla-observer map show --for-ai``).
OVERRIDES_FORMAT_HELP = """\
The overrides file is TOML, in the workspace as overrides.toml. Every item
needs a "reason": what was wrong and what evidence justifies the change.
Nothing takes effect until `corla-observer map apply` checks the whole file
and records it in the event log. FILE below is a URL, the end of a URL
("files/LaPlata.csv"), a file name, a ref from `map show` (upload:Weld:manifest),
or a SHA-256 (or its first 8 or more hex digits).

format = 1
election = "2026/primary"            # optional; normally inferred from URLs

[[file]]                             # reassign or ignore one file
match = "FILE"
kind = "county_manifest"             # optional: a classifier kind
county = "La Plata"                  # optional
ignore = true                        # optional
reason = "..."

[[supply]]                           # a file from another source
name = "LaPlata_BallotManifest.csv"
path = "relative/or/absolute/path"   # or: url = "https://..."
sha256 = "..."                       # required; checked before use
kind = "county_manifest"             # optional; else classified by content
county = "La Plata"                  # optional
reason = "..."

[[adapter]]                          # run workspace/adapters/<module>.py
match = "FILE"                       # the input file
module = "fix_layout"                # def convert(data: bytes, context: dict) -> bytes | str
function = "convert"                 # optional
name = "Fixed.csv"                   # optional: the output's name
kind = "county_manifest"             # optional; else classified by content
county = "Weld"                      # optional
ignore_input = true                  # optional: stop using the input file
reason = "..."

[seed]
value = "49006417086137856424"
reason = "announced at the public dice roll, 2026-07-13"

[roll_time]                          # the dice roll, e.g. as scheduled
value = "2026-11-16 10:00"           # Colorado time, or ISO 8601 with an offset
reason = "scheduled in the Secretary of State's notice"

[round_start]                        # when the audit's first round started
value = "2026-11-16 13:00"
reason = "..."

[county_order]                       # county IDs for multi-county domains
use = "default"                      # or: ids = { Adams = 1, ... } (all counties used)
reason = "..."

[[county_alias]]                     # a misspelled county name in a file
value = "Prowes"
county = "Prowers"
reason = "..."

[[manifest_adjustment]]              # see corla_observer.sampling.ManifestAdjustment
county = "Alamosa"
append = [{ tabulator = "102", batch = "139", count = 11, location = "" }]
insert_after = [{ after = ["102", "138"], tabulator = "102", batch = "139", count = 11 }]
remove = [["101", "5"]]
evidence = ["finalReports/CountyBallotManifestToCVRcomparison.csv: Alamosa,102,139,11,11,0"]
reason = "..."

[[contest]]                          # add, change or exclude a targeted contest
name = "Attorney General - REP (Weld)"
counties = ["Weld"]                  # optional for an existing contest
statewide = false                    # optional
sample_count = 99                    # optional
exclude = true                       # optional
reason = "..."

# Estimate inputs supplied by hand (corla_observer.estimate), for when no
# published source has them. Each ranks below every audit center source
# (CORLA's contest list, the manifests, the target list) and below the
# election night results: a later posting replaces it by itself.
[[estimate]]                         # one contest's inputs
contest = "Attorney General - Republican Party (Weld)"   # as the data map names it
margin = 1234                        # or: winner_votes = 5000 and loser_votes = 3766
final = false                        # optional: from final results (narrows the low estimate)
enr_contest = "..."                  # optional: the contest's name in the results
ballot_cards = 250000                # optional: the domain's ballot cards
reason = "the county's results page, 4 Nov 9 PM"

[[county_estimate]]                  # one county's inputs
county = "Weld"
ballots_cast = 160000                # optional
cards_per_ballot = 2                 # optional; or ballot_cards = 320000
reason = "..."
"""

_SECTION_KEYS: dict[str, tuple[set[str], set[str]]] = {
    # section: (required keys, optional keys); "reason" is always required.
    "file": ({"match"}, {"kind", "county", "ignore"}),
    "supply": ({"name", "sha256"}, {"path", "url", "kind", "county"}),
    "adapter": ({"match", "module"}, {"function", "name", "kind", "county", "ignore_input"}),
    "seed": ({"value"}, set()),
    "roll_time": ({"value"}, set()),
    "round_start": ({"value"}, set()),
    "county_order": (set(), {"use", "ids"}),
    "county_alias": ({"value", "county"}, set()),
    "manifest_adjustment": ({"county"}, {"append", "insert_after", "remove", "evidence"}),
    "contest": ({"name"}, {"counties", "statewide", "sample_count", "exclude"}),
    "estimate": ({"contest"}, {"margin", "winner_votes", "loser_votes", "final", "enr_contest", "ballot_cards"}),
    "county_estimate": ({"county"}, {"ballots_cast", "cards_per_ballot", "ballot_cards"}),
}
_TABLE_SECTIONS = frozenset({"seed", "county_order", "roll_time", "round_start"})


@dataclass(frozen=True)
class OverrideItem:
    """One item of an overrides file: its section, 1-based index in the section, and its values."""

    section: str
    index: int
    values: dict[str, Any]

    @property
    def label(self) -> str:
        if self.section in _TABLE_SECTIONS:
            return f"[{self.section}]"
        return f"[[{self.section}]] #{self.index}"

    @property
    def reason(self) -> str:
        return str(self.values.get("reason", ""))


@dataclass(frozen=True)
class Overrides:
    """A parsed and schema-checked overrides file."""

    text: str
    sha256: str
    election: str | None
    items: tuple[OverrideItem, ...]

    def section(self, name: str) -> list[OverrideItem]:
        return [item for item in self.items if item.section == name]


def parse_overrides(text: str) -> Overrides:
    """Parse an overrides file and check its shape (not yet against the map).

    Raises:
        OverridesError: listing every problem found.
    """
    sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise OverridesError([f"not valid TOML: {exc}"]) from None
    errors: list[str] = []
    version = document.pop("format", OVERRIDES_FORMAT)
    if version != OVERRIDES_FORMAT:
        errors.append(f"format is {version!r}; this version reads format {OVERRIDES_FORMAT}")
    election = document.pop("election", None)
    if election is not None and not isinstance(election, str):
        errors.append("election must be a string such as \"2026/primary\"")
    items: list[OverrideItem] = []
    for section, value in document.items():
        if section not in _SECTION_KEYS:
            errors.append(f"unknown section {section!r}; expected one of {', '.join(sorted(_SECTION_KEYS))}")
            continue
        if section in _TABLE_SECTIONS:
            if not isinstance(value, dict):
                errors.append(f"[{section}] must be a table, written [{section}]")
                continue
            tables = [value]
        else:
            if not isinstance(value, list) or not all(isinstance(t, dict) for t in value):
                errors.append(f"{section} must be written as [[{section}]] tables")
                continue
            tables = value
        required, optional = _SECTION_KEYS[section]
        for index, table in enumerate(tables, start=1):
            item = OverrideItem(section, index, dict(table))
            keys = set(table)
            for key in sorted(required - keys):
                errors.append(f"{item.label}: missing {key!r}")
            for key in sorted(keys - required - optional - {"reason", "by"}):
                errors.append(f"{item.label}: unknown key {key!r}")
            reason = table.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"{item.label}: a non-empty \"reason\" is required")
            elif len(reason) > _MAX_REASON:
                errors.append(f"{item.label}: reason is longer than {_MAX_REASON} characters")
            errors.extend(f"{item.label}: {message}" for message in _check_item(item))
            errors.extend(f"{item.label}: {message}" for message in _check_estimate_item(item))
            items.append(item)
    if errors:
        raise OverridesError(errors)
    return Overrides(text=text, sha256=sha256, election=election, items=tuple(items))


def _is_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _check_item(item: OverrideItem) -> Iterator[str]:
    v = item.values
    for key in ("match", "name", "module", "function", "value"):
        if key in v and not _is_str(v[key]):
            yield f"{key} must be a non-empty string"
    if "kind" in v and v["kind"] not in KINDS:
        yield f"kind {v['kind']!r} is not one of: {', '.join(KINDS)}"
    if "county" in v and (not isinstance(v["county"], str) or canonical_county(v["county"]) is None):
        yield f"county {v.get('county')!r} is not a Colorado county"
    for key in ("ignore", "statewide", "exclude", "ignore_input", "final"):
        if key in v and not isinstance(v[key], bool):
            yield f"{key} must be true or false"
    if item.section == "supply":
        if ("path" in v) == ("url" in v):
            yield "give exactly one of path and url"
        if "url" in v and not str(v["url"]).lower().startswith("https://"):
            yield "url must be an https:// URL"
        if not (isinstance(v.get("sha256"), str) and _SHA_RE.match(v["sha256"].lower())):
            yield "sha256 must be 64 hex digits"
    if item.section == "adapter":
        if "module" in v and not (isinstance(v["module"], str) and _MODULE_RE.match(v["module"])):
            yield "module must be a Python module name (lowercase letters, digits, _)"
        if "function" in v and not (isinstance(v["function"], str) and v["function"].isidentifier()):
            yield "function must be a Python identifier"
    if item.section == "seed" and not (isinstance(v.get("value"), str) and v["value"].isdigit()):
        yield "value must be a string of digits (write it in quotes)"
    if item.section in ("roll_time", "round_start") and isinstance(v.get("value"), str):
        try:
            parse_time(v["value"])
        except ValueError as exc:
            yield str(exc)
    if item.section == "county_order":
        if ("use" in v) == ("ids" in v):
            yield 'give exactly one of use = "default" and ids = { County = ID, ... }'
        if "use" in v and v["use"] != "default":
            yield 'use must be "default"'
        if "ids" in v:
            ids = v["ids"]
            if not isinstance(ids, dict) or not ids:
                yield "ids must be a table of county = integer"
            else:
                for name, number in ids.items():
                    if canonical_county(name) is None:
                        yield f"ids: {name!r} is not a Colorado county"
                    if not isinstance(number, int) or isinstance(number, bool):
                        yield f"ids: {name} must be an integer"
                if len(set(ids.values())) != len(ids):
                    yield "ids: two counties have the same ID"
    if item.section == "county_alias" and canonical_county(str(v.get("value", ""))) is not None:
        yield f"value {v['value']!r} is already a county's name"
    if item.section == "manifest_adjustment":
        try:
            adjustment_from_item(item)
        except (ValueError, TypeError, KeyError) as exc:
            yield f"not a valid adjustment: {exc}"
        if not any(v.get(k) for k in ("append", "insert_after", "remove")):
            yield "give at least one of append, insert_after and remove"
        if "evidence" in v and not (isinstance(v["evidence"], list) and all(isinstance(e, str) for e in v["evidence"])):
            yield "evidence must be a list of strings"
    if item.section == "contest":
        if "counties" in v:
            counties = v["counties"]
            if not isinstance(counties, list) or not all(
                isinstance(c, str) and canonical_county(c) for c in counties
            ):
                yield "counties must be a list of Colorado county names"
        if "sample_count" in v and not (
            isinstance(v["sample_count"], int) and not isinstance(v["sample_count"], bool) and v["sample_count"] >= 0
        ):
            yield "sample_count must be a whole number"


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _check_estimate_item(item: OverrideItem) -> Iterator[str]:
    v = item.values
    if item.section == "estimate":
        if not _is_str(v.get("contest")):
            yield "contest must be the contest's name, as the data map names it"
        if "enr_contest" in v and not _is_str(v["enr_contest"]):
            yield "enr_contest must be a non-empty string"
        votes = [k for k in ("winner_votes", "loser_votes") if k in v]
        if "margin" in v and votes:
            yield "give margin, or winner_votes and loser_votes, not both"
        if len(votes) == 1:
            yield "give both winner_votes and loser_votes"
        for key in ("margin", "ballot_cards"):
            if key in v and not _positive_int(v[key]):
                yield f"{key} must be a positive whole number"
        if len(votes) == 2:
            winner, loser = v["winner_votes"], v["loser_votes"]
            if not (isinstance(winner, int) and isinstance(loser, int) and not isinstance(winner, bool) and not isinstance(loser, bool)):
                yield "winner_votes and loser_votes must be whole numbers"
            elif winner <= loser or loser < 0:
                yield "winner_votes must be more than loser_votes"
        if not any(k in v for k in ("margin", "winner_votes", "enr_contest", "ballot_cards")):
            yield "give at least one of margin (or winner_votes and loser_votes), enr_contest and ballot_cards"
    if item.section == "county_estimate":
        for key in ("ballots_cast", "ballot_cards"):
            if key in v and not _positive_int(v[key]):
                yield f"{key} must be a positive whole number"
        cards = v.get("cards_per_ballot")
        if "cards_per_ballot" in v and (isinstance(cards, bool) or not isinstance(cards, (int, float)) or cards < 1):
            yield "cards_per_ballot must be a number of at least 1"
        if "cards_per_ballot" in v and "ballot_cards" in v:
            yield "give cards_per_ballot or ballot_cards, not both"
        if not any(k in v for k in ("ballots_cast", "cards_per_ballot", "ballot_cards")):
            yield "give at least one of ballots_cast, cards_per_ballot and ballot_cards"


def _batch(values: Mapping[str, Any], county: str) -> ManifestBatch:
    count = values["count"]
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError(f"count must be a whole number, got {count!r}")
    return ManifestBatch(
        county=county,
        tabulator_id=str(values["tabulator"]),
        batch_number=str(values["batch"]),
        ballot_count=count,
        location=str(values.get("location", "")),
    )


def _key(value: Any) -> tuple[str, str]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"a batch key is [tabulator, batch], got {value!r}")
    return (str(value[0]), str(value[1]))


def adjustment_from_item(item: OverrideItem) -> ManifestAdjustment:
    """The `ManifestAdjustment` a ``[[manifest_adjustment]]`` item describes, with its evidence in the reason."""
    v = item.values
    county = canonical_county(str(v["county"]))
    if county is None:
        raise ValueError(f"{v['county']!r} is not a Colorado county")
    reason = str(v.get("reason", "")).strip()
    evidence = v.get("evidence") or []
    if evidence:
        reason = f"{reason} [evidence: {'; '.join(str(e) for e in evidence)}]"
    return ManifestAdjustment(
        county=county,
        reason=reason,
        remove=tuple(_key(k) for k in v.get("remove", [])),
        insert_after=tuple((_key(i["after"]), _batch(i, county)) for i in v.get("insert_after", [])),
        append=tuple(_batch(a, county) for a in v.get("append", [])),
    )


# ---------------------------------------------------------------------------
# Reading files' tables


def table_rows(data: bytes, classification: Classification) -> tuple[list[str], list[dict[str, str]]]:
    """A classified table's header and data rows, read the way the classifier found them.

    Uses the evidence's format, delimiter, sheet and header row, so every
    header spelling and layout the classifier accepts is read the same way.
    Excel ``="..."`` wrapping is removed. Returns ``([], [])`` for anything
    that is not a CSV, JSON or workbook table.
    """
    evidence = classification.evidence
    header_row = evidence.get("header_row")
    fmt = evidence.get("format")
    rows: list[list[str]]
    if fmt == "csv" and isinstance(header_row, int):
        text, _ = decode_text(data)
        delimiter = evidence.get("delimiter")
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter if isinstance(delimiter, str) else ",")
        rows = list(reader)
    elif fmt == "xlsx" and isinstance(header_row, int) and isinstance(evidence.get("sheet"), str):
        try:
            rows = list(XlsxReader(data).rows(str(evidence["sheet"])))
        except XlsxError:
            return [], []
    elif fmt == "json":
        value = json.loads(decode_text(data)[0])
        header = list(dict.fromkeys(str(k) for item in value for k in item))
        return header, [{k: "" if item.get(k) is None else str(item.get(k)) for k in header} for item in value]
    else:
        return [], []
    index = header_row - 1
    if index >= len(rows):
        return [], []
    header = [_unwrap(cell).strip() for cell in rows[index]]
    result = []
    for row in rows[index + 1 :]:
        if not any(cell.strip() for cell in row):
            continue
        result.append({name: _unwrap(row[i]).strip() if i < len(row) else "" for i, name in enumerate(header) if name})
    return header, result


def _unwrap(cell: str) -> str:
    text = cell.strip()
    if text.startswith('="') and text.endswith('"'):
        return text[2:-1]
    return cell


def column(header: Sequence[str], *spellings: str) -> str | None:
    """The header cell matching any spelling, compared as the classifier compares them."""
    wanted = {normalize_header(s) for s in spellings}
    return next((h for h in header if normalize_header(h) in wanted), None)


def role_column(classification: Classification, role: str) -> str | None:
    """The header text the classifier matched for ``role``, if any."""
    matched = classification.evidence.get("matched_columns")
    if isinstance(matched, dict):
        value = matched.get(role)
        return value if isinstance(value, str) else None
    return None


# ---------------------------------------------------------------------------
# Gathering the log


@dataclass
class _Gathered:
    entries: list[MapEntry] = field(default_factory=list)
    #: Candidate audit times by ``which`` (`AUDIT_TIMES`): (UTC time, source).
    times: dict[str, list[Sourced]] = field(default_factory=dict)
    audit_boards: dict[str, Sourced] = field(default_factory=dict)
    overrides_event: tuple[int, str, str] | None = None  # seq, sha256, actor
    supplied: dict[str, dict[str, JSONValue]] = field(default_factory=dict)  # name -> data
    adapter_runs: list[dict[str, JSONValue]] = field(default_factory=list)
    seed_events: list[Event] = field(default_factory=list)
    seed_state: SeedState = field(default_factory=SeedState)


def _str(value: JSONValue) -> str | None:
    return value if isinstance(value, str) else None


def _classification_from(data: Mapping[str, JSONValue]) -> Classification:
    evidence = data.get("evidence")
    return Classification(
        kind=_str(data.get("kind")) or "unknown",
        county=_str(data.get("county")),
        evidence=dict(evidence) if isinstance(evidence, dict) else {},
    )


def _effective_time(event: Event) -> str:
    """When an event's file was really obtained (UTC): a rehearsal's simulated time, else ``server_time``."""
    simulated = event.data.get("simulated")
    if isinstance(simulated, dict) and isinstance(simulated.get("time"), str):
        return str(simulated["time"])
    return event.server_time


def _url_name(url: str) -> str:
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    return path.rstrip("/").rsplit("/", 1)[-1] or url


def _gather(log: EventLog, workspace: Workspace) -> _Gathered:
    gathered = _Gathered()
    fetched: dict[str, dict[str, Any]] = {}
    uploads: dict[tuple[str, str], dict[str, Any]] = {}
    upload_classified: dict[str, Classification] = {}
    for event in log.iter_events():
        data = event.data
        kind = event.kind
        if kind == "fetch.file":
            url = _str(data.get("url"))
            if url:
                state = fetched.setdefault(url, {})
                sha = _str(data.get("sha256"))
                if state.get("sha256") != sha or state.get("missing"):
                    state["obtained"] = _effective_time(event)
                state.update(
                    sha256=sha,
                    size=data.get("size"),
                    seq=event.seq,
                    missing=False,
                    simulated=data.get("simulated") if isinstance(data.get("simulated"), dict) else None,
                )
        elif kind == "fetch.unchanged":
            url = _str(data.get("url"))
            if url and url in fetched:
                fetched[url]["missing"] = False
        elif kind == "fetch.missing":
            url = _str(data.get("url"))
            if url and url in fetched:
                fetched[url]["missing"] = True
                fetched[url]["missing_reason"] = _str(data.get("reason"))
        elif kind == "fetch.classified":
            url = _str(data.get("url"))
            if url:
                fetched.setdefault(url, {})["classified"] = (_str(data.get("sha256")), _classification_from(data))
        elif kind == "file.received" and event.county and not is_test_county(event.county):
            # A test county's uploads are kept out of the map (corla_observer.classify.TEST_COUNTIES).
            upload_kind = _str(data.get("kind"))
            sha = _str(data.get("sha256"))
            if upload_kind and sha:
                before = uploads.get((event.county, upload_kind))
                uploads[(event.county, upload_kind)] = {
                    "sha256": sha,
                    "size": data.get("size"),
                    "name": _str(data.get("original_name")) or upload_kind,
                    "seq": event.seq,
                    "obtained": before["obtained"] if before and before["sha256"] == sha else _effective_time(event),
                }
        elif kind == "file.classified" and event.file_sha256:
            recorded = data.get("classification")
            if isinstance(recorded, dict):
                upload_classified[event.file_sha256] = _classification_from(recorded)
        elif kind == "audit_boards.recorded" and event.county and not is_test_county(event.county):
            count = data.get("count")
            if isinstance(count, int):
                source = f"recorded by {event.actor} (event {event.seq})"
                if data.get("placeholder"):
                    source = f"PLACEHOLDER {source}: {data.get('note') or 'not known'}"
                gathered.audit_boards[event.county] = Sourced(count, source)
        elif kind in SEED_EVENT_KINDS:
            gathered.seed_events.append(event)
            if kind == "seed.entered" and _str(data.get("seed")):
                if not gathered.times.get("seed"):
                    gathered.times["seed"] = [
                        Sourced(event.server_time, f"the seed was entered by {event.actor} (event {event.seq})")
                    ]
        elif kind == "audit.time_recorded":
            which, moment = _str(data.get("which")), _str(data.get("time"))
            if which in AUDIT_TIMES and moment:
                gathered.times.setdefault(which, []).append(
                    Sourced(moment, f"recorded by {event.actor} (event {event.seq}): {data.get('source') or ''}".strip())
                )
        elif kind == "map.overrides_applied":
            sha = _str(data.get("sha256"))
            if sha:
                gathered.overrides_event = (event.seq, sha, event.actor)
        elif kind == "map.file_supplied":
            name = _str(data.get("name"))
            if name:
                gathered.supplied[name] = dict(data) | {"seq": event.seq, "obtained": _effective_time(event)}
        elif kind == "map.adapter_ran":
            gathered.adapter_runs.append(dict(data) | {"seq": event.seq, "obtained": _effective_time(event)})

    gathered.seed_state = seed_state(gathered.seed_events)
    for url, state in fetched.items():
        sha = state.get("sha256")
        if not sha:
            continue
        classified = state.get("classified")
        if classified is not None and classified[0] == sha:
            classification = classified[1]
        else:
            classification = _classify_stored(workspace, sha)
        entry = MapEntry(
            ref=url,
            source="fetch",
            name=_url_name(url),
            sha256=sha,
            size=state.get("size") if isinstance(state.get("size"), int) else None,
            kind=classification.kind,
            county=classification.county,
            election=election_from_url(url),
            classified_kind=classification.kind,
            classified_county=classification.county,
            evidence=classification.evidence,
            seq=state.get("seq"),
            simulated=state.get("simulated"),
            obtained=state.get("obtained"),
        )
        if state.get("missing"):
            entry.ignored = True
            entry.notes.append(f"no longer available at the audit center ({state.get('missing_reason')})")
        gathered.entries.append(entry)
    for (county, upload_kind), info in sorted(uploads.items()):
        sha = info["sha256"]
        if upload_kind == "redacted_cvr":
            classification = Classification("cvr", county, {"note": "a redacted CVR that passed the redaction check"})
        else:
            classification = upload_classified.get(sha) or _classify_stored(workspace, sha)
        map_kind = _UPLOAD_KIND.get(upload_kind, classification.kind)
        entry = MapEntry(
            ref=f"upload:{county}:{upload_kind}",
            source="upload",
            name=info["name"],
            sha256=sha,
            size=info["size"] if isinstance(info["size"], int) else None,
            kind=map_kind,
            county=county,
            election=None,
            classified_kind=classification.kind,
            classified_county=classification.county,
            evidence=classification.evidence,
            seq=info["seq"],
            obtained=info["obtained"],
        )
        entry.notes.append(f"uploaded for {county} as {upload_kind}")
        if map_kind != classification.kind:
            entry.notes.append(f"classified by content as {classification.kind}")
        gathered.entries.append(entry)
    return gathered


def _classify_stored(workspace: Workspace, sha256: str) -> Classification:
    try:
        return classify_bytes(workspace.get_bytes(sha256))
    except ObjectNotFoundError:
        return Classification("unknown", None, {"error": "not in the workspace"})


# ---------------------------------------------------------------------------
# Building the map


def _resolve(entries: Sequence[MapEntry], match: str) -> list[MapEntry]:
    """The entries a FILE reference names; see `OVERRIDES_FORMAT_HELP`.

    Tried in order, the first kind of match that finds anything wins: exact
    ref, SHA-256 or prefix (which may name several files with that content),
    URL ending, file name.
    """
    exact = [e for e in entries if e.ref == match]
    if exact:
        return exact
    lowered = match.lower()
    if _SHA_PREFIX_RE.match(lowered):
        by_hash = [e for e in entries if e.sha256.startswith(lowered)]
        if by_hash:
            return by_hash
    suffix = [e for e in entries if e.ref.endswith("/" + match.lstrip("/"))]
    if suffix:
        return suffix
    return [e for e in entries if e.name == match]


def resolve_one(entries: Sequence[MapEntry], match: str) -> tuple[list[MapEntry], str | None]:
    """Resolve a FILE reference; return the entries and an error message if it is unusable.

    Several entries are allowed only when they all have the same content.
    """
    found = _resolve(entries, match)
    if not found:
        return [], f"{match!r} matches no file in the map"
    if len({e.sha256 for e in found}) > 1:
        refs = ", ".join(e.ref for e in found[:6])
        return found, f"{match!r} matches {len(found)} different files ({refs}); use a fuller URL, a ref or a SHA-256"
    return found, None


def _latest_overrides(gathered: _Gathered, workspace: Workspace) -> tuple[Overrides | None, list[Problem]]:
    if gathered.overrides_event is None:
        return None, []
    seq, sha, _ = gathered.overrides_event
    try:
        text = workspace.get_bytes(sha).decode("utf-8")
        return parse_overrides(text), []
    except (ObjectNotFoundError, UnicodeDecodeError, OverridesError) as exc:
        return None, [
            Problem("error", "overrides_unreadable", f"the overrides applied in event {seq} cannot be read: {exc}")
        ]


def build_map(
    log: EventLog,
    workspace: Workspace,
    *,
    preview: Overrides | None = None,
) -> DataMap:
    """Build the election's data map from the event log; see the module documentation.

    The overrides used are those last applied (``map.overrides_applied``),
    or ``preview`` instead, to see the effect of an edited file before
    applying it. A preview's ``[[supply]]`` and ``[[adapter]]`` items take
    effect only once applied (unless their files are already recorded).
    """
    gathered = _gather(log, workspace)
    problems: list[Problem] = []
    if preview is not None:
        overrides: Overrides | None = preview
    else:
        overrides, problems = _latest_overrides(gathered, workspace)
    entries = gathered.entries
    aliases: dict[str, str] = {}
    if overrides is not None:
        for item in overrides.section("county_alias"):
            county = canonical_county(item.values["county"])
            assert county is not None
            aliases[item.values["value"]] = county
        _add_supplied(entries, overrides, gathered, workspace, problems)
        _add_adapter_outputs(entries, overrides, gathered, workspace, problems)
        _apply_file_items(entries, overrides, problems)

    election, election_source = _election(entries, overrides)
    for entry in entries:
        if entry.election and election and entry.election != election and not entry.ignored:
            entry.ignored = True
            entry.notes.append(f"its URL names election {entry.election}, not {election}")
        if entry.election is None and entry.source == "fetch" and entry.kind not in ("html_page", "unknown"):
            entry.notes.append("its URL names no election")

    content = _Content(workspace)
    imported = _imported_manifests(entries, content)
    manifests = _choose_manifests(entries, aliases, content, imported, problems)
    _check_imported(manifests, imported, problems)
    adjustments = (
        [adjustment_from_item(item) for item in overrides.section("manifest_adjustment")] if overrides else []
    )
    for adjustment in adjustments:
        chosen = manifests.get(adjustment.county)
        if chosen is None:
            problems.append(
                Problem(
                    "error",
                    "adjustment_without_manifest",
                    f"a manifest adjustment for {adjustment.county}, which has no manifest",
                    county=adjustment.county,
                )
            )
            continue
        try:
            apply_manifest_adjustment(chosen.batches, adjustment)
        except ValueError as exc:
            problems.append(
                Problem(
                    "error",
                    "adjustment_does_not_fit",
                    f"the manifest adjustment for {adjustment.county} does not fit its manifest: {exc}",
                    refs=(chosen.ref,),
                    county=adjustment.county,
                )
            )
    seed = _seed(entries, gathered, overrides, problems)
    times = _audit_times(gathered.times, overrides, election)
    order = _county_order(entries, overrides, content, problems)
    contests = _contests(entries, overrides, content, manifests, imported, problems)
    target_cards, target_cards_ref = _target_cards(entries, content)
    _check_contests(contests, manifests, order, adjustments, problems)
    applied_seq = gathered.overrides_event[0] if gathered.overrides_event and preview is None else None
    if preview is None:
        _note_unapplied(workspace, overrides, problems)
    return DataMap(
        election=election,
        election_source=election_source,
        entries=entries,
        manifests=manifests,
        seed=seed,
        county_order=order,
        contests=contests,
        adjustments=adjustments,
        county_aliases=aliases,
        audit_boards=gathered.audit_boards,
        problems=problems,
        overrides_sha256=overrides.sha256 if overrides else None,
        overrides_seq=applied_seq,
        overrides_text=overrides.text if overrides else None,
        preview=preview is not None,
        roll_time=times.roll,
        round_start=times.round_start,
        target_cards=target_cards,
        target_cards_ref=target_cards_ref,
        estimate_items=(overrides.section("estimate") + overrides.section("county_estimate")) if overrides else [],
        upload_hashes=_upload_hash_checks(entries, content),
    )


def _note_unapplied(workspace: Workspace, overrides: Overrides | None, problems: list[Problem]) -> None:
    path = workspace.root / OVERRIDES_FILENAME
    if not path.is_file():
        return
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if overrides is None or sha != overrides.sha256:
        problems.append(
            Problem(
                "warning",
                "overrides_not_applied",
                f"{OVERRIDES_FILENAME} has been edited since it was last applied",
                hint="review it with `map show --preview`, then `map apply` it",
            )
        )


def _add_supplied(
    entries: list[MapEntry], overrides: Overrides, gathered: _Gathered, workspace: Workspace, problems: list[Problem]
) -> None:
    for item in overrides.section("supply"):
        v = item.values
        sha = str(v["sha256"]).lower()
        name = str(v["name"])
        if not workspace.has(sha):
            problems.append(
                Problem(
                    "error",
                    "supplied_file_not_stored",
                    f"{item.label} {name}: not yet stored",
                    hint="run `map apply`, which reads and checks the file",
                )
            )
            continue
        classification = _classify_stored(workspace, sha)
        event = gathered.supplied.get(name)
        entry = MapEntry(
            ref=f"supplied:{name}",
            source="supplied",
            name=name,
            sha256=sha,
            size=event.get("size") if event and isinstance(event.get("size"), int) else None,  # type: ignore[arg-type]
            kind=classification.kind,
            county=classification.county,
            election=None,
            classified_kind=classification.kind,
            classified_county=classification.county,
            evidence=classification.evidence,
            seq=event.get("seq") if event and isinstance(event.get("seq"), int) else None,  # type: ignore[arg-type]
            obtained=_str(event.get("obtained")) if event else None,
        )
        source = v.get("url") or f"a file named {Path(str(v.get('path'))).name}"
        entry.notes.append(f"supplied by {item.label} from {source}: {item.reason}")
        _set_kind_county(entry, item)
        entries.append(entry)


def _add_adapter_outputs(
    entries: list[MapEntry], overrides: Overrides, gathered: _Gathered, workspace: Workspace, problems: list[Problem]
) -> None:
    for item in overrides.section("adapter"):
        v = item.values
        inputs, error = resolve_one(entries, str(v["match"]))
        if error:
            problems.append(Problem("error", "adapter_input", f"{item.label}: {error}"))
            continue
        source = inputs[0]
        function = str(v.get("function", "convert"))
        run = next(
            (
                r
                for r in reversed(gathered.adapter_runs)
                if r.get("module") == v["module"]
                and r.get("function") == function
                and r.get("input_sha256") == source.sha256
            ),
            None,
        )
        if run is None or not isinstance(run.get("output_sha256"), str):
            problems.append(
                Problem(
                    "error",
                    "adapter_not_run",
                    f"{item.label}: adapter {v['module']}.{function} has not been run on {source.ref}",
                    refs=(source.ref,),
                    hint="run `map apply`",
                )
            )
            continue
        output_sha = str(run["output_sha256"])
        name = str(v.get("name") or run.get("output_name") or f"{v['module']}-{source.name}")
        classification = _classify_stored(workspace, output_sha)
        entry = MapEntry(
            ref=f"adapter:{name}",
            source="adapter",
            name=name,
            sha256=output_sha,
            size=run.get("output_size") if isinstance(run.get("output_size"), int) else None,  # type: ignore[arg-type]
            kind=classification.kind,
            county=classification.county,
            election=source.election,
            classified_kind=classification.kind,
            classified_county=classification.county,
            evidence=classification.evidence,
            seq=run.get("seq") if isinstance(run.get("seq"), int) else None,  # type: ignore[arg-type]
            obtained=_str(run.get("obtained")),
        )
        entry.notes.append(
            f"produced by adapter {v['module']}.{function} (source sha256 {str(run.get('source_sha256'))[:12]}) "
            f"from {source.ref}, by {item.label}: {item.reason}"
        )
        _set_kind_county(entry, item)
        entries.append(entry)
        if v.get("ignore_input"):
            for e in inputs:
                e.ignored = True
                e.notes.append(f"replaced by the output of {item.label}")


def _set_kind_county(entry: MapEntry, item: OverrideItem) -> None:
    v = item.values
    if "kind" in v and v["kind"] != entry.kind:
        entry.notes.append(f"kind set to {v['kind']} (was {entry.kind}) by {item.label}")
        entry.kind = str(v["kind"])
    if "county" in v:
        county = canonical_county(str(v["county"]))
        if county != entry.county:
            entry.notes.append(f"county set to {county} (was {entry.county}) by {item.label}")
            entry.county = county


def _apply_file_items(entries: list[MapEntry], overrides: Overrides, problems: list[Problem]) -> None:
    for item in overrides.section("file"):
        found, error = resolve_one(entries, str(item.values["match"]))
        if error:
            problems.append(Problem("error", "override_match", f"{item.label}: {error}"))
            continue
        for entry in found:
            _set_kind_county(entry, item)
            if "ignore" in item.values:
                entry.ignored = bool(item.values["ignore"])
                entry.notes.append(f"{'ignored' if entry.ignored else 'used again'} by {item.label}: {item.reason}")
            elif not any(item.label in note for note in entry.notes):
                entry.notes.append(f"changed by {item.label}: {item.reason}")
            else:
                entry.notes.append(f"{item.label}: {item.reason}")


def _election(entries: Sequence[MapEntry], overrides: Overrides | None) -> tuple[str | None, str]:
    if overrides is not None and overrides.election:
        return overrides.election, "overrides"
    counts = Counter(
        e.election for e in entries if e.source == "fetch" and e.election and e.kind not in ("html_page", "unknown")
    )
    if not counts:
        return None, "none: no fetched data file names an election"
    election, count = counts.most_common(1)[0]
    return election, f"named by {count} of {sum(counts.values())} fetched data files' URLs"


def _manifest_counties(batches: Sequence[ManifestBatch]) -> list[str]:
    return sorted({b.county for b in batches})


def _choose_manifests(
    entries: Sequence[MapEntry],
    aliases: Mapping[str, str],
    content: _Content,
    imported: Mapping[str, ImportedManifest],
    problems: list[Problem],
) -> dict[str, ChosenManifest]:
    loaded: dict[str, list[tuple[MapEntry, list[ManifestBatch]]]] = defaultdict(list)
    seen: dict[str, MapEntry] = {}
    for entry in entries:
        if entry.kind != "county_manifest" or entry.ignored:
            continue
        name_county = county_from_filename(entry.name) if entry.source == "fetch" else None
        if name_county and entry.classified_county and name_county != entry.classified_county:
            problems.append(
                Problem(
                    "warning",
                    "manifest_under_other_name",
                    f"{entry.name} holds a manifest for {entry.classified_county}, not {name_county}",
                    refs=(entry.ref,),
                    hint=(
                        f"{name_county}'s real manifest may be missing: check with the county, and supply it "
                        "or ignore this file"
                    ),
                    county=name_county,
                )
            )
        if entry.sha256 in seen:
            first = seen[entry.sha256]
            entry.notes.append(f"same content as {first.ref}")
            continue
        seen[entry.sha256] = entry
        batches = _load_manifest(entry, aliases, content, problems)
        if batches is None:
            continue
        names = _manifest_counties(batches)
        counties = {canonical_county(n) or n for n in names}
        if len(counties) != 1:
            odd = [n for n in names if canonical_county(n) is None]
            problems.append(
                Problem(
                    "error",
                    "manifest_names_several_counties",
                    f"{entry.name} names {', '.join(repr(n) for n in names)} in its County column",
                    refs=(entry.ref,),
                    hint=(
                        f"if {', '.join(repr(n) for n in odd)} is a misspelling, add a [[county_alias]]"
                        if odd
                        else "ignore it, or supply each county's manifest separately"
                    ),
                    county=entry.county,
                )
            )
            continue
        county = next(iter(counties))
        if canonical_county(county) is None:
            problems.append(
                Problem(
                    "error",
                    "manifest_county_unknown",
                    f"{entry.name} names {county!r}, which is not a Colorado county",
                    refs=(entry.ref,),
                    hint="add a [[county_alias]], or set the county with a [[file]] override",
                )
            )
            continue
        if entry.county and canonical_county(entry.county) != county and entry.source == "upload":
            problems.append(
                Problem(
                    "warning",
                    "upload_county_mismatch",
                    f"{entry.name}, uploaded for {entry.county}, names {county}",
                    refs=(entry.ref,),
                    county=entry.county,
                )
            )
        batches = [replace(b, county=county) for b in batches]
        loaded[county].append((entry, batches))

    chosen: dict[str, ChosenManifest] = {}
    for county, candidates in sorted(loaded.items()):
        candidates.sort(key=lambda c: (_SOURCE_PRIORITY.get(c[0].source, 9), c[0].ref))
        first_entry, first_batches = candidates[0]
        key = _batch_keys(first_batches)
        agree = [e for e, b in candidates[1:] if _batch_keys(b) == key]
        disagree = [(e, b) for e, b in candidates[1:] if _batch_keys(b) != key]
        if disagree:
            described = "; ".join(
                f"{e.ref} ({len(b)} batches, {sum(x.ballot_count for x in b)} cards{_batch_difference(first_batches, b)})"
                for e, b in [(first_entry, first_batches), *disagree]
            )
            refs = tuple(e.ref for e, _ in [(first_entry, first_batches), *disagree])
            record = imported.get(county)
            matching = [(e, b) for e, b in candidates if record is not None and e.sha256 == record.sha256]
            if record is not None and len(matching) == 1:
                first_entry, first_batches = matching[0]
                key = _batch_keys(first_batches)
                agree = [e for e, b in candidates if e is not first_entry and _batch_keys(b) == key]
                problems.append(
                    Problem(
                        "warning",
                        "conflicting_manifests",
                        f"{county} has manifests that list different batches: {described}; using "
                        f"{first_entry.ref}, byte-identical to the manifest CORLA recorded importing "
                        f"({record.filename}, {colorado_wall_time(record.timestamp)})",
                        refs=refs + (record.ref,),
                        hint="check with the county which one is right; override with [[file]] ... ignore = true",
                        county=county,
                    )
                )
                for e, _ in candidates:
                    if e is not first_entry and e not in agree:
                        e.notes.append(f"not used: lists different batches from {first_entry.ref}")
            else:
                first_entry, first_batches = max(candidates, key=lambda c: (c[0].obtained or "", c[0].seq or 0, c[0].ref))
                key = _batch_keys(first_batches)
                agree = [e for e, b in candidates if e is not first_entry and _batch_keys(b) == key]
                when = f", obtained {colorado_time(first_entry.obtained)}" if first_entry.obtained else ""
                problems.append(
                    Problem(
                        "warning",
                        "conflicting_manifests",
                        f"{county} has manifests that list different batches: {described}; using "
                        f"{first_entry.ref}, the one obtained most recently{when}",
                        refs=refs,
                        hint=(
                            "check with the county which one is right; to use another, ignore this one with "
                            "[[file]] ... ignore = true, saying why"
                        ),
                        county=county,
                    )
                )
                for e, _ in candidates:
                    if e is not first_entry and e not in agree:
                        e.notes.append(f"not used: lists different batches from {first_entry.ref}, obtained later")
                first_entry.notes.append("chosen among conflicting manifests as the one obtained most recently")
        for e in agree:
            e.notes.append(f"lists the same batches as {first_entry.ref}, which is used")
        first_entry.notes.append(f"used as {county}'s manifest")
        chosen[county] = ChosenManifest(county, first_entry.ref, first_entry.sha256, first_batches, [e.ref for e in agree])
    return chosen


def _batch_difference(reference: Sequence[ManifestBatch], other: Sequence[ManifestBatch]) -> str:
    """A short description of how ``other``'s rows differ from ``reference``'s ("" if they don't)."""
    ours = Counter(_batch_keys(reference))
    theirs = Counter(_batch_keys(other))
    if ours == theirs:
        return "" if _batch_keys(reference) == _batch_keys(other) else "; same batches in another order"
    only_theirs = sorted((theirs - ours).elements())[:4]
    only_ours = sorted((ours - theirs).elements())[:4]

    def show(keys: Sequence[tuple[str, str, int]]) -> str:
        return ", ".join(f"{t}-{b} ({n})" for t, b, n in keys) or "none"

    return f"; rows only here: {show(only_theirs)}; rows only in the first: {show(only_ours)}"


def _batch_keys(batches: Sequence[ManifestBatch]) -> list[tuple[str, str, int]]:
    return [(b.tabulator_id, b.batch_number, b.ballot_count) for b in batches]


def _load_manifest(
    entry: MapEntry, aliases: Mapping[str, str], content: _Content, problems: list[Problem]
) -> list[ManifestBatch] | None:
    data = content.get(entry.sha256)
    if data is None:
        problems.append(Problem("error", "file_not_stored", f"{entry.ref}: content not in the workspace", refs=(entry.ref,)))
        return None
    try:
        return load_manifest_bytes(data, aliases, entry.name)
    except (ValueError, csv.Error) as exc:
        problems.append(
            Problem(
                "error",
                "manifest_does_not_load",
                f"{entry.name} does not load as a manifest: {exc}",
                refs=(entry.ref,),
                hint=(
                    "supply a corrected copy, or write an adapter that converts it to the usual layout "
                    "(County, Tabulator, Batch, # of Ballot Cards, Location)"
                ),
                county=entry.county,
            )
        )
        return None


def load_manifest_bytes(
    data: bytes, aliases: Mapping[str, str] | None = None, name: str = "manifest"
) -> list[ManifestBatch]:
    """`corla_observer.sampling.load_county_manifest` on content rather than a path.

    The content is decoded as the classifier decodes it (UTF-8, UTF-16 with a
    byte order mark, or Latin-1), so a Latin-1 manifest loads too.
    """
    text, _ = decode_text(data)
    return parse_county_manifest(io.StringIO(text, newline=""), name, aliases)


class _Content:
    """Stored files' content, read from the workspace (and checked) once each."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self._cache: dict[str, bytes | None] = {}

    def get(self, sha256: str) -> bytes | None:
        if sha256 not in self._cache:
            try:
                self._cache[sha256] = self.workspace.get_bytes(sha256)
            except ObjectNotFoundError:
                self._cache[sha256] = None
        return self._cache[sha256]


def _seed(
    entries: Sequence[MapEntry],
    gathered: _Gathered,
    overrides: Overrides | None,
    problems: list[Problem],
) -> Sourced | None:
    candidates: list[Sourced] = []
    state = gathered.seed_state
    current = state.current
    if current is not None:
        confirmation = state.confirmation
        confirmed = (
            "" if confirmation is None else f", confirmed by {confirmation.second.actor} (event {confirmation.seq})"
        )
        candidates.append(
            Sourced(current.seed, f"entered by {current.actor} (event {current.seq}){confirmed}: {current.source}".strip())
        )
    mismatch = state.unresolved_mismatch
    if mismatch is not None:
        problems.append(
            Problem(
                "error",
                "seed_mismatch",
                f"a second entry of the seed did not match: {mismatch.first.actor} entered {mismatch.first.seed}, "
                f"{mismatch.second.actor} entered {mismatch.second.seed} (event {mismatch.seq})",
                hint="check the announced seed, and enter the right one with `corla-observer seed set`",
            )
        )
    for entry in entries:
        if entry.ignored:
            continue
        if entry.kind == "seed" and isinstance(entry.evidence.get("seed"), str):
            candidates.append(Sourced(entry.evidence["seed"], f"seed file {entry.ref}"))
        if entry.kind in ("state_audit_report", "county_audit_report") and isinstance(
            entry.evidence.get("audit_random_seed"), str
        ):
            candidates.append(Sourced(entry.evidence["audit_random_seed"], f"{entry.kind} {entry.ref}"))
    if overrides is not None and overrides.section("seed"):
        item = overrides.section("seed")[0]
        chosen = Sourced(str(item.values["value"]), f"[seed] override: {item.reason}")
        others = [c for c in candidates if c.value != chosen.value]
        if others:
            problems.append(
                Problem(
                    "warning",
                    "seed_override_differs",
                    f"the [seed] override {chosen.value} differs from: "
                    + "; ".join(f"{c.value} ({c.source})" for c in others),
                )
            )
        return chosen
    if not candidates:
        problems.append(
            Problem(
                "error",
                "no_seed",
                "no seed yet",
                hint="enter it with `corla-observer seed set` once it is announced",
            )
        )
        return None
    values = {c.value for c in candidates}
    if len(values) > 1:
        problems.append(
            Problem(
                "error",
                "seed_conflict",
                "different seeds: " + "; ".join(f"{c.value} ({c.source})" for c in candidates),
                hint="find out which is right and set it with a [seed] override",
            )
        )
    if len(candidates[0].value) < 20:
        problems.append(Problem("warning", "short_seed", f"the seed {candidates[0].value} is shorter than 20 digits"))
    return candidates[0]


@dataclass(frozen=True)
class AuditTimes:
    """The election's roll time and first round's start (UTC), with their sources, and the election."""

    roll: Sourced | None
    round_start: Sourced | None
    election: str | None = None


def _audit_times(candidates: Mapping[str, Sequence[Sourced]], overrides: Overrides | None, election: str | None) -> AuditTimes:
    """The earliest known roll time and round start, from the seed's entry, recorded times and overrides."""
    found: dict[str, list[Sourced]] = {
        "roll": list(candidates.get("seed", ())) + list(candidates.get("roll", ())),
        "round_start": list(candidates.get("round_start", ())),
    }
    if overrides is not None:
        for which in AUDIT_TIMES:
            for item in overrides.section("roll_time" if which == "roll" else which):
                found[which].append(Sourced(parse_time(str(item.values["value"])), f"{item.label} override: {item.reason}"))
    chosen = {which: min(values, key=lambda s: s.value) if values else None for which, values in found.items()}
    return AuditTimes(chosen["roll"], chosen["round_start"], election)


def audit_times(log: EventLog, workspace: Workspace) -> AuditTimes:
    """The roll time and round start the data map would use, read from the log without building the map.

    For the fetcher, which flags files that change after them. The election
    is chosen as the map chooses it: the overrides' ``election``, else the
    election most classified data files' URLs name.
    """
    times: dict[str, list[Sourced]] = {}
    overrides_sha: str | None = None
    elections: dict[str, str | None] = {}
    for event in log.iter_events():
        data = event.data
        if event.kind == "seed.entered" and not times.get("seed"):
            times["seed"] = [Sourced(event.server_time, f"the seed was entered by {event.actor} (event {event.seq})")]
        elif event.kind == "audit.time_recorded":
            which, moment = _str(data.get("which")), _str(data.get("time"))
            if which in AUDIT_TIMES and moment:
                times.setdefault(which, []).append(
                    Sourced(moment, f"recorded by {event.actor} (event {event.seq}): {data.get('source') or ''}".strip())
                )
        elif event.kind == "map.overrides_applied":
            overrides_sha = _str(data.get("sha256"))
        elif event.kind == "fetch.classified":
            url = _str(data.get("url"))
            if url:
                kind = _str(data.get("kind"))
                elections[url] = election_from_url(url) if kind not in (None, "html_page", "unknown") else None
    overrides = None
    if overrides_sha is not None:
        try:
            overrides = parse_overrides(workspace.get_bytes(overrides_sha).decode("utf-8"))
        except (ObjectNotFoundError, UnicodeDecodeError, OverridesError):
            overrides = None
    if overrides is not None and overrides.election:
        election: str | None = overrides.election
    else:
        counts = Counter(e for e in elections.values() if e)
        election = counts.most_common(1)[0][0] if counts else None
    return _audit_times(times, overrides, election)


def record_audit_time(log: EventLog, which: str, moment: str, actor: str, source: str) -> int:
    """Record when the seed was rolled (``which`` ``roll``) or the first round started (``round_start``).

    ``moment`` is read by `parse_time` (Colorado time unless it has an
    offset); ``source`` says how it is known. The data map uses the earliest
    roll time known, including the seed's entry. Returns the event's seq.

    Raises:
        ValueError: for an unknown ``which``, an unreadable time or an invalid actor.
    """
    check_actor(actor)
    if which not in AUDIT_TIMES:
        raise ValueError(f"which must be one of {', '.join(AUDIT_TIMES)}, got {which!r}")
    data = {"which": which, "time": parse_time(moment), "given": moment.strip()[:100], "source": source.strip()[:500]}
    return log.append("audit.time_recorded", actor, data=data).seq


def _county_order(
    entries: Sequence[MapEntry], overrides: Overrides | None, content: _Content, problems: list[Problem]
) -> Sourced:
    if overrides is not None and overrides.section("county_order"):
        item = overrides.section("county_order")[0]
        if "ids" in item.values:
            ids = {canonical_county(name) or name: int(number) for name, number in item.values["ids"].items()}
            return Sourced(ids, f"[county_order] override: {item.reason}")
        return Sourced(dict(DEFAULT_COUNTY_IDS), f"Colorado's standard county numbering, by [county_order]: {item.reason}")
    files = [e for e in entries if e.kind == "contest_ids_by_county" and not e.ignored]
    for entry in files:
        ids, error = _ids_from_file(entry, content)
        if error:
            problems.append(Problem("warning", "contest_ids_unreadable", f"{entry.ref}: {error}", refs=(entry.ref,)))
            continue
        differ = {c: (n, DEFAULT_COUNTY_IDS.get(c)) for c, n in ids.items() if DEFAULT_COUNTY_IDS.get(c) != n}
        if differ:
            problems.append(
                Problem(
                    "warning",
                    "county_ids_differ_from_standard",
                    f"{entry.name} gives county IDs that differ from the standard numbering: {differ}",
                    refs=(entry.ref,),
                )
            )
        return Sourced(ids, f"contest_ids_by_county file {entry.ref}")
    return Sourced(
        dict(DEFAULT_COUNTY_IDS),
        "Colorado's standard county numbering (CORLA's own county IDs have matched it in every election "
        "surveyed; its ContestIDsByCounty file is published only after the audit)",
    )


def _ids_from_file(entry: MapEntry, content: _Content) -> tuple[dict[str, int], str | None]:
    data = content.get(entry.sha256)
    if data is None:
        return {}, "content not in the workspace"
    classification = Classification(entry.classified_kind, entry.classified_county, entry.evidence)
    header, rows = table_rows(data, classification)
    id_col = role_column(classification, "county_id")
    county_col = role_column(classification, "county")
    if not rows or id_col is None or county_col is None:
        return {}, "no county_id and county columns"
    ids: dict[str, int] = {}
    for row in rows:
        county = canonical_county(row.get(county_col, ""))
        try:
            number = int(row.get(id_col, ""))
        except ValueError:
            continue
        if county is None:
            continue
        if ids.setdefault(county, number) != number:
            return {}, f"{county} has two IDs"
    return ids, None


def _corla_contest_counties(entries: Sequence[MapEntry], content: _Content) -> dict[str, list[str]]:
    for entry in entries:
        if entry.kind != "contest_ids_by_county" or entry.ignored:
            continue
        data = content.get(entry.sha256)
        if data is None:
            continue
        classification = Classification(entry.classified_kind, entry.classified_county, entry.evidence)
        _, rows = table_rows(data, classification)
        contest_col = role_column(classification, "contest")
        county_col = role_column(classification, "county")
        if contest_col is None or county_col is None:
            continue
        mapping: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            county = canonical_county(row.get(county_col, ""))
            if county:
                mapping[row.get(contest_col, "")].add(county)
        return {name: sorted(counties, key=county_key) for name, counties in mapping.items()}
    return {}


_COUNTY_SUFFIX_RE = re.compile(r"\(([^()]+)\)\s*$")


def _int(value: str | None) -> int | None:
    try:
        return int(str(value).replace(",", "").strip())
    except ValueError:
        try:
            number = float(str(value).replace(",", "").strip())
        except ValueError:
            return None
        return int(number) if number.is_integer() else None


def round_target(audited: int | None, optimistic: int | None) -> int | None:
    """CORLA's cumulative draws for a contest after the round a contest list was posted for.

    ``max(optimistic + 1, audited)``: the optimistic sample size for the
    round plus CORLA's one extra draw, or the draws already made if more.
    A round-one list posted before the audit holds zeros in
    ``audited_sample_count`` but fills in ``optimistic_samples_to_audit``;
    a list published after its round holds both. Checked on every contest
    transition of the 2022 general, 2024 general and 2025 audits
    (docs/AUDITCENTER_FORMATS.md). None when neither is known.
    """
    candidates = [n for n in ((optimistic + ROUND_EXTRA_DRAWS) if optimistic else None, audited) if n]
    return max(candidates) if candidates else None


#: Draws CORLA makes in a round beyond the optimistic sample size.
ROUND_EXTRA_DRAWS = 1


@dataclass(frozen=True)
class ContestListRound:
    """One fetched contest list and the round it is for, with how that round was found."""

    entry: MapEntry
    round: int | None
    round_source: str


def contest_list_round(entry: MapEntry, content: _Content) -> ContestListRound:
    """The round a contest list is for: from its URL (``round2/``, ``...Round1.csv``), else from its content.

    By content, a list whose ``audited_sample_count`` is zero for every
    targeted contest was posted before any ballot was audited, so it is
    round one's. Otherwise the round is unknown (None).
    """
    number = round_of(entry.ref)
    if number is not None:
        return ContestListRound(entry, number, f"its URL names round {number}")
    data = content.get(entry.sha256)
    if data is not None:
        classification = Classification(entry.classified_kind, entry.classified_county, entry.evidence)
        header, rows = table_rows(data, classification)
        reason_col = role_column(classification, "audit_reason") or "audit_reason"
        audited_col = column(header, "audited_sample_count")
        targeted = [r for r in rows if r.get(reason_col, "").strip().lower() in TARGETED_REASONS]
        if audited_col and targeted and all(not _int(r.get(audited_col)) for r in targeted):
            return ContestListRound(entry, 1, "no ballot audited yet in it (audited_sample_count all zero)")
    return ContestListRound(entry, None, "its round is not known")


def _contests(
    entries: Sequence[MapEntry],
    overrides: Overrides | None,
    content: _Content,
    manifests: Mapping[str, ChosenManifest],
    imported: Mapping[str, ImportedManifest],
    problems: list[Problem],
) -> list[ContestSpec]:
    contests: list[ContestSpec] = []
    lists = [contest_list_round(e, content) for e in entries if e.kind == "contests_list" and not e.ignored]
    targets = [e for e in entries if e.kind == "target_contests" and not e.ignored]
    if lists:
        contests = _contests_from_lists(lists, _corla_contest_counties(entries, content), content, manifests, problems)
    if not contests and targets:
        contests = _contests_from_targets(_latest(targets), content, manifests, imported, problems)
    elif targets and contests:
        _add_target_details(contests, _latest(targets), content)
    if overrides is not None:
        for item in overrides.section("contest"):
            _apply_contest_item(contests, item, manifests, problems)
    if not contests:
        problems.append(
            Problem(
                "error",
                "no_contests",
                "no targeted contests: neither a contest list nor a target contest list has been found",
                hint="add [[contest]] overrides with each contest's counties and sample count",
            )
        )
    return contests


def _latest(entries: Sequence[MapEntry]) -> MapEntry:
    """Of several files of one kind, the one fetched or recorded last."""
    return max(entries, key=lambda e: (e.seq or 0, e.ref))


def _contests_from_lists(
    lists: Sequence[ContestListRound],
    contest_counties: Mapping[str, list[str]],
    content: _Content,
    manifests: Mapping[str, ChosenManifest],
    problems: list[Problem],
) -> list[ContestSpec]:
    """The targeted contests with their round-one draws, from the contest lists of every round fetched.

    Files may arrive in any order, and several rounds at once: each list
    gives its own round's cumulative target (`round_target`), and the
    round-one sample count comes from the round-one list (the latest one
    fetched, if several), or, without one, from the earliest later round's
    list, whose ``audited_sample_count`` is the draws made before that
    round. A list whose round is not known counts as round one only when
    no list names a round.
    """
    known = [item for item in lists if item.round is not None]
    unknown = [item for item in lists if item.round is None]
    by_round: dict[int, list[ContestListRound]] = defaultdict(list)
    for item in known:
        by_round[int(item.round or 0)].append(item)
    if not known and unknown:
        latest = max(unknown, key=lambda i: (i.entry.seq or 0, i.entry.ref))
        by_round[1].append(ContestListRound(latest.entry, 1, "the only contest list (its round is not known)"))
        unknown = [i for i in unknown if i is not latest]
    if unknown:
        problems.append(
            Problem(
                "info",
                "contest_list_round_unknown",
                f"{len(unknown)} contest list(s) of unknown round not used: " + ", ".join(i.entry.ref for i in unknown[:6]),
                refs=tuple(i.entry.ref for i in unknown),
                hint="reassign or ignore them with a [[file]] override if one should be used",
            )
        )
    per_round: dict[int, tuple[ContestListRound, list[ContestSpec]]] = {}
    for number in sorted(by_round):
        items = by_round[number]
        chosen = max(items, key=lambda i: (i.entry.seq or 0, i.entry.ref))
        specs = _contests_from_list(chosen.entry, contest_counties, content, manifests, problems)
        per_round[number] = (chosen, specs)
        if len(items) > 1:
            targets = {i.entry.ref: _targets_of(i.entry, content) for i in items}
            differ = len({tuple(sorted(t.items())) for t in targets.values()}) > 1
            problems.append(
                Problem(
                    "warning" if differ else "info",
                    "several_contest_lists",
                    f"{len(items)} contest lists for round {number}"
                    + (" with different targets" if differ else " (same targets)")
                    + f"; using {chosen.entry.ref}",
                    refs=tuple(i.entry.ref for i in items),
                    hint="ignore the one that is wrong" if differ else "",
                )
            )
    if not per_round:
        return []
    first_round = min(per_round)
    chosen, contests = per_round[first_round]
    by_name = {c.name: c for c in contests}
    for number, (item, specs) in sorted(per_round.items()):
        for spec in specs:
            target = spec.round_targets.get(0)
            base = by_name.get(spec.name)
            if base is None:
                continue
            if target is not None:
                base.round_targets[number] = target
            if number == first_round and first_round > 1:
                if spec.audited_before is not None and spec.audited_before > 0:
                    base.sample_count = spec.audited_before
                    base.count_source = (
                        f"audited_sample_count in {item.entry.name} (round {number}'s contest list): "
                        f"the draws made before round {number}; no round-one list has been fetched"
                    )
    for spec in contests:
        spec.round_targets.pop(0, None)
        spec.notes.append(f"contest list for round {first_round}: {chosen.entry.ref} ({chosen.round_source})")
    return [c for c in contests if c.sample_count]


def _targets_of(entry: MapEntry, content: _Content) -> dict[str, int]:
    found = {}
    for spec in _contests_from_list(entry, {}, content, {}, []):
        if spec.round_targets.get(0):
            found[spec.name] = spec.round_targets[0]
    return found


def _contests_from_list(
    entry: MapEntry,
    contest_counties: Mapping[str, list[str]],
    content: _Content,
    manifests: Mapping[str, ChosenManifest],
    problems: list[Problem],
) -> list[ContestSpec]:
    """The targeted contests of one contest list, each with its round target under ``round_targets[0]``."""
    data = content.get(entry.sha256)
    if data is None:
        return []
    classification = Classification(entry.classified_kind, entry.classified_county, entry.evidence)
    header, rows = table_rows(data, classification)
    name_col = role_column(classification, "contest") or "contest_name"
    audited_col = column(header, "audited_sample_count")
    optimistic_col = column(header, "optimistic_samples_to_audit")
    count_col = audited_col or role_column(classification, "sample_count")
    reason_col = role_column(classification, "audit_reason") or "audit_reason"
    cards_col = column(header, "ballot_card_count")
    margin_col = column(header, "min_margin")
    risk_col = column(header, "risk_limit")
    contests = []
    for row in rows:
        reason = row.get(reason_col, "").strip().lower()
        audited = _int(row.get(count_col or "", ""))
        optimistic = _int(row.get(optimistic_col or "", "")) if optimistic_col else None
        target = round_target(audited, optimistic)
        if reason not in TARGETED_REASONS or not target:
            continue
        name = row.get(name_col, "")
        statewide = reason == "state_wide_contest"
        counties = list(contest_counties.get(name, []))
        notes = []
        if not counties:
            if statewide:
                counties = sorted(manifests, key=county_key)
                notes.append("counties: every county with a manifest (no contest-to-county file)")
            else:
                suffix = _COUNTY_SUFFIX_RE.search(name)
                county = canonical_county(suffix.group(1)) if suffix else None
                county = county or next(
                    (c for c in COLORADO_COUNTIES if name.lower().startswith(c.lower() + " county")), None
                )
                if county:
                    counties = [county]
                    notes.append(f"county {county} from the contest's name (no contest-to-county file)")
                else:
                    problems.append(
                        Problem(
                            "error",
                            "contest_without_counties",
                            f"contest {name!r}: its counties are not known",
                            refs=(entry.ref,),
                            hint="add a [[contest]] override with its counties",
                        )
                    )
        if optimistic_col and optimistic and (audited or 0) < optimistic + ROUND_EXTRA_DRAWS:
            count_source = (
                f"optimistic_samples_to_audit {optimistic} + {ROUND_EXTRA_DRAWS} in {entry.name}"
                + (f" (audited so far: {audited})" if audited else " (no ballot audited yet)")
            )
        else:
            count_source = f"audited_sample_count in {entry.name}"
        contests.append(
            ContestSpec(
                name=name,
                counties=counties,
                statewide=statewide,
                sample_count=target,
                count_source=count_source,
                source=f"contests_list {entry.ref}",
                audit_reason=reason,
                ballot_card_count=_int(row.get(cards_col or "", "")),
                margin=_int(row.get(margin_col or "", "")),
                risk_limit=row.get(risk_col or "", "") or None,
                notes=notes,
                round_targets={0: target},
                audited_before=audited,
            )
        )
    return contests


_CARDS_PER_BALLOT_RE = re.compile(r"(\d+)\s*ballot\s*cards?\s*per\s*ballot", re.IGNORECASE)


def _target_rows(entry: MapEntry, content: _Content) -> tuple[list[str], list[dict[str, str]], Classification] | None:
    data = content.get(entry.sha256)
    if data is None:
        return None
    classification = Classification(entry.classified_kind, entry.classified_county, entry.evidence)
    header, rows = table_rows(data, classification)
    return header, rows, classification


def _target_details(row: Mapping[str, str], header: Sequence[str]) -> tuple[int | None, int | None]:
    """The target list row's denominator (margin / diluted margin) and cards per ballot (from its remarks)."""
    margin = _int(row.get(column(header, "contest margin") or "", ""))
    diluted_text = row.get(column(header, "diluted margin") or "", "").strip()
    denominator = None
    try:
        diluted = float(diluted_text)
    except ValueError:
        diluted = 0.0
    if margin and diluted > 0:
        denominator = round(margin / diluted)
    remarks = row.get(column(header, "remarks") or "", "")
    match = _CARDS_PER_BALLOT_RE.search(remarks)
    return denominator, (int(match.group(1)) if match else None)


def target_cards_per_ballot(header: Sequence[str], rows: Sequence[Mapping[str, str]], classification: Classification) -> dict[str, tuple[int, str]]:
    """Per county, the ballot cards per ballot the target list's remarks give ("2 ballot cards per ballot*"), with the remark.

    The Secretary of State notes it on each multi-card county's rows, and
    computes that county's diluted margin over its ballots times it. A
    county whose rows disagree keeps the highest number.
    """
    county_col = role_column(classification, "county")
    remarks_col = column(header, "remarks")
    found: dict[str, tuple[int, str]] = {}
    if county_col is None or remarks_col is None:
        return found
    for row in rows:
        county = canonical_county(row.get(county_col, "").strip())
        remark = row.get(remarks_col, "").strip()
        match = _CARDS_PER_BALLOT_RE.search(remark)
        if county is None or match is None:
            continue
        cards = int(match.group(1))
        if cards > found.get(county, (0, ""))[0]:
            found[county] = (cards, remark[:200])
    return found


def _target_cards(entries: Sequence[MapEntry], content: _Content) -> tuple[dict[str, tuple[int, str]], str | None]:
    targets = [e for e in entries if e.kind == "target_contests" and not e.ignored]
    if not targets:
        return {}, None
    entry = _latest(targets)
    found = _target_rows(entry, content)
    if found is None:
        return {}, None
    header, rows, classification = found
    return target_cards_per_ballot(header, rows, classification), entry.ref


def _add_target_details(contests: Sequence[ContestSpec], entry: MapEntry, content: _Content) -> None:
    """Note the target list's denominator and cards per ballot on contests that came from CORLA's list."""
    found = _target_rows(entry, content)
    if found is None:
        return
    header, rows, classification = found
    name_col = role_column(classification, "contest") or "Contest"
    county_col = role_column(classification, "county")
    names = [c.name for c in contests]
    for row in rows:
        place = row.get(county_col or "", "").strip()
        county = canonical_county(place)
        name = corla_contest_name_for(row.get(name_col, "").strip(), county, names)
        if name is None:
            continue
        spec = next(c for c in contests if c.name == name)
        spec.target_denominator, spec.target_cards_per_ballot = _target_details(row, header)
        if spec.margin is None:
            spec.margin = _int(row.get(column(header, "contest margin") or "", ""))


_PARTY_ABBREVIATIONS = {
    "democratic party": "DEM",
    "republican party": "REP",
    "libertarian party": "LBR",
    "green party": "GRN",
    "american constitution party": "ACN",
    "unity party": "UNI",
    "approval voting party": "APV",
}


def corla_contest_name_for(name: str, county: str | None, published: Iterable[str]) -> str | None:
    """CORLA's name for a contest named as in the target list, if one of ``published`` matches.

    The target list says "Attorney General - Republican Party" for Weld,
    where CORLA says "Attorney General - REP (Weld)"; a county's own contest
    has no county suffix in CORLA ("Alamosa County Assessor - DEM").
    """
    names = set(published)
    if name in names:
        return name
    base = re.sub(r"\s*\([^()]*\)\s*$", "", name) if county else name
    for long, short in _PARTY_ABBREVIATIONS.items():
        base = re.sub(rf"\b{re.escape(long)}\b", short, base, flags=re.IGNORECASE)
    for candidate in ([f"{base} ({county})", base] if county else [base]):
        if candidate in names:
            return candidate
    # Then compared loosely (`contest_key`): "Moffat County Commissioner -
    # District 3 - Republican Party" is CORLA's "Moffat County Commissioner
    # District 3 - REP", and "... - District 1 - Republican Party" its
    # "... - District 1 (REP)".
    wanted = contest_key(base)
    for published_name in sorted(names):
        suffix = _COUNTY_SUFFIX_RE.search(published_name)
        suffix_county = canonical_county(suffix.group(1)) if suffix else None
        if suffix_county is not None and suffix_county != county:
            continue
        if contest_key(published_name) == wanted:
            return published_name
    return None


_VOTE_FOR_SUFFIX_RE = re.compile(r"\s*\(vote\s*for\s*=?\s*\d+\)\s*$", re.IGNORECASE)


def contest_key(name: str) -> str:
    """A contest's name as compared across sources (CORLA, the target list, the results, a CVR).

    Without "(Vote For N)" or a county suffix such as "(Weld)", with the
    parties' names abbreviated as CORLA does (Republican Party is REP), in
    lower case, and with every run of punctuation and spaces as one space.
    """
    text = _VOTE_FOR_SUFFIX_RE.sub("", name.strip())
    suffix = _COUNTY_SUFFIX_RE.search(text)
    if suffix and canonical_county(suffix.group(1)):
        text = text[: suffix.start()]
    lowered = text.lower()
    for long, short in _PARTY_ABBREVIATIONS.items():
        lowered = lowered.replace(long, short.lower())
    return re.sub(r"[^a-z0-9]+", " ", lowered).strip()


def _contests_from_targets(
    entry: MapEntry,
    content: _Content,
    manifests: Mapping[str, ChosenManifest],
    imported: Mapping[str, ImportedManifest],
    problems: list[Problem],
) -> list[ContestSpec]:
    data = content.get(entry.sha256)
    if data is None:
        return []
    classification = Classification(entry.classified_kind, entry.classified_county, entry.evidence)
    header, rows = table_rows(data, classification)
    name_col = role_column(classification, "contest") or "Contest"
    county_col = role_column(classification, "county")
    risk_col = role_column(classification, "risk_limit")
    margin_col = column(header, "contest margin")
    contests = []
    for row in rows:
        name = row.get(name_col, "").strip()
        place = row.get(county_col or "", "").strip()
        if not name:
            continue
        statewide = county_key(place) in ("colorado", "statewide", "state")
        county = canonical_county(place)
        if not statewide and county is None:
            if place:
                problems.append(
                    Problem(
                        "warning",
                        "target_contest_place",
                        f"target contest {name!r} is for {place!r}, not a county or Colorado",
                        refs=(entry.ref,),
                        hint="add a [[contest]] override with its counties, or exclude it",
                    )
                )
            continue
        notes = []
        if statewide:
            counties, note = _statewide_counties(manifests, imported, problems)
            notes.append(note)
        else:
            counties = [county or place]
        denominator, cards = _target_details(row, header)
        contests.append(
            ContestSpec(
                name=name if statewide else f"{name} ({county})",
                counties=counties,
                statewide=statewide,
                sample_count=None,
                count_source="to be estimated from the target list's margin (CORLA's own count is published later)",
                source=f"target_contests {entry.ref}",
                margin=_int(row.get(margin_col or "", "")),
                risk_limit=row.get(risk_col or "", "") or None,
                notes=notes,
                target_denominator=denominator,
                target_cards_per_ballot=cards,
            )
        )
    return contests


def _statewide_counties(
    manifests: Mapping[str, ChosenManifest], imported: Mapping[str, ImportedManifest], problems: list[Problem]
) -> tuple[list[str], str]:
    """The counties of a statewide contest before CORLA's contest-to-county file is published."""
    if imported:
        return (
            sorted(imported, key=county_key),
            "counties: every county CORLA's upload status shows importing a manifest",
        )
    without = [c for c in COLORADO_COUNTIES if c not in manifests]
    if without:
        problems.append(
            Problem(
                "warning",
                "statewide_counties_assumed",
                "the statewide contest is taken to cover every county with a manifest; none yet for "
                + ", ".join(without),
                hint=(
                    "a county still missing its manifest would shrink the statewide domain and shift every "
                    "draw after it: fetch again, or ask for the file; a county that does not take part "
                    "(one that counts by hand) needs nothing"
                ),
            )
        )
    return sorted(manifests, key=county_key), "counties: every county with a manifest"


@dataclass(frozen=True)
class ImportedManifest:
    """The last manifest CORLA's upload status shows a county's import of."""

    county: str
    sha256: str
    filename: str
    timestamp: str
    ref: str


def _imported_manifests(entries: Sequence[MapEntry], content: _Content) -> dict[str, ImportedManifest]:
    """Per county, the last manifest CORLA recorded importing, from the latest upload status file."""
    return _imported_files(entries, content, "manifest")


def _imported_files(entries: Sequence[MapEntry], content: _Content, wanted: str) -> dict[str, ImportedManifest]:
    """Per county, the last file of a type CORLA recorded importing (``manifest`` or ``cvr``), from the latest upload status file."""
    files = [e for e in entries if e.kind == "upload_status" and not e.ignored]
    if not files:
        return {}
    entry = _latest(files)
    data = content.get(entry.sha256)
    if data is None:
        return {}
    classification = Classification(entry.classified_kind, entry.classified_county, entry.evidence)
    header, rows = table_rows(data, classification)
    county_col = role_column(classification, "county")
    hash_col = role_column(classification, "computed_hash")
    type_col = role_column(classification, "type") or column(header, "type")
    status_col = role_column(classification, "status")
    file_col = role_column(classification, "filename")
    time_col = column(header, "timestamp")
    if not (county_col and hash_col and type_col and status_col):
        return {}
    found: dict[str, ImportedManifest] = {}
    for row in sorted(rows, key=lambda r: r.get(time_col or "", "")):
        county = canonical_county(row.get(county_col, ""))
        digest = row.get(hash_col, "").strip().lower()
        if (
            county
            and wanted in row.get(type_col, "").lower()
            and row.get(status_col, "").strip().upper() == "IMPORTED"
            and _SHA_RE.match(digest)
        ):
            found[county] = ImportedManifest(
                county, digest, row.get(file_col or "", ""), row.get(time_col or "", ""), entry.ref
            )
    return found


@dataclass(frozen=True)
class UploadHashCheck:
    """A county's upload here compared by SHA-256 with the audit center's copy and with what CORLA recorded importing.

    ``kind`` is ``manifest`` or ``redacted_cvr``; ``posted`` lists the
    audit center's manifests for the county that are byte-identical to the
    upload, ``posted_other`` those that are not; ``corla`` is the import
    CORLA's upload status records (None when it records none, or when no
    upload status has been fetched: ``status_posted``).
    """

    county: str
    kind: str
    ref: str
    sha256: str
    status_posted: bool
    corla: ImportedManifest | None
    posted: tuple[str, ...] = ()
    posted_other: tuple[str, ...] = ()

    @property
    def corla_match(self) -> bool | None:
        return None if self.corla is None else self.corla.sha256 == self.sha256

    def lines(self) -> list[str]:
        what = "manifest" if self.kind == "manifest" else "redacted CVR"
        out = [f"{self.county}'s {what} as uploaded here ({self.sha256[:12]}):"]
        if self.kind == "manifest":
            if self.posted:
                out.append("  byte-identical to the audit center's posted manifest (" + ", ".join(_short_ref(r) for r in self.posted) + ")")
            elif self.posted_other:
                out.append("  NOT the audit center's posted manifest (" + ", ".join(_short_ref(r) for r in self.posted_other) + ")")
            else:
                out.append("  the audit center has not posted the county's manifest yet")
        if not self.status_posted:
            out.append("  CORLA's upload hashes: not yet posted (no upload status file from the audit center)")
        elif self.corla is None:
            out.append(f"  CORLA's upload status records no {what.split()[-1]} import for {self.county}")
        elif self.corla_match:
            out.append(
                f"  byte-identical to the file CORLA recorded importing ({self.corla.filename}, "
                f"{colorado_wall_time(self.corla.timestamp)}, SHA-256 {self.corla.sha256[:12]})"
            )
        else:
            expected = " (expected if the county redacted the file, or exported it again)" if self.kind == "redacted_cvr" else ""
            out.append(
                f"  not the file CORLA recorded importing ({self.corla.filename}, {colorado_wall_time(self.corla.timestamp)}, "
                f"SHA-256 {self.corla.sha256[:12]}){expected}"
            )
        return out

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "county": self.county,
            "kind": self.kind,
            "ref": self.ref,
            "sha256": self.sha256,
            "status_posted": self.status_posted,
            "corla": None if self.corla is None else {
                "sha256": self.corla.sha256, "filename": self.corla.filename, "timestamp": self.corla.timestamp, "ref": self.corla.ref,
            },
            "corla_match": self.corla_match,
            "posted": list(self.posted),
            "posted_other": list(self.posted_other),
        }  # fmt: skip


def _upload_hash_checks(entries: Sequence[MapEntry], content: _Content) -> list[UploadHashCheck]:
    """Each county's latest uploaded manifest and redacted CVR, compared with the audit center's and CORLA's hashes."""
    status_posted = any(e.kind == "upload_status" and not e.ignored for e in entries)
    imported = {"manifest": _imported_files(entries, content, "manifest"), "redacted_cvr": _imported_files(entries, content, "cvr")}
    latest: dict[tuple[str, str], MapEntry] = {}
    for entry in entries:
        if entry.source != "upload" or entry.ignored or not entry.county:
            continue
        kind = {"county_manifest": "manifest", "cvr": "redacted_cvr"}.get(entry.kind)
        if kind is None:
            continue
        key = (entry.county, kind)
        if key not in latest or (entry.seq or 0) >= (latest[key].seq or 0):
            latest[key] = entry
    checks = []
    for (county, kind), entry in sorted(latest.items(), key=lambda kv: (county_key(kv[0][0]), kv[0][1])):
        posted: list[str] = []
        other: list[str] = []
        if kind == "manifest":
            for e in entries:
                if e.source == "fetch" and e.kind == "county_manifest" and e.county == county and not e.ignored:
                    (posted if e.sha256 == entry.sha256 else other).append(e.ref)
        checks.append(
            UploadHashCheck(county, kind, entry.ref, entry.sha256, status_posted, imported[kind].get(county), tuple(posted), tuple(other))
        )
    return checks


def imported_manifests(datamap: DataMap, workspace: Workspace) -> dict[str, ImportedManifest]:
    """Per county, the last manifest CORLA's latest upload status shows it importing."""
    return _imported_manifests(datamap.entries, _Content(workspace))


def _check_imported(
    manifests: Mapping[str, ChosenManifest], imported: Mapping[str, ImportedManifest], problems: list[Problem]
) -> None:
    for county, record in sorted(imported.items()):
        chosen = manifests.get(county)
        if chosen is None:
            continue
        hashes = {chosen.sha256}
        if record.sha256 in hashes:
            chosen.notes.append(
                f"byte-identical to the manifest CORLA recorded importing ({record.filename}, {colorado_wall_time(record.timestamp)})"
            )
        else:
            problems.append(
                Problem(
                    "warning",
                    "manifest_differs_from_import",
                    f"{county}'s manifest ({chosen.sha256[:12]}) is not the file CORLA recorded importing "
                    f"({record.filename}, {colorado_wall_time(record.timestamp)}, SHA-256 {record.sha256[:12]})",
                    refs=(chosen.ref, record.ref),
                    hint=(
                        "the same batches in another layout is harmless; different batches mean CORLA used "
                        "another manifest: ask the county for the imported file"
                    ),
                    county=county,
                )
            )


def _apply_contest_item(
    contests: list[ContestSpec], item: OverrideItem, manifests: Mapping[str, ChosenManifest], problems: list[Problem]
) -> None:
    v = item.values
    name = str(v["name"])
    spec = next((c for c in contests if c.name == name), None)
    if v.get("exclude"):
        if spec is None:
            problems.append(Problem("warning", "override_match", f"{item.label}: no contest named {name!r} to exclude"))
        else:
            contests.remove(spec)
        return
    if spec is None:
        statewide = bool(v.get("statewide", False))
        counties = [canonical_county(c) or c for c in v.get("counties", [])]
        if statewide and not counties:
            counties = sorted(manifests, key=county_key)
        if not counties:
            problems.append(Problem("error", "contest_without_counties", f"{item.label}: a new contest needs counties"))
            return
        spec = ContestSpec(
            name=name,
            counties=counties,
            statewide=statewide,
            sample_count=None,
            count_source="not given",
            source=f"{item.label}: {item.reason}",
        )
        contests.append(spec)
    else:
        spec.notes.append(f"changed by {item.label}: {item.reason}")
        if "counties" in v:
            spec.counties = [canonical_county(c) or c for c in v["counties"]]
        if "statewide" in v:
            spec.statewide = bool(v["statewide"])
    if "sample_count" in v:
        spec.sample_count = int(v["sample_count"])
        spec.count_source = f"{item.label}: {item.reason}"


def _check_contests(
    contests: Sequence[ContestSpec],
    manifests: Mapping[str, ChosenManifest],
    order: Sourced,
    adjustments: Sequence[ManifestAdjustment],
    problems: list[Problem],
) -> None:
    ids = {county_key(c): n for c, n in order.value.items()}
    missing_reported: set[str] = set()
    for spec in contests:
        for county in spec.counties:
            if county not in manifests and county not in missing_reported:
                missing_reported.add(county)
                problems.append(
                    Problem(
                        "error",
                        "no_manifest",
                        f"no usable manifest for {county} (needed for {spec.name})",
                        hint="upload the county's manifest, or supply one with a [[supply]] override",
                        county=county,
                    )
                )
            if len(spec.counties) > 1 and county_key(county) not in ids:
                problems.append(
                    Problem("error", "no_county_id", f"{county} has no county ID for {spec.name}'s domain", county=county)
                )
        if spec.sample_count is None and spec.margin is None:
            problems.append(
                Problem(
                    "error",
                    "no_sample_count",
                    f"{spec.name}: no sample count and no margin to estimate one from",
                    hint="add sample_count with a [[contest]] override",
                    county=spec.counties[0] if len(spec.counties) == 1 else None,
                )
            )
        if spec.sample_count is None and spec.margin is not None:
            problems.append(
                Problem(
                    "info",
                    "estimated_sample_count",
                    f"{spec.name}: the sample count will be estimated; CORLA's own count is published later",
                    county=spec.counties[0] if len(spec.counties) == 1 else None,
                )
            )
        if spec.ballot_card_count is not None and all(c in manifests for c in spec.counties):
            total = sum(manifests[c].ballot_cards for c in spec.counties)
            for adjustment in adjustments:
                if adjustment.county in spec.counties:
                    total += sum(b.ballot_count for b in adjustment.append)
                    total += sum(b.ballot_count for _, b in adjustment.insert_after)
                    removed = set(adjustment.remove)
                    total -= sum(
                        b.ballot_count for b in manifests[adjustment.county].batches if b.key in removed
                    )
            if total != spec.ballot_card_count:
                problems.append(
                    Problem(
                        "error",
                        "domain_size_differs",
                        f"{spec.name}: the manifests (with adjustments) hold {total} ballot cards; "
                        f"CORLA's contest list says {spec.ballot_card_count}",
                        hint=(
                            "a shortfall is the signature of a batch missing from a published manifest: "
                            "compare CORLA's manifest-to-CVR comparison with the manifests, and add a "
                            "[[manifest_adjustment]] with that evidence"
                        ),
                        county=spec.counties[0] if len(spec.counties) == 1 else None,
                    )
                )


# ---------------------------------------------------------------------------
# Applying overrides


@dataclass(frozen=True)
class ApplyResult:
    """What `apply_overrides` did."""

    applied: bool
    sha256: str
    errors: tuple[str, ...] = ()
    #: The seq of the ``map.overrides_applied`` (or ``_rejected``) event.
    event_seq: int | None = None
    supplied: tuple[str, ...] = ()
    adapters: tuple[str, ...] = ()


#: Largest file an override may supply, or an adapter may produce.
MAX_SUPPLIED_BYTES = 512 * 1024 * 1024

#: Problem codes that mean an override could not be carried out.
_OVERRIDE_PROBLEMS = frozenset(
    {"override_match", "adapter_input", "adapter_not_run", "supplied_file_not_stored", "adjustment_does_not_fit"}
)


def _read_supplied(item: OverrideItem, base_dir: Path, fetch_url: Callable[[str], bytes]) -> bytes:
    v = item.values
    if "url" in v:
        return fetch_url(str(v["url"]))
    path = Path(str(v["path"])).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    if path.stat().st_size > MAX_SUPPLIED_BYTES:
        raise ValueError(f"larger than {MAX_SUPPLIED_BYTES} bytes")
    return path.read_bytes()


def _default_fetch_url(url: str) -> bytes:
    from corla_observer.fetch import FetchPolicy, Fetcher

    host = urllib.parse.urlsplit(url).hostname or ""
    result = Fetcher(FetchPolicy(allowed_hosts=frozenset({host}), max_bytes=MAX_SUPPLIED_BYTES)).fetch(url)
    if not result.ok:
        raise ValueError(f"HTTP {result.status}")
    return result.body


def load_adapter(workspace: Workspace, module: str, function: str) -> tuple[Callable[..., Any], bytes]:
    """Load ``<workspace>/adapters/<module>.py`` and return its function and source bytes.

    The source is read once, and the bytes hashed are the bytes run. Only a
    regular file directly in the adapters directory is accepted.

    Raises:
        ValueError: if the module or function does not exist or is not allowed.
    """
    if not _MODULE_RE.match(module) or not function.isidentifier():
        raise ValueError(f"not an allowed adapter name: {module}.{function}")
    directory = (workspace.root / ADAPTERS_DIRNAME).resolve()
    path = directory / f"{module}.py"
    if path.is_symlink() or not path.is_file() or path.resolve().parent != directory:
        raise ValueError(f"no adapter module {ADAPTERS_DIRNAME}/{module}.py in the workspace")
    source = path.read_bytes()
    spec = importlib.util.spec_from_loader(f"corla_observer_adapter_{module}", loader=None)
    assert spec is not None
    namespace = importlib.util.module_from_spec(spec)
    namespace.__file__ = str(path)
    code = compile(source, str(path), "exec")
    exec(code, namespace.__dict__)  # noqa: S102 - an adapter named in the applied overrides
    fn = getattr(namespace, function, None)
    if not callable(fn):
        raise ValueError(f"{ADAPTERS_DIRNAME}/{module}.py has no function {function}")
    return fn, source


def apply_overrides(
    workspace: Workspace,
    log: EventLog,
    actor: str,
    *,
    path: Path | None = None,
    fetch_url: Callable[[str], bytes] | None = None,
) -> ApplyResult:
    """Check the overrides file, carry out its supplies and adapters, and record it as applied.

    ``path`` defaults to the workspace's ``overrides.toml``; relative
    ``[[supply]]`` paths are relative to its directory. ``fetch_url`` gets a
    supplied URL's content (by default with `corla_observer.fetch`, HTTPS
    only, from that URL's host).

    Supplied files and adapter outputs are stored, and their
    ``map.file_supplied`` and ``map.adapter_ran`` events recorded, before the
    whole file is checked against the map; if anything fails, a
    ``map.overrides_rejected`` event lists why, and the previously applied
    overrides stay in effect.

    Raises:
        ValueError: if ``actor`` is not a valid event actor.
    """
    check_actor(actor)
    path = path or workspace.root / OVERRIDES_FILENAME
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()

    def reject(errors: Sequence[str]) -> ApplyResult:
        event = log.append(
            "map.overrides_rejected",
            actor,
            data={"sha256": sha, "errors": [e[:500] for e in errors[:40]]},
            file_sha256=sha,
        )
        return ApplyResult(False, sha, tuple(errors), event.seq)

    try:
        overrides = parse_overrides(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return reject(["the overrides file is not UTF-8"])
    except OverridesError as exc:
        return reject(exc.errors)

    errors: list[str] = []
    fetcher = fetch_url or _default_fetch_url
    gathered = _gather(log, workspace)
    supplied: list[str] = []
    for item in overrides.section("supply"):
        v = item.values
        declared = str(v["sha256"]).lower()
        name = str(v["name"])
        recorded = gathered.supplied.get(name)
        if workspace.has(declared) and recorded is not None and recorded.get("sha256") == declared:
            continue
        try:
            content = workspace.get_bytes(declared) if workspace.has(declared) else _read_supplied(
                item, path.parent, fetcher
            )
        except (OSError, ValueError) as exc:
            errors.append(f"{item.label} {name}: could not read it: {exc}")
            continue
        actual = hashlib.sha256(content).hexdigest()
        if actual != declared:
            errors.append(f"{item.label} {name}: its SHA-256 is {actual}, not {declared} as the override says")
            continue
        stored = workspace.put_bytes(content)
        data: dict[str, JSONValue] = {
            "name": name,
            "sha256": stored.sha256,
            "size": stored.size,
            "reason": item.reason,
        }
        if "url" in v:
            data |= {"source": "url", "url": str(v["url"])}
        else:
            data |= {"source": "path", "path_name": Path(str(v["path"])).name}
        log.append("map.file_supplied", actor, data=data, file_sha256=stored.sha256)
        supplied.append(name)

    adapters: list[str] = []
    if overrides.section("adapter"):
        base = build_map(log, workspace, preview=_without(overrides, "adapter"))
        entries = [e for e in base.entries if e.source != "adapter"]
        gathered = _gather(log, workspace)
        for item in overrides.section("adapter"):
            v = item.values
            function = str(v.get("function", "convert"))
            inputs, error = resolve_one(entries, str(v["match"]))
            if error:
                errors.append(f"{item.label}: {error}")
                continue
            source_entry = inputs[0]
            try:
                fn, source = load_adapter(workspace, str(v["module"]), function)
            except (ValueError, SyntaxError, OSError) as exc:
                errors.append(f"{item.label}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - an adapter's own import-time error
                errors.append(f"{item.label}: loading {v['module']} raised {type(exc).__name__}: {exc}")
                continue
            source_sha = hashlib.sha256(source).hexdigest()
            done = any(
                r.get("module") == v["module"]
                and r.get("function") == function
                and r.get("source_sha256") == source_sha
                and r.get("input_sha256") == source_entry.sha256
                for r in gathered.adapter_runs
            )
            if done:
                continue
            context = {
                "name": source_entry.name,
                "ref": source_entry.ref,
                "kind": v.get("kind") or source_entry.kind,
                "county": v.get("county") or source_entry.county,
            }
            try:
                output = fn(workspace.get_bytes(source_entry.sha256), context)
            except Exception as exc:  # noqa: BLE001 - reported, never hidden
                errors.append(f"{item.label}: {v['module']}.{function} raised {type(exc).__name__}: {exc}")
                continue
            if isinstance(output, str):
                output = output.encode("utf-8")
            if not isinstance(output, bytes) or not output or len(output) > MAX_SUPPLIED_BYTES:
                errors.append(f"{item.label}: {v['module']}.{function} must return non-empty bytes or str")
                continue
            workspace.put_bytes(source)
            stored = workspace.put_bytes(output)
            name = str(v.get("name") or f"{v['module']}-{source_entry.name}")
            log.append(
                "map.adapter_ran",
                actor,
                data={
                    "module": str(v["module"]),
                    "function": function,
                    "source_sha256": source_sha,
                    "input": source_entry.ref,
                    "input_sha256": source_entry.sha256,
                    "output_name": name,
                    "output_sha256": stored.sha256,
                    "output_size": stored.size,
                    "reason": item.reason,
                },
                file_sha256=stored.sha256,
            )
            adapters.append(name)

    if not errors:
        result = build_map(log, workspace, preview=overrides)
        errors.extend(
            p.message for p in result.problems if p.code in _OVERRIDE_PROBLEMS and p.severity == "error"
        )
    if errors:
        rejected = reject(errors)
        return replace(rejected, supplied=tuple(supplied), adapters=tuple(adapters))

    workspace.put_bytes(raw)
    items: list[JSONValue] = [
        {"section": item.section, "index": item.index, "reason": item.reason[:300]} for item in overrides.items
    ]
    data = {
        "sha256": sha,
        "size": len(raw),
        "items": items,
        "supplied": [str(item.values["name"]) for item in overrides.section("supply")],
        "adapters": [
            f"{item.values['module']}.{item.values.get('function', 'convert')}" for item in overrides.section("adapter")
        ],
    }
    if len(overrides.text) + sum(len(item.reason[:300]) for item in overrides.items) <= _MAX_TEXT_IN_EVENT:
        data["text"] = overrides.text
    event = log.append("map.overrides_applied", actor, data=data, file_sha256=sha)
    return ApplyResult(True, sha, (), event.seq, tuple(supplied), tuple(adapters))


def _without(overrides: Overrides, section: str) -> Overrides:
    return replace(overrides, items=tuple(i for i in overrides.items if i.section != section))


def record_seed(log: EventLog, seed: str, actor: str, source: str) -> int:
    """Record an entry of a seed (``seed.entered``, `corla_observer.seeds.enter_seed`); return its seq.

    ``source`` says where it came from ("announced at the public dice roll,
    2026-07-13 10:10 MDT"). The seed takes effect at once; a second entry by
    another coordinator is optional (`corla_observer.seeds.confirm_seed`).

    Raises:
        ValueError: if the seed is not 20 digits, or the actor is invalid.
    """
    return enter_seed(log, seed, actor, source)


# ---------------------------------------------------------------------------
# Editing the overrides file


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"cannot write {type(value).__name__} as TOML")


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key, ensure_ascii=False)


#: Override keys whose values are always text, never parsed as TOML: a seed
#: like 09175059342981586505 must keep its leading zero.
TEXT_KEYS = frozenset(
    {"match", "name", "path", "url", "sha256", "module", "function", "value", "county", "kind", "use", "reason"}
)


def parse_cli_value(text: str, key: str | None = None) -> Any:
    """A ``--set KEY=VALUE`` value: TOML (a number, true/false, [list], {table}, "string") or else plain text.

    Values of `TEXT_KEYS` are always plain text.
    """
    if key in TEXT_KEYS:
        return text
    try:
        return tomllib.loads(f"v = {text}")["v"]
    except tomllib.TOMLDecodeError:
        return text


OVERRIDES_HEADER = f"""\
# Data map overrides for this election's workspace (corla-observer).
# Edit, then run `corla-observer map apply --workspace DIR --actor coordinator:ID`.
# `corla-observer map show --for-ai` prints the format.
format = {OVERRIDES_FORMAT}
"""


def append_override(path: Path, section: str, values: Mapping[str, Any]) -> str:
    """Append one override item to the file at ``path`` (created if missing); return the new text.

    The whole file is checked (`parse_overrides`) before anything is
    written, so a bad item is never left in it.

    Raises:
        OverridesError: if the result would not check out, or the section is
            a single table ([seed], [county_order]) that is already present.
    """
    if section not in _SECTION_KEYS:
        raise OverridesError([f"unknown section {section!r}; expected one of {', '.join(sorted(_SECTION_KEYS))}"])
    text = path.read_text(encoding="utf-8") if path.is_file() else OVERRIDES_HEADER
    header = f"[{section}]" if section in _TABLE_SECTIONS else f"[[{section}]]"
    if section in _TABLE_SECTIONS and re.search(rf"^\s*\[{section}\]\s*$", text, re.MULTILINE):
        raise OverridesError([f"{header} is already in {path.name}; edit it there"])
    lines = [header] + [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in values.items()]
    new_text = text.rstrip("\n") + "\n\n" + "\n".join(lines) + "\n"
    parse_overrides(new_text)
    path.write_text(new_text, encoding="utf-8")
    return new_text


# ---------------------------------------------------------------------------
# Describing the map


def _short_ref(ref: str) -> str:
    if ref.startswith("https://www.coloradosos.gov/pubs/elections/RLA/"):
        return "RLA/" + ref[len("https://www.coloradosos.gov/pubs/elections/RLA/") :]
    return ref


def describe(
    datamap: DataMap,
    *,
    for_ai: bool = False,
    show_all: bool = False,
    flags: Sequence[Flag] | None = None,
    view: ProblemView | None = None,
) -> str:
    """The data map as text: for a person, or (``for_ai``) compact and self-contained for an AI assistant.

    The AI form adds what the tool is, the full problem list with hints, the
    applied overrides' text and the overrides format, so it can be pasted
    into an assistant as is. ``flags`` (`corla_observer.flags.Flag`, from
    `corla_observer.flags.check_flags`) are listed after the problems, each
    with what to investigate, and the problems a flag already reports are
    then left out (`corla_observer.flags.unflagged_problems`).

    ``view`` (`corla_observer.due.view_for`) judges the problems as of a
    moment: inputs not yet due are summed up as "not yet expected" instead
    of listed as problems. Without it, every problem is listed. Either way,
    a problem many counties share is one line (`corla_observer.due.group_problems`).
    """
    out: list[str] = []
    w = out.append
    if for_ai:
        w("# corla-observer data map")
        w(
            "corla-observer supports independent observers of Colorado's risk-limiting audits. It fetches the "
            "Secretary of State's Audit Center files, classifies each by content, and predicts CORLA's ballot "
            "selections from the county manifests, the seed and the targeted contests. This is the election's "
            "data map: what each file was taken to be, what was chosen for sampling, and what is wrong or "
            "missing. Corrections are made only through the overrides file (format below), each with a reason; "
            "the coordinator applies it and it is recorded in a hash-chained event log."
        )
        w("")
    w(f"election: {datamap.election} ({datamap.election_source})")
    if datamap.preview:
        w("PREVIEW of an unapplied overrides file")
    if datamap.overrides_sha256:
        applied = f"applied in event {datamap.overrides_seq}" if datamap.overrides_seq else "not applied"
        w(f"overrides: sha256 {datamap.overrides_sha256[:16]}, {applied}")
    else:
        w("overrides: none applied")
    w(f"seed: {datamap.seed.value + ' (' + datamap.seed.source + ')' if datamap.seed else 'none'}")
    for label, moment in (("roll time", datamap.roll_time), ("first round started", datamap.round_start)):
        w(f"{label}: {colorado_time(moment.value) + ' (' + moment.source + ')' if moment else 'not known'}")
    w(f"county-ID order: {datamap.county_order.source}")
    if datamap.county_aliases:
        w("county aliases: " + ", ".join(f"{k!r} -> {v}" for k, v in sorted(datamap.county_aliases.items())))
    for adjustment in datamap.adjustments:
        w(f"manifest adjustment, {adjustment.county}: {json.dumps(adjustment_to_dict(adjustment), ensure_ascii=False)}")
    w("")
    w(f"manifests ({len(datamap.manifests)} counties, ballot cards each):")
    ordered = sorted(datamap.manifests.items(), key=lambda kv: county_key(kv[0]))
    if for_ai and not show_all:
        plain = [f"{c} {m.ballot_cards}" for c, m in ordered if not m.agrees_with and m.ref.startswith("https://")]
        w("  from the audit center: " + ", ".join(plain))
        ordered = [(c, m) for c, m in ordered if m.agrees_with or not m.ref.startswith("https://")]
    for county, chosen in ordered:
        agree = f"; same batches as {', '.join(_short_ref(r) for r in chosen.agrees_with)}" if chosen.agrees_with else ""
        w(f"  {county}: {_short_ref(chosen.ref)} ({len(chosen.batches)} batches, {chosen.ballot_cards} cards){agree}")
        if show_all:
            for note in chosen.notes:
                w(f"    {note}")
    w("")
    sources = Counter(spec.count_source for spec in datamap.contests)
    common = sources.most_common(1)[0][0] if sources else ""
    w(f"targeted contests ({len(datamap.contests)}; sample counts from {common} unless noted):")
    for spec in datamap.contests:
        counties = "statewide" if spec.statewide else ", ".join(spec.counties)
        count = spec.sample_count if spec.sample_count is not None else "estimate"
        source = "" if spec.count_source == common else f" ({spec.count_source})"
        w(f"  {spec.name} [{counties}]: {count}{source}")
        for note in spec.notes:
            w(f"    {note}")
    if datamap.upload_hashes:
        w("")
        w("uploads compared with the audit center and CORLA's upload hashes:")
        for check in datamap.upload_hashes:
            for line in check.lines():
                w(f"  {line}")
    if datamap.audit_boards:
        w("")
        w("audit boards: " + "; ".join(f"{c} {s.value} ({s.source})" for c, s in sorted(datamap.audit_boards.items())))
    w("")
    by_kind = Counter(e.kind for e in datamap.entries if not e.ignored)
    w(f"files ({len(datamap.entries)}; in use by kind: " + ", ".join(f"{k} {n}" for k, n in sorted(by_kind.items())) + "):")
    for entry in datamap.entries:
        interesting = (
            show_all
            or entry.ignored
            or entry.source != "fetch"
            or entry.kind != entry.classified_kind
            or entry.county != entry.classified_county
            or entry.kind not in ("county_manifest", "html_page", "unknown")
            or len(entry.notes) > 1
            or any("same" not in n and "used as" not in n for n in entry.notes)
        )
        if not interesting:
            continue
        status = "IGNORED " if entry.ignored else ""
        fmt = entry.evidence.get("format")
        sim = " (from the mirror)" if entry.simulated else ""
        w(
            f"  {status}{_short_ref(entry.ref)}{sim}: {entry.kind}"
            + (f", {entry.county}" if entry.county else "")
            + (f", {fmt}" if fmt and entry.kind == "unknown" else "")
            + f", sha256 {entry.sha256[:12]}"
        )
        for note in entry.notes:
            w(f"    {note}")
    if not show_all:
        w("  (manifests used as is and pages are not listed; `map show --all` lists every file)")
    w("")
    from corla_observer.due import stage, view_lines

    if view is None:
        problems = datamap.check()
        if flags is not None:
            from corla_observer.flags import unflagged_problems

            problems = unflagged_problems(problems, flags)
        followed = sorted({e.county for e in datamap.entries if e.source == "upload" and e.county})
        view = stage(datamap, problems, now=None, calendar=_no_calendar(), followed=followed)
    out.extend(view_lines(view))
    if flags is not None:
        w("")
        high = sum(1 for f in flags if f.severity == "high")
        w(f"flags ({len(flags)}, {high} high): changes and uploads to investigate" if flags else "flags: none")
        from corla_observer.flags import flag_lines

        for flag in flags:
            out.extend(flag_lines(flag))
    if for_ai:
        w("")
        w("## The overrides file now applied")
        w(datamap.overrides_text.rstrip() if datamap.overrides_text else "(none)")
        w("")
        w("## Overrides format")
        w(OVERRIDES_FORMAT_HELP.rstrip())
        w("")
        w(
            "## Adapters\nAn [[adapter]] names workspace/adapters/<module>.py, whose convert(data: bytes, "
            "context: dict) -> bytes | str turns one file into the layout the tool expects (for a manifest: a "
            "CSV with County, Tabulator, Batch, # of Ballot Cards, Location). context has name, ref, kind and "
            "county. Keep it short and deterministic: no network, no other files. Its source hash, input and "
            "output are recorded."
        )
        w("")
        w(
            "## What to suggest\nPropose the smallest overrides that fix the problems above, each with a reason "
            "naming the evidence (a file and the lines in it). Do not guess data that should come from a "
            "county or the Secretary of State: say what to ask for instead."
        )
    return "\n".join(out) + "\n"


def _no_calendar() -> Any:
    from corla_observer.due import Calendar

    return Calendar({}, "no calendar")


def explain(datamap: DataMap, match: str) -> str:
    """Why a file is in the map as it is: its classification evidence, overrides and use."""
    found = _resolve(datamap.entries, match)
    if not found:
        return f"{match!r} matches no file in the data map\n"
    out: list[str] = []
    for entry in found:
        out.append(f"{entry.ref}")
        out.append(f"  source: {entry.source}" + (f", simulated: {json.dumps(entry.simulated)}" if entry.simulated else ""))
        out.append(f"  sha256: {entry.sha256}  size: {entry.size}")
        out.append(f"  election: {entry.election}")
        out.append(f"  classified as: {entry.classified_kind}" + (f", county {entry.classified_county}" if entry.classified_county else ""))
        out.append(f"  used as: {'IGNORED' if entry.ignored else entry.kind}" + (f", county {entry.county}" if entry.county else ""))
        evidence = entry.evidence
        if "matched_columns" in evidence:
            out.append(f"  header row {evidence.get('header_row')}: matched {json.dumps(evidence['matched_columns'], ensure_ascii=False)}")
        why = {k: v for k, v in evidence.items() if k not in ("matched_columns", "header_row")}
        out.append(f"  evidence: {json.dumps(why, ensure_ascii=False, sort_keys=True)[:1500]}")
        name_county = county_from_filename(entry.name)
        if name_county:
            out.append(f"  its name suggests {name_county}")
        for note in entry.notes:
            out.append(f"  - {note}")
        for county, chosen in datamap.manifests.items():
            if chosen.ref == entry.ref:
                out.append(f"  chosen as {county}'s manifest: {len(chosen.batches)} batches, {chosen.ballot_cards} cards")
        for problem in datamap.problems:
            if entry.ref in problem.refs:
                out.append(f"  {problem.severity.upper()} {problem.code}: {problem.message}")
                if problem.hint:
                    out.append(f"    hint: {problem.hint}")
    return "\n".join(out) + "\n"
