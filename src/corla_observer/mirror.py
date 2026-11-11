"""Keeping the public mirror of the Audit Center up to date: docs/MIRROR.md.

The public mirror (``github.com/nealmcb/auditcenter``) keeps every file the Audit Center published, at the
path it was published under. The server keeps a git clone of it (``mirror_clone`` in the workspace's
``schedule.toml``; `clone_path`), and after every crawl the scheduler (`corla_observer.schedule.run_cycle`)
calls `after_crawl`:

1. **Update** (`update_mirror`): every Audit Center file the event log holds as currently fetched
   (`current_files`) whose content differs from the version in the clone's ``HEAD`` is written there,
   from the copy the crawl stored in the workspace, at its mirror path: a file under
   ``/pubs/elections/RLA/`` at its path relative to that directory
   (`corla_observer.timestamp.mirror_path`), and the Audit Center page itself as a dated snapshot,
   ``auditCenters/auditCenter_<Colorado time of the fetch>.html``, when it differs from the latest
   snapshot there. A page is compared without Cloudflare's per-response token
   (`corla_observer.fetch.normalize_html`), any other file byte for byte. Nothing is ever deleted,
   moved or renamed. If anything was written, the clone gets one commit for the crawl, headed
   ``Audit Center files as fetched 2026-11-16 10:42 MST`` (`HEADING`), whose body lists each file
   as new or changed, with the commit holding a changed file's previous version, and names the crawl
   in the event log. A folder outside a sparse checkout is added to it first.
2. **Wait.** The commit is not pushed: commits from several crawls can wait together
   (`waiting_commits`).
3. **Publish** (`publish_mirror`), when a coordinator presses Publish on ``/coord``, or after each
   crawl in a window with ``publish_mirror = true``: GitHub's ``main`` is fetched, the waiting
   commits are replayed on top of it in the order of the crawls, each still its own commit, and
   pushed; only then is ``mirror.updated`` recorded. Where GitHub's ``main`` meanwhile got the same
   file, GitHub's version is kept if its content is the same (for a page, but for Cloudflare's
   token); otherwise the file fetched here goes on top, and the commit's message names the version
   on GitHub too. Other commits waiting in the clone (an evidence commit whose push failed, say) are
   replayed after the mirror's and left waiting.

Pushing uses whatever credentials the clone is set up with (docs/MIRROR.md: a fine-grained token read
by a credential helper, and an explicit HTTPS push URL). Nothing here reads, stores or passes a
credential, and git is run with ``GIT_TERMINAL_PROMPT=0`` so that a missing one fails instead of
waiting for a person. Commits published from another machine (fetched from the server's clone and
pushed from there) are found on GitHub's ``main`` by the next update or publication and recorded as
published (``published_from`` ``elsewhere``).

A failure in any of this never fails or holds up the crawl: `after_crawl` records it
(``mirror.error``), returns it in the cycle's summary, and ``/coord`` shows it (`mirror_status`);
the next crawl's update tries again.

Events (actor ``system``, or the coordinator who pressed Publish):

``mirror.updated``
    Commits now on GitHub's ``main``: ``repository`` (its address, or ``a local repository``),
    ``branch``, ``commits`` (each with ``commit``, ``heading`` and ``files``: ``path`` and
    ``change``, ``new`` or ``changed``), ``published_from`` (``this server`` or ``elsewhere``),
    ``pushed`` (how: ``remote``, ``transport`` -- ``https``, ``ssh``, ``local`` or ``other`` -- and
    ``url`` without credentials, or null for a local repository; null if published elsewhere),
    ``replayed`` (whether the commits were replayed onto a newer ``main``), ``kept_github_version``
    (paths whose version on GitHub was kept), ``both_versions`` (``path`` and ``github_commit``,
    for a file GitHub's ``main`` held in another version, replaced by the one fetched here) and
    ``dropped`` (headings of commits left with nothing to add).
``mirror.error``
    ``step`` (``fetch``, ``update`` or ``publish``) and ``error``: recorded when a step fails with
    an error other than the last one recorded for it, and with ``error`` null once it works again.

No local paths are recorded in events or commit messages. Standard library only, and git.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from corla_observer.events import EventLog, JSONValue, check_actor
from corla_observer.fetch import normalize_html
from corla_observer.localtime import colorado_time, parse_instant, to_colorado
from corla_observer.workspace import Workspace

#: The heading of every commit the mirror update makes, followed by the crawl's time in Colorado time.
HEADING = "Audit Center files as fetched"
#: The clone's remote and branch: GitHub's ``main``.
REMOTE = "origin"
BRANCH = "main"
#: Where the mirror keeps the Audit Center page's snapshots.
SNAPSHOT_DIR = "auditCenters"
SNAPSHOT_PREFIX = "auditCenter_"
#: The Audit Center page's path, which lies outside the RLA directory.
AUDIT_CENTER_PATH = "/pubs/elections/auditcenter.html"
#: How far back GitHub's ``main`` is searched for this server's commits published from elsewhere.
SEARCH_COMMITS = 300

_FOOTER_RE = re.compile(
    r"the crawl is event (\d+) in the observers' event log, with state ([0-9a-f]{64})"
)
_STEPS = ("fetch", "update", "publish")
_MAX_ERROR = 500


class MirrorError(Exception):
    """The clone could not be read, updated or published."""


# ---------------------------------------------------------------------------
# git


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        raise MirrorError("git is not installed")
    environment = dict(os.environ) | {"GIT_TERMINAL_PROMPT": "0"} | dict(env or {})
    result = subprocess.run(
        [git, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        env=environment,
        input=input,
    )
    if check and result.returncode != 0:
        raise MirrorError(
            f"git {' '.join(args[:2])} failed: {_short(result.stderr or result.stdout)}"
        )
    return result


def _git_bytes(repo: Path, *args: str) -> bytes:
    git = shutil.which("git")
    if git is None:
        raise MirrorError("git is not installed")
    result = subprocess.run(
        [git, "-C", str(repo), *args], capture_output=True, timeout=600, check=False
    )
    if result.returncode != 0:
        raise MirrorError(
            f"git {' '.join(args[:2])} failed: {_short(result.stderr.decode('utf-8', 'replace'))}"
        )
    return result.stdout


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_ERROR else text[: _MAX_ERROR - 3] + "..."


def _rev(repo: Path, rev: str) -> str | None:
    found = _git(
        repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", check=False
    )
    return (found.stdout.strip() or None) if found.returncode == 0 else None


def _is_ancestor(repo: Path, older: str, newer: str) -> bool:
    return (
        _git(repo, "merge-base", "--is-ancestor", older, newer, check=False).returncode
        == 0
    )


def _upstream(repo: Path) -> str | None:
    """GitHub's ``main`` as the clone last fetched it, or None if never fetched."""
    return _rev(repo, f"refs/remotes/{REMOTE}/{BRANCH}")


def check_clone(repo: Path) -> None:
    """Refuse anything but a git working tree on `BRANCH` with the remote `REMOTE`."""
    if not repo.is_dir():
        raise MirrorError(f"the mirror's clone is not there: {repo}")
    inside = _git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise MirrorError(f"not a git working tree: {repo}")
    branch = _git(
        repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False
    ).stdout.strip()
    if branch != BRANCH:
        raise MirrorError(
            f"the mirror's clone is on {branch or 'a detached HEAD'}, not {BRANCH}"
        )
    if REMOTE not in _git(repo, "remote").stdout.split():
        raise MirrorError(f"the mirror's clone has no remote named {REMOTE}")


def _fetch(repo: Path) -> None:
    _git(repo, "fetch", "--quiet", REMOTE, BRANCH)


def _object_format(repo: Path) -> str:
    found = _git(repo, "rev-parse", "--show-object-format", check=False).stdout.strip()
    return "sha256" if found == "sha256" else "sha1"


@dataclass(frozen=True)
class _Entry:
    mode: str
    blob: str


def _tree(repo: Path, rev: str, paths: Sequence[str] = ()) -> dict[str, _Entry]:
    """The blobs at ``rev``: every one, or those at ``paths``."""
    if not paths and _rev(repo, rev) is None:
        return {}
    out = _git(
        repo,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        rev,
        *(["--", *paths] if paths else []),
    ).stdout
    entries: dict[str, _Entry] = {}
    for item in out.split("\0"):
        if not item:
            continue
        meta, _, path = item.partition("\t")
        mode, kind, blob = meta.split()
        if kind == "blob":
            entries[path] = _Entry(mode, blob)
    return entries


_BLOB_IDS: dict[tuple[str, str], str] = {}


def _blob_id(workspace: Workspace, sha256: str, object_format: str) -> str:
    """The git object ID of a stored file, as `git hash-object` would give it (cached by content)."""
    key = (object_format, sha256)
    if key not in _BLOB_IDS:
        path = workspace.path_for(sha256)
        digest = hashlib.new(object_format)
        digest.update(b"blob %d\0" % path.stat().st_size)
        with open(path, "rb") as f:
            while chunk := f.read(1024 * 1024):
                digest.update(chunk)
        _BLOB_IDS[key] = digest.hexdigest()
    return _BLOB_IDS[key]


def _same_page(a: bytes, b: bytes) -> bool:
    return a == b or normalize_html(a) == normalize_html(b)


def _is_page(path: str) -> bool:
    return path.lower().endswith((".html", ".htm"))


def _remote_web_address(repo: Path) -> str:
    """The remote's address for the record: without credentials, or ``a local repository``."""
    _, clean = _transport(_git(repo, "remote", "get-url", REMOTE).stdout.strip())
    return clean if clean is not None else "a local repository"


def _transport(url: str) -> tuple[str, str | None]:
    """How a URL pushes (``https``, ``ssh``, ``local`` or ``other``), and the URL without credentials (None if local)."""
    if re.match(r"^https?://", url):
        return "https", re.sub(r"^(https?://)[^/@]+@", r"\1", url)
    if url.startswith("ssh://") or re.match(r"^[\w.-]+@[\w.-]+:", url):
        return "ssh", url
    if (
        url.startswith("file://")
        or url.startswith("/")
        or url.startswith(".")
        or "://" not in url
        and ":" not in url
    ):
        return "local", None
    return "other", re.sub(r"^([a-z+]+://)[^/@]+@", r"\1", url)


def _web_link(repo: Path, commit: str, path: str) -> str | None:
    from corla_observer.timestamp import _web_link as link

    url = _git(repo, "remote", "get-url", REMOTE, check=False).stdout.strip()
    return (
        link(re.sub(r"^(https?://)[^/@]+@", r"\1", url), commit, path) if url else None
    )


# ---------------------------------------------------------------------------
# Configuration


def clone_path(workspace: Workspace, schedule: Any) -> Path | None:
    """The server's clone of the mirror that ``schedule`` (`corla_observer.schedule.Schedule`) names, or None.

    ``~`` is the server user's home; a relative path is taken from the workspace."""
    named = getattr(schedule, "mirror_clone", None)
    if not named:
        return None
    path = Path(str(named)).expanduser()
    return path if path.is_absolute() else workspace.root / path


def configured_clone(workspace: Workspace) -> Path | None:
    """The clone the workspace's ``schedule.toml`` names, or None (also when it cannot be read)."""
    from corla_observer.schedule import ScheduleError, load_schedule

    try:
        return clone_path(workspace, load_schedule(workspace))
    except ScheduleError:
        return None


# ---------------------------------------------------------------------------
# What the log holds


@dataclass(frozen=True)
class FetchedFile:
    """An Audit Center file as the event log last recorded it."""

    url: str
    sha256: str
    #: For a page, the SHA-256 of its content without Cloudflare's token.
    normalized_sha256: str | None
    #: When those bytes were fetched (UTC, as recorded).
    fetched: str


def current_files(log: EventLog) -> dict[str, FetchedFile]:
    """Every URL's latest real fetch (``fetch.file``), unless it was recorded missing since.

    Fetches replayed from the mirror (``simulated``) are left out: they never go back into it."""
    files: dict[str, FetchedFile] = {}
    for event in log.iter_events():
        if not event.kind.startswith("fetch.") or "simulated" in event.data:
            continue
        url = event.data.get("url")
        if not isinstance(url, str):
            continue
        if event.kind == "fetch.file" and isinstance(event.data.get("sha256"), str):
            normalized = event.data.get("normalized_sha256")
            files[url] = FetchedFile(
                url,
                str(event.data["sha256"]),
                normalized if isinstance(normalized, str) else None,
                event.server_time,
            )
        elif event.kind == "fetch.missing":
            files.pop(url, None)
    return files


@dataclass(frozen=True)
class Crawl:
    """The latest real crawl in the log: its ``fetch.crawl_finished`` event, state digest and start."""

    seq: int
    state_sha256: str
    started: str


def last_crawl(log: EventLog) -> Crawl | None:
    started: str | None = None
    found: Crawl | None = None
    for event in log.iter_events():
        if "simulated" in event.data:
            continue
        if event.kind == "fetch.crawl_started":
            started = event.server_time
        elif event.kind == "fetch.crawl_finished":
            found = Crawl(
                event.seq,
                str(event.data.get("state_sha256") or ""),
                started or event.server_time,
            )
    return found


def _recorded_commits(log: EventLog) -> set[str]:
    found: set[str] = set()
    for event in log.iter_events():
        if event.kind == "mirror.updated":
            commits = event.data.get("commits")
            for item in commits if isinstance(commits, list) else []:
                if isinstance(item, dict) and isinstance(item.get("commit"), str):
                    found.add(str(item["commit"]))
    return found


def _crawl_states(log: EventLog) -> dict[int, str]:
    return {
        e.seq: str(e.data.get("state_sha256"))
        for e in log.iter_events()
        if e.kind == "fetch.crawl_finished"
    }


# ---------------------------------------------------------------------------
# Commits


@dataclass(frozen=True)
class MirrorCommit:
    """A commit in the clone: its ID, heading, whether the mirror update made it, and its files."""

    commit: str
    heading: str
    is_mirror: bool
    #: Each file the commit adds or changes (or, in a commit not the mirror update's, removes), with ``new``,
    #: ``changed`` or ``removed``.
    files: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "commit": self.commit,
            "heading": self.heading,
            "files": [{"path": p, "change": c} for p, c in self.files],
        }


_CHANGES = {"A": "new", "M": "changed", "T": "changed", "D": "removed"}


def _commit_files(repo: Path, commit: str) -> tuple[tuple[str, str], ...]:
    out = _git(
        repo,
        "diff-tree",
        "-r",
        "-z",
        "--no-commit-id",
        "--name-status",
        "--root",
        commit,
    ).stdout
    parts = [p for p in out.split("\0") if p]
    return tuple(
        (parts[i + 1], _CHANGES.get(parts[i][:1], "changed"))
        for i in range(0, len(parts) - 1, 2)
    )


def _describe(repo: Path, commit: str) -> MirrorCommit:
    heading = _git(repo, "log", "-1", "--format=%s", commit).stdout.strip()
    return MirrorCommit(
        commit, heading, heading.startswith(HEADING + " "), _commit_files(repo, commit)
    )


def waiting_commits(repo: Path) -> list[MirrorCommit]:
    """The commits on the clone's ``main`` not on GitHub's ``main`` as last fetched, oldest first.

    Empty if the clone has never fetched GitHub's ``main``."""
    upstream = _upstream(repo)
    if upstream is None:
        return []
    shas = _git(repo, "rev-list", "--reverse", f"{upstream}..HEAD").stdout.split()
    return [_describe(repo, sha) for sha in shas]


def mirror_commits_waiting(repo: Path) -> bool:
    """Whether the clone holds mirror commits not yet on GitHub's ``main`` (as last fetched; nothing is fetched)."""
    try:
        return any(c.is_mirror for c in waiting_commits(repo))
    except MirrorError:
        return False


def published_elsewhere(repo: Path, log: EventLog) -> list[MirrorCommit]:
    """This server's mirror commits found on GitHub's ``main`` (as last fetched) that no ``mirror.updated`` names:
    published from another machine. A commit is this server's if its message names a crawl in this log, by
    event number and state digest. Oldest first."""
    upstream = _upstream(repo)
    if upstream is None:
        return []
    out = _git(
        repo,
        "log",
        f"-n{SEARCH_COMMITS}",
        "--format=%H%x1f%B%x1e",
        f"--grep=^{HEADING} ",
        upstream,
    ).stdout
    recorded = _recorded_commits(log)
    states = _crawl_states(log)
    found: list[MirrorCommit] = []
    for record in out.split("\x1e"):
        sha, _, body = record.strip().partition("\x1f")
        match = _FOOTER_RE.search(body)
        if (
            not sha
            or sha in recorded
            or match is None
            or states.get(int(match.group(1))) != match.group(2)
        ):
            continue
        found.append(_describe(repo, sha))
    return found[::-1]


def _record_published(
    log: EventLog, repo: Path, actor: str, commits: Sequence[MirrorCommit], *, published_from: str, pushed: Mapping[str, Any] | None,
    replayed: bool = False, kept: Sequence[str] = (), both: Sequence[Mapping[str, str]] = (), dropped: Sequence[str] = (),
) -> int:  # fmt: skip
    return log.append(
        "mirror.updated",
        actor,
        data={
            "repository": _remote_web_address(repo),
            "branch": BRANCH,
            "commits": [c.to_dict() for c in commits],
            "published_from": published_from,
            "pushed": dict(pushed) if pushed is not None else None,
            "replayed": replayed,
            "kept_github_version": list(kept),
            "both_versions": [dict(b) for b in both],
            "dropped": list(dropped),
        },
    ).seq


def _sync(repo: Path, log: EventLog, actor: str) -> list[MirrorCommit]:
    """Fetch GitHub's ``main``, record this server's commits published from elsewhere, and bring the clone's
    ``main`` up to GitHub's if it has nothing of its own waiting. Returns the commits recorded."""
    _fetch(repo)
    found = published_elsewhere(repo, log)
    if found:
        _record_published(
            log, repo, actor, found, published_from="elsewhere", pushed=None
        )
    upstream = _upstream(repo)
    head = _rev(repo, "HEAD")
    if upstream and head and head != upstream and _is_ancestor(repo, head, upstream):
        _git(repo, "merge", "--ff-only", "--quiet", upstream)
    return found


# ---------------------------------------------------------------------------
# Updating


@dataclass(frozen=True)
class MirrorFile:
    """A file the mirror update writes: its path in the mirror, ``new`` or ``changed``, and where it came from."""

    path: str
    change: str
    sha256: str
    url: str
    #: A note for the commit's message: the previous version, or the latest snapshot before a new one.
    note: str | None = None


@dataclass
class UpdateResult:
    """What `update_mirror` did."""

    #: The commit made (None if nothing changed).
    commit: str | None = None
    heading: str | None = None
    files: list[MirrorFile] = field(default_factory=list)
    #: Commits found published from elsewhere, and recorded as published.
    published_elsewhere: list[MirrorCommit] = field(default_factory=list)
    #: Why GitHub's ``main`` could not be fetched, if it could not (the update goes on without it).
    fetch_error: str | None = None
    #: Folders added to the clone's sparse checkout.
    sparse_added: list[str] = field(default_factory=list)


def _snapshot_time(path: str) -> Any:
    name = path.rsplit("/", 1)[-1]
    if not (name.startswith(SNAPSHOT_PREFIX) and name.endswith(".html")):
        return None
    return parse_instant(name[len(SNAPSHOT_PREFIX) : -len(".html")])


def latest_snapshot(tree: Mapping[str, _Entry]) -> str | None:
    """The latest Audit Center snapshot in ``auditCenters/``, by the time in its name."""
    dated = [
        (when, path)
        for path in tree
        if path.startswith(SNAPSHOT_DIR + "/")
        for when in [_snapshot_time(path)]
        if when is not None
    ]
    return max(dated)[1] if dated else None


def snapshot_name(fetched: str) -> str:
    """The snapshot of the Audit Center page fetched at ``fetched`` (UTC): named for that time in Colorado time."""
    moment = parse_instant(fetched)
    if moment is None:
        raise MirrorError(f"not a time: {fetched!r}")
    return f"{SNAPSHOT_DIR}/{SNAPSHOT_PREFIX}{to_colorado(moment).isoformat(timespec='seconds')}.html"


def is_audit_center_page(url: str) -> bool:
    return urlsplit(url).path.lower() == AUDIT_CENTER_PATH


def plan_update(
    workspace: Workspace, repo: Path, files: Mapping[str, FetchedFile]
) -> list[MirrorFile]:
    """The files `update_mirror` would write: each whose content differs from the version in the clone's ``HEAD``."""
    from corla_observer.timestamp import mirror_path

    tree = _tree(repo, "HEAD")
    fmt = _object_format(repo)
    planned: list[MirrorFile] = []

    def differs(path: str, item: FetchedFile) -> bool | None:
        """None if the clone has no file at ``path``, else whether its content differs."""
        entry = tree.get(path)
        if entry is None:
            return None
        if entry.blob == _blob_id(workspace, item.sha256, fmt):
            return False
        if item.normalized_sha256 is None:
            return True
        return (
            hashlib.sha256(
                normalize_html(_git_bytes(repo, "cat-file", "blob", entry.blob))
            ).hexdigest()
            != item.normalized_sha256
        )

    for url, item in sorted(files.items()):
        if not workspace.has(item.sha256):
            continue
        if is_audit_center_page(url):
            latest = latest_snapshot(tree)
            if latest is not None and differs(latest, item) is False:
                continue
            name = snapshot_name(item.fetched)
            if name in tree:
                continue
            note = (
                f"a snapshot of the Audit Center page; the latest before it is {latest}"
                if latest
                else "a snapshot of the Audit Center page"
            )
            planned.append(MirrorFile(name, "new", item.sha256, url, note))
            continue
        path = mirror_path(url)
        if path is None:
            continue
        found = differs(path, item)
        if found is None:
            planned.append(MirrorFile(path, "new", item.sha256, url))
        elif found:
            planned.append(MirrorFile(path, "changed", item.sha256, url))
    return planned


def _sparse_add(repo: Path, paths: Sequence[str]) -> list[str]:
    """Add to a sparse checkout the folder (an election's, such as ``2026/general``, or ``auditCenters``) of each
    path it does not cover yet; nothing if the checkout is not sparse."""
    if (
        _git(
            repo, "config", "--bool", "core.sparseCheckout", check=False
        ).stdout.strip()
        != "true"
    ):
        return []
    listed = [
        line.strip().strip("/")
        for line in _git(repo, "sparse-checkout", "list").stdout.splitlines()
        if line.strip()
    ]
    wanted: list[str] = []
    for path in paths:
        folder = "/".join(path.split("/")[:-1][:2])
        if (
            folder
            and not any(folder == d or folder.startswith(d + "/") for d in listed)
            and folder not in wanted
        ):
            wanted.append(folder)
    if wanted:
        _git(repo, "sparse-checkout", "add", *wanted)
    return wanted


def _previous_note(repo: Path, path: str, upstream: str | None) -> str | None:
    """The commit holding ``path``'s previous version: its ID and link once on GitHub's ``main``; for a commit still
    waiting (whose ID changes if it is replayed), its heading."""
    from corla_observer.timestamp import _last_change

    last = _last_change(repo, path)
    if last is None:
        return None
    if upstream is None or _is_ancestor(repo, last, upstream):
        link = _web_link(repo, last, path)
        return f"previous version: commit {last}" + (f", {link}" if link else "")
    return f'previous version: in the commit "{_git(repo, "log", "-1", "--format=%s", last).stdout.strip()}"'


def commit_message(
    heading: str, files: Sequence[MirrorFile], crawl: Crawl, software: str
) -> str:
    """The mirror update's commit message: its heading, each file new or changed with its note, and the crawl."""
    lines = [heading, ""]
    for f in files:
        lines.append(f"{f.change.capitalize()}: {f.path}")
        if f.note:
            lines.append(f"  {f.note}")
    lines += [
        "",
        f"Fetched by {software}; the crawl is event {crawl.seq} in the observers' event log, with state {crawl.state_sha256}.",
    ]
    return "\n".join(lines) + "\n"


def update_mirror(
    workspace: Workspace, log: EventLog, repo: Path, *, actor: str = "system"
) -> UpdateResult:
    """Write the Audit Center files whose content changed into the clone and commit them, once, for the latest crawl.

    GitHub's ``main`` is fetched first, to record this server's commits published from elsewhere and to bring
    the clone up to date when it has nothing waiting; if that fails, the update goes on (``fetch_error``).
    Nothing is pushed.

    Raises:
        MirrorError: if the clone is not usable, or writing or committing fails.
    """
    from corla_observer.timestamp import software_version

    check_actor(actor)
    repo = Path(repo)
    check_clone(repo)
    result = UpdateResult()
    try:
        result.published_elsewhere = _sync(repo, log, actor)
    except MirrorError as exc:
        result.fetch_error = str(exc)
    crawl = last_crawl(log)
    if crawl is None:
        return result
    planned = plan_update(workspace, repo, current_files(log))
    if not planned:
        return result
    paths = [f.path for f in planned]
    result.sparse_added = _sparse_add(repo, paths)
    for f in planned:
        dest = repo / f.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink() or dest.is_dir():
            raise MirrorError(f"{f.path} in the clone is not a plain file")
        workspace.copy_to(f.sha256, dest)
        os.chmod(dest, 0o644)
    _git(repo, "add", "--", *paths)
    staged = set(
        _git(repo, "diff", "--cached", "--name-only", "-z", "--", *paths).stdout.split(
            "\0"
        )
    ) - {""}
    upstream = _upstream(repo)
    files = [f if f.change == "new" else MirrorFile(f.path, f.change, f.sha256, f.url, _previous_note(repo, f.path, upstream))
             for f in planned if f.path in staged]  # fmt: skip
    if not files:
        return result
    heading = f"{HEADING} {colorado_time(crawl.started, seconds=False)}"
    message = commit_message(heading, files, crawl, software_version())
    _git(repo, "commit", "--quiet", "-m", message, "--", *[f.path for f in files])
    result.commit = _git(repo, "rev-parse", "HEAD").stdout.strip()
    result.heading = heading
    result.files = files
    return result


# ---------------------------------------------------------------------------
# Publishing


@dataclass
class PublishResult:
    """What `publish_mirror` did."""

    #: The mirror commits pushed, as pushed.
    commits: list[MirrorCommit] = field(default_factory=list)
    #: Commits found published from elsewhere, and recorded as published.
    published_elsewhere: list[MirrorCommit] = field(default_factory=list)
    replayed: bool = False
    kept_github_version: list[str] = field(default_factory=list)
    both_versions: list[dict[str, str]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    pushed: dict[str, Any] | None = None
    #: The ``mirror.updated`` event recording the push.
    event_seq: int | None = None
    notes: list[str] = field(default_factory=list)


def _push_target(repo: Path) -> dict[str, Any]:
    url = _git(repo, "remote", "get-url", "--push", REMOTE).stdout.strip()
    transport, clean = _transport(url)
    return {"remote": REMOTE, "transport": transport, "url": clean}


@dataclass
class _Replay:
    chain: list[tuple[MirrorCommit, str | None]] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    both: list[dict[str, str]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


def _blob(repo: Path, blob: str) -> bytes:
    return _git_bytes(repo, "cat-file", "blob", blob)


def _replay(repo: Path, commits: Sequence[MirrorCommit], base: str) -> _Replay:
    """Replay ``commits`` onto ``base``, in order, each still its own commit with its author and message.

    For each file a commit adds or changes: if ``base`` (or the commits replayed before it) already holds the
    same content (a page: but for Cloudflare's token), that version is kept and the file left out of the commit;
    if it holds other content than the commit's original parent had, the commit's version goes on top and its
    message names the commit holding the other one. A commit left with nothing is dropped."""
    done = _Replay()
    parent = base
    with tempfile.TemporaryDirectory(prefix="corla-mirror-") as scratch:
        env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        for c in commits:
            original_parent = _rev(repo, f"{c.commit}^")
            paths = [p for p, _ in c.files]
            ours = _tree(repo, c.commit, paths) if paths else {}
            theirs = _tree(repo, parent, paths) if paths else {}
            before = (
                _tree(repo, original_parent, paths) if paths and original_parent else {}
            )
            _git(repo, "read-tree", parent, env=env)
            applied = False
            notes: list[str] = []
            for path, change in c.files:
                if change == "removed":
                    if path in theirs:
                        _git(
                            repo, "update-index", "--force-remove", "--", path, env=env
                        )
                        applied = True
                    continue
                mine, other = ours[path], theirs.get(path)
                if other is not None and (
                    other.blob == mine.blob
                    or (
                        _is_page(path)
                        and _same_page(_blob(repo, other.blob), _blob(repo, mine.blob))
                    )
                ):
                    done.kept.append(path)
                    continue
                previous = before.get(path)
                if other is not None and (
                    previous is None or other.blob != previous.blob
                ):
                    holder = _git(
                        repo, "log", "-1", "--format=%H", parent, "--", path
                    ).stdout.strip()
                    done.both.append({"path": path, "github_commit": holder})
                    link = _web_link(repo, holder, path)
                    notes.append(f"GitHub's main already held another version of {path}, in commit {holder}"
                                 + (f" ({link})" if link else "") + "; this commit puts the version fetched here on top of it.")  # fmt: skip
                _git(
                    repo,
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"{mine.mode},{mine.blob},{path}",
                    env=env,
                )
                applied = True
            if not applied:
                done.dropped.append(c.heading)
                done.chain.append((c, None))
                continue
            tree = _git(repo, "write-tree", env=env).stdout.strip()
            message = (
                _git(repo, "log", "-1", "--format=%B", c.commit).stdout.rstrip("\n")
                + "\n"
            )
            if notes:
                message += "\n" + "\n".join(notes) + "\n"
            name, email, date = (
                _git(repo, "log", "-1", "--format=%an%x00%ae%x00%aI", c.commit)
                .stdout.strip()
                .split("\0")
            )
            author = {
                "GIT_AUTHOR_NAME": name,
                "GIT_AUTHOR_EMAIL": email,
                "GIT_AUTHOR_DATE": date,
            }
            parent = _git(
                repo,
                "commit-tree",
                tree,
                "-p",
                parent,
                "-F",
                "-",
                env=author,
                input=message,
            ).stdout.strip()
            done.chain.append((c, parent))
    return done


def _publishable(repo: Path) -> list[MirrorCommit]:
    """The clone's waiting commits, oldest first; refused if they include a merge."""
    upstream = _upstream(repo)
    if (
        upstream is not None
        and _git(repo, "rev-list", "--merges", f"{upstream}..HEAD").stdout.strip()
    ):
        raise MirrorError(
            "the clone's waiting commits include a merge; publish them by hand"
        )
    return waiting_commits(repo)


def publish_mirror(
    log: EventLog, repo: Path, *, actor: str = "system", tries: int = 3
) -> PublishResult:
    """Publish the clone's waiting mirror commits: fetch GitHub's ``main``, replay them on top in order, push,
    and only then record ``mirror.updated``.

    The clone's other waiting commits are replayed after them and stay waiting. A push refused because
    GitHub's ``main`` moved meanwhile is tried again, up to ``tries`` times. With nothing waiting, nothing is
    pushed or recorded (beyond commits found published from elsewhere).

    Raises:
        MirrorError: if the clone is not usable, GitHub's ``main`` cannot be fetched, or the push fails
            (the commits then stay waiting).
    """
    check_actor(actor)
    repo = Path(repo)
    check_clone(repo)
    result = PublishResult()
    head = _rev(repo, "HEAD")
    for attempt in range(tries):
        result.published_elsewhere += _sync(repo, log, actor)
        waiting = _publishable(repo)
        # The mirror's commits first, in the order of the crawls; any others after them, still waiting.
        ordered = [c for c in waiting if c.is_mirror] + [
            c for c in waiting if not c.is_mirror
        ]
        if not any(c.is_mirror for c in ordered):
            return result
        upstream = _upstream(repo)
        assert upstream is not None
        if _rev(repo, f"{ordered[0].commit}^") == upstream and ordered == waiting:
            replay = _Replay(chain=[(c, c.commit) for c in ordered])
        else:
            replay = _replay(repo, ordered, upstream)
        pushed = [
            (c, new) for c, new in replay.chain if c.is_mirror and new is not None
        ]
        tip = next(
            (new for _, new in reversed(replay.chain) if new is not None), upstream
        )
        if pushed:
            target = pushed[-1][1]
            assert target is not None
            push = _git(
                repo,
                "push",
                "--quiet",
                REMOTE,
                f"{target}:refs/heads/{BRANCH}",
                check=False,
            )
            if push.returncode != 0:
                detail = _short(push.stderr or push.stdout)
                if attempt + 1 < tries and re.search(
                    r"rejected|non-fast-forward|fetch first", detail
                ):
                    continue
                raise MirrorError(f"push failed: {detail}")
        result.replayed = replay.chain != [(c, c.commit) for c in ordered]
        result.kept_github_version, result.both_versions, result.dropped = (
            replay.kept,
            replay.both,
            replay.dropped,
        )
        result.commits = [_describe(repo, new) for _, new in pushed if new is not None]
        if tip != head:
            moved = _git(repo, "reset", "--quiet", "--keep", tip, check=False)
            if moved.returncode != 0:
                result.notes.append(
                    f"published, but the clone's main could not be moved to the commits as pushed: {_short(moved.stderr)}"
                )
        if pushed:
            result.pushed = _push_target(repo)
            result.event_seq = _record_published(
                log, repo, actor, result.commits, published_from="this server", pushed=result.pushed, replayed=result.replayed,
                kept=replay.kept, both=replay.both, dropped=replay.dropped,
            )  # fmt: skip
        return result
    raise MirrorError("push refused again and again: GitHub's main kept moving")


# ---------------------------------------------------------------------------
# After each crawl, and the coordinators' view


def record_error(
    log: EventLog, step: str, error: str | None, actor: str = "system"
) -> int | None:
    """Record ``mirror.error`` for ``step`` if ``error`` differs from the last one recorded for it (None: it works)."""
    last: str | None = None
    for event in log.iter_events():
        if event.kind == "mirror.error" and event.data.get("step") == step:
            value = event.data.get("error")
            last = value if isinstance(value, str) else None
    if error == last:
        return None
    return log.append(
        "mirror.error",
        actor,
        data={"step": step, "error": error[:_MAX_ERROR] if error else None},
    ).seq


def after_crawl(
    workspace: Workspace,
    log: EventLog,
    *,
    window: Any = None,
    moment: str | None = None,
    actor: str = "system",
) -> dict[str, Any] | None:
    """The scheduler's mirror step after a crawl: update the clone; in a window with ``publish_mirror`` (``window``,
    or else the schedule's window at ``moment``), publish. None if ``schedule.toml`` names no clone.

    Never raises: each failure is recorded (`record_error`) and returned in the summary's ``errors``. The summary
    has ``committed`` (the commit's heading, or null), ``files`` (the paths written), ``waiting`` (mirror commits
    waiting after this), ``published`` (commits pushed), ``published_elsewhere`` and ``errors``.
    """
    from corla_observer.schedule import ScheduleError, load_schedule

    try:
        schedule = load_schedule(workspace)
    except ScheduleError:
        return None
    repo = clone_path(workspace, schedule)
    if repo is None:
        return None
    summary: dict[str, Any] = {
        "committed": None,
        "files": [],
        "waiting": 0,
        "published": [],
        "published_elsewhere": [],
        "errors": [],
    }

    def failed(step: str, exc: Exception) -> None:
        text = _short(
            f"{type(exc).__name__}: {exc}"
            if not isinstance(exc, MirrorError)
            else str(exc)
        )
        summary["errors"].append(f"{step}: {text}")
        try:
            record_error(log, step, text, actor)
        except Exception:  # noqa: BLE001 - recording a failure must not fail the cycle
            pass

    try:
        updated = update_mirror(workspace, log, repo, actor=actor)
        summary["committed"] = updated.heading
        summary["files"] = [f.path for f in updated.files][:100]
        summary["published_elsewhere"] = [c.commit for c in updated.published_elsewhere]
        if updated.fetch_error:
            failed("fetch", MirrorError(updated.fetch_error))
        else:
            record_error(log, "fetch", None, actor)
        record_error(log, "update", None, actor)
    except Exception as exc:  # noqa: BLE001 - a mirror failure never fails the crawl
        failed("update", exc)
    if window is None and moment is not None and schedule is not None:
        window = schedule.window_at(moment)
    try:
        if getattr(window, "publish_mirror", False) and mirror_commits_waiting(repo):
            published = publish_mirror(log, repo, actor=actor)
            summary["published"] = [c.commit for c in published.commits]
            record_error(log, "publish", None, actor)
    except Exception as exc:  # noqa: BLE001
        failed("publish", exc)
    try:
        summary["waiting"] = sum(1 for c in waiting_commits(repo) if c.is_mirror)
    except Exception:  # noqa: BLE001
        pass
    return summary


def publish_now(
    workspace: Workspace, log: EventLog, actor: str
) -> tuple[PublishResult | None, str | None]:
    """A coordinator's Publish: publish the configured clone's waiting commits; the result, or the error (recorded)."""
    repo = configured_clone(workspace)
    if repo is None:
        return None, "schedule.toml names no mirror clone (mirror_clone)"
    try:
        result = publish_mirror(log, repo, actor=actor)
    except MirrorError as exc:
        record_error(log, "publish", str(exc), actor)
        return None, str(exc)
    record_error(log, "publish", None, actor)
    return result, None


def outside_links_seen(log: EventLog, limit: int = 50) -> list[dict[str, JSONValue]]:
    """The links outside the RLA directory recorded so far (``fetch.outside_link``), newest first, with ``server_time``."""
    found = [
        dict(e.data) | {"server_time": e.server_time}
        for e in log.iter_events()
        if e.kind == "fetch.outside_link"
    ]
    return found[::-1][:limit]


def mirror_status(workspace: Workspace, log: EventLog) -> dict[str, Any]:
    """What ``/coord`` shows of the mirror: whether a clone is named, the commits waiting (as last fetched; nothing
    is fetched here), the last publication, each step's current error, and the windows that publish at once."""
    from corla_observer.schedule import ScheduleError, load_schedule

    status: dict[str, Any] = {"configured": False, "clone": None, "problem": None, "waiting": [], "others_waiting": 0,
                              "last_published": None, "errors": [], "auto_windows": []}  # fmt: skip
    try:
        schedule = load_schedule(workspace)
    except ScheduleError as exc:
        status["problem"] = f"schedule.toml: {exc}"
        return status
    repo = clone_path(workspace, schedule)
    if repo is None or schedule is None:
        return status
    status["configured"] = True
    status["clone"] = str(repo)
    status["auto_windows"] = [w.name for w in schedule.windows if w.publish_mirror]
    try:
        check_clone(repo)
        waiting = waiting_commits(repo)
        status["waiting"] = [c for c in waiting if c.is_mirror]
        status["others_waiting"] = sum(1 for c in waiting if not c.is_mirror)
    except MirrorError as exc:
        status["problem"] = str(exc)
    errors: dict[str, tuple[str | None, str]] = {}
    for event in log.iter_events():
        if event.kind == "mirror.error" and event.data.get("step") in _STEPS:
            value = event.data.get("error")
            errors[str(event.data["step"])] = (
                value if isinstance(value, str) else None,
                event.server_time,
            )
        elif event.kind == "mirror.updated":
            commits = event.data.get("commits")
            status["last_published"] = {"time": event.server_time, "commits": len(commits) if isinstance(commits, list) else 0,
                                        "from": event.data.get("published_from"), "actor": event.actor}  # fmt: skip
    status["errors"] = [
        {"step": s, "error": e, "time": t} for s, (e, t) in errors.items() if e
    ]
    return status
