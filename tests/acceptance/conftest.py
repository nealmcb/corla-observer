"""Fixtures for the acceptance tests: pinned public files from the audit center mirror."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mirror import MirrorUnavailable, load_mirror_files, materialize

# The synthetic-file helpers in tests/, for acceptance tests run on their own.
_TESTS = str(Path(__file__).resolve().parent.parent)
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)


@pytest.fixture(scope="session")
def mirror_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory holding every file in mirror_files.json at its mirror path, verified.

    Skips the requesting tests if the files can't be obtained (see mirror.py).
    """
    try:
        return materialize(load_mirror_files(), tmp_path_factory.mktemp("auditcenter"))
    except MirrorUnavailable as exc:
        pytest.skip(
            "public audit center files unavailable; set CORLA_OBSERVER_MIRROR to a clone of "
            f"https://github.com/nealmcb/auditcenter or allow downloads ({exc})"
        )
