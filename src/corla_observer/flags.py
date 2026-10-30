"""Change flags: every file change and every upload an observer should look into, with what to investigate.

In July 2026, CORLA's upload status recorded a county manifest imported at
11:46 on roll day, after the dice roll at about 10:10, and a copy observers
held listed one batch where the imported file listed another, which changed
one selected ballot. So any change to the files the selection depends on after
the roll needs investigation, and so do many other changes. A `Flag` is one
such finding, with a ``severity`` (``high`` or ``normal``), a ``reason``, a
message and what to investigate.

The fetcher raises flags as ``fetch.alert`` events as it fetches
(`corla_observer.fetch`), with the roll time known then. `check_flags`
derives every flag from the whole event log and the data map, so a flag is
also found when the roll time becomes known only later, and merges in the
alert events already recorded. `record_flags` records the ones not yet
recorded as ``map.alert`` events. Flags describe what happened, so they stay
even after a file is corrected.

One problem is one flag. A flag that brings several findings together (every
row of the upload status about a county, every copy of a county's manifest,
every file with the same content) lists each in its ``aspects``, and the keys
the findings had on their own in its ``aliases``, so alert events recorded
under them are merged into it. `unflagged_problems` leaves out the data map
problems that a flag already reports, so a page lists each finding once.

The rules (``roll`` is `corla_observer.datamap.DataMap.roll_time`,
``round start`` its ``round_start``):

``changed_after_roll`` (high)
    A manifest, CVR, target contest list, contest list or county-ID file
    (`WATCHED_KINDS`), fetched or uploaded, that is new, changed or removed
    after the roll, with the time and both hashes. Fetched files of another
    election than the map's are not judged against its roll.
``upload_after_roll`` (high)
    Per county, every manifest or CVR upload that CORLA's upload status
    (``uploadStatus.csv`` or ``upload_status.csv``, every version fetched)
    records after the roll, each row with its file name, type, status and
    time (the status file's times are Colorado time).
``upload_hash_mismatch``, ``upload_failed`` (high)
    Per county, every upload the upload status marks ``HASH_MISMATCH`` or
    ``FAILED``, in one flag: ``upload_hash_mismatch`` if any row is a
    mismatch, else ``upload_failed``.
``manifest_differs_from_import`` (high)
    Per county whose import CORLA recorded, every manifest posted by the
    audit center, uploaded by a coordinator or supplied by an override that
    is not byte-identical to the last manifest CORLA recorded importing, each
    with whether it lists the same batches when the imported file is at hand.
``duplicate_content`` (high for manifests and CVRs, otherwise normal)
    Per content hash, identical content under more than one county's name,
    at any time, naming every county and file; each posted manifest whose
    file name names another county than its content is one of its aspects.
``manifest_under_other_name`` (high)
    Per content hash, a posted manifest whose file name names one county and
    whose content names another, when no other county's file has the same
    content.
``seed_entry_mismatch`` (high)
    An optional second entry of the seed, by another coordinator, that did
    not match the entry in effect (`corla_observer.seeds`), with both values.
``seed_changed`` (high)
    A re-entry of the seed, by any coordinator, with a different value from
    the entry it replaced, with both values.
``seed_differs_from_audit_center`` (high)
    A seed published on the audit center (a seed file, or a State or county
    audit report's "Audit Random Seed") that differs from the seed in
    effect.
``extract_sources_disagree`` (high)
    A county's latest extract whose two sources of selected ballots, the
    county's pull list and CORLA's posted selections, list different
    ballots (`corla_observer.extract`): the one that arrived first defines
    the extract, and the other's differences are listed.
``totals_differ`` (normal)
    A difference the totals check (`corla_observer.totals`) found worth a
    flag in its latest run: the results against a county's redacted CVR or
    CORLA's vote totals, CORLA's contest list against the manifests, a
    manifest against the ballots cast, or two results files generated at
    the same time; one flag per difference, with the numbers.
``changed``, ``missing`` (normal)
    Any other file that changes or disappears, at any time (pages are
    compared after normalization by the fetcher and are not flagged).
``changed_after_round_start`` (normal)
    Any file that changes or disappears after the first round started.

Standard library only.
"""

from __future__ import annotations

import urllib.parse
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from corla_observer.classify import Classification, canonical_county, classify_bytes, county_from_filename
from corla_observer.datamap import (
    DataMap,
    Problem,
    build_map,
    column,
    election_from_url,
    imported_manifests,
    load_manifest_bytes,
    parse_time,
    role_column,
    table_rows,
)
from corla_observer.events import Event, EventLog, JSONValue, check_actor
from corla_observer.localtime import colorado_time, colorado_wall_time
from corla_observer.seeds import seed_state
from corla_observer.workspace import ObjectNotFoundError, Workspace

#: Kinds of file whose change after the roll is a high-severity flag.
WATCHED_KINDS = frozenset({"county_manifest", "cvr", "target_contests", "contests_list", "contest_ids_by_county"})
#: Severities, most severe first.
SEVERITIES = ("high", "normal")
#: Kinds whose identical content under two counties' names is high severity.
_COUNTY_FILE_KINDS = frozenset({"county_manifest", "cvr"})
#: The data map's kind for each upload kind.
_UPLOAD_KIND = {"redacted_cvr": "cvr", "manifest": "county_manifest", "pull_list": "pull_list"}
_CHANGE_REASONS = frozenset({"changed", "missing", "changed_after_roll", "changed_after_round_start"})
_RLA = "https://www.coloradosos.gov/pubs/elections/RLA/"
#: Most refs kept in a recorded flag.
_MAX_REFS = 40

#: What to investigate, by reason.
INVESTIGATE: dict[str, str] = {
    "extract_sources_disagree": (
        "the county's pull list and CORLA's posted selections should list the same ballots: compare the extract's "
        "checks.other_source lists, ask the county which list its boards follow, and check the manifest and the "
        "seed the prediction used"
    ),
    "changed_after_roll": (
        "the selection depends on this file, and it changed after the dice roll: compare the two versions, ask the "
        "Secretary of State or the county when and why it changed, find out which version CORLA used, and predict "
        "again from that one"
    ),
    "upload_after_roll": (
        "CORLA imported this file after the roll, so the selection may rest on a file observers did not have: get "
        "the imported file, compare it with the posted and county-provided copies, and predict again"
    ),
    "upload_hash_mismatch": (
        "the hash the county submitted is not the hash of the file CORLA received: ask the county which file it "
        "meant, and check that the file CORLA finally imported is the one it hashed"
    ),
    "upload_failed": (
        "CORLA could not import this upload: check that a later import for this county and type succeeded, and "
        "what was changed to make it succeed"
    ),
    "manifest_differs_from_import": (
        "this copy is not the file CORLA imported: get the imported file from the county, or compare batch by "
        "batch; different batches change which ballots are selected"
    ),
    "manifest_under_other_name": (
        "the county named in the file name may have no correct manifest posted: check with the Secretary of State "
        "and the county, and use the county's own copy"
    ),
    "duplicate_content": (
        "identical files under different counties' names: one county's file is probably posted under another's "
        "name; find each county's real file"
    ),
    "seed_entry_mismatch": (
        "a second coordinator entered a different seed: check the seed announced at the dice roll (the "
        "ceremony's record, observers' notes), and enter the right one again with `seed set`; check any prediction "
        "or extract made from a wrong seed"
    ),
    "seed_changed": (
        "the seed was entered again with a different value: check which was announced at the dice roll, and "
        "predict again and rebuild any extract made from the wrong one"
    ),
    "seed_differs_from_audit_center": (
        "the seed the audit center publishes is not the seed entered here: check which was announced at the dice "
        "roll, ask the Secretary of State, and if ours was wrong enter the right one and predict again"
    ),
    "totals_differ": (
        "the results and the other counts should agree: ask the county why they differ, since ballots the CVR or "
        "CORLA's tally does not have may not be in the audit's population; check the files' times and the "
        "comparison's other lines on /coord/estimates"
    ),
    "changed": "compare the two versions and note what changed and whether it matters to the audit",
    "missing": "check whether the file was moved or withdrawn; the last copy is kept in the workspace",
    "changed_after_round_start": (
        "files should not change while the audit is under way: compare the versions and ask the Secretary of "
        "State why it changed"
    ),
}


def effective_time(event: Event) -> str:
    """When an event's file was really fetched or received (UTC).

    A rehearsal's events carry the time the data was gathered in their
    ``simulated`` marker; any other event's time is its ``server_time``.
    """
    simulated = event.data.get("simulated")
    if isinstance(simulated, dict) and isinstance(simulated.get("time"), str):
        return str(simulated["time"])
    return event.server_time


def change_rule(
    kind: str | None, change: str, moment: str, roll: str | None, round_start: str | None
) -> tuple[str, str] | None:
    """The ``(reason, severity)`` of a file of ``kind`` that is ``new``, ``changed`` or ``missing`` at ``moment``.

    None if it is not flagged: a new file other than a watched one after the
    roll, or a page. ``roll`` and ``round_start`` are UTC times in the event
    log's format, or None if not known.
    """
    if kind == "html_page":
        return None
    if kind in WATCHED_KINDS and roll is not None and moment > roll:
        return "changed_after_roll", "high"
    if change == "new":
        return None
    if round_start is not None and moment > round_start:
        return "changed_after_round_start", "normal"
    return ("missing" if change == "missing" else "changed"), "normal"


@dataclass(frozen=True)
class Flag:
    """One change or upload to investigate; see the module documentation."""

    severity: str
    reason: str
    message: str
    #: Identifies the finding, so it is listed and recorded once.
    key: str
    counties: tuple[str, ...] = ()
    #: When it happened (UTC), where known.
    time: str | None = None
    refs: tuple[str, ...] = ()
    sha256: str | None = None
    previous_sha256: str | None = None
    details: dict[str, JSONValue] = field(default_factory=dict)
    #: The seqs of the alert events (``fetch.alert``, ``map.alert``) recording it.
    alerts: tuple[int, ...] = ()
    #: The separate findings this flag groups (one per file, upload row or
    #: copy), each a dict with at least ``reason`` and ``message``.
    aspects: tuple[dict[str, JSONValue], ...] = ()
    #: Keys the grouped findings had on their own (alert events recorded
    #: under them are merged into this flag).
    aliases: tuple[str, ...] = ()

    @property
    def reasons(self) -> tuple[str, ...]:
        """The reason, then every other reason among its aspects."""
        found = [self.reason] + [str(a["reason"]) for a in self.aspects if isinstance(a.get("reason"), str)]
        return tuple(dict.fromkeys(found))

    @property
    def investigate(self) -> str:
        return "; and ".join(INVESTIGATE.get(r, "look at the file and the change") for r in self.reasons)

    def describe(self) -> str:
        """One line: severity, reason, message, time and hashes."""
        hashes = ""
        if self.previous_sha256 and self.sha256:
            hashes = f"; sha256 {_short_hash(self.previous_sha256)} -> {_short_hash(self.sha256)}"
        elif self.previous_sha256 or self.sha256:
            hashes = f"; sha256 {_short_hash(self.previous_sha256 or self.sha256)}"
        when = f" [{colorado_time(self.time)}]" if self.time else ""
        return f"{self.severity.upper()} {self.reason}: {self.message}{hashes}{when}"

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "severity": self.severity,
            "reason": self.reason,
            "message": self.message,
            "investigate": self.investigate,
            "key": self.key,
            "counties": list(self.counties),
            "time": self.time,
            "refs": list(self.refs[:_MAX_REFS]),
            "sha256": self.sha256,
            "previous_sha256": self.previous_sha256,
            "details": dict(self.details),
            "alerts": list(self.alerts),
            "aspects": [dict(a) for a in self.aspects[:_MAX_REFS]],
            "aliases": list(self.aliases[:_MAX_REFS]),
            "reasons": list(self.reasons),
        }


def _short_hash(value: str | None) -> str:
    return value[:12] if value else "none"


def short_ref(ref: str) -> str:
    """An audit center URL as ``RLA/...``; any other ref unchanged."""
    return "RLA/" + urllib.parse.unquote(ref[len(_RLA) :]) if ref.startswith(_RLA) else ref


def _url_name(url: str) -> str:
    return urllib.parse.unquote(urllib.parse.urlsplit(url).path).rstrip("/").rsplit("/", 1)[-1]


def _str(value: JSONValue) -> str | None:
    return value if isinstance(value, str) else None


class _Kinds:
    """Each stored file's classification: as recorded in the log, else by classifying it again."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.recorded: dict[str, Classification] = {}

    def record(self, sha256: str | None, data: Mapping[str, JSONValue]) -> None:
        if sha256 and sha256 not in self.recorded:
            evidence = data.get("evidence")
            self.recorded[sha256] = Classification(
                _str(data.get("kind")) or "unknown",
                _str(data.get("county")),
                dict(evidence) if isinstance(evidence, dict) else {},
            )

    def get(self, sha256: str | None) -> Classification:
        if sha256 is None:
            return Classification("unknown")
        if sha256 not in self.recorded:
            try:
                self.recorded[sha256] = classify_bytes(self.workspace.get_bytes(sha256))
            except ObjectNotFoundError:
                self.recorded[sha256] = Classification("unknown", None, {"error": "not in the workspace"})
        return self.recorded[sha256]


@dataclass
class _Seen:
    """Where a file's content was seen: ``(ref, county claimed, time)``, in order."""

    places: list[tuple[str, str | None, str]] = field(default_factory=list)


def check_flags(log: EventLog, workspace: Workspace, datamap: DataMap | None = None) -> list[Flag]:
    """Every flag the event log and the data map raise, high severity first, then by time.

    ``datamap`` is built from the log if not given. Alert events already in
    the log for a flag are listed in its ``alerts``; an alert with no derived
    counterpart (an earlier version's, say) is listed as a flag of its own.
    """
    datamap = datamap or build_map(log, workspace)
    roll = datamap.roll_time.value if datamap.roll_time else None
    round_start = datamap.round_start.value if datamap.round_start else None
    events = list(log.iter_events())
    kinds = _Kinds(workspace)
    for event in events:
        if event.kind == "fetch.classified":
            kinds.record(_str(event.data.get("sha256")), event.data)
        elif event.kind == "file.classified" and isinstance(event.data.get("classification"), dict):
            kinds.record(event.file_sha256, event.data["classification"])  # type: ignore[arg-type]

    flags: dict[str, Flag] = {}

    def add(flag: Flag | None) -> None:
        if flag is not None:
            flags.setdefault(flag.key, flag)

    seen: dict[str, _Seen] = {}
    uploads: dict[tuple[str, str], str] = {}
    status_files: list[tuple[str, str]] = []
    for event in events:
        data = event.data
        moment = effective_time(event)
        if event.kind == "fetch.file":
            url, sha = _str(data.get("url")), _str(data.get("sha256"))
            if url and sha and data.get("normalized_sha256") is None:
                name_county = county_from_filename(_url_name(url))
                seen.setdefault(sha, _Seen()).places.append((url, name_county, moment))
                if kinds.get(sha).kind == "upload_status" and (url, sha) not in status_files:
                    status_files.append((url, sha))
        elif event.kind in ("fetch.new", "fetch.changed", "fetch.missing"):
            if data.get("compared") == "normalized_html":
                continue
            url = _str(data.get("url"))
            if not url:
                continue
            change = event.kind.removeprefix("fetch.")
            sha = None if change == "missing" else _str(data.get("sha256"))
            previous = _str(data.get("previous_sha256"))
            if sha is None and previous is None:
                continue
            same = election_from_url(url) in (None, datamap.election) or datamap.election is None
            add(
                _change_flag(
                    url, change, moment, sha, previous, kinds, roll if same else None, round_start if same else None
                )
            )
        elif event.kind == "file.received" and event.county:
            upload_kind, sha = _str(data.get("kind")), _str(data.get("sha256"))
            if not upload_kind or not sha:
                continue
            before = uploads.get((event.county, upload_kind))
            uploads[(event.county, upload_kind)] = sha
            if before == sha:
                continue
            ref = f"upload:{event.county}:{upload_kind}"
            seen.setdefault(sha, _Seen()).places.append((ref, event.county, moment))
            add(
                _change_flag(
                    ref,
                    "new" if before is None else "changed",
                    moment,
                    sha,
                    before,
                    kinds,
                    roll,
                    round_start,
                    kind=_UPLOAD_KIND.get(upload_kind, upload_kind),
                    county=event.county,
                )
            )

    for flag in _status_flags(status_files, kinds, workspace, roll):
        add(flag)
    for flag in _import_flags(datamap, workspace, seen, kinds):
        add(flag)
    for flag in _name_flags(seen, kinds):
        add(flag)
    for flag in _seed_flags(events, datamap):
        add(flag)
    for flag in _extract_flags(events):
        add(flag)
    for flag in _totals_flags(events):
        add(flag)

    by_alias = {alias: flag.key for flag in flags.values() for alias in flag.aliases}
    for event in events:
        if event.kind not in ("fetch.alert", "map.alert"):
            continue
        key = _alert_key(event.data)
        if key is None:
            continue
        key = key if key in flags else by_alias.get(key, key)
        if key in flags:
            if event.seq not in flags[key].alerts:
                flags[key] = replace(flags[key], alerts=flags[key].alerts + (event.seq,))
        else:
            flags[key] = _flag_from_alert(event, key)
    return sorted(
        flags.values(),
        key=lambda f: (SEVERITIES.index(f.severity) if f.severity in SEVERITIES else 9, f.time or "~", f.reason, f.key),
    )


def _change_flag(
    ref: str,
    change: str,
    moment: str,
    sha: str | None,
    previous: str | None,
    kinds: _Kinds,
    roll: str | None,
    round_start: str | None,
    *,
    kind: str | None = None,
    county: str | None = None,
) -> Flag | None:
    new_class = kinds.get(sha) if sha else None
    old_class = kinds.get(previous) if previous else None
    if kind is None:
        candidates = [c.kind for c in (new_class, old_class) if c is not None]
        kind = next((k for k in candidates if k in WATCHED_KINDS), candidates[0] if candidates else None)
    rule = change_rule(kind, change, moment, roll, round_start)
    if rule is None:
        return None
    reason, severity = rule
    county = county or next((c.county for c in (new_class, old_class) if c is not None and c.county), None)
    what = {"new": "new", "changed": "changed", "missing": "removed"}[change]
    if reason == "changed_after_roll":
        when = f" after the roll ({colorado_time(roll)})"
    elif reason == "changed_after_round_start":
        when = f" after the first round started ({colorado_time(round_start)})"
    else:
        when = ""
    subject = f"{short_ref(ref)} ({kind}" + (f", {county}" if county else "") + ")"
    details: dict[str, JSONValue] = {"ref": ref, "change": change, "kind": kind}
    return Flag(
        severity,
        reason,
        f"{subject} {what}{when}",
        key=f"change|{ref}|{sha if change != 'missing' else previous}",
        counties=(county,) if county else (),
        time=moment,
        refs=(ref,),
        sha256=sha,
        previous_sha256=previous,
        details=details,
    )


@dataclass(frozen=True)
class _StatusRow:
    """One upload row of CORLA's upload status that needs a flag."""

    county: str | None
    reasons: tuple[str, ...]
    message: str
    key: str
    time: str | None
    url: str
    details: dict[str, JSONValue]


def _status_rows(url: str, sha: str, kinds: _Kinds, workspace: Workspace, roll: str | None) -> Iterable[_StatusRow]:
    """The flagged rows of one version of CORLA's upload status file."""
    try:
        data = workspace.get_bytes(sha)
    except ObjectNotFoundError:
        return
    classification = kinds.get(sha)
    header, rows = table_rows(data, classification)
    county_col = role_column(classification, "county")
    file_col = role_column(classification, "filename")
    type_col = role_column(classification, "type") or column(header, "type", "file type")
    status_col = role_column(classification, "status")
    hash_status_col = role_column(classification, "hash_status")
    time_col = column(header, "timestamp", "time")
    computed_col = role_column(classification, "computed_hash")
    submitted_col = role_column(classification, "submitted_hash")
    for row in rows:

        def cell(name: str | None) -> str:
            return row.get(name, "").strip() if name else ""

        raw_county, filename, file_type = cell(county_col), cell(file_col), cell(type_col)
        status, hash_status, given = cell(status_col).upper(), cell(hash_status_col).upper(), cell(time_col)
        county = canonical_county(raw_county) or raw_county or None
        try:
            moment: str | None = parse_time(given) if given else None
        except ValueError:
            moment = None
        details: dict[str, JSONValue] = {
            "ref": url,
            "county": county,
            "filename": filename,
            "type": file_type,
            "status": status or None,
            "timestamp": given or None,
            "computed_hash": cell(computed_col).lower() or None,
            "submitted_hash": cell(submitted_col).lower() or None,
        }
        if hash_status:
            details["hash_status"] = hash_status
        computed, submitted = details["computed_hash"], details["submitted_hash"]
        hashes = f" (computed hash {str(computed)[:12]}, submitted {str(submitted)[:12]})" if computed or submitted else ""
        when = colorado_wall_time(given) if given else "an unknown time"
        message = f"{file_type or 'file'} upload {filename!r}, {status or hash_status or 'no status'}, at {when}{hashes}"
        row_key = "|".join(str(details[k]) for k in ("county", "filename", "type", "status", "timestamp", "computed_hash"))
        lowered = file_type.lower()
        reasons = []
        if moment and roll and moment > roll and ("manifest" in lowered or "cvr" in lowered):
            reasons.append("upload_after_roll")
        if status == "HASH_MISMATCH" or "MISMATCH" in hash_status:
            reasons.append("upload_hash_mismatch")
        if status == "FAILED":
            reasons.append("upload_failed")
        if reasons:
            yield _StatusRow(county, tuple(reasons), message, row_key, moment, url, details)


def _status_flags(
    status_files: Sequence[tuple[str, str]], kinds: _Kinds, workspace: Workspace, roll: str | None
) -> Iterable[Flag]:
    """Flags from CORLA's upload status, every version of it, one per county and problem.

    Per county, one flag for every manifest or CVR upload after the roll
    (``upload_after_roll``), and one for every row marked HASH_MISMATCH or
    FAILED (``upload_hash_mismatch`` if any row is a mismatch, else
    ``upload_failed``), each listing its rows as aspects.
    """
    rows: dict[str, _StatusRow] = {}
    for url, sha in status_files:
        for row in _status_rows(url, sha, kinds, workspace, roll):
            rows.setdefault(row.key, row)
    groups: dict[tuple[str | None, str], list[_StatusRow]] = {}
    for row in rows.values():
        if "upload_after_roll" in row.reasons:
            groups.setdefault((row.county, "late"), []).append(row)
        if {"upload_hash_mismatch", "upload_failed"} & set(row.reasons):
            groups.setdefault((row.county, "errors"), []).append(row)
    for (county, group), members in sorted(groups.items(), key=lambda kv: (kv[0][0] or "", kv[0][1])):
        members.sort(key=lambda r: (r.time or "~", r.key))
        wanted: tuple[str, ...]
        if group == "late":
            reason = "upload_after_roll"
            wanted = ("upload_after_roll",)
            summary = f"{len(members)} manifest or CVR upload{'s' if len(members) != 1 else ''} after the roll ({colorado_time(roll)})"
        else:
            wanted = ("upload_hash_mismatch", "upload_failed")
            mismatches = sum(1 for r in members if "upload_hash_mismatch" in r.reasons)
            failed = sum(1 for r in members if "upload_failed" in r.reasons)
            reason = "upload_hash_mismatch" if mismatches else "upload_failed"
            parts = [
                f"{n} {one if n == 1 else many}"
                for n, one, many in ((mismatches, "hash mismatch", "hash mismatches"), (failed, "failed upload", "failed uploads"))
                if n
            ]
            summary = " and ".join(parts)
        aspects = tuple(
            dict[str, JSONValue]({"reason": r, "message": row.message, "time": row.time}) | row.details
            for row in members
            for r in row.reasons
            if r in wanted
        )
        listed = "; ".join(row.message for row in members[:4]) + ("; ..." if len(members) > 4 else "")
        yield Flag(
            "high",
            reason,
            f"{county or 'unknown county'}: {summary} in CORLA's upload status: {listed}",
            key=f"upload_status|{county}|{group}",
            counties=(county,) if county else (),
            time=next((r.time for r in members if r.time), None),
            refs=tuple(dict.fromkeys(r.url for r in members)),
            details={"rows": len(members)},
            aspects=aspects,
            aliases=tuple(f"{r}|{row.key}" for row in members for r in row.reasons if r in wanted),
        )


def _import_flags(datamap: DataMap, workspace: Workspace, seen: Mapping[str, _Seen], kinds: _Kinds) -> Iterable[Flag]:
    """Per county, one flag for every manifest seen that is not the one CORLA recorded importing."""
    imported = imported_manifests(datamap, workspace)
    by_county: dict[str, dict[str, list[tuple[str, str | None]]]] = {}
    for sha, where in seen.items():
        classification = kinds.get(sha)
        if classification.kind != "county_manifest":
            continue
        for ref, claimed, moment in where.places:
            county = claimed if ref.startswith("upload:") else classification.county
            if county:
                by_county.setdefault(county, {}).setdefault(sha, []).append((ref, moment))
    for entry in datamap.entries:
        if entry.source in ("supplied", "adapter") and entry.kind == "county_manifest" and entry.county:
            by_county.setdefault(entry.county, {}).setdefault(entry.sha256, []).append((entry.ref, entry.obtained))
    for county, record in sorted(imported.items()):
        copies = [(sha, places) for sha, places in sorted(by_county.get(county, {}).items()) if sha != record.sha256]
        if not copies:
            continue
        aspects: list[dict[str, JSONValue]] = []
        all_refs: list[str] = []
        all_times: list[str] = []
        for sha, places in copies:
            refs = tuple(dict.fromkeys(ref for ref, _ in places))
            times = sorted(t for _, t in places if t)
            all_refs.extend(refs)
            all_times.extend(times)
            compared = _batches_compared(workspace, sha, record.sha256, datamap)
            aspects.append(
                {
                    "reason": "manifest_differs_from_import",
                    "message": f"{', '.join(short_ref(r) for r in refs[:4])} (sha256 {sha[:12]}){compared}",
                    "sha256": sha,
                    "refs": list(refs),
                    "time": times[0] if times else None,
                }
            )
        what = (
            ", ".join(str(a["message"]) for a in aspects[:3])
            if len(aspects) <= 3
            else ", ".join(str(a["message"]) for a in aspects[:3]) + f", and {len(aspects) - 3} more"
        )
        noun = "a copy of the manifest is" if len(copies) == 1 else f"{len(copies)} copies of the manifest are"
        yield Flag(
            "high",
            "manifest_differs_from_import",
            f"{county}: {noun} not the manifest CORLA recorded importing "
            f"({record.filename}, {colorado_wall_time(record.timestamp)}): {what}",
            key=f"import|{county}|{record.sha256}",
            counties=(county,),
            time=min(all_times) if all_times else None,
            refs=tuple(dict.fromkeys(all_refs)) + (record.ref,),
            sha256=record.sha256,
            previous_sha256=copies[0][0] if len(copies) == 1 else None,
            details={"imported_filename": record.filename, "imported_timestamp": record.timestamp, "copies": len(copies)},
            aspects=tuple(aspects),
            aliases=tuple(f"import|{county}|{sha}|{record.sha256}" for sha, _ in copies),
        )


def _batches_compared(workspace: Workspace, sha: str, imported: str, datamap: DataMap) -> str:
    """How the batches of two manifests compare, if both are at hand."""
    try:
        ours = load_manifest_bytes(workspace.get_bytes(sha), datamap.county_aliases)
        theirs = load_manifest_bytes(workspace.get_bytes(imported), datamap.county_aliases)
    except ObjectNotFoundError:
        return "; the imported file is not at hand to compare batches"
    except ValueError:
        return ""
    mine = Counter((b.tabulator_id, b.batch_number, b.ballot_count) for b in ours)
    other = Counter((b.tabulator_id, b.batch_number, b.ballot_count) for b in theirs)
    if mine == other:
        return "; it lists the same batches"

    def show(keys: Counter[tuple[str, str, int]]) -> str:
        return ", ".join(f"{t}-{b} ({n})" for t, b, n in sorted(keys.elements())[:4]) or "none"

    return f"; DIFFERENT BATCHES: only in this copy {show(mine - other)}; only in the imported file {show(other - mine)}"


def _first_places(where: _Seen) -> dict[str, tuple[str | None, str]]:
    """Each ref the content was seen at, with the county it claimed and when it was first seen there."""
    first: dict[str, tuple[str | None, str]] = {}
    for ref, claimed, moment in where.places:
        first.setdefault(ref, (claimed, moment))
    return first


def _name_flags(seen: Mapping[str, _Seen], kinds: _Kinds) -> Iterable[Flag]:
    """Per content hash, one flag for a county file under several counties' names or under another county's name.

    Identical content under more than one county's name is ``duplicate_content``,
    naming every county and file, with each posted manifest whose name names
    another county than its content as an aspect; a manifest under another
    county's name, posted only there, is ``manifest_under_other_name``.
    """
    for sha, where in seen.items():
        classification = kinds.get(sha)
        if classification.kind not in _COUNTY_FILE_KINDS:
            continue
        claims: dict[str, str] = {}
        other_names: list[dict[str, JSONValue]] = []
        for ref, claimed, moment in where.places:
            county = claimed or classification.county
            if county:
                claims.setdefault(county, moment)
            if (
                classification.kind == "county_manifest"
                and not ref.startswith("upload:")
                and claimed
                and classification.county
                and claimed != classification.county
                and not any(a["ref"] == ref for a in other_names)
            ):
                other_names.append(
                    {
                        "reason": "manifest_under_other_name",
                        "message": f"{short_ref(ref)} holds {classification.county}'s manifest, not {claimed}'s",
                        "ref": ref,
                        "named": claimed,
                        "time": moment,
                    }
                )
        refs = tuple(dict.fromkeys(ref for ref, _, _ in where.places))
        aliases = tuple(f"other_name|{a['ref']}|{sha}" for a in other_names)
        content = f"; it is {classification.county}'s {classification.kind}" if classification.county else ""
        if len(claims) > 1:
            named = sorted(claims)
            files = ", ".join(short_ref(r) for r in refs[:6]) + (f", and {len(refs) - 6} more" if len(refs) > 6 else "")
            yield Flag(
                "high",
                "duplicate_content",
                f"identical {classification.kind} content under the names of {', '.join(named)}: {files}{content}",
                key=f"duplicate|{sha}",
                counties=tuple(sorted(set(named) | ({classification.county} if classification.county else set()))),
                time=min(claims.values()),
                refs=refs,
                sha256=sha,
                aspects=tuple(
                    [
                        dict[str, JSONValue](
                            reason="duplicate_content",
                            message=f"{short_ref(ref)} under {claimed or classification.county}'s name",
                            ref=ref,
                            time=moment,
                        )
                        for ref, (claimed, moment) in _first_places(where).items()
                        if not any(o["ref"] == ref for o in other_names)
                    ]
                    + other_names
                ),
                aliases=aliases,
            )
        elif other_names:
            named = sorted({str(a["named"]) for a in other_names})
            yield Flag(
                "high",
                "manifest_under_other_name",
                "; ".join(str(a["message"]) for a in other_names[:4]),
                key=f"other_name|{sha}",
                counties=tuple(named) + ((classification.county,) if classification.county else ()),
                time=min(str(a["time"]) for a in other_names),
                refs=tuple(str(a["ref"]) for a in other_names),
                sha256=sha,
                aspects=tuple(other_names),
                aliases=aliases,
            )


_SEED_KINDS = {"seed": "seed", "state_audit_report": "audit_random_seed", "county_audit_report": "audit_random_seed"}


def _seed_flags(events: Sequence[Event], datamap: DataMap) -> Iterable[Flag]:
    """Mismatched second entries, changed seeds, and audit center seeds unlike the one in effect."""
    state = seed_state(events)
    for outcome in state.mismatches:
        yield Flag(
            "high",
            "seed_entry_mismatch",
            f"{outcome.first.actor} entered the seed {outcome.first.seed} and {outcome.second.actor} entered "
            f"{outcome.second.seed}",
            key=f"seed_entry_mismatch:{outcome.seq}",
            time=outcome.second.time,
            details={
                "first_seq": outcome.first.seq,
                "second_seq": outcome.second.seq,
                "first_seed": outcome.first.seed,
                "second_seed": outcome.second.seed,
            },
        )
    for change in state.changes:
        yield Flag(
            "high",
            "seed_changed",
            f"{change.entry.actor} entered the seed {change.entry.seed}, replacing {change.previous.seed} "
            f"entered by {change.previous.actor}",
            key=f"seed_changed:{change.entry.seq}",
            time=change.entry.time,
            details={
                "previous_seq": change.previous.seq,
                "seq": change.entry.seq,
                "previous_seed": change.previous.seed,
                "seed": change.entry.seed,
            },
        )
    confirmed = state.seed
    if confirmed is None:
        return
    for entry in datamap.entries:
        field_name = _SEED_KINDS.get(entry.kind)
        published = entry.evidence.get(field_name) if field_name and not entry.ignored else None
        if isinstance(published, str) and published != confirmed:
            yield Flag(
                "high",
                "seed_differs_from_audit_center",
                f"{short_ref(entry.ref)} gives the seed {published}, but the seed entered here is {confirmed}",
                key=f"seed_differs_from_audit_center:{entry.ref}:{entry.sha256}",
                refs=(entry.ref,),
                sha256=entry.sha256,
                details={"published": published, "in_effect": confirmed, "kind": entry.kind},
            )


def _extract_flags(events: Sequence[Event]) -> Iterable[Flag]:
    """Per county, its latest extract if its pull list and CORLA's posted selections disagree."""
    latest: dict[str, Event] = {}
    for event in events:
        if event.kind == "extract.built" and event.county and event.data.get("purpose", "audit") == "audit" and event.data.get("order") != "targeted":
            latest[event.county] = event
    for county, event in sorted(latest.items()):
        if not event.data.get("sources_disagree"):
            continue
        other = str(event.data.get("other_source") or "the other source")
        defined = "CORLA's posted selections" if other == "pull_list" else "the county's pull list"
        yield Flag(
            "high",
            "extract_sources_disagree",
            f"{county}'s pull list and CORLA's posted selections list different ballots; the extract follows {defined}, "
            f"which arrived first (extract {str(event.file_sha256)[:12]})",
            key=f"extract_sources_disagree:{county}:{event.file_sha256}",
            counties=(county,),
            time=event.server_time,
            sha256=event.file_sha256,
            details={"other_source": other, "extract_seq": event.seq},
        )


def _totals_flags(events: Sequence[Event]) -> Iterable[Flag]:
    """The flagged differences of the latest ``totals.checked`` event."""
    latest = next((e for e in reversed(events) if e.kind == "totals.checked"), None)
    if latest is None:
        return
    for item in latest.data.get("differences") or []:  # type: ignore[union-attr]
        if not isinstance(item, dict) or not item.get("flag") or not isinstance(item.get("key"), str):
            continue
        county = _str(item.get("county"))
        yield Flag(
            "normal",
            "totals_differ",
            str(item.get("line")),
            key=str(item["key"]),
            counties=(county,) if county else (),
            time=latest.server_time,
            details={"check": item.get("check"), "contest": item.get("contest"), "totals_seq": latest.seq},
        )


def _alert_key(data: Mapping[str, JSONValue]) -> str | None:
    """The flag key an alert event records."""
    if isinstance(data.get("key"), str):
        return str(data["key"])
    reason = _str(data.get("reason"))
    if reason in _CHANGE_REASONS:
        change = _str(data.get("change")) or ("missing" if reason == "missing" else "changed")
        sha = _str(data.get("previous_sha256")) if change == "missing" else _str(data.get("sha256"))
        return f"change|{data.get('url')}|{sha}"
    if reason == "duplicate_content":
        return f"duplicate|{data.get('content_sha256')}"
    if reason == "manifest_under_other_name":
        return f"other_name|{data.get('url')}|{data.get('sha256')}"
    return f"alert|{reason}|{data.get('url')}" if reason else None


def _flag_from_alert(event: Event, key: str) -> Flag:
    """A flag for an alert event with no derived counterpart."""
    data = event.data
    reason = _str(data.get("reason")) or "alert"
    severity = _str(data.get("severity")) or ("normal" if reason in ("changed", "missing") else "high")
    urls = data.get("urls")
    refs = tuple(str(u) for u in urls) if isinstance(urls, list) else ((str(data["url"]),) if data.get("url") else ())
    message = _str(data.get("message")) or (
        f"{', '.join(short_ref(r) for r in refs[:6]) or 'a file'}: {reason}" + (f" ({data['detail']})" if data.get("detail") else "")
    )
    counties = data.get("counties")
    return Flag(
        severity,
        reason,
        message,
        key=key,
        counties=tuple(str(c) for c in counties) if isinstance(counties, list) else (),
        time=_str(data.get("time")) or effective_time(event),
        refs=refs,
        sha256=_str(data.get("sha256")) or _str(data.get("content_sha256")),
        previous_sha256=_str(data.get("previous_sha256")),
        alerts=(event.seq,),
    )


#: Data map problems, by code, that report what a flag reports, with the flag reasons that report it.
PROBLEM_FLAG_REASONS: dict[str, frozenset[str]] = {
    "manifest_differs_from_import": frozenset({"manifest_differs_from_import"}),
    "manifest_under_other_name": frozenset({"manifest_under_other_name"}),
    "seed_mismatch": frozenset({"seed_entry_mismatch"}),
}


def unflagged_problems(problems: Iterable[Problem], flags: Sequence[Flag]) -> list[Problem]:
    """The problems no flag already reports, so that a page lists each finding once.

    A problem of a code in `PROBLEM_FLAG_REASONS` is left out if a flag
    with one of its reasons (among the flag's aspects too) is about the
    problem's county, or the problem is about no county.
    """
    kept = []
    for problem in problems:
        reasons = PROBLEM_FLAG_REASONS.get(problem.code)
        if reasons and any(
            reasons & set(f.reasons) and (problem.county is None or problem.county in f.counties) for f in flags
        ):
            continue
        kept.append(problem)
    return kept


def flag_lines(flag: Flag, indent: str = "  ") -> list[str]:
    """A flag as text lines: its description, what to investigate, and each aspect when it groups several."""
    lines = [f"{indent}{flag.describe()}", f"{indent}  investigate: {flag.investigate}"]
    if len(flag.aspects) > 1:
        for aspect in flag.aspects[:_MAX_REFS]:
            when = f" [{colorado_time(str(aspect['time']))}]" if isinstance(aspect.get("time"), str) else ""
            lines.append(f"{indent}  - {aspect.get('message')}{when}")
    return lines


def filter_flags(flags: Sequence[Flag], *, severity: str | None = None, counties: Iterable[str] = ()) -> list[Flag]:
    """The flags at least as severe as ``severity``, about any of ``counties`` (or about no county)."""
    wanted = {canonical_county(c) or c for c in counties}
    limit = SEVERITIES.index(severity) if severity in SEVERITIES else len(SEVERITIES)
    return [
        f
        for f in flags
        if (f.severity not in SEVERITIES or SEVERITIES.index(f.severity) <= limit)
        and (not wanted or not f.counties or wanted & set(f.counties))
    ]


def record_flags(log: EventLog, flags: Iterable[Flag], actor: str) -> list[int]:
    """Record each flag not yet in an alert event as a ``map.alert`` event; return their seqs.

    The event's data is the flag's `Flag.to_dict` (refs cut to 40).
    """
    check_actor(actor)
    seqs = []
    for flag in flags:
        if flag.alerts:
            continue
        data = flag.to_dict()
        data.pop("alerts")
        sha = flag.sha256 or flag.previous_sha256
        event = log.append(
            "map.alert",
            actor,
            data=data,
            county=flag.counties[0] if len(flag.counties) == 1 else None,
            file_sha256=sha if _is_sha(sha) else None,
        )
        seqs.append(event.seq)
    return seqs


# ---------------------------------------------------------------------------
# Addressed flags
#
# Flags are for coordinators: many are addressed before the public would see
# them. A coordinator marks a flag addressed, with a note, by a
# ``flag.addressed`` event (``key``, ``covers``: the flag's key and aliases
# then, ``reason``, ``counties``, ``message`` and ``note``), and may reopen
# it with ``flag.reopened`` (``key`` and ``note``). A flag stays addressed
# while every finding it groups was covered when it was addressed; a new
# finding grouped into it (another upload row, another copy) opens it again.

ADDRESSED_KIND = "flag.addressed"
REOPENED_KIND = "flag.reopened"
#: Longest note kept.
MAX_NOTE = 2000


@dataclass(frozen=True)
class Addressed:
    """A coordinator's marking of a flag as addressed."""

    key: str
    seq: int
    actor: str
    time: str
    note: str
    covers: frozenset[str]

    def to_dict(self) -> dict[str, JSONValue]:
        return {"key": self.key, "seq": self.seq, "actor": self.actor, "time": self.time, "note": self.note}


def addressed_flags(events: Iterable[Event]) -> dict[str, Addressed]:
    """Each flag key's latest marking as addressed, unless it was reopened since."""
    found: dict[str, Addressed] = {}
    for event in events:
        key = _str(event.data.get("key"))
        if key is None:
            continue
        if event.kind == ADDRESSED_KIND:
            covers = event.data.get("covers")
            found[key] = Addressed(
                key,
                event.seq,
                event.actor,
                event.server_time,
                _str(event.data.get("note")) or "",
                frozenset(str(c) for c in covers) if isinstance(covers, list) else frozenset({key}),
            )
        elif event.kind == REOPENED_KIND:
            found.pop(key, None)
    return found


def addressing(flag: Flag, addressed: Mapping[str, Addressed]) -> tuple[Addressed | None, bool]:
    """The flag's marking as addressed, if any, and whether it still holds (no new finding since)."""
    marking = addressed.get(flag.key)
    if marking is None:
        return None, False
    return marking, {flag.key, *flag.aliases} <= marking.covers


def split_addressed(flags: Sequence[Flag], addressed: Mapping[str, Addressed]) -> tuple[list[Flag], list[Flag]]:
    """The flags still open, and those addressed (whose marking still holds)."""
    still_open: list[Flag] = []
    done: list[Flag] = []
    for flag in flags:
        (done if addressing(flag, addressed)[1] else still_open).append(flag)
    return still_open, done


def record_addressed(log: EventLog, flag: Flag, actor: str, note: str, *, session_id: str | None = None) -> int:
    """Record that a coordinator addressed a flag, with a note saying how; return the event's seq.

    Raises:
        ValueError: for an empty note or an invalid actor.
    """
    check_actor(actor)
    note = "".join(ch for ch in note if ch.isprintable() or ch == "\n").strip()[:MAX_NOTE]
    if not note:
        raise ValueError("say how the flag was addressed")
    data: dict[str, JSONValue] = {
        "key": flag.key,
        "covers": [flag.key, *flag.aliases],
        "reason": flag.reason,
        "counties": list(flag.counties),
        "message": flag.message[:2000],
        "note": note,
    }
    return log.append(
        ADDRESSED_KIND, actor, county=flag.counties[0] if len(flag.counties) == 1 else None, session_id=session_id, data=data
    ).seq


def record_reopened(log: EventLog, key: str, actor: str, note: str = "", *, session_id: str | None = None) -> int:
    """Record that a coordinator reopened an addressed flag; return the event's seq."""
    check_actor(actor)
    note = "".join(ch for ch in note if ch.isprintable()).strip()[:MAX_NOTE]
    return log.append(REOPENED_KIND, actor, session_id=session_id, data={"key": key[:1000], "note": note}).seq


def _is_sha(value: str | None) -> bool:
    return value is not None and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
