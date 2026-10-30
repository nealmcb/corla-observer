"""Acceptance test for upload detection (corla_observer.intake) on the 2026 primary's real county files.

Reads the observers' copies of La Plata's, Morgan's and Weld's files from
a checkout of the public audit center mirror (``CORLA_OBSERVER_MIRROR``,
https://github.com/nealmcb/auditcenter), and checks that each file's kind
and county are detected from its content alone. Skipped without the
mirror.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from corla_observer.intake import detect

pytestmark = pytest.mark.slow

FINAL_COMMIT = "ba18ba6"  # 2026-07-16: CORLA's final reports, the manifests and the observers' files
KNOWN = ["La Plata", "Morgan", "Weld"]
EXPECTED = {
    "LaPlata_BallotManifest.csv": ("manifest", "La Plata"),
    "LaPlata_RedactedTest_NoContestBalance_CVR_Export_20260709081424.csv": ("redacted_cvr", "La Plata"),
    "Morgan_BallotManifest.csv": ("manifest", "Morgan"),
    "Morgan_CVR_Export_20260709092315_Redacted.csv": ("redacted_cvr", "Morgan"),
    "Weld_BallotManifest.csv": ("manifest", "Weld"),
    "Weld_CVR_Export_20260709140755.csv": ("redacted_cvr", "Weld"),
    "SOS_ENR_detail_20260710.xlsx": ("results", "state"),
    # CORLA's own audit reports are not a kind coordinators upload: the coordinator is asked, with the county they name first.
    "LaPlata_AuditReport_Round1_20260713.xlsx": (None, None),
    "Morgan_AuditReport_Round1_20260713.xlsx": (None, None),
}


@pytest.fixture(scope="module")
def mirror() -> Path:
    path = os.environ.get("CORLA_OBSERVER_MIRROR")
    if not path or not (Path(path) / ".git").exists():
        pytest.skip("set CORLA_OBSERVER_MIRROR to a git checkout of https://github.com/nealmcb/auditcenter")
    if subprocess.run(["git", "-C", path, "cat-file", "-e", f"{FINAL_COMMIT}^{{commit}}"], capture_output=True).returncode:
        pytest.skip(f"the mirror checkout lacks commit {FINAL_COMMIT}")
    return Path(path)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_detected_from_content(mirror: Path, name: str, tmp_path: Path) -> None:
    data = subprocess.run(
        ["git", "-C", str(mirror), "show", f"{FINAL_COMMIT}:2026/primary/observerfiles/{name}"], check=True, capture_output=True
    ).stdout
    path = tmp_path / "upload"  # no telling name: the content decides
    path.write_bytes(data)
    found = detect(path, "upload", KNOWN)
    assert (found.kind, found.county) == EXPECTED[name], found
    if found.kind is None:
        assert found.counties[0] == name.split("_")[0].replace("LaPlata", "La Plata")
