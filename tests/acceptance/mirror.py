"""Fetch pinned public files from the audit center mirror for the acceptance tests.

The acceptance tests use real public files that Colorado's Secretary of State
published in the state's Audit Center. They are not committed here. Instead,
`mirror_files.json` lists each one by its path in the public mirror,
https://github.com/nealmcb/auditcenter (which keeps the audit center's
``<year>/<election>/files/`` and ``<year>/<election>/finalReports/`` layout),
the mirror commit to read it at, and its SHA-256.

`fetch` obtains a listed file's bytes by, in order:

1. a local clone of the mirror, if the environment variable
   ``CORLA_OBSERVER_MIRROR`` names one: ``git show <commit>:<path>``;
2. a download cache, ``$CORLA_OBSERVER_CACHE`` if set, otherwise
   ``corla-observer/mirror`` under the user cache directory
   (``$XDG_CACHE_HOME``, default ``~/.cache``);
3. a download of ``https://raw.githubusercontent.com/nealmcb/auditcenter/<commit>/<path>``
   into that cache.

Every file is checked against its SHA-256 before use, whichever way it was
obtained. A mismatch from the clone or the download raises `MirrorMismatch`;
a mismatched cached copy is discarded and downloaded again. If no source
works, `MirrorUnavailable` is raised, which the test fixtures turn into a
skip.

To pin more files, run this module against a clone, a commit and paths in
the mirror; it prints entries for `mirror_files.json`::

    python tests/acceptance/mirror.py CLONE COMMIT PATH [PATH ...]

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

MIRROR_FILES_JSON = Path(__file__).with_name("mirror_files.json")
RAW_URL = "https://raw.githubusercontent.com/nealmcb/auditcenter/{commit}/{path}"
MIRROR_ENV = "CORLA_OBSERVER_MIRROR"
CACHE_ENV = "CORLA_OBSERVER_CACHE"
DOWNLOAD_TIMEOUT_SECONDS = 60


class MirrorUnavailable(Exception):
    """No source could supply a pinned file."""


class MirrorMismatch(Exception):
    """A source supplied content whose SHA-256 is not the pinned one."""


@dataclass(frozen=True)
class MirrorFile:
    """One pinned file: its path in the mirror, the commit to read it at, and its SHA-256."""

    path: str
    commit: str
    sha256: str

    @property
    def url(self) -> str:
        """The file's raw download URL at its pinned commit."""
        return RAW_URL.format(commit=self.commit, path=self.path)


def load_mirror_files(json_path: Path = MIRROR_FILES_JSON) -> list[MirrorFile]:
    """Read the pinned file list."""
    data = json.loads(json_path.read_text(encoding="utf-8"))
    return [MirrorFile(e["path"], e["commit"], e["sha256"]) for e in data["files"]]


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def default_cache_dir() -> Path:
    """``$CORLA_OBSERVER_CACHE``, else ``corla-observer/mirror`` in the user cache directory."""
    override = os.environ.get(CACHE_ENV)
    if override:
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "corla-observer" / "mirror"


def _check(entry: MirrorFile, content: bytes, source: str) -> bytes:
    actual = sha256_hex(content)
    if actual != entry.sha256:
        raise MirrorMismatch(
            f"{entry.path} at {entry.commit} from {source}: SHA-256 {actual}, "
            f"expected {entry.sha256}"
        )
    return content


def read_from_clone(clone: Path, entry: MirrorFile) -> bytes:
    """Return the file's bytes at its pinned commit in a local clone of the mirror.

    Raises `MirrorUnavailable` if git can't produce it (for example, the clone
    doesn't have that commit yet), and `MirrorMismatch` on a wrong hash.
    """
    result = subprocess.run(
        ["git", "-C", str(clone), "show", f"{entry.commit}:{entry.path}"],
        capture_output=True,
    )
    if result.returncode != 0:
        raise MirrorUnavailable(
            f"git show {entry.commit}:{entry.path} failed in {clone}: "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    return _check(entry, result.stdout, f"clone {clone}")


def download(url: str) -> bytes:
    """Fetch a URL's body, raising `MirrorUnavailable` on any network or HTTP error."""
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            body: bytes = response.read()
            return body
    except (urllib.error.URLError, OSError) as exc:
        raise MirrorUnavailable(f"could not download {url}: {exc}") from exc


def read_via_cache(
    cache_dir: Path, entry: MirrorFile, downloader: Callable[[str], bytes] = download
) -> bytes:
    """Return the file's bytes from the cache, downloading it there first if needed.

    The cache holds each file at ``<cache_dir>/<commit>/<path>``. A cached
    copy with the wrong hash is discarded and downloaded again; a download
    with the wrong hash raises `MirrorMismatch` and is not cached.
    """
    cached = cache_dir / entry.commit / entry.path
    if cached.is_file():
        content = cached.read_bytes()
        if sha256_hex(content) == entry.sha256:
            return content
        cached.unlink()
    content = _check(entry, downloader(entry.url), entry.url)
    cached.parent.mkdir(parents=True, exist_ok=True)
    partial = cached.with_name(cached.name + ".partial")
    partial.write_bytes(content)
    partial.replace(cached)
    return content


def fetch(
    entry: MirrorFile,
    clone: Path | None = None,
    cache_dir: Path | None = None,
    downloader: Callable[[str], bytes] = download,
) -> bytes:
    """Return a pinned file's verified bytes: from `clone` if given, else via the cache.

    `clone` and `cache_dir` default to ``$CORLA_OBSERVER_MIRROR`` and
    `default_cache_dir()`. If the clone can't supply the file, the cache and
    download are tried next. Raises `MirrorUnavailable` if nothing works,
    with every source's reason, and `MirrorMismatch` on a wrong hash.
    """
    if clone is None and os.environ.get(MIRROR_ENV):
        clone = Path(os.environ[MIRROR_ENV])
    reasons: list[str] = []
    if clone is not None:
        try:
            return read_from_clone(clone, entry)
        except MirrorUnavailable as exc:
            reasons.append(str(exc))
    try:
        return read_via_cache(cache_dir or default_cache_dir(), entry, downloader)
    except MirrorUnavailable as exc:
        reasons.append(str(exc))
    raise MirrorUnavailable("; ".join(reasons))


def materialize(entries: Sequence[MirrorFile], dest: Path) -> Path:
    """Write each pinned file to ``dest/<path>``, verified, and return `dest`.

    Raises `MirrorUnavailable` or `MirrorMismatch` as `fetch` does.
    """
    for entry in entries:
        target = dest / entry.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(fetch(entry))
    return dest


def pin(clone: Path, commit: str, paths: Sequence[str]) -> list[dict[str, str]]:
    """Return `mirror_files.json` entries for `paths` at `commit` in a local clone."""
    entries = []
    for path in paths:
        content = subprocess.run(
            ["git", "-C", str(clone), "show", f"{commit}:{path}"],
            check=True,
            capture_output=True,
        ).stdout
        entries.append({"path": path, "commit": commit, "sha256": sha256_hex(content)})
    return entries


def main(argv: Sequence[str]) -> int:
    if len(argv) < 4:
        print(__doc__, file=sys.stderr)
        return 2
    print(json.dumps(pin(Path(argv[1]), argv[2], argv[3:]), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
