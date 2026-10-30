"""Fast tests for corla_observer.rehearse, on a tiny synthetic mirror built as a git repository in the test.

The real rehearsal on the July 2026 primary is the slow acceptance test
tests/acceptance/test_rehearsal_2026_primary.py.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
from typer.testing import CliRunner

from corla_observer.cli import app
from corla_observer.events import EventLog, verify_jsonl
from corla_observer.rehearse import (
    AUDIT_CENTER_RLA_URL,
    MIRROR_REPOSITORY,
    RehearsalError,
    audit_center_url,
    mirror_path,
    mirror_paths,
    run_all,
    stage_fetch,
    stage_uploads,
)
from corla_observer.sampling import build_domain_from_manifests, select_ballots
from corla_observer.workspace import Workspace
from synthetic import (
    ADAMS_MANIFEST,
    CONTEST_IDS,
    ELECTION,
    ballots,
    contests_list,
    election_files,
    make_cvr,
    published_selections,
)

SEED = "12345678901234567890"
COUNTY_FILES = {"Adams": {"redacted_cvr": "Adams_CVR.csv", "manifest": "Adams_manifest.csv"}}
OVERRIDES = 'format = 1\nelection = "2026/general"\n'


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.org", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(repo: Path, files: Mapping[str, bytes], message: str) -> str:
    for path, data in files.items():
        target = repo / ELECTION / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def expected_draws(count_statewide: int = 9, count_adams: int = 4) -> dict[str, list[str]]:
    """CORLA's selections for the synthetic election, computed independently of the data map."""
    from synthetic import BOULDER_MANIFEST, WELD_MANIFEST

    from corla_observer.datamap import load_manifest_bytes

    adams, boulder, weld = (load_manifest_bytes(m) for m in (ADAMS_MANIFEST, BOULDER_MANIFEST, WELD_MANIFEST))
    statewide = build_domain_from_manifests([adams, boulder, weld], {"adams": 1, "boulder": 7, "weld": 62})
    return {
        "Governor - DEM": [d.imprinted_id for d in select_ballots(SEED, statewide, count_statewide)][::-1],
        "Adams County Clerk - REP": [d.imprinted_id for d in select_ballots(SEED, build_domain_from_manifests([adams]), count_adams)],
    }


@pytest.fixture
def mirror(tmp_path: Path) -> tuple[Path, list[str]]:
    """A mirror with three commits: before the roll (Adams's manifest wrong), corrected, final reports."""
    repo = tmp_path / "mirror-checkout"
    repo.mkdir()
    git(repo, "init", "-q")
    wrong_adams = b"County,Tabulator,Batch,# of Ballots\r\nAdams,101,1,30\r\nAdams,101,9,20\r\n"
    observer = {
        "observerfiles/Adams_CVR.csv": make_cvr(ballots(12, "Mayor", "Council")),
        "observerfiles/Adams_manifest.csv": ADAMS_MANIFEST,
        "SHA256SUMS.txt": b"0000  files/Adams.csv\n",
    }
    first = commit(repo, election_files(files_Adams_csv=wrong_adams) | observer, "before the roll")
    second = commit(repo, election_files(), "Adams corrected")
    finals = published_selections(expected_draws()) | {
        "finalReports/ContestsListRound1.csv": contests_list(),
        "finalReports/ContestIDsByCounty.csv": CONTEST_IDS,
    }
    third = commit(repo, finals, "final reports")
    return repo, [first, second, third]


def test_urls_and_paths() -> None:
    url = audit_center_url("2026/primary/files/La Plata.csv")
    assert url == AUDIT_CENTER_RLA_URL + "2026/primary/files/La%20Plata.csv"
    assert mirror_path(url) == "2026/primary/files/La Plata.csv"
    assert mirror_path("https://example.org/x") is None


def test_fetch_from_the_mirror_is_marked_simulated(tmp_path: Path, mirror: tuple[Path, list[str]]) -> None:
    repo, commits = mirror
    assert not any("observerfiles" in p or "SHA256SUMS" in p for p in mirror_paths(repo, commits[0], ELECTION))
    ws = Workspace.open(tmp_path / "ws")
    with EventLog.open(ws.root) as log:
        first = stage_fetch(ws, log, repo, commits[0][:10], election=ELECTION)
        second = stage_fetch(ws, log, repo, commits[1], election=ELECTION)
        events = list(log.iter_events())
    assert first.count("new") == 5 and second.count("changed") == 1
    assert [a["reason"] for a in second.alerts] == ["changed"]
    fetch_events = [e for e in events if e.kind.startswith("fetch.")]
    markers = [e.data["simulated"] for e in fetch_events]
    assert all(isinstance(m, dict) and m["repository"] == MIRROR_REPOSITORY for m in markers)
    assert {m["commit"] for m in markers if isinstance(m, dict)} == set(commits[:2])
    files = [e for e in events if e.kind == "fetch.file"]
    assert all(str(e.data["url"]).startswith(AUDIT_CENTER_RLA_URL + ELECTION) for e in files)
    assert all(str(e.data["final_url"]).startswith("https://raw.githubusercontent.com/nealmcb/auditcenter/") for e in files)
    # The mirror's local path is never recorded.
    assert str(repo) not in (ws.root / "events.jsonl").read_text()
    with pytest.raises(RehearsalError):
        stage_fetch(ws, EventLog.open(ws.root), repo, "no-such-commit", election=ELECTION)


def test_run_all_reproduces_the_selections(tmp_path: Path, mirror: tuple[Path, list[str]]) -> None:
    repo, commits = mirror
    workspace = tmp_path / "rehearsal"
    report = run_all(
        workspace,
        repo,
        commits=commits,
        counties=["Adams"],
        election=ELECTION,
        county_files=COUNTY_FILES,
        uploads_commit=commits[0],
        overrides_text=OVERRIDES,
        seed=SEED,
        seed_source="test",
    )
    assert report.uploads is not None
    assert all(r.accepted for r in report.uploads.results)
    assert report.uploads.audit_boards == {"Adams": (1, True)}
    assert report.overrides is not None and report.overrides.applied
    assert report.prediction is not None
    assert [(c.name, c.status) for c in report.comparisons] == [
        ("Governor - DEM", "match"),
        ("Adams County Clerk - REP", "match"),
    ]
    assert verify_jsonl(workspace / "events.jsonl").ok
    kinds = [e.kind for e in EventLog.open(workspace, read_only=True).iter_events()]
    for kind in ("fetch.alert", "redaction.checked", "audit_boards.recorded", "map.overrides_applied",
                 "seed.entered", "prediction.made", "prediction.compared"):  # fmt: skip
        assert kind in kinds
    with pytest.raises(RehearsalError, match="not empty"):
        run_all(workspace, repo, commits=commits)


def test_stages_rerun_on_their_own(tmp_path: Path, mirror: tuple[Path, list[str]]) -> None:
    repo, commits = mirror
    runner = CliRunner()
    root = str(tmp_path / "ws")
    listed = runner.invoke(app, ["rehearse", "commits"])
    assert listed.exit_code == 0 and "a4a2b935445406da32f6ce01ba9bc317990c99fa" in listed.output
    for c in commits[:2]:
        fetched = runner.invoke(
            app, ["rehearse", "fetch", "--workspace", root, "--mirror", str(repo), "--commit", c, "--election", ELECTION]
        )
        assert fetched.exit_code == 0, fetched.output
    seeded = runner.invoke(app, ["rehearse", "seed", "--workspace", root, "--seed", SEED, "--source", "test"])
    assert seeded.exit_code == 0, seeded.output
    predicted = runner.invoke(app, ["rehearse", "predict", "--workspace", root, "--county", "Adams"])
    assert predicted.exit_code == 0, predicted.output
    assert "(ESTIMATED count)" in predicted.output
    early = runner.invoke(app, ["rehearse", "compare", "--workspace", root])
    assert early.exit_code == 1 and "not in the workspace" in early.output
    final = runner.invoke(
        app, ["rehearse", "fetch", "--workspace", root, "--mirror", str(repo), "--commit", commits[2], "--election", ELECTION]
    )
    assert final.exit_code == 0, final.output
    compared = runner.invoke(app, ["rehearse", "compare", "--workspace", root])
    # Estimated counts (from the target list's margins) can differ from CORLA's; the draws are a prefix.
    assert compared.exit_code == 0, compared.output
    assert "MISMATCH" not in compared.output
    repredicted = runner.invoke(app, ["rehearse", "predict", "--workspace", root, "--county", "Adams"])
    assert "(ESTIMATED count)" not in repredicted.output
    compared = runner.invoke(app, ["rehearse", "compare", "--workspace", root])
    assert compared.exit_code == 0 and compared.output.count("MATCH ") == 2


def test_uploads_go_through_the_redaction_check(tmp_path: Path, mirror: tuple[Path, list[str]]) -> None:
    repo, commits = mirror
    rare = make_cvr(ballots(12, "Mayor", "Council") + ballots(2, "Mayor", "Fire", precinct="9"))
    commit(repo, {"observerfiles/Adams_CVR.csv": rare}, "a CVR with a rare style")
    runner = CliRunner()
    root = str(tmp_path / "ws")
    result = stage_uploads(
        Workspace.open(root),
        EventLog.open(root),
        repo,
        commit="HEAD",
        counties=["Adams"],
        audit_boards={"Adams": 3},
        election=ELECTION,
        county_files=COUNTY_FILES,
    )
    cvr = next(r for r in result.results if r.kind == "redacted_cvr")
    assert not cvr.accepted and cvr.reason == "redaction_check_failed"
    assert result.audit_boards == {"Adams": (3, False)}
    status = runner.invoke(app, ["status", "--workspace", root])
    assert "audit boards: 3" in status.output and "PLACEHOLDER" not in status.output
