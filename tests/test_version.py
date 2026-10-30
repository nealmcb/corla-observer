"""The running software's identity (`corla_observer.version`): from git, from a version file, or unknown."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from corla_observer import __version__
from corla_observer.version import SoftwareIdentity, from_file, from_git, identify

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def test_version() -> None:
    assert __version__ == "0.1.0"


def _repo(tmp_path: Path) -> Path:
    """A git checkout with a package at src/corla_observer, one commit; the package's directory."""
    package = tmp_path / "src" / "corla_observer"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("x = 1\n")
    (tmp_path / "README").write_text("readme\n")

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=Ann Example", "-c", "user.email=ann@example.org", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "First line", "-m", "The body.")
    return package


@needs_git
def test_from_git_and_the_dirty_flag(tmp_path: Path) -> None:
    package = _repo(tmp_path)
    found = from_git(package)
    assert found is not None and found.source == "git" and not found.dirty and found.commit and len(found.commit) == 40
    assert found.label == f"{__version__} ({found.commit[:12]})" and found.software == f"corla-observer {found.label}"
    assert found.log["subject"] == "First line" and found.log["body"] == "The body." and "author" not in found.log
    assert "ann@example.org" not in repr(found) and "Ann Example" not in repr(found)
    assert found.log["hash"] == found.commit and found.commit_url == f"https://github.com/nealmcb/corla-observer/commit/{found.commit}"
    # An untracked file outside src/ (a workspace, a scratch file) does not make it dirty; one under src/ does, as does a changed tracked file.
    (tmp_path / "notes.txt").write_text("scratch\n")
    assert from_git(package).dirty is False  # type: ignore[union-attr]
    (package / "new.py").write_text("y = 2\n")
    assert from_git(package).dirty is True  # type: ignore[union-attr]
    (package / "new.py").unlink()
    (tmp_path / "README").write_text("changed\n")
    dirty = from_git(package)
    assert dirty is not None and dirty.dirty and dirty.label.endswith("+dirty)")


@needs_git
def test_fallbacks(tmp_path: Path) -> None:
    # Not a checkout (or a checkout whose src/corla_observer is not this package): no git identity.
    plain = tmp_path / "site-packages" / "corla_observer"
    plain.mkdir(parents=True)
    assert from_git(plain) is None
    assert identify(plain) == SoftwareIdentity(__version__)
    assert identify(plain).label == f"{__version__} (commit unknown)" and identify(plain).commit_url is None
    # A version file written at build time.
    (plain / "version.json").write_text(json.dumps({"commit": "ab" * 20, "dirty": False, "log": {"subject": "Built"}}))
    built = identify(plain)
    assert built.source == "file" and built.commit == "ab" * 20 and built.label == f"{__version__} ({'ab' * 6})" and built.log == {"subject": "Built"}
    (plain / "version.json").write_text("not json")
    assert from_file(plain) is None and identify(plain).commit is None
    # Without git at all.
    assert from_git(_repo(tmp_path / "repo"), git=str(tmp_path / "no-such-git")) is None
