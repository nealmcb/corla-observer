"""The running software's identity: its package version and the git commit it runs from.

Deployments are git checkouts, so the commit says exactly which code ran.
`current` determines it once per process (`identify` does the work):

- the package version (``corla_observer.__version__``);
- if the package runs from a git checkout (the checkout's ``src/corla_observer``
  is this package), the commit (``git rev-parse HEAD``), whether the tree
  is dirty (a tracked file changed, or a file not yet tracked under
  ``src/``), and the commit's ``git log -1`` entry (hash, date,
  subject and body);
- otherwise, a ``version.json`` written beside this module at build time
  (``{"commit": ..., "dirty": ..., "log": {...}}``), if there is one;
- otherwise the commit is unknown.

`SoftwareIdentity.software` is the string recorded wherever the software
is named (the events' ``software`` field, the evidence SUMS header, the
printout's provenance sheet): ``corla-observer 0.1.0 (f855b1a1c2d3)``, with
``+dirty`` after the commit when the tree is dirty, or ``(commit unknown)``.
The web app's footer links it to ``/version``, which shows the full commit,
the dirty flag, the log entry and a link to the commit on GitHub (`REPOSITORY`).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from corla_observer import __version__

PACKAGE_DIR = Path(__file__).resolve().parent
#: Where the published source is; a commit's page is ``REPOSITORY + "/commit/" + hash``.
REPOSITORY = "https://github.com/nealmcb/corla-observer"
#: Written at build time where the package is not run from a checkout.
VERSION_FILE = "version.json"
_GIT_TIMEOUT = 10
#: ``git log -1``'s fields, separated by NUL characters.
#: The parts of the commit's log entry shown: no author name or email, since the version page is public.
LOG_FIELDS = ("hash", "date", "subject", "body")
_LOG_FORMAT = "%H%x00%aI%x00%s%x00%b"


@dataclass(frozen=True)
class SoftwareIdentity:
    version: str
    commit: str | None = None
    dirty: bool = False
    #: The commit's ``git log -1`` entry: ``hash``, ``date``, ``subject``, ``body`` (never the author).
    log: dict[str, str] = field(default_factory=dict)
    #: Where it was learnt: "git", "file" or "unknown".
    source: str = "unknown"

    @property
    def short(self) -> str | None:
        return self.commit[:12] if self.commit else None

    @property
    def label(self) -> str:
        """``0.1.0 (f855b1a1c2d3)``, ``0.1.0 (f855b1a1c2d3+dirty)`` or ``0.1.0 (commit unknown)``."""
        if not self.commit:
            return f"{self.version} (commit unknown)"
        return f"{self.version} ({self.short}{'+dirty' if self.dirty else ''})"

    @property
    def software(self) -> str:
        """The name recorded for this software: ``corla-observer`` and `label`."""
        return f"corla-observer {self.label}"

    @property
    def commit_url(self) -> str | None:
        return f"{REPOSITORY}/commit/{self.commit}" if self.commit else None

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "commit": self.commit, "dirty": self.dirty, "software": self.software, "log": dict(self.log), "source": self.source}


def _git(git: str, cwd: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run([git, "-C", str(cwd), *args], capture_output=True, text=True, timeout=_GIT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def from_git(package_dir: Path = PACKAGE_DIR, git: str | None = None) -> SoftwareIdentity | None:
    """The identity from the git checkout the package runs from; None if it does not run from one."""
    git = git or shutil.which("git")
    if git is None:
        return None
    top = _git(git, package_dir, "rev-parse", "--show-toplevel")
    if top is None or top.returncode != 0 or (Path(top.stdout.strip()).resolve() / "src" / package_dir.name) != package_dir.resolve():
        return None
    root = Path(top.stdout.strip())
    head = _git(git, root, "rev-parse", "HEAD")
    if head is None or head.returncode != 0:
        return None
    commit = head.stdout.strip()
    tracked = _git(git, root, "status", "--porcelain", "--untracked-files=no")
    untracked = _git(git, root, "status", "--porcelain", "--untracked-files=normal", "--", "src")
    dirty = any(p is None or p.returncode != 0 or p.stdout.strip() for p in (tracked, untracked))
    log: dict[str, str] = {}
    entry = _git(git, root, "log", "-1", f"--format={_LOG_FORMAT}", commit)
    if entry is not None and entry.returncode == 0:
        parts = entry.stdout.rstrip("\n").split("\x00")
        if len(parts) == 4:
            log = dict(zip(LOG_FIELDS, (p.strip() for p in parts), strict=True))
    return SoftwareIdentity(__version__, commit, dirty, log, "git")


def from_file(package_dir: Path = PACKAGE_DIR) -> SoftwareIdentity | None:
    """The identity from a ``version.json`` written at build time; None if there is none or it is unusable."""
    try:
        data = json.loads((package_dir / VERSION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    commit = data.get("commit") if isinstance(data, dict) else None
    if not isinstance(commit, str) or not commit:
        return None
    log = data.get("log")
    return SoftwareIdentity(__version__, commit, bool(data.get("dirty")), {str(k): str(v) for k, v in log.items() if str(k) in LOG_FIELDS} if isinstance(log, dict) else {}, "file")


def identify(package_dir: Path = PACKAGE_DIR, git: str | None = None) -> SoftwareIdentity:
    """This software's identity: from git, else from the version file, else with the commit unknown."""
    return from_git(package_dir, git) or from_file(package_dir) or SoftwareIdentity(__version__)


@cache
def current() -> SoftwareIdentity:
    """The running software's identity, determined once."""
    return identify()


def software() -> str:
    """The running software's name, as recorded: ``corla-observer 0.1.0 (f855b1a1c2d3)``."""
    return current().software
