"""Uploads without saying what a file is or whose: its kind and county detected from its content.

A coordinator uploads one or more files, and nothing more; `detect` works
out what each one is and which county it is from:

**Kind** (`KINDS`), by content (`corla_observer.classify.classify_bytes`,
`corla_observer.enr.results_form`), never by name:

- ``redacted_cvr``: a Dominion cast vote record export (classified ``cvr``);
- ``manifest``: a county's ballot manifest (``county_manifest``);
- ``pull_list``: CORLA's "List of ballots to audit" (``pull_list``);
- ``results``: a Clarity election night results file (detail.xml,
  detail.xlsx, either zipped, summary.csv or .zip, summary.json) that
  `corla_observer.enr.read_results` reads with at least one contest.

**County**, from the evidence below, strongest first; each piece found is
listed in `Detection.evidence`:

- the file itself: a CVR's first row names its election ("2026 Weld County
  Primary Election"); a manifest's or pull list's County column (or a
  manifest's first header cell); a Clarity detail file's own region and
  election name, or its ballots cast by county (more than one county: the
  state's results);
- the file's name ("Weld_CVR_Export_20260709.csv"), weak evidence, used
  only when the content names no county, and only for one of the
  workspace's counties;
- the workspace's known counties (`known_counties`: the election setup's
  pilot counties, the dashboard's counties, and every county something has
  arrived from) are the cross-check: a county the content names that is
  not among them is accepted with a warning.

When the content contradicts the file name, the content wins and the
contradiction is a warning. When the kind or the county cannot be told
(nothing names a county, or the content names several), `Detection` lists
the candidates and the coordinator chooses (`PendingStore` keeps the file
in the staging directory meanwhile); it is never guessed.

`take` detects a file (with optional overrides, ``--kind`` and
``--county`` on the command line, or the coordinator's choice), records an
``upload.detected`` event (``original_name``, ``sha256``, ``size``,
``kind``, ``county``, ``evidence``, ``warnings``, ``chosen``: which of the
kind and county the coordinator chose rather than the content showing
them) and passes it on to `corla_observer.uploads.receive` (with its
redaction check and every other safety check) or
`corla_observer.enr.receive_results`.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from corla_observer.classify import (
    COLORADO_COUNTIES,
    Classification,
    canonical_county,
    classify_bytes,
    county_from_filename,
    decode_text,
    known_county,
    test_county,
)
from corla_observer.enr import (
    MAX_RESULTS_UPLOAD_BYTES,
    ResultsError,
    ResultsUpload,
    detail_header,
    read_results,
    receive_results,
    results_form,
)
from corla_observer.events import EventLog, JSONValue, check_actor
from corla_observer.redaction import DEFAULT_THRESHOLD
from corla_observer.uploads import (
    MAX_UPLOAD_BYTES,
    STAGING_DIRNAME,
    UploadError,
    UploadResult,
    receive,
)
from corla_observer.workspace import CHUNK_SIZE, Workspace

#: The kinds of file detected, as `corla_observer.uploads.KINDS` plus election night results.
KINDS = ("redacted_cvr", "manifest", "pull_list", "results")
KIND_LABELS = {
    "redacted_cvr": "Redacted CVR",
    "manifest": "Ballot manifest",
    "pull_list": "Pull list",
    "results": "Election night results",
}
#: The classifier's kinds that are upload kinds.
_UPLOAD_KIND = {"cvr": "redacted_cvr", "county_manifest": "manifest", "pull_list": "pull_list"}

#: Files up to this size are read whole to detect them; larger ones (only a CVR can be) by their first `HEAD_BYTES`.
FULL_READ_BYTES = 64 * 1024 * 1024
HEAD_BYTES = 4 * 1024 * 1024
#: The most files in one upload.
MAX_FILES = 20
#: The most bytes in one upload, all files together: room for two of the largest CVRs CORLA has recorded (Denver's, 311 MB).
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
#: The largest single file accepted before its kind is known: the largest of any kind's limit.
MAX_FILE_BYTES = max(*MAX_UPLOAD_BYTES.values(), MAX_RESULTS_UPLOAD_BYTES)

#: The note recorded for a results file uploaded without a source URL or a note.
DEFAULT_RESULTS_NOTE = "uploaded by hand on the coordinator's upload page"


@dataclass(frozen=True)
class Detection:
    """What a file was found to be, whose, and why."""

    kind: str | None
    #: The county, or for results ``state`` or a county; None if it could not be told.
    county: str | None
    #: Each piece of evidence used, in words.
    evidence: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    #: The candidates to choose from where the kind or the county could not be told.
    kinds: tuple[str, ...] = ()
    counties: tuple[str, ...] = ()
    #: What the classifier called it, and the results form, if any.
    classified_as: str | None = None
    results_form: str | None = None
    #: Which of "kind" and "county" were chosen (an override or the coordinator's choice) rather than detected.
    chosen: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.kind is not None and self.county is not None

    def problem(self) -> str:
        """Why it is not complete, in words ("" if it is)."""
        parts = []
        if self.kind is None:
            parts.append("what kind of file this is")
        if self.county is None:
            if self.counties and test_county(self.counties[0]) and self.kind:
                return f"names {self.counties[0]}, a fictional test county: a test county's files are used only when it is chosen"
            parts.append("whose results these are" if self.kind == "results" else "which county this file is from")
        return "could not tell " + " or ".join(parts) if parts else ""

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "kind": self.kind,
            "county": self.county,
            "evidence": list(self.evidence),
            "warnings": list(self.warnings),
            "candidate_kinds": list(self.kinds),
            "candidate_counties": list(self.counties),
            "classified_as": self.classified_as,
            "results_form": self.results_form,
            "chosen": list(self.chosen),
        }


# ---------------------------------------------------------------------------
# Counties named in text


def _county_pattern(name: str) -> re.Pattern[str]:
    words = [re.escape(w) for w in name.split()]
    return re.compile(r"(?<![A-Za-z])" + r"\s*".join(words) + r"(?![A-Za-z])", re.IGNORECASE)


_COUNTY_PATTERNS = [(name, _county_pattern(name)) for name in sorted(COLORADO_COUNTIES, key=len, reverse=True)]


def counties_in_text(text: str) -> list[str]:
    """The Colorado counties named in a line of text, such as an election's name ("2026 Weld County Primary Election"), in order of appearance.

    Longer names are matched first, so that "Rio Grande" is not also read as
    "Grand". A name followed by "County" is preferred: if any are, only they
    count, so "2026 Lake County Primary" is Lake even if a word elsewhere is
    another county's name.
    """
    text = text[:500]
    spans: list[tuple[int, int, str, bool]] = []
    for name, pattern in _COUNTY_PATTERNS:
        for match in pattern.finditer(text):
            if any(a < match.end() and match.start() < b for a, b, _, _ in spans):
                continue
            followed = bool(re.match(r"\s*county\b", text[match.end() :], re.IGNORECASE))
            spans.append((match.start(), match.end(), name, followed))
    spans.sort()
    strong = [name for _, _, name, followed in spans if followed]
    found = strong or [name for _, _, name, _ in spans]
    return list(dict.fromkeys(found))


def cvr_election_name(data: bytes) -> str | None:
    """The first cell of a Dominion CVR export's first row: the election's name ("2026 Weld County Primary Election")."""
    text, _ = decode_text(data[:65536])
    first = text.split("\n", 1)[0].strip("\r﻿")
    cell = first.split(",", 1)[0].strip().strip('"').strip()
    if cell.startswith("=") and cell[1:2] == '"':
        cell = cell[2:].rstrip('"')
    return cell or None


def county_in_name(name: str) -> str | None:
    """The county a file's name suggests (weak evidence): `corla_observer.classify.county_from_filename`, or else the one county named as a word in it ("Weld_ballots_to_audit.csv")."""
    found = county_from_filename(name)
    if found:
        return found
    stem = name.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    named = counties_in_text(re.sub(r"[_\-.]+", " ", stem))
    return named[0] if len(named) == 1 else None


# ---------------------------------------------------------------------------
# Detection


def known_counties(workspace: Workspace, log: EventLog | None = None, extra: Iterable[str] = ()) -> list[str]:
    """The counties this workspace is about: the election setup's pilot counties, ``extra``, and every county something has arrived from."""
    from corla_observer.election import SetupError, load_setup

    found: set[str] = set()
    try:
        setup = load_setup(workspace)
    except (SetupError, OSError, ValueError):
        setup = None
    if setup is not None:
        found.update(setup.pilot_counties)
    for name in extra:
        county = known_county(name)
        if county:
            found.add(county)
    if log is not None:
        for event in log.iter_events():
            if event.kind in ("file.received", "upload.detected", "extract.built", "audit_boards.recorded") and event.county:
                county = known_county(event.county)
                if county:
                    found.add(county)
    return sorted(found)


def _read_for_detection(path: Path) -> tuple[bytes, bool]:
    """The file's bytes, whole if at most `FULL_READ_BYTES`, else its first `HEAD_BYTES` up to a line end; and whether it is whole."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size <= FULL_READ_BYTES:
            return f.read(), True
        head = f.read(HEAD_BYTES)
    cut = head.rfind(b"\n")
    return (head[: cut + 1] if cut > 0 else head), False


def _content_counties(kind: str, data: bytes, classification: Classification) -> tuple[list[str], list[str], list[str]]:
    """The counties the content names, with the evidence for them, and warnings."""
    evidence: list[str] = []
    warnings: list[str] = []
    found: list[str] = []
    if kind == "redacted_cvr":
        name = cvr_election_name(data)
        if name:
            found = counties_in_text(name)
            if found:
                evidence.append(f"the CVR's first row names the election \"{name}\"")
            elif _fictional_in(name):
                evidence.append(f"the CVR's first row names the election \"{name}\", of {_fictional_in(name)}, a fictional test county (practice only)")
            else:
                evidence.append(f"the CVR's first row names the election \"{name}\", which names no county")
    elif kind in ("manifest", "pull_list"):
        column = "County column"
        if classification.evidence.get("county_from_header_cell"):
            column = "first header cell"
        several = classification.evidence.get("counties")
        if classification.county is not None:
            county = canonical_county(classification.county)
            if test_county(classification.county):
                evidence.append(f"its {column} names {test_county(classification.county)}, a fictional test county (practice only)")
            elif county:
                found = [county]
                rows = classification.evidence.get("data_rows")
                evidence.append(f"its {column} names {county}" + (f" ({rows} rows)" if isinstance(rows, int) else ""))
            else:
                warnings.append(f"its county column holds {classification.county!r}, which is not a Colorado county")
        elif isinstance(several, list) and several:
            found = [c for c in (canonical_county(str(v)) for v in several) if c]
            evidence.append(f"its {column} names {len(found)} counties: {', '.join(found)}")
        else:
            label = "manifest" if kind == "manifest" else "pull list"
            evidence.append(f"the {label} has no County column")
    return found, evidence, warnings


def _fictional_in(text: str) -> str | None:
    """A fictional test county (`corla_observer.classify.TEST_COUNTIES`) named in ``text``, if any."""
    for word in re.findall(r"[A-Za-z]+", text):
        found = test_county(word)
        if found:
            return found
    return None


def _fictional(kind: str | None, data: bytes, classification: Classification) -> str | None:
    """The fictional test county a manifest, pull list or CVR names, if it names one: offered first, but used only when chosen."""
    if kind in ("manifest", "pull_list") and classification.county:
        return test_county(classification.county)
    if kind == "redacted_cvr":
        name = cvr_election_name(data)
        return _fictional_in(name) if name else None
    return None


def _results_scope(data: bytes, form: str) -> tuple[list[str], list[str]]:
    """Whose results a results file holds: ``["state"]``, counties, or nothing; with the evidence."""
    evidence: list[str] = []
    if not form.startswith("detail"):
        evidence.append(f"a {form} does not say whose results it is")
        return [], evidence
    try:
        results = read_results(data, "state")
    except (ResultsError, ValueError, KeyError, OSError):
        return [], evidence
    if len(results.ballots_cast) > 1:
        evidence.append(f"it has ballots cast for {len(results.ballots_cast)} counties: the state's results")
        return ["state"], evidence
    header = detail_header(data) if form in ("detail.xml", "detailxml.zip") else {}
    found: list[str] = []
    region = header.get("region", "")
    if region:
        county = canonical_county(region)
        if county:
            found.append(county)
            evidence.append(f"its Region is {region!r}")
        elif region.strip().lower() in ("co", "colorado", "state of colorado"):
            evidence.append(f"its Region is {region!r}: the state's results")
            return ["state"], evidence
    election = header.get("election_name") or results.election_name or ""
    if election:
        named = counties_in_text(election)
        if named:
            evidence.append(f"its election name is {election!r}")
            found += [c for c in named if c not in found]
    if len(results.ballots_cast) == 1:
        only = next(iter(results.ballots_cast))
        evidence.append(f"its ballots cast are for {only} only")
        if only not in found:
            found.append(only)
    return found, evidence


def detect(path: Path | str, original_name: str, known: Sequence[str] = ()) -> Detection:
    """Detect a file's kind and county (see the module documentation); ``known`` are the workspace's counties."""
    path = Path(path)
    data, whole = _read_for_detection(path)
    classification = classify_bytes(data)
    evidence: list[str] = []
    warnings: list[str] = []
    kind = _UPLOAD_KIND.get(classification.kind)
    form = ""
    if kind is not None:
        matched = classification.evidence.get("matched_columns")
        columns = ", ".join(str(v) for v in matched.values()) if isinstance(matched, dict) else ""
        evidence.append(f"{KIND_LABELS[kind]}: its columns match ({columns})" if columns else f"{KIND_LABELS[kind]}: by its content")
    elif whole:
        form = results_form(data)
        if form:
            try:
                contests = len(read_results(data, "state").contests)
            except (ResultsError, ValueError, KeyError, OSError):
                contests = 0
            if contests:
                kind = "results"
                evidence.append(f"{KIND_LABELS['results']}: a Clarity {form} with {contests} contests")
    if kind == "redacted_cvr" and not whole:
        evidence.append(f"(detected from its first {HEAD_BYTES // (1024 * 1024)} MB)")

    county: str | None = None
    candidates: list[str] = []
    named: list[str] = []
    if kind == "results":
        named, more = _results_scope(data, form)
        evidence += more
    elif kind is not None:
        named, more, warned = _content_counties(kind, data, classification)
        evidence += more
        warnings += warned
    by_name = county_in_name(original_name)
    if len(named) == 1:
        county = named[0]
        if by_name and by_name != county and county != "state":
            warnings.append(f"the file name suggests {by_name}, but its content names {county}: the content is used")
        elif by_name == county:
            evidence.append(f"its name ({original_name}) agrees")
    elif len(named) > 1:
        candidates = named
        evidence.append("the content names several counties: choose one")
    elif kind is not None and _fictional(kind, data, classification):
        # A test county is never taken from the content alone: it is offered first, and used only when chosen.
        candidates = [str(_fictional(kind, data, classification)), *known]
    elif kind is not None:
        if by_name and (not known or by_name in known):
            county = by_name
            evidence.append(f"from its file name only ({original_name}); the content names no county")
        elif by_name:
            evidence.append(f"its file name suggests {by_name}, which is not one of this workspace's counties")
            candidates = [by_name, *known]
        else:
            candidates = list(known) or list(COLORADO_COUNTIES)
            if kind == "results":
                candidates = ["state", *candidates]
    if county is not None and county != "state" and known and county not in known:
        warnings.append(f"{county} is not one of this workspace's counties ({', '.join(known)})")
    kinds: tuple[str, ...] = ()
    if kind is None:
        kinds = KINDS
        if classification.kind != "unknown":
            evidence.append(f"classified as {classification.kind.replace('_', ' ')}, which is not a kind uploaded here")
        else:
            fmt = classification.evidence.get("format")
            evidence.append(f"not recognized{f' ({fmt})' if isinstance(fmt, str) else ''}")
        candidates = candidates or list(known) or list(COLORADO_COUNTIES)
        other = canonical_county(classification.county or "")
        if other:
            evidence.append(f"it names {other}")
            candidates = [other, *candidates]
    return Detection(
        kind,
        county,
        tuple(evidence),
        tuple(warnings),
        kinds,
        tuple(dict.fromkeys(candidates)) if county is None else (),
        classification.kind,
        form or None,
    )


def with_choices(detection: Detection, kind: str | None = None, county: str | None = None) -> Detection:
    """The detection with the kind and county given by a person (an override or a choice), checked.

    Raises:
        UploadError: an unknown kind, or a county that is not a Colorado county (or ``state`` for results).
    """
    chosen = list(detection.chosen)
    evidence = list(detection.evidence)
    new_kind, new_county = detection.kind, detection.county
    if kind:
        if kind not in KINDS:
            raise UploadError(f"unknown kind {kind!r}; expected one of {', '.join(KINDS)}")
        if kind != detection.kind:
            chosen.append("kind")
            evidence.append(f"kind chosen as {KIND_LABELS[kind]}" + (f" (detected: {KIND_LABELS[detection.kind]})" if detection.kind else ""))
        new_kind = kind
    if county:
        if new_kind == "results" and county.strip().lower() == "state":
            named: str | None = "state"
        else:
            named = known_county(county)
        if named is None:
            raise UploadError(f"not a Colorado county: {county!r}")
        if named != detection.county:
            chosen.append("county")
            evidence.append(f"county chosen as {named}" + (f" (detected: {detection.county})" if detection.county else ""))
        new_county = named
    if new_kind != "results" and new_county == "state":
        new_county = None
    return Detection(
        new_kind,
        new_county,
        tuple(evidence),
        detection.warnings,
        () if new_kind else detection.kinds,
        () if new_county else (detection.counties or tuple(COLORADO_COUNTIES)),
        detection.classified_as,
        detection.results_form,
        tuple(dict.fromkeys(chosen)),
    )


# ---------------------------------------------------------------------------
# Taking a file in


@dataclass
class Intake:
    """What happened to one uploaded file."""

    original_name: str
    size: int
    detection: Detection
    upload: UploadResult | None = None
    results: ResultsUpload | None = None
    #: The pending file's id, when the coordinator must choose its kind or county.
    pending: str | None = None
    error: str | None = None
    seq: int | None = None

    @property
    def accepted(self) -> bool:
        if self.upload is not None:
            return self.upload.accepted
        return self.results is not None and self.results.accepted

    @property
    def rejected(self) -> bool:
        return (self.upload is not None and not self.upload.accepted) or (self.results is not None and not self.results.accepted) or (
            self.error is not None and self.pending is None
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _clean_name(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    return ("".join(ch for ch in base if ch.isprintable()).strip() or "(unnamed)")[:255]


def take(
    workspace: Workspace,
    log: EventLog,
    path: Path | str,
    original_name: str,
    actor: str,
    *,
    kind: str | None = None,
    county: str | None = None,
    known: Sequence[str] = (),
    detection: Detection | None = None,
    source_url: str = "",
    note: str = "",
    file_time: str | None = None,
    threshold: int = DEFAULT_THRESHOLD,
    session_id: str | None = None,
) -> Intake:
    """Detect a file's kind and county (or take ``kind`` and ``county`` as given), record ``upload.detected`` and receive it.

    If the kind or the county cannot be told, nothing is recorded and the
    returned `Intake` says so (``error``), with the detection's candidates;
    the caller asks the coordinator, keeping the file (`PendingStore`), or
    reports it (the command line). ``source_url``, ``note`` and
    ``file_time`` are for a results file (`corla_observer.enr.receive_results`);
    without a source URL or a note, `DEFAULT_RESULTS_NOTE` is recorded.

    Raises:
        UploadError: an invalid actor, kind or county given.
    """
    path = Path(path)
    try:
        check_actor(actor)
    except ValueError as exc:
        raise UploadError(str(exc)) from None
    name = _clean_name(original_name)
    size = path.stat().st_size
    found = detection or detect(path, name, known)
    found = with_choices(found, kind, county)
    if not found.complete:
        return Intake(name, size, found, error=found.problem())
    assert found.kind is not None and found.county is not None
    sha256 = _sha256(path)
    seq = log.append(
        "upload.detected",
        actor,
        county=found.county if found.county != "state" else None,
        file_sha256=sha256,
        session_id=session_id,
        data={"original_name": name, "sha256": sha256, "size": size} | found.to_dict(),
    ).seq
    intake = Intake(name, size, found, seq=seq)
    if found.kind == "results":
        if size > MAX_RESULTS_UPLOAD_BYTES:
            intake.error = f"a results file larger than {MAX_RESULTS_UPLOAD_BYTES} bytes is refused"
            log.append(
                "results.upload_rejected", actor, county=found.county if found.county != "state" else None, session_id=session_id,
                data={"original_name": name, "scope": found.county, "reason": intake.error, "sha256": sha256, "size": size, "form": ""},
            )  # fmt: skip
            return intake
        try:
            intake.results = receive_results(
                workspace, log, path.read_bytes(), name, actor, scope=found.county, source_url=source_url,
                note=note if (note.strip() or source_url.strip()) else DEFAULT_RESULTS_NOTE, file_time=file_time,
            )  # fmt: skip
        except ResultsError as exc:
            intake.error = str(exc)
        return intake
    intake.upload = receive(workspace, log, found.county, found.kind, path, name, actor, threshold=threshold)
    return intake


# ---------------------------------------------------------------------------
# Files waiting for the coordinator's choice

PENDING_DIRNAME = "pending"
#: How long a file waits for the coordinator's choice before it is deleted, in seconds.
PENDING_SECONDS = 2 * 60 * 60
#: The most files waiting at once; the oldest go first.
MAX_PENDING = 40
_PENDING_ID = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class Pending:
    id: str
    path: Path
    original_name: str
    actor: str
    detection: Detection
    created: float
    extra: Mapping[str, str] = field(default_factory=dict)


class PendingStore:
    """Files waiting for a coordinator to choose their kind or county, in ``<workspace>/staging/pending/`` (never backed up), deleted after `PENDING_SECONDS`."""

    def __init__(self, workspace: Workspace, clock: object = time.time) -> None:
        self.root = workspace.root / STAGING_DIRNAME / PENDING_DIRNAME
        self._clock = clock

    def _now(self) -> float:
        return float(self._clock())  # type: ignore[operator]

    def put(self, path: Path, original_name: str, actor: str, detection: Detection, extra: Mapping[str, str] | None = None) -> str:
        """Move a staged file here (same filesystem: the staging directory); return its id."""
        self.sweep()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        ident = secrets.token_hex(16)
        directory = self.root / ident
        directory.mkdir(mode=0o700)
        os.replace(path, directory / "file")
        meta = {
            "original_name": original_name,
            "actor": actor,
            "detection": detection.to_dict(),
            "created": self._now(),
            "extra": dict(extra or {}),
        }
        (directory / "meta.json").write_text(json.dumps(meta))
        return ident

    def get(self, ident: str, actor: str) -> Pending | None:
        """The waiting file with this id, if it is this actor's and has not expired."""
        if not _PENDING_ID.match(ident):
            return None
        directory = self.root / ident
        try:
            meta = json.loads((directory / "meta.json").read_text())
        except (OSError, ValueError):
            return None
        if meta.get("actor") != actor or self._now() - float(meta.get("created", 0)) > PENDING_SECONDS:
            return None
        d = meta["detection"]
        detection = Detection(
            d.get("kind"), d.get("county"), tuple(d.get("evidence") or ()), tuple(d.get("warnings") or ()),
            tuple(d.get("candidate_kinds") or ()), tuple(d.get("candidate_counties") or ()),
            d.get("classified_as"), d.get("results_form"), tuple(d.get("chosen") or ()),
        )  # fmt: skip
        extra = {str(k): str(v) for k, v in (meta.get("extra") or {}).items()}
        return Pending(ident, directory / "file", str(meta.get("original_name")), actor, detection, float(meta["created"]), extra)

    def discard(self, ident: str) -> None:
        if _PENDING_ID.match(ident):
            shutil.rmtree(self.root / ident, ignore_errors=True)
        try:
            self.root.rmdir()  # only if nothing else is waiting
        except OSError:
            pass

    def sweep(self) -> None:
        """Delete expired waiting files, and the oldest beyond `MAX_PENDING`."""
        if not self.root.is_dir():
            return
        entries = []
        for directory in self.root.iterdir():
            try:
                created = float(json.loads((directory / "meta.json").read_text()).get("created", 0))
            except (OSError, ValueError):
                created = 0.0
            entries.append((created, directory))
        entries.sort()
        now = self._now()
        for n, (created, directory) in enumerate(entries):
            if now - created > PENDING_SECONDS or len(entries) - n >= MAX_PENDING:
                shutil.rmtree(directory, ignore_errors=True)
