"""Timestamping the evidence: versioned SHA256SUMS files, OpenTimestamps proofs and Sigstore signatures.

This automates the July 2026 procedure (see docs/TIMESTAMPING.md) as one
command, `commit_evidence`:

1. **Collect** the evidence set from the workspace: every file the data map
   uses (fetched audit center files, county uploads, files supplied by an
   override, adapter outputs), the overrides file last applied, the source
   of every adapter whose output is used, the latest prediction, and each
   county's latest extract with its stored page index (the per-page hashes
   printed on the extract, ``page_index_sha256`` in ``extract.built``) and
   its printouts (each board's PDF in each layout, ``pdfs``).
   Every stored object is re-hashed on the way, so a corrupt store stops
   the commitment rather than committing to it.
2. **Write** a new versioned SHA256SUMS file (``SHA256SUMS.txt``, then
   ``SHA256SUMS.v2.txt``, ``SHA256SUMS.v3.txt`` and so on) in the output
   repository: one ``<sha256>  <path>`` line per file in sorted path order,
   after a ``#`` header giving the election, the time (UTC), the event log's
   head (sequence number and hash, which commits to the whole log) and the
   software version. A version number that exists, or ever existed in the
   repository's history, is never used again, and the file is created
   exclusively, so nothing is overwritten.
3. **Stamp** it with OpenTimestamps (``ots stamp``), if the ``ots`` command
   is installed, producing ``<sums>.ots``. The proof is pending until a
   calendar's Bitcoin transaction confirms; `upgrade_proofs` fetches the
   completed proof later.
4. **Sign** it with Sigstore (``cosign sign-blob --bundle``), if ``cosign``
   is installed, producing ``<sums>.cosign-bundle``: keyless (an OIDC
   identity, recorded in the public Rekor log) unless a key file is given.
5. **Commit** the SUMS file and its proofs (and, only if asked, copies of
   the evidence files themselves) to the output repository, and push if it
   has a remote and pushing was not turned off.
6. **Record** an ``evidence.committed`` event.

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
    file names made), ``rekor_log_index`` (or null), ``commit`` (the output
    repository's commit hash) and ``pushed``.
``evidence.commit_failed``
    ``sums_name``, ``sums_sha256``, ``version``, ``error``: the SUMS file was
    written but could not be committed.
``evidence.proofs_upgraded``
    ``upgraded`` and ``still_pending`` (proof file paths within the output
    repository), ``skipped`` (why, if ``ots`` was not run), ``commit`` and
    ``pushed``, and ``bitcoin_blocks``: for each proof upgraded to a Bitcoin
    attestation, the block's height (null if it could not be parsed).

No local paths are recorded in events, SUMS files or commit messages.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from corla_observer.classify import is_test_county
from corla_observer.datamap import DataMap, build_map
from corla_observer.events import EventLog, JSONValue, check_actor
from corla_observer.redaction import PASS
from corla_observer.workspace import Workspace, sha256_of_file

#: The first SUMS file's name; later versions are ``SHA256SUMS.v<N>.txt``.
SUMS_BASENAME = "SHA256SUMS"
SUMS_NAME_RE = re.compile(r"^SHA256SUMS(?:\.v([1-9][0-9]*))?\.txt$")
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


def collect_evidence(workspace: Workspace, log: EventLog, datamap: DataMap | None = None) -> EvidenceSet:
    """The evidence set: see the module documentation.

    Raises:
        TimestampError: if a stored object is missing or corrupt.
    """
    log_head = log.head()
    datamap = datamap or build_map(log, workspace)
    adapter_sources: dict[str, tuple[str, str]] = {}  # output sha -> (module, source sha)
    redaction_passed: set[str] = set()
    prediction: tuple[int, str] | None = None
    extracts: dict[str, tuple[int, str, str | None, dict[str, dict[str, str]]]] = {}
    for event in log.iter_events():
        if event.seq > log_head[0]:
            break
        if event.kind == "map.adapter_ran":
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

    def add(
        path: str, sha256: str, origin: str, ref: str, *,
        kind: str | None = None, county: str | None = None, publishable: bool = True,
    ) -> None:
        candidates.append(EvidenceFile(path, sha256, 0, origin, ref, kind, county, publishable))

    ignored = 0
    for entry in datamap.entries:
        if entry.ignored:
            ignored += 1
            continue
        county = entry.county
        if entry.source == "fetch":
            path = fetched_path(entry.ref)
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


def sums_name(version: int) -> str:
    return f"{SUMS_BASENAME}.txt" if version == 1 else f"{SUMS_BASENAME}.v{version}.txt"


def sums_version(name: str) -> int | None:
    match = SUMS_NAME_RE.match(name)
    if match is None:
        return None
    return int(match.group(1)) if match.group(1) else 1


def render_sums(evidence: EvidenceSet, election: str, version: int, moment: datetime) -> bytes:
    """The SUMS file's content: a ``#`` header, then ``<sha256>  <path>`` lines in sorted path order."""
    seq, head = evidence.log_head
    lines = [
        f"# SHA-256 digests of the observers' evidence for election {election}, written by corla-observer.",
        f"# version: {version}",
        f"# time: {moment.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"# event-log-head: {seq} {head}",
        f"# software: {evidence.software}",
        f"# files: {len(evidence.files)}",
        f"# check: sha256sum --check --ignore-missing {sums_name(version)}",
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
    commit: str
    pushed: bool
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
        raise TimestampError(f"--subdir must be a plain relative path such as observerfiles/evidence: {subdir!r}")
    return "/".join(parts)


def default_subdir(election: str) -> str:
    return f"{election}/observerfiles/evidence"


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
    run = run_tool(argv, cwd=path.parent, timeout=timeout, echo=echo, interactive=True)
    if run.returncode == 0 and bundle.is_file() and bundle.stat().st_size > 0:
        match = _REKOR_INDEX_RE.search(run.output)
        index = int(match.group(1)) if match else None
        detail = f"signed {mode}" + (f"; Rekor log index {index}" if index is not None else "")
        return Step("sigstore", "done", detail), index
    bundle.unlink(missing_ok=True)
    why = "timed out" if run.returncode is None else f"exit status {run.returncode}"
    return Step("sigstore", "failed", f"cosign sign-blob {mode} {why}: {_short(run.output)}"), None


def commit_evidence(
    workspace: Workspace,
    log: EventLog,
    *,
    election: str,
    out_repo: Path,
    subdir: str | None = None,
    actor: str = "system",
    push: bool = True,
    include_files: bool = False,
    ots: bool = True,
    cosign: CosignOptions | None = None,
    timeout: float = DEFAULT_TOOL_TIMEOUT,
    clock: Callable[[], datetime] | None = None,
    echo: Echo | None = None,
) -> CommitResult:
    """Collect, write, stamp, sign, commit and record a new evidence version; see the module documentation.

    Raises:
        TimestampError: if the evidence cannot be collected or the SUMS file
            cannot be written or committed (a SUMS file written but not
            committed is recorded as ``evidence.commit_failed``).
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
    evidence = collect_evidence(workspace, log, datamap)
    target = out_repo / subdir if subdir else out_repo
    target.mkdir(parents=True, exist_ok=True)

    if include_files:
        _avoid_collisions(evidence, target)

    used = existing_versions(out_repo, subdir)
    version = max(used, default=0) + 1
    name = sums_name(version)
    moment = (clock or (lambda: datetime.now(UTC)))()
    content = render_sums(evidence, election, version, moment)
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

        included = _copy_included(workspace, evidence, target, echo) if include_files else []
        if include_files:
            withheld = [f.path for f in evidence.files if not f.publishable]
            detail = f"{len(included)} copied"
            if withheld:
                detail += f"; withheld (no passing redaction check on record): {', '.join(withheld)}"
            steps.append(Step("include_files", "done", detail))
        rel = [f"{subdir}/{p}" if subdir else p for p in [name, *proofs, *included]]
        message = _commit_message(election, name, evidence, steps, included)
        commit = _commit(out_repo, rel, message)
        steps.append(Step("git_commit", "done", commit))
    except BaseException as exc:
        log.append(
            "evidence.commit_failed",
            actor,
            file_sha256=sums_sha,
            data={"sums_name": name, "sums_sha256": sums_sha, "version": version, "subdir": subdir, "error": _short(str(exc))},
        )
        raise

    push_step = _push_step(out_repo, push, echo)
    steps.append(push_step)
    pushed = push_step.status == "done"
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
            "rekor_log_index": rekor_index,
            "commit": commit,
            "pushed": pushed,
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
        commit=commit,
        pushed=pushed,
        event_seq=event.seq,
        rekor_log_index=rekor_index,
    )


def _avoid_collisions(evidence: EvidenceSet, target: Path) -> None:
    """Give a file a new path if a different file already sits at its path in the output directory."""
    renamed: list[EvidenceFile] = []
    for item in evidence.files:
        dest = target / item.path
        if dest.is_file() and sha256_of_file(dest)[0] != item.sha256:
            item = EvidenceFile(
                f"{item.path}~{item.sha256[:12]}", item.sha256, item.size, item.origin, item.ref,
                item.kind, item.county, item.publishable,
            )
        renamed.append(item)
    evidence.files = sorted(renamed, key=lambda f: f.path)


def _copy_included(workspace: Workspace, evidence: EvidenceSet, target: Path, echo: Echo | None) -> list[str]:
    copied: list[str] = []
    for item in evidence.files:
        if not item.publishable:
            if echo is not None:
                echo(f"not copying {item.path}: no passing redaction check is on record for it")
            continue
        dest = target / item.path
        if dest.is_file() and sha256_of_file(dest)[0] == item.sha256:
            copied.append(item.path)
            continue
        if dest.exists():
            raise TimestampError(f"{item.path} exists in the output directory with other content")
        dest.parent.mkdir(parents=True, exist_ok=True)
        workspace.copy_to(item.sha256, dest)
        os.chmod(dest, 0o644)
        copied.append(item.path)
    return copied


def _commit_message(election: str, name: str, evidence: EvidenceSet, steps: Sequence[Step], included: Sequence[str]) -> str:
    seq, head = evidence.log_head
    lines = [
        f"Observer evidence for {election}: {name} ({len(evidence.files)} files)",
        "",
        f"Event log head: {seq} {head}",
        f"Software: {evidence.software}",
    ]
    for step in steps:
        lines.append(f"{step.name}: {step.status} ({step.detail})")
    if included:
        lines.append(f"Includes copies of {len(included)} evidence files.")
    lines.extend(f"Note: {note}" for note in evidence.notes)
    return "\n".join(lines) + "\n"


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
    """Run ``ots upgrade`` on the repository's pending ``.ots`` files, commit the upgraded ones and record an event."""
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
    base = Path(base) if base is not None else sums_path.parent
    data = sums_path.read_bytes()
    parsed = parse_sums(data.decode("utf-8", errors="replace"))
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
