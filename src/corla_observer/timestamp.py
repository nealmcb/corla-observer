"""Timestamping the evidence: versioned SHA256SUMS files, OpenTimestamps proofs and Sigstore signatures.

This automates the July 2026 procedure (see docs/TIMESTAMPING.md) in two
commands. `commit_evidence` makes the commitment:

1. **Collect** the evidence set from the workspace: every file the data map
   uses (fetched audit center files, county uploads, files supplied by an
   override, adapter outputs), the overrides file last applied, the source
   of every adapter whose output is used, the latest prediction, and each
   county's latest extract with its stored page index (the per-page hashes
   printed on the extract, ``page_index_sha256`` in ``extract.built``) and
   its printouts (each board's PDF in each layout, ``pdfs``).
   Every stored object is re-hashed on the way, so a corrupt store stops
   the commitment rather than committing to it.
2. **Write** a new versioned SHA256SUMS file, named for its version and the
   time it was written, in UTC (``SHA256SUMS-v1-20261106T193720Z.txt``, then
   ``SHA256SUMS-v2-...`` and so on; `sums_name`), in the output
   repository: one ``<sha256>  <path>`` line per file in sorted path order,
   after a ``#`` header giving the election, the time (UTC), the event log's
   head (sequence number and hash, which commits to the whole log) and the
   software version. A version number that exists, that ever existed in the
   repository's history, or that the event log records, is never used
   again, and the file is created exclusively, so nothing is overwritten.
3. **Stamp** it with OpenTimestamps (``ots stamp``), if the ``ots`` command
   is installed, producing ``<sums>.ots``. The proof is pending until a
   calendar's Bitcoin transaction confirms.
4. **Sign** it with Sigstore (``cosign sign-blob --bundle``), if ``cosign``
   is installed, producing ``<sums>.cosign-bundle``: keyless (an OIDC
   identity, recorded in the public Rekor log) unless a key file is given.
5. **Copy** the evidence files themselves next to it, as ``include`` says:
   none, the files fetched from the Audit Center (``fetched``), or every
   publishable file (``all``: also the counties' uploads that passed the
   redaction check, and the derived extracts and printouts).
   A page fetched again unchanged but for Cloudflare's per-response token
   is listed as its earliest fetch with that content, so it is published
   once; a file whose content changed replaces the earlier copy at the same
   path, the repository's history keeping the earlier one.
6. **Record** an ``evidence.committed`` event: what is fixed from that
   moment on (the SUMS file's hash, its proofs, the log head), and no git
   commit, since none is final yet.

`publish_evidence` publishes it, once, when the proof is complete: it
completes the OpenTimestamps proof (``ots upgrade``), makes one commit in
the output repository holding the SUMS file, its proofs and any copied
files, pushes it, and only then records an ``evidence.published`` event
naming that commit, which is never rewritten. So the event log names only
commits that are public and final.

Each step's outcome (done, pending, skipped because the tool is missing or
was turned off, or failed with the tool's output) is returned and recorded;
nothing is skipped silently.

`verify_evidence` checks a SUMS file: it recomputes the hash of every listed
file available locally and runs ``ots verify`` and ``cosign verify-blob`` on
the proofs where those tools exist.

Events written here (``data`` always JSON; ``file_sha256`` is the SUMS file's):

``evidence.committed``
    ``election``, ``sums_name``, ``sums_sha256``, ``version``, ``subdir``,
    ``files`` (how many), ``included_files`` (how many copied), ``log_head_seq``
    and ``log_head_hash`` (as written in the SUMS header), ``software``,
    ``steps`` (each step's ``status`` and ``detail``), ``proofs`` (the proof
    file names made), ``included`` (the copied files, relative to
    ``subdir``) and ``rekor_log_index`` (or null). (Events written before
    publishing was a step of its own also have ``commit`` and ``pushed``.)
``evidence.commit_failed``
    ``sums_name``, ``sums_sha256``, ``version``, ``error``: the SUMS file was
    written but stamping, signing or copying failed.
``evidence.published``
    ``sums_name``, ``sums_sha256``, ``committed_in_event`` (the
    ``evidence.committed`` event's number), ``repository`` (the remote's
    address), ``branch``, ``commit`` (pushed), ``files`` (paths within the
    repository), ``opentimestamps`` (``complete, Bitcoin block N``, or
    ``pending``), ``rekor_log_index``, ``replaces_unpublished_commits``:
    any commit an earlier ``evidence.committed`` named that was never
    published, and ``replaced``: each file published that replaced an
    earlier version, with the commit that published that version (also
    named, with a link, in the commit's message).
``evidence.proofs_upgraded``
    ``upgraded`` and ``still_pending`` (proof file paths within the output
    repository), ``skipped`` (why, if ``ots`` was not run), ``commit`` and
    ``pushed``, and ``bitcoin_blocks``: for each proof upgraded to a Bitcoin
    attestation, the block's height (null if it could not be parsed).

No local paths are recorded in events, SUMS files or commit messages.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from corla_observer.classify import is_test_county
from corla_observer.datamap import DataMap, build_map
from corla_observer.localtime import to_colorado
from corla_observer.events import EventLog, JSONValue, check_actor, verify_jsonl
from corla_observer.redaction import PASS
from corla_observer.workspace import Workspace, sha256_of_file

#: A SUMS file's name: ``SHA256SUMS-v<N>-<UTC time, ISO 8601 basic format>.txt``. The time is the
#: one in its header, when it was written, as the writer's clock gave it: the proof of time is in
#: its OpenTimestamps proof and Sigstore entry, not its name. No colons, so it can be checked out
#: on Windows. Names of the earlier form, ``SHA256SUMS.txt`` (version 1) and
#: ``SHA256SUMS.v<N>.txt`` (as for the July 2026 primary), are still read.
SUMS_BASENAME = "SHA256SUMS"
SUMS_NAME_RE = re.compile(r"^SHA256SUMS(?:-v([1-9][0-9]*)-[0-9]{8}T[0-9]{6}Z|\.v([1-9][0-9]*))?\.txt$")
OTS_SUFFIX = ".ots"
COSIGN_SUFFIX = ".cosign-bundle"

#: Event kinds whose ``file_sha256`` is an extract committed to observers.
EXTRACT_EVENT_KINDS = frozenset({"practice.extract_built", "extract.built"})

#: Default time limit, in seconds, for one run of ``ots`` or ``cosign``.
DEFAULT_TOOL_TIMEOUT = 300

#: OpenTimestamps attestation tags (from the OpenTimestamps format): a
#: Bitcoin block header attestation, and a pending calendar attestation.
OTS_BITCOIN_TAG = bytes.fromhex("0588960d73d71901")
OTS_PENDING_TAG = bytes.fromhex("83dfe30d2ef90c8e")

_ELECTION_RE = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$")
_SUBDIR_PART_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SUMS_LINE_RE = re.compile(r"^([0-9a-f]{64}) [ *](.+)$")
_REKOR_INDEX_RE = re.compile(r"index:?\s*(\d+)", re.IGNORECASE)
_MAX_DETAIL = 600

#: The pieces of the event log's (and SoS's) RLA URL path that the
#: evidence paths of fetched files are made relative to.
_RLA_PATH = "/pubs/elections/RLA/"

Echo = Callable[[str], None]


class TimestampError(Exception):
    """The evidence could not be collected, written or committed."""


# ---------------------------------------------------------------------------
# Collecting the evidence set


@dataclass(frozen=True)
class EvidenceFile:
    """One file of the evidence set.

    ``path`` is its path in the SUMS file (relative, with ``/``), ``origin``
    says what it is (``fetch``, ``upload``, ``supplied``, ``adapter``,
    ``overrides``, ``adapter_source``, ``prediction``, ``extract``,
    ``page_index`` or ``printout``), and
    ``ref`` where it came from (a URL, a data map reference or an event seq).
    """

    path: str
    sha256: str
    size: int
    origin: str
    ref: str
    kind: str | None = None
    county: str | None = None
    #: Whether copying the file into a public repository is safe. False only
    #: for an uploaded CVR without a passing redaction check on record.
    publishable: bool = True


@dataclass
class EvidenceSet:
    election: str | None
    files: list[EvidenceFile]
    log_head: tuple[int, str]
    software: str
    notes: list[str] = field(default_factory=list)


def _safe_name(text: str, fallback: str = "file") -> str:
    name = _SAFE_NAME_RE.sub("_", text).strip("._")
    return name[:120] or fallback


def _safe_path(parts: Iterable[str]) -> str:
    return "/".join(_safe_name(p) for p in parts if p not in ("", ".", ".."))


def fetched_path(url: str) -> str:
    """The evidence path of a fetched file: ``auditcenter/`` plus its path under the RLA directory."""
    split = urlsplit(url)
    path = unquote(split.path)
    if _RLA_PATH in path:
        rest = path.split(_RLA_PATH, 1)[1]
    else:
        rest = f"{split.hostname or 'host'}/{path.lstrip('/')}"
    if rest.endswith("/") or not rest:
        rest += "index.html"
    if split.query:
        rest += "_" + hashlib.sha256(split.query.encode()).hexdigest()[:8]
    return "auditcenter/" + _safe_path(rest.split("/"))


def software_version() -> str:
    """This software, as the SUMS header names it: `corla_observer.version.software` (version, commit, ``+dirty``)."""
    from corla_observer.version import software

    return software()


#: Kinds of Audit Center file that change routinely as the audit goes on: they are mirrored, and their
#: history is in the event log, but they are not committed to, so a published check fails only when a
#: file that should not change did.
VOLATILE_KINDS = frozenset({"html_page", "upload_status", "audit_activity_report", "vote_totals", "unknown"})


def mirror_path(url: str) -> str | None:
    """Where the audit center mirror keeps a file fetched from ``url``: its path under the RLA directory, as
    the Audit Center publishes it (``2026/general/files/X.xlsx``); None for a file outside that directory."""
    path = unquote(urlsplit(url).path)
    if _RLA_PATH not in path:
        return None
    rest = path.split(_RLA_PATH, 1)[1]
    return _safe_path(rest.split("/")) if rest and not rest.endswith("/") else None


def collect_evidence(workspace: Workspace, log: EventLog, datamap: DataMap | None = None, election: str | None = None) -> EvidenceSet:
    """The evidence set: see the module documentation.

    Paths are relative to the election's folder in the mirror (``election``, else the data map's
    election): an Audit Center file at the path the Audit Center publishes it under that folder
    (`mirror_path`), as the mirror keeps it, everything else under ``observerfiles/``. Audit Center
    files of `VOLATILE_KINDS`, or outside the election's folder, are not listed.

    Raises:
        TimestampError: if a stored object is missing or corrupt.
    """
    log_head = log.head()
    datamap = datamap or build_map(log, workspace)
    folder = (election or datamap.election or "").strip("/")
    adapter_sources: dict[str, tuple[str, str]] = {}  # output sha -> (module, source sha)
    redaction_passed: set[str] = set()
    prediction: tuple[int, str] | None = None
    extracts: dict[str, tuple[int, str, str | None, dict[str, dict[str, str]]]] = {}
    # A page fetched again differs only by the token Cloudflare puts in each response: its content, with
    # that stripped (``normalized_sha256``), is unchanged. Such a page is listed as its earliest fetch
    # with that content, so it is published once, not again with each fetch.
    normalized_of: dict[str, str] = {}  # a fetch's bytes -> their normalized content
    earliest: dict[tuple[str, str], str] = {}  # (URL, normalized content) -> the earliest fetch's bytes
    for event in log.iter_events():
        if event.seq > log_head[0]:
            break
        if event.kind == "fetch.file":
            url, raw, normalized = event.data.get("url"), event.data.get("sha256"), event.data.get("normalized_sha256")
            if isinstance(url, str) and isinstance(raw, str) and isinstance(normalized, str):
                normalized_of[raw] = normalized
                earliest.setdefault((url, normalized), raw)
        elif event.kind == "map.adapter_ran":
            out, src, module = event.data.get("output_sha256"), event.data.get("source_sha256"), event.data.get("module")
            if isinstance(out, str) and isinstance(src, str) and isinstance(module, str):
                adapter_sources[out] = (module, src)
        elif event.kind == "redaction.checked" and event.data.get("verdict") == PASS and event.file_sha256:
            redaction_passed.add(event.file_sha256)
        elif event.kind == "prediction.made" and event.file_sha256:
            prediction = (event.seq, event.file_sha256)
        elif event.kind in EXTRACT_EVENT_KINDS and event.file_sha256 and not is_test_county(event.county):
            # The RLA sample's extract, and each targeted list sent to the county (docs/TARGETING.md, "Provenance"):
            # the county acts on a sent list, so it is part of the record, under its name ("targeted-list-1").
            index = event.data.get("page_index_sha256")
            found = event.data.get("pdfs")
            pdfs = {str(k): {str(b): str(h) for b, h in v.items()} for k, v in found.items() if isinstance(v, dict)} if isinstance(found, dict) else {}
            name = event.county or ""
            if event.data.get("order") == "targeted":
                label = str(event.data.get("list_name") or event.data.get("list") or "targeted")
                name = f"{name}-" + re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-").lower()
            extracts[name] = (event.seq, event.file_sha256, index if isinstance(index, str) else None, pdfs)

    candidates: list[EvidenceFile] = []
    notes: list[str] = []
    volatile = 0

    def add(
        path: str, sha256: str, origin: str, ref: str, *,
        kind: str | None = None, county: str | None = None, publishable: bool = True,
    ) -> None:
        if origin != "fetch":
            path = "observerfiles/" + path
        candidates.append(EvidenceFile(path, sha256, 0, origin, ref, kind, county, publishable))

    ignored = 0
    for entry in datamap.entries:
        if entry.ignored:
            ignored += 1
            continue
        county = entry.county
        if entry.source == "fetch":
            kind = entry.classified_kind or entry.kind
            mirrored = mirror_path(entry.ref)
            if kind in VOLATILE_KINDS or mirrored is None or not (mirrored + "/").startswith(folder + "/") or not folder:
                volatile += 1
                continue
            path = mirrored[len(folder) + 1 :]
            content = normalized_of.get(entry.sha256)
            if content is not None:
                add(path, earliest.get((entry.ref, content), entry.sha256), entry.source, entry.ref, kind=entry.kind, county=county)
                continue
        elif entry.source == "upload":
            path = _safe_path(["uploads", county or "unknown", entry.kind, entry.name])
        elif entry.source == "supplied":
            path = _safe_path(["supplied", entry.name])
        elif entry.source == "adapter":
            path = _safe_path(["adapter-output", entry.name])
            if entry.sha256 in adapter_sources:
                module, src = adapter_sources[entry.sha256]
                add(_safe_path(["adapters", f"{module}.py"]), src, "adapter_source", f"adapter {module}")
        else:
            path = _safe_path([entry.source, entry.name])
        is_cvr = entry.ref.endswith(":redacted_cvr") or "cvr" in (entry.kind, entry.classified_kind)
        publishable = not (entry.source == "upload" and is_cvr and entry.sha256 not in redaction_passed)
        add(path, entry.sha256, entry.source, entry.ref, kind=entry.kind, county=county, publishable=publishable)
    if ignored:
        notes.append(f"{ignored} file(s) the data map ignores are not listed")
    if volatile:
        notes.append(f"{volatile} Audit Center file(s) that change routinely (pages, status files) or lie outside the election's folder are not listed; the event log records them")
    if datamap.overrides_sha256 and not datamap.preview:
        add("overrides/overrides.toml", datamap.overrides_sha256, "overrides", f"event {datamap.overrides_seq}")
    if prediction is not None:
        add("derived/prediction.json", prediction[1], "prediction", f"event {prediction[0]}")
    else:
        notes.append("no prediction has been recorded yet")
    for county, (seq, sha, index_sha, pdfs) in sorted(extracts.items()):
        add(_safe_path(["derived", f"extract-{county or 'all'}.json"]), sha, "extract", f"event {seq}", county=county or None)
        if index_sha is not None:
            add(
                _safe_path(["derived", f"extract-{county or 'all'}-pages.json"]), index_sha, "page_index",
                f"event {seq}", county=county or None,
            )
        for layout, by_board in sorted(pdfs.items()):
            for board, pdf_sha in sorted(by_board.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
                add(
                    _safe_path(["derived", f"extract-{county or 'all'}-board{board}-{layout}.pdf"]), pdf_sha, "printout",
                    f"event {seq}", county=county or None,
                )  # fmt: skip

    files: list[EvidenceFile] = []
    taken: dict[str, str] = {}
    for item in candidates:
        try:
            size = workspace.verify(item.sha256)
        except Exception as exc:  # ObjectNotFoundError, CorruptObjectError
            raise TimestampError(f"{item.ref}: {exc}") from exc
        path = item.path
        if taken.get(path, item.sha256) != item.sha256:
            path = f"{path}~{item.sha256[:12]}"
        if taken.get(path) == item.sha256:
            continue
        taken[path] = item.sha256
        files.append(
            EvidenceFile(path, item.sha256, size, item.origin, item.ref, item.kind, item.county, item.publishable)
        )
    files.sort(key=lambda f: f.path)
    return EvidenceSet(election=datamap.election, files=files, log_head=log_head, software=software_version(), notes=notes)


# ---------------------------------------------------------------------------
# SUMS files


def sums_name(version: int, moment: datetime) -> str:
    """The SUMS file's name for its version and the time it is written (see `SUMS_NAME_RE`)."""
    return f"{SUMS_BASENAME}-v{version}-{moment.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}.txt"


def sums_version(name: str) -> int | None:
    """A SUMS file's version from its name, in either form; None if it is not a SUMS file's name."""
    match = SUMS_NAME_RE.match(name)
    if match is None:
        return None
    return int(match.group(1) or match.group(2) or 1)


def render_sums(evidence: EvidenceSet, election: str, version: int, moment: datetime, subdir: str = "") -> bytes:
    """The SUMS file's content: a ``#`` header, then ``<sha256>  <path>`` lines in sorted path order.

    The paths are relative to the election's folder (``base:``, from the SUMS file's own folder,
    ``subdir``); the ``check:`` line is the command that checks them, run from that folder.
    """
    seq, head = evidence.log_head
    rel = PurePosixPath(subdir).relative_to(election).as_posix() if subdir and (subdir + "/").startswith(election + "/") else ""
    base = "/".join(".." for _ in PurePosixPath(rel).parts) if rel and rel != "." else "."
    name = sums_name(version, moment)
    lines = [
        f"# SHA-256 digests of the observers' evidence for election {election}, written by corla-observer.",
        f"# version: {version}",
        f"# time: {moment.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"# event-log-head: {seq} {head}",
        f"# software: {evidence.software}",
        f"# files: {len(evidence.files)}",
        f"# base: {base}",
        f"# check: (from {election}/) sha256sum --check {rel + '/' if rel and rel != '.' else ''}{name}",
    ]
    for item in sorted(evidence.files, key=lambda f: f.path):
        if "\n" in item.path or "\\" in item.path:
            raise TimestampError(f"unsafe evidence path {item.path!r}")
        lines.append(f"{item.sha256}  {item.path}")
    return ("\n".join(lines) + "\n").encode("utf-8")


@dataclass(frozen=True)
class SumsFile:
    header: dict[str, str]
    entries: list[tuple[str, str]]  # (sha256, path)
    malformed: list[str]


def parse_sums(text: str) -> SumsFile:
    header: dict[str, str] = {}
    entries: list[tuple[str, str]] = []
    malformed: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("#"):
            key, sep, value = line[1:].strip().partition(":")
            if sep and " " not in key:
                header[key] = value.strip()
            continue
        match = _SUMS_LINE_RE.match(line)
        if match is None:
            malformed.append(line[:200])
        else:
            entries.append((match.group(1), match.group(2)))
    return SumsFile(header, entries, malformed)


def _is_safe_relative(path: str) -> bool:
    pure = PurePosixPath(path)
    return not pure.is_absolute() and ".." not in pure.parts and not path.startswith("~")


# ---------------------------------------------------------------------------
# Running tools and git


@dataclass(frozen=True)
class ToolRun:
    #: None if the tool was stopped for exceeding its time limit.
    returncode: int | None
    output: str


def run_tool(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    echo: Echo | None = None,
    interactive: bool = False,
) -> ToolRun:
    """Run a tool, collecting its combined output (and echoing it line by line as it comes)."""
    proc = subprocess.Popen(
        list(argv),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=None if interactive else subprocess.DEVNULL,
        text=True,
        errors="replace",
    )
    lines: list[str] = []

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            if echo is not None:
                echo(line.rstrip("\n"))

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    returncode: int | None
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        returncode = None
    reader.join(5)
    return ToolRun(returncode, "".join(lines))


def _short(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _MAX_DETAIL else "..." + text[-_MAX_DETAIL:]


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        raise TimestampError("git is not installed")
    result = subprocess.run([git, "-C", str(repo), *args], capture_output=True, text=True, timeout=600, check=False)
    if check and result.returncode != 0:
        raise TimestampError(f"git {' '.join(args[:2])} failed: {_short(result.stderr or result.stdout)}")
    return result


def _check_repo(repo: Path) -> None:
    if not repo.is_dir():
        raise TimestampError(f"no such directory: {repo}")
    result = _git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise TimestampError(f"not a git working tree: {repo}")


def _commit(repo: Path, paths: Sequence[str], message: str) -> str:
    _git(repo, "add", "--", *paths)
    _git(repo, "commit", "--quiet", "-m", message, "--", *paths)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _push(repo: Path, echo: Echo | None) -> tuple[bool, str]:
    """Push the current branch if the repository has a remote; return whether it pushed, and why not."""
    remotes = _git(repo, "remote").stdout.split()
    if not remotes:
        return False, "the repository has no remote"
    upstream = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
    if upstream.returncode == 0:
        result = _git(repo, "push", "--quiet", check=False)
    else:
        remote = "origin" if "origin" in remotes else remotes[0]
        result = _git(repo, "push", "--quiet", remote, "HEAD", check=False)
    if result.returncode != 0:
        detail = _short(result.stderr or result.stdout)
        if echo is not None:
            echo(f"push failed: {detail}")
        return False, f"push failed: {detail}"
    return True, "pushed"


def _push_step(repo: Path, push: bool, echo: Echo | None) -> Step:
    if not push:
        return Step("push", "skipped", "--no-push given")
    pushed, detail = _push(repo, echo)
    if pushed:
        return Step("push", "done", detail)
    return Step("push", "skipped" if "no remote" in detail else "failed", detail)


def logged_versions(log: EventLog, subdir: str) -> set[int]:
    """Every SUMS version the event log records as committed (or failed) in ``subdir``, published or not."""
    found: set[int] = set()
    for event in log.iter_events():
        if event.kind in ("evidence.committed", "evidence.commit_failed") and event.data.get("subdir") == subdir:
            version = event.data.get("version")
            if type(version) is int:
                found.add(version)
    return found


def existing_versions(repo: Path, subdir: str) -> set[int]:
    """Every SUMS version in the directory now, or ever added there in the repository's history."""
    found: set[int] = set()
    directory = repo / subdir
    if directory.is_dir():
        found |= {v for p in directory.iterdir() if (v := sums_version(p.name)) is not None}
    history = _git(repo, "log", "--all", "--format=", "--name-only", "--", subdir or ".", check=False)
    for line in history.stdout.splitlines():
        rel = PurePosixPath(line.strip())
        if str(rel.parent) == (subdir or "."):
            if (v := sums_version(rel.name)) is not None:
                found.add(v)
    return found


# ---------------------------------------------------------------------------
# Committing


@dataclass(frozen=True)
class Step:
    """One step's outcome: ``done``, ``pending`` (an OpenTimestamps proof awaiting Bitcoin), ``skipped`` or ``failed``."""

    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, JSONValue]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class CosignOptions:
    enabled: bool = True
    #: A cosign private key file; keyless (Sigstore OIDC) signing if None.
    key: Path | None = None


@dataclass
class CommitResult:
    election: str
    version: int
    sums_name: str
    sums_sha256: str
    subdir: str
    evidence: EvidenceSet
    steps: list[Step]
    proofs: list[str]
    included: list[str]
    event_seq: int
    rekor_log_index: int | None = None

    @property
    def failed(self) -> list[Step]:
        return [s for s in self.steps if s.status == "failed"]


def check_election(election: str) -> str:
    if not _ELECTION_RE.match(election):
        raise TimestampError(f"not an election key such as 2026/general: {election!r}")
    return election


def check_subdir(subdir: str) -> str:
    parts = [p for p in subdir.strip("/").split("/") if p]
    if subdir.startswith("/") or any(p in (".", "..") or not _SUBDIR_PART_RE.match(p) for p in parts):
        raise TimestampError(f"--subdir must be a plain relative path such as observerfiles: {subdir!r}")
    return "/".join(parts)


def default_subdir(election: str) -> str:
    return f"{election}/observerfiles"


def stamp_ots(path: Path, *, timeout: float, echo: Echo | None = None) -> Step:
    """Run ``ots stamp`` on ``path``, making ``path.ots``."""
    ots = shutil.which("ots")
    if ots is None:
        return Step("opentimestamps", "skipped", "the ots command (opentimestamps-client) is not installed")
    run = run_tool([ots, "stamp", path.name], cwd=path.parent, timeout=timeout, echo=echo)
    proof = path.with_name(path.name + OTS_SUFFIX)
    if run.returncode == 0 and proof.is_file():
        status = "done" if OTS_BITCOIN_TAG in proof.read_bytes() else "pending"
        detail = "submitted to the calendars; awaiting Bitcoin confirmation" if status == "pending" else "complete"
        return Step("opentimestamps", status, detail)
    why = "timed out" if run.returncode is None else f"exit status {run.returncode}"
    return Step("opentimestamps", "failed", f"ots stamp {why}: {_short(run.output)}")


def sign_cosign(path: Path, options: CosignOptions, *, timeout: float, echo: Echo | None = None) -> tuple[Step, int | None]:
    """Run ``cosign sign-blob --bundle`` on ``path``, making ``path.cosign-bundle``; also return the Rekor log index."""
    if not options.enabled:
        return Step("sigstore", "skipped", "signing turned off (--no-cosign)"), None
    cosign = shutil.which("cosign")
    if cosign is None:
        return Step("sigstore", "skipped", "the cosign command is not installed"), None
    bundle = path.with_name(path.name + COSIGN_SUFFIX)
    argv = [cosign, "sign-blob", "--yes", "--bundle", bundle.name]
    mode = "keyless (Sigstore OIDC identity)"
    if options.key is not None:
        argv += ["--key", str(options.key.resolve())]
        mode = "with a key file"
    argv.append(path.name)
    # Keyless, cosign's input is closed so that it uses the device flow: it prints a link and a code to
    # check, and the signer logs in there, with nothing to type back (with a terminal it would wait for
    # a code to be pasted, behind a prompt this output does not show). A key file's password is typed.
    run = run_tool(argv, cwd=path.parent, timeout=timeout, echo=echo, interactive=options.key is not None)
    if run.returncode == 0 and bundle.is_file() and bundle.stat().st_size > 0:
        match = _REKOR_INDEX_RE.search(run.output)
        index = int(match.group(1)) if match else None
        detail = f"signed {mode}" + (f"; Rekor log index {index}" if index is not None else "")
        return Step("sigstore", "done", detail), index
    bundle.unlink(missing_ok=True)
    why = "timed out" if run.returncode is None else f"exit status {run.returncode}"
    return Step("sigstore", "failed", f"cosign sign-blob {mode} {why}: {_short(run.output)}"), None


def bundle_identity(bundle: Path) -> tuple[str, str] | None:
    """A keyless cosign bundle's signer: the identity (an email, or a workflow URI) and the OIDC issuer; None for a key file's, or if unreadable."""
    try:
        from cryptography import x509

        cert_b64 = json.loads(bundle.read_text(encoding="utf-8")).get("cert")
        if not isinstance(cert_b64, str):
            return None
        cert = x509.load_pem_x509_certificate(base64.b64decode(cert_b64))
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        found = names.get_values_for_type(x509.RFC822Name) or names.get_values_for_type(x509.UniformResourceIdentifier)
        issuer = ""
        for oid in ("1.3.6.1.4.1.57264.1.8", "1.3.6.1.4.1.57264.1.1"):  # Fulcio's issuer extensions, the newer first
            try:
                raw = cert.extensions.get_extension_for_oid(x509.ObjectIdentifier(oid)).value.value  # type: ignore[attr-defined]
            except x509.ExtensionNotFound:
                continue
            text = raw.decode("utf-8", "replace")
            issuer = text[text.find("http") :] if "http" in text else text
            break
        return (found[0], issuer) if found and issuer else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def commit_evidence(
    workspace: Workspace,
    log: EventLog,
    *,
    election: str,
    out_repo: Path,
    subdir: str | None = None,
    actor: str = "system",
    include: str = "none",
    ots: bool = True,
    cosign: CosignOptions | None = None,
    timeout: float = DEFAULT_TOOL_TIMEOUT,
    clock: Callable[[], datetime] | None = None,
    echo: Echo | None = None,
) -> CommitResult:
    """Collect, write, stamp, sign and record a new evidence version, to publish later; see the module documentation.

    Raises:
        TimestampError: if the evidence cannot be collected or the SUMS file
            cannot be written (a SUMS file written whose stamping, signing or
            copying then failed is recorded as ``evidence.commit_failed``).
    """
    check_actor(actor)
    check_election(election)
    subdir = check_subdir(subdir if subdir is not None else default_subdir(election))
    cosign = cosign or CosignOptions()
    out_repo = Path(out_repo)
    _check_repo(out_repo)
    datamap = build_map(log, workspace)
    if datamap.election is not None and datamap.election != election:
        raise TimestampError(f"the data map's election is {datamap.election}, not {election}")
    evidence = collect_evidence(workspace, log, datamap, election=election)
    target = out_repo / subdir if subdir else out_repo
    target.mkdir(parents=True, exist_ok=True)

    include = check_include(include)

    used = existing_versions(out_repo, subdir) | logged_versions(log, subdir)
    version = max(used, default=0) + 1
    moment = (clock or (lambda: datetime.now(UTC)))().replace(microsecond=0)
    name = sums_name(version, moment)
    content = render_sums(evidence, election, version, moment, subdir)
    sums_path = target / name
    try:
        with open(sums_path, "xb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
    except FileExistsError:
        raise TimestampError(f"{subdir}/{name} already exists; it is never overwritten") from None
    sums_sha = hashlib.sha256(content).hexdigest()
    workspace.put_bytes(content)

    try:
        steps: list[Step] = []
        proofs: list[str] = []
        if ots:
            step = stamp_ots(sums_path, timeout=timeout, echo=echo)
        else:
            step = Step("opentimestamps", "skipped", "stamping turned off (--no-ots)")
        steps.append(step)
        if step.status in ("done", "pending"):
            proofs.append(name + OTS_SUFFIX)
        step, rekor_index = sign_cosign(sums_path, cosign, timeout=timeout, echo=echo)
        steps.append(step)
        if step.status == "done":
            proofs.append(name + COSIGN_SUFFIX)

        included = _copy_included(workspace, evidence, out_repo / election, echo, include) if include != "none" else []
        if include != "none":
            withheld = [f.path for f in evidence.files if not f.publishable and (include == "all" or f.origin == "fetch")]
            detail = f"{len(included)} copied" + (" (the files fetched from the Audit Center)" if include == "fetched" else "")
            if withheld:
                detail += f"; withheld (no passing redaction check on record): {', '.join(withheld)}"
            steps.append(Step("include_files", "done", detail))
    except BaseException as exc:
        log.append(
            "evidence.commit_failed",
            actor,
            file_sha256=sums_sha,
            data={"sums_name": name, "sums_sha256": sums_sha, "version": version, "subdir": subdir, "error": _short(str(exc))},
        )
        raise

    event = log.append(
        "evidence.committed",
        actor,
        file_sha256=sums_sha,
        data={
            "election": election,
            "sums_name": name,
            "sums_sha256": sums_sha,
            "version": version,
            "subdir": subdir,
            "files": len(evidence.files),
            "included_files": len(included),
            "log_head_seq": evidence.log_head[0],
            "log_head_hash": evidence.log_head[1],
            "software": evidence.software,
            "steps": [s.to_dict() for s in steps],
            "proofs": list(proofs),
            "included": list(included),
            "rekor_log_index": rekor_index,
        },
    )
    return CommitResult(
        election=election,
        version=version,
        sums_name=name,
        sums_sha256=sums_sha,
        subdir=subdir,
        evidence=evidence,
        steps=steps,
        proofs=proofs,
        included=included,
        event_seq=event.seq,
        rekor_log_index=rekor_index,
    )


#: What ``include`` copies: nothing; the files fetched from the Audit Center (public already); or
#: every publishable file (also the counties' uploads that passed the redaction check, and the
#: derived extracts and printouts, which carry the expected votes of their CVRs).
INCLUDE_CHOICES = ("none", "fetched", "all")


def check_include(include: str) -> str:
    if include not in INCLUDE_CHOICES:
        raise TimestampError(f"include must be one of {', '.join(INCLUDE_CHOICES)}, not {include!r}")
    return include


def _copy_included(workspace: Workspace, evidence: EvidenceSet, target: Path, echo: Echo | None, include: str = "all") -> list[str]:
    copied: list[str] = []
    for item in evidence.files:
        if include == "fetched" and item.origin != "fetch":
            continue
        if not item.publishable:
            if echo is not None:
                echo(f"not copying {item.path}: no passing redaction check is on record for it")
            continue
        dest = target / item.path
        if dest.is_file() and sha256_of_file(dest)[0] == item.sha256:
            copied.append(item.path)
            continue
        # A new version (a corrected manifest, a page whose content changed) replaces the earlier copy at
        # the same path; the repository's history keeps the earlier one, with the commit that published it.
        if dest.exists() and not dest.is_file():
            raise TimestampError(f"{item.path} is a directory in the output directory")
        dest.unlink(missing_ok=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        workspace.copy_to(item.sha256, dest)
        os.chmod(dest, 0o644)
        copied.append(item.path)
    return copied


def check_title(title: str | None) -> str | None:
    """A commit headline: one line, not empty, at most 100 characters."""
    if title is None:
        return None
    title = title.strip()
    if not title or "\n" in title or "\r" in title or len(title) > 100:
        raise TimestampError("the title must be one line of 1 to 100 characters")
    return title


def verify_command(name: str, signer: tuple[str, str]) -> str:
    """The cosign command that checks a keyless signature of ``name`` against its signer."""
    return (f"cosign verify-blob --bundle {name}{COSIGN_SUFFIX} --certificate-identity {signer[0]} "
            f"--certificate-oidc-issuer {signer[1]} {name}")  # fmt: skip


def _web_link(remote: str, commit: str, path: str) -> str | None:
    """A link to ``path`` as of ``commit`` on GitHub, if the remote is a GitHub repository."""
    match = re.match(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+)/([^/]+?)(?:\.git)?/?$", remote)
    return f"https://github.com/{match.group(1)}/{match.group(2)}/blob/{commit}/{path}" if match else None


def _last_change(repo: Path, path: str, tries: int = 8) -> str | None:
    """The commit that last changed ``path``. In a shallow clone, the oldest commit present seems to have added every
    file, so history is fetched further back until the commit found is not that edge (or ``tries`` runs out: None)."""
    for _ in range(tries):
        last = _git(repo, "log", "-1", "--format=%H", "--", path, check=False).stdout.strip()
        shallow = repo / ".git" / "shallow"
        edge = set(shallow.read_text().split()) if shallow.is_file() else set()
        if not last or last not in edge:
            return last or None
        if _git(repo, "fetch", "--quiet", "--deepen=100", check=False).returncode != 0:
            return None
    return None


def _replaced_files(repo: Path, files: Sequence[str]) -> dict[str, str]:
    """Of ``files``, those already in the repository with other content: each with the commit that last published it."""
    replaced: dict[str, str] = {}
    for line in _git(repo, "status", "--porcelain", "--", *files).stdout.splitlines():
        if line[:2] != "??" and "M" in line[:2]:  # a tracked file, modified
            path = line[3:].strip()
            if PurePosixPath(path).name == LOG_NAME:
                continue  # the event log only grows: each publication is the earlier one and more
            last = _last_change(repo, path)
            if last:
                replaced[path] = last
    return replaced


def _publish_message(
    name: str, data: Mapping[str, Any], event_seq: int, *, title: str | None, ots_status: str, signer: tuple[str, str] | None,
    replaced: Mapping[str, str] | None = None, remote: str | None = None, log: Mapping[str, Any] | None = None,
) -> str:  # fmt: skip
    """The published commit's message: ``title`` (or a default headline), then what it holds and how to check it."""
    default = f"Observer evidence for {data.get('election')}: {name} ({data.get('files')} files)"
    lines = [title or default, ""]
    if title:
        lines.append(f"{name}: {data.get('files')} files.")
    lines += [
        f"Event log head: {data.get('log_head_seq')} {data.get('log_head_hash')}",
        f"Software: {data.get('software')}",
        f"OpenTimestamps: {ots_status}",
    ]
    if signer is not None:
        lines += [f"Signed keyless by {signer[0]} (issuer {signer[1]}), Rekor log index {data.get('rekor_log_index')}. Check with:",
                  verify_command(name, signer)]  # fmt: skip
    elif data.get("rekor_log_index") is not None:
        lines.append(f"Signed with Sigstore, Rekor log index {data.get('rekor_log_index')}.")
    if data.get("included"):
        lines.append(f"Includes copies of {len(data['included'])} evidence files.")
    for path, previous in sorted((replaced or {}).items()):
        link = _web_link(remote, previous, path) if remote else None
        lines += [f"Changed since its last publication: {path}", f"  previous version: commit {previous}" + (f", {link}" if link else "")]
    if log is not None:
        lines.append(f"Event log: {PurePosixPath(str(log['path'])).name}, events 1 to {log['events']} (head {log['head_hash']}).")
    lines.append(f"Recorded in the observers' event log as event {event_seq}.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Publishing


@dataclass
class PublishResult:
    sums_name: str
    sums_sha256: str
    files: list[str]
    message: str
    opentimestamps: str
    commit: str | None
    event_seq: int | None
    replaces: list[str]


def _complete_proof(path: Path, *, timeout: float, echo: Echo | None) -> tuple[bool, int | None]:
    """Try to complete an OpenTimestamps proof in place (``ots upgrade``); whether it is complete, and its Bitcoin block."""
    if not proof_is_complete(path.read_bytes()):
        ots = shutil.which("ots")
        if ots is None:
            raise TimestampError("the ots command (opentimestamps-client) is not installed")
        before = path.read_bytes()
        backup = path.with_name(path.name + ".bak")
        had_backup = backup.exists()
        run_tool([ots, "upgrade", path.name], cwd=path.parent, timeout=timeout, echo=echo)
        if not had_backup:
            backup.unlink(missing_ok=True)
        if not path.is_file():  # never leave the proof missing
            path.write_bytes(before)
    data = path.read_bytes()
    if not proof_is_complete(data):
        return False, None
    heights = bitcoin_block_heights(data)
    return True, (min(heights) if heights else None)


def _remote_address(repo: Path) -> str:
    """The remote's address, without any credentials in it."""
    remotes = _git(repo, "remote").stdout.split()
    if not remotes:
        raise TimestampError("the repository has no remote to publish to")
    url = _git(repo, "remote", "get-url", "origin" if "origin" in remotes else remotes[0]).stdout.strip()
    return re.sub(r"^(https?://)[^/@]+@", r"\1", url)


def _first_fetched(log: EventLog, sha: str | None) -> str | None:
    """The server time of the first fetch whose bytes have SHA-256 ``sha``."""
    for event in log.iter_events():
        if event.kind == "fetch.file" and event.data.get("sha256") == sha:
            return event.server_time
    return None


def legacy_mirror_place(rel: str, fetched_at: str | None) -> str | None:
    """Where the mirror keeps a file a pre-layout SUMS file lists as ``auditcenter/...``: its path under the RLA
    directory; for the Audit Center's main page, a snapshot in ``auditCenters/`` named for its fetch time in
    Colorado time (``auditCenter-2026-11-06T17:24:08-07:00.html``); None for anything else."""
    if not rel.startswith("auditcenter/"):
        return None
    rest = rel.removeprefix("auditcenter/")
    if rest == "www.coloradosos.gov/pubs/elections/auditCenter.html":
        if fetched_at is None:
            return None
        local = to_colorado(datetime.fromisoformat(fetched_at.replace("Z", "+00:00")))
        return f"auditCenters/auditCenter-{local.isoformat(timespec='seconds')}.html"
    first = rest.split("/", 1)[0]
    return None if "." in first and "/" in rest else rest


#: The published event log's name, beside the SUMS files.
LOG_NAME = "events.jsonl"


def _check_log_passes(path: Path, seq: int, digest: str) -> None:
    """Refuse a log that does not hold event ``seq`` with hash ``digest`` (the head a SUMS file's header names)."""
    with open(path, "rb") as f:
        for n, line in enumerate(f, start=1):
            if n == seq:
                if json.loads(line).get("hash") != digest:
                    raise TimestampError(f"the event log's event {seq} is not the head the SUMS file names")
                return
    raise TimestampError(f"the event log has no event {seq}, the head the SUMS file names")


def unpublished(log: EventLog) -> list[Any]:
    """The ``evidence.committed`` events whose SUMS file no ``evidence.published`` event names, oldest first."""
    committed: list[Any] = []
    published: set[str] = set()
    for event in log.iter_events():
        if event.kind == "evidence.committed":
            committed.append(event)
        elif event.kind == "evidence.published":
            published.add(str(event.data.get("sums_sha256")))
    return [e for e in committed if e.data.get("sums_sha256") not in published]


def publish_evidence(
    log: EventLog,
    *,
    out_repo: Path,
    sums_name: str | None = None,
    actor: str = "system",
    title: str | None = None,
    allow_pending: bool = False,
    dry_run: bool = False,
    timeout: float = DEFAULT_TOOL_TIMEOUT,
    echo: Echo | None = None,
    include_log: bool = True,
    workspace: Workspace | None = None,
) -> PublishResult:
    """Publish a commitment: complete its proof, make one commit holding it, push it, and record ``evidence.published``.

    The commitment is the oldest one the log records and no ``evidence.published`` names (or the
    one named ``sums_name``); its SUMS file must be in ``out_repo`` with the hash the log records.
    The OpenTimestamps proof must be complete (a Bitcoin attestation) unless ``allow_pending``.
    ``dry_run`` completes the proof if it can and returns the commit message and files, pending or
    not, but commits, pushes and records nothing. The commit is made on the repository's current branch; the event
    is recorded only once the push has succeeded, so the log names only a commit that is public.
    If the last commit already holds exactly these files and has not been pushed (a push that
    failed), it is pushed as it is.

    With ``include_log`` (the default), the commit also holds the event log as it stands, written
    as ``events.jsonl`` beside the SUMS file (`LOG_NAME`), so that the log head in every SUMS
    file's header can be checked against it: it must verify, and pass through that head. Each
    publication replaces it with the longer log; it only ever grows. Its event range and head
    are in the commit's message and the ``evidence.published`` event (``log``).

    Raises:
        TimestampError: if there is nothing to publish, the SUMS file is missing or differs, the
            proof is still pending, or the push fails.
    """
    check_actor(actor)
    title = check_title(title)
    out_repo = Path(out_repo)
    _check_repo(out_repo)
    waiting = unpublished(log)
    if sums_name is not None:
        waiting = [e for e in waiting if e.data.get("sums_name") == sums_name]
    if not waiting:
        raise TimestampError("no commitment waiting to be published" + (f" named {sums_name}" if sums_name else ""))
    event = waiting[0]
    data = event.data
    name, subdir = str(data["sums_name"]), str(data.get("subdir") or "")
    target = out_repo / subdir if subdir else out_repo
    sums_path = target / name
    if not sums_path.is_file():
        raise TimestampError(f"{subdir}/{name} is not in the repository's working tree")
    if hashlib.sha256(sums_path.read_bytes()).hexdigest() != data["sums_sha256"]:
        raise TimestampError(f"{subdir}/{name} differs from the SUMS file the log records (event {event.seq})")

    names = [name] + [p for p in data.get("proofs") or [] if (target / p).is_file()]
    files = [f"{subdir}/{n}" if subdir else n for n in names]
    # Copied files: relative to the election's folder (``base`` in the SUMS header); in earlier SUMS files, to its own.
    parsed_sums = parse_sums(sums_path.read_text(encoding="utf-8", errors="replace"))
    if "base" in parsed_sums.header:
        root = str(data.get("election") or "")
        files += [f"{root}/{n}" if root else str(n) for n in data.get("included") or []]
    else:
        # A SUMS file from before the mirror layout (``auditcenter/...`` paths beside it): its Audit Center files
        # are placed where the mirror keeps them (`legacy_mirror_place`), from the workspace by hash.
        by_path = {path: sha for sha, path in parsed_sums.entries}
        for rel in data.get("included") or []:
            sha = by_path.get(str(rel))
            dest = legacy_mirror_place(str(rel), _first_fetched(log, sha)) if sha else None
            if dest is None or workspace is None or sha is None:
                files.append(f"{subdir}/{rel}" if subdir else str(rel))
                continue
            if not dry_run:
                path = out_repo / dest
                if not (path.is_file() and sha256_of_file(path)[0] == sha):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.unlink(missing_ok=True)
                    workspace.copy_to(sha, path)
                    os.chmod(path, 0o644)
            files.append(dest)
    ots_path = target / f"{name}{OTS_SUFFIX}"
    if ots_path.is_file():
        complete, block = _complete_proof(ots_path, timeout=timeout, echo=echo)
        ots_status = (f"complete, Bitcoin block {block}" if block else "complete") if complete else "pending"
        if not complete and not allow_pending and not dry_run:
            raise TimestampError(f"{name}'s OpenTimestamps proof is still pending Bitcoin confirmation; publish later, or with --allow-pending")
    else:
        ots_status = "none"
    bundle = target / f"{name}{COSIGN_SUFFIX}"
    signer = bundle_identity(bundle) if bundle.is_file() else None
    replaced = _replaced_files(out_repo, files)
    remotes = _git(out_repo, "remote").stdout.split()
    remote = _remote_address(out_repo) if remotes else None
    published_log: dict[str, Any] | None = None
    if include_log:
        log_rel = f"{subdir}/{LOG_NAME}" if subdir else LOG_NAME
        head_seq, head_hash = log.head()
        if not dry_run:
            exported = target / LOG_NAME
            log.export_jsonl(exported)
            checked = verify_jsonl(exported)
            if not checked.ok or (checked.head_seq, checked.head_hash) != (head_seq, head_hash):
                raise TimestampError(f"the exported event log does not verify: {'; '.join(checked.errors)[:300]}")
            _check_log_passes(exported, int(data["log_head_seq"]), str(data["log_head_hash"]))
        files.append(log_rel)
        published_log = {"path": log_rel, "events": head_seq, "head_hash": head_hash}
    message = _publish_message(name, data, event.seq, title=title, ots_status=ots_status, signer=signer, replaced=replaced, remote=remote,
                               log=published_log)  # fmt: skip
    earlier = data.get("commit")
    if dry_run:
        return PublishResult(name, str(data["sums_sha256"]), files, message, ots_status, None, None, [])

    head = _git(out_repo, "rev-parse", "HEAD").stdout.strip()
    changed = _git(out_repo, "status", "--porcelain", "--untracked-files=all", "--", *files).stdout.strip()
    in_head = set(_git(out_repo, "show", "--name-only", "--format=", "HEAD").stdout.split())
    retry = not changed and set(files) <= in_head and not _git(out_repo, "branch", "-r", "--contains", head).stdout.strip()
    if not retry:
        if not changed:
            raise TimestampError(f"{subdir}/{name} and its proofs are already committed; nothing to publish")
        _git(out_repo, "add", "--", *files)
        _git(out_repo, "commit", "--quiet", "-m", message, "--", *files)
    commit = _git(out_repo, "rev-parse", "HEAD").stdout.strip()
    pushed, detail = _push(out_repo, echo)
    if not pushed:
        raise TimestampError(f"committed {commit[:12]} but did not publish it ({detail}); nothing recorded. Fix that and run this again.")
    branch = _git(out_repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    replaces = [str(earlier)] if isinstance(earlier, str) and earlier and earlier != commit else []
    published = log.append(
        "evidence.published",
        actor,
        file_sha256=str(data["sums_sha256"]),
        data={
            "sums_name": name,
            "sums_sha256": data["sums_sha256"],
            "committed_in_event": event.seq,
            "repository": _remote_address(out_repo),
            "branch": branch,
            "commit": commit,
            "files": files,
            "opentimestamps": ots_status,
            "rekor_log_index": data.get("rekor_log_index"),
            "replaces_unpublished_commits": replaces,
            "replaced": dict(replaced),
            "log": published_log,
        },
    )
    return PublishResult(name, str(data["sums_sha256"]), files, message, ots_status, commit, published.seq, replaces)


# ---------------------------------------------------------------------------
# Upgrading OpenTimestamps proofs


@dataclass
class UpgradeResult:
    upgraded: list[str]
    still_pending: list[str]
    complete: list[str]
    failed: list[str]
    skipped: str | None
    commit: str | None
    pushed: bool
    event_seq: int
    steps: list[Step] = field(default_factory=list)


def proof_is_complete(data: bytes) -> bool:
    """Whether an OpenTimestamps proof already holds a Bitcoin attestation."""
    return OTS_BITCOIN_TAG in data


def bitcoin_block_heights(proof: bytes) -> list[int]:
    """The Bitcoin block heights attested in an OpenTimestamps proof (empty if none, or if it can't be parsed)."""
    heights: list[int] = []
    start = 0
    while True:
        i = proof.find(OTS_BITCOIN_TAG, start)
        if i < 0:
            return heights
        start = i + len(OTS_BITCOIN_TAG)
        values = []
        j = start
        for _ in range(2):  # the payload's length, then the height
            value, shift = 0, 0
            while j < len(proof):
                byte = proof[j]
                j += 1
                value |= (byte & 0x7F) << shift
                shift += 7
                if not byte & 0x80:
                    break
            else:
                break
            values.append(value)
        if len(values) == 2 and 0 < values[0] <= 10:
            heights.append(values[1])


def upgrade_proofs(
    log: EventLog,
    *,
    out_repo: Path,
    subdir: str | None = None,
    actor: str = "system",
    push: bool = True,
    include_complete: bool = False,
    timeout: float = DEFAULT_TOOL_TIMEOUT,
    echo: Echo | None = None,
) -> UpgradeResult:
    """Run ``ots upgrade`` on the repository's pending ``.ots`` files, commit the upgraded ones and record an event.

    For proofs already committed (as for the July 2026 primary); a commitment published with
    `publish_evidence` is published with its proof complete.
    """
    check_actor(actor)
    out_repo = Path(out_repo)
    _check_repo(out_repo)
    scope = check_subdir(subdir) if subdir else "."
    tracked = [p for p in _git(out_repo, "ls-files", "-z", "--", scope).stdout.split("\0") if p.endswith(OTS_SUFFIX)]
    upgraded: list[str] = []
    pending: list[str] = []
    complete: list[str] = []
    failed: list[str] = []
    blocks: dict[str, int | None] = {}
    skipped: str | None = None
    ots = shutil.which("ots")
    if ots is None:
        skipped = "the ots command (opentimestamps-client) is not installed"
    for rel in sorted(tracked):
        path = out_repo / rel
        before = path.read_bytes()
        if proof_is_complete(before) and not include_complete:
            complete.append(rel)
            continue
        if ots is None:
            pending.append(rel)
            continue
        backup = path.with_name(path.name + ".bak")
        had_backup = backup.exists()
        run = run_tool([ots, "upgrade", path.name], cwd=path.parent, timeout=timeout, echo=echo)
        if not had_backup:
            backup.unlink(missing_ok=True)
        if not path.is_file():  # never leave the proof missing
            path.write_bytes(before)
            failed.append(rel)
            continue
        after = path.read_bytes()
        if after != before:
            upgraded.append(rel)
            if not proof_is_complete(after):
                pending.append(rel)
            else:
                heights = bitcoin_block_heights(after)
                blocks[rel] = min(heights) if heights else None
        elif run.returncode is None:
            failed.append(rel)
        else:
            pending.append(rel)
    commit: str | None = None
    steps: list[Step] = []
    if upgraded:
        message = f"Upgrade {len(upgraded)} OpenTimestamps proof(s)\n\n" + "\n".join(upgraded) + "\n"
        commit = _commit(out_repo, upgraded, message)
        steps.append(Step("git_commit", "done", commit))
        steps.append(_push_step(out_repo, push, echo))
    pushed = any(s.name == "push" and s.status == "done" for s in steps)
    event = log.append(
        "evidence.proofs_upgraded",
        actor,
        data={
            "upgraded": list(upgraded),
            "still_pending": list(pending),
            "failed": list(failed),
            "skipped": skipped,
            "commit": commit,
            "pushed": pushed,
            "bitcoin_blocks": dict(blocks),
        },
    )
    return UpgradeResult(upgraded, pending, complete, failed, skipped, commit, pushed, event.seq, steps)


# ---------------------------------------------------------------------------
# Verifying


@dataclass(frozen=True)
class Check:
    """One verification result: ``ok``, ``mismatch``, ``missing``, ``pending``, ``error``, ``skipped`` or ``failed``."""

    status: str
    subject: str
    detail: str = ""


@dataclass
class VerifyReport:
    checks: list[Check]
    header: dict[str, str]

    @property
    def ok(self) -> bool:
        return not any(c.status in ("mismatch", "error", "failed") for c in self.checks)

    def count(self, status: str) -> int:
        return sum(1 for c in self.checks if c.status == status)


def verify_evidence(
    sums_path: Path,
    *,
    base: Path | None = None,
    workspace: Workspace | None = None,
    cosign_key: Path | None = None,
    certificate_identity: str | None = None,
    certificate_oidc_issuer: str | None = None,
    timeout: float = DEFAULT_TOOL_TIMEOUT,
) -> VerifyReport:
    """Check a SUMS file's listed files (where available locally) and its proofs (where the tools exist).

    Files are looked for under ``base`` (by default the SUMS file's
    directory), then, by hash, in ``workspace`` if given.
    """
    sums_path = Path(sums_path)
    data = sums_path.read_bytes()
    parsed = parse_sums(data.decode("utf-8", errors="replace"))
    if base is None:
        declared = parsed.header.get("base", ".")
        base = sums_path.parent / declared if _is_safe_relative(declared) or set(PurePosixPath(declared).parts) <= {".."} else sums_path.parent
    base = Path(base)
    checks: list[Check] = [Check("ok", sums_path.name, f"sha256 {hashlib.sha256(data).hexdigest()}")]
    for line in parsed.malformed:
        checks.append(Check("error", "malformed line", line))
    for sha, path in parsed.entries:
        if not _is_safe_relative(path):
            checks.append(Check("error", path, "not a safe relative path; not read"))
            continue
        local = base / path
        if local.is_file():
            actual = sha256_of_file(local)[0]
            checks.append(Check("ok", path) if actual == sha else Check("mismatch", path, f"listed {sha}, file has {actual}"))
        elif workspace is not None and workspace.has(sha):
            try:
                workspace.verify(sha)
                checks.append(Check("ok", path, "the workspace's copy"))
            except Exception as exc:
                checks.append(Check("mismatch", path, f"the workspace's copy: {exc}"))
        else:
            checks.append(Check("missing", path, "not available locally"))

    proof = sums_path.with_name(sums_path.name + OTS_SUFFIX)
    if not proof.is_file():
        checks.append(Check("missing", proof.name, "no OpenTimestamps proof"))
    elif (ots := shutil.which("ots")) is None:
        state = "complete" if proof_is_complete(proof.read_bytes()) else "pending"
        checks.append(Check("skipped", proof.name, f"ots is not installed (the proof looks {state}); try opentimestamps.org"))
    else:
        run = run_tool([ots, "verify", proof.name], cwd=proof.parent, timeout=timeout)
        if run.returncode == 0:
            status = "ok"
        elif not proof_is_complete(proof.read_bytes()):
            status = "pending"
        elif "bitcoin node" in run.output.lower() or "bitcoin.conf" in run.output.lower():
            status = "skipped"  # a complete proof, but no Bitcoin node to check it against
        else:
            status = "failed"
        checks.append(Check(status, proof.name, f"ots verify: {_short(run.output)}"))

    bundle = sums_path.with_name(sums_path.name + COSIGN_SUFFIX)
    if not bundle.is_file():
        checks.append(Check("missing", bundle.name, "no Sigstore bundle"))
    elif (cosign := shutil.which("cosign")) is None:
        checks.append(Check("skipped", bundle.name, "cosign is not installed; try search.sigstore.dev with the SUMS file's sha256"))
    else:
        argv = [cosign, "verify-blob", "--bundle", bundle.name]
        note = ""
        if cosign_key is not None:
            argv += ["--key", str(Path(cosign_key).resolve())]
        elif certificate_identity or certificate_oidc_issuer:
            argv += ["--certificate-identity", certificate_identity or "", "--certificate-oidc-issuer", certificate_oidc_issuer or ""]
        else:
            argv += ["--certificate-identity-regexp", ".*", "--certificate-oidc-issuer-regexp", ".*"]
            note = " (the signer's identity was not checked: give --certificate-identity and --certificate-oidc-issuer)"
        argv.append(sums_path.name)
        run = run_tool(argv, cwd=sums_path.parent, timeout=timeout)
        status = "ok" if run.returncode == 0 else "failed"
        checks.append(Check(status, bundle.name, f"cosign verify-blob{note}: {_short(run.output)}"))
    return VerifyReport(checks, parsed.header)
