"""Fast tests for corla_observer.walkthrough: a general election's timeline replayed on a synthetic mirror.

The synthetic election (tests/synthetic.py) is replayed in the order of
the 2026 general election's calendar: the target list, the results, the
manifests, the county's own files, the roll and seed, then CORLA's
reports. At each step the estimates, the prediction and the extract's
certain and possible ballots are checked. The same replay of the real July
2026 primary is `walkthrough init --county Weld` on the public mirror
(docs/REHEARSAL.md).
"""

# ruff: noqa: F811

from __future__ import annotations

from pathlib import Path

import pytest

from corla_observer.estimate import last_estimate
from corla_observer.events import EventLog, verify_jsonl
from corla_observer.extract import latest_extract
from corla_observer.walkthrough import (
    TIMELINES,
    Replay,
    Step,
    WalkthroughError,
    init_walkthrough,
    next_step,
    state,
)
from corla_observer.workspace import Workspace
from synthetic import CONTEST_IDS, ballots, clarity_detail, contests_list, election_files, make_cvr, published_selections
from test_rehearse import SEED, commit, expected_draws, git

RESULTS = clarity_detail(
    {
        "Governor - Democratic Party (Vote For 1)": {"A": {"Adams": 30, "Boulder": 30, "Weld": 30}, "B": {"Adams": 20, "Boulder": 25, "Weld": 30}},
        "Adams County Clerk - Republican Party (Vote For 1)": {"C": {"Adams": 32}, "D": {"Adams": 12}},
    },
    {"Adams": 50, "Boulder": 50, "Weld": 65},
    zipped=False,
)


@pytest.fixture
def replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "mirror"
    repo.mkdir()
    git(repo, "init", "-q")
    files = election_files()
    targets = commit(repo, {"files/targetContests.csv": files["files/targetContests.csv"], "riskLimit.html": files["riskLimit.html"]}, "targets")
    results = commit(repo, {"observerfiles/detail.xml": RESULTS}, "results")
    manifests = commit(repo, files, "manifests")
    commit(repo, {"observerfiles/Adams_CVR.csv": make_cvr(ballots(50, "Mayor", "Council")), "observerfiles/Adams_manifest.csv": files["files/Adams.csv"]}, "Adams's files")
    finals = commit(
        repo,
        published_selections(expected_draws()) | {"finalReports/ContestsListRound1.csv": contests_list(), "finalReports/ContestIDsByCounty.csv": CONTEST_IDS},
        "final reports",
    )
    uploads = git(repo, "rev-parse", "HEAD~1")
    steps = [
        Step("2026-11-06T17:00:00-07:00", "target list", "fetch", targets[:7]),
        Step("2026-11-07T10:00:00-07:00", "results", "results", results[:7], "2026/general/observerfiles/detail.xml"),
        Step("2026-11-13T15:00:00-07:00", "manifests", "fetch", manifests[:7]),
        Step("2026-11-14T09:00:00-07:00", "Adams's files", "uploads"),
        Step("2026-11-16T10:10:00-07:00", "roll", "seed"),
        Step("2026-11-17T16:00:00-07:00", "CORLA's reports", "fetch", finals[:7]),
    ]
    monkeypatch.setitem(
        TIMELINES,
        "2026/general",
        Replay(
            lambda county: steps, SEED, "test", "2026-11-16T10:10:00-07:00", "test",
            {"Adams": {"redacted_cvr": "Adams_CVR.csv", "manifest": "Adams_manifest.csv"}},
            {"Adams": ("2026-11-14T09:00:00-07:00", "test")}, 'format = 1\nelection = "2026/general"\n', uploads,
        ),
    )  # fmt: skip
    return repo


def test_the_general_timeline_step_by_step(tmp_path: Path, replay: Path) -> None:
    root = tmp_path / "walk"
    ws = init_walkthrough(root, election="2026/general", county="Adams", mirror=replay)
    with EventLog.open(root) as log:
        walk = state(ws, log)
        assert walk.done == 0 and walk.now is None and walk.next is not None and walk.next.title == "target list"
        next_step(ws, log)  # the target list: estimates from its margins and denominators
        first = last_estimate(log)
        assert first is not None
        next_step(ws, log)  # the results: the margins from them
        second = last_estimate(log)
        assert second is not None and second[0] > first[0]
        contests = second[1]["contests"]
        assert isinstance(contests, list)
        sources = {str(c["name"]): str(c["margin_source"]) for c in contests if isinstance(c, dict)}
        assert "state's results" in sources["Governor - Democratic Party"]
        next_step(ws, log)  # manifests
        next_step(ws, log)  # Adams's own files
        _, notes = next_step(ws, log)  # the roll: the seed, the prediction and Adams's extract
        assert any("predicted order" in n for n in notes), notes
        found = latest_extract(ws, log, "Adams")
        assert found is not None
        extract = found[0]
        assert extract["order"] == "predicted"
        # Estimated counts: some ballots only possible (beyond the low estimate).
        assert any(not b["certain"] for b in extract["ballots"]) and any(b["certain"] for b in extract["ballots"])
        _, notes = next_step(ws, log)  # CORLA's reports: its selections define the extract, all certain
        found = latest_extract(ws, log, "Adams")
        assert found is not None and found[0]["order"] == "audit_center"
        assert all(b["certain"] for b in found[0]["ballots"])
        walk = state(ws, log)
        assert walk.next is None and walk.now == "2026-11-17T23:00:00.000Z"
        with pytest.raises(WalkthroughError, match="over"):
            next_step(ws, log)
        steps = [e for e in log.iter_events() if e.kind == "walkthrough.step"]
    assert [e.data["index"] for e in steps] == list(range(6))
    assert verify_jsonl(root / "events.jsonl").ok
    assert str(replay) not in (root / "events.jsonl").read_text()
    # Starting again keeps the accounts and nothing else.
    (root / "accounts").mkdir(exist_ok=True)
    (root / "accounts" / "coordinators.json").write_text("{}")
    with pytest.raises(WalkthroughError, match="already"):
        init_walkthrough(root, election="2026/general", county="Adams", mirror=replay)
    init_walkthrough(root, election="2026/general", county="Adams", mirror=replay, force=True)
    assert (root / "accounts" / "coordinators.json").exists()
    with EventLog.open(root) as log:
        assert state(Workspace.open(root), log).done == 0
    with pytest.raises(WalkthroughError, match="not a walkthrough"):
        other = tmp_path / "other"
        other.mkdir()
        (other / "x").write_text("x")
        init_walkthrough(other, election="2026/general", county="Adams", mirror=replay)


def test_the_web_pages_say_it_is_a_walkthrough(tmp_path: Path, replay: Path) -> None:
    from fastapi.testclient import TestClient

    from corla_observer.web.accounts import AccountStore
    from corla_observer.web.app import AppConfig, create_app
    from test_web import PASSWORD, SECRET, csrf_of, login

    root = tmp_path / "walk"
    init_walkthrough(root, election="2026/general", county="Adams", mirror=replay)
    with EventLog.open(root) as log:
        AccountStore(root).add("alice", PASSWORD, log)
    with TestClient(create_app(AppConfig(workspace=root, secret=SECRET)), base_url="https://testserver") as client:
        assert "WALKTHROUGH" in client.get("/").text
        login(client)
        page = client.get("/coord/walkthrough")
        assert "Next step: target list" in page.text and "before the first step" in page.text
        client.post("/coord/walkthrough/next", data={"csrf": csrf_of(page)})
        page = client.get("/coord/walkthrough")
        assert "Next step: results" in page.text and "2026-11-06 17:00" in page.text
        # Only the target list has arrived: nothing else is a problem yet.
        datamap = client.get("/coord/map").text
        assert "Not yet expected" in datamap and "0 of 63 posted" in datamap and "Problems (0)" in datamap
        assert "no_manifest" not in datamap and "no manifest" not in datamap
        assert "Data map: 0 problems" in client.get("/coord").text
        assert "nothing blocking" in client.get("/").text
        home = client.get("/coord")
        assert "nothing is fetched from the real sites" in home.text


def test_the_primary_applies_nothing_known_only_later() -> None:
    from corla_observer.walkthrough import primary_2026_timeline

    replay = TIMELINES["2026/primary"]
    steps = primary_2026_timeline("Weld")
    with_overrides = [(n, s) for n, s in enumerate(steps) if s.overrides]
    # The alias with the manifests it is about; the manifest adjustment only with CORLA's final reports.
    assert [(s.commit, "Prowes" in s.overrides, "Alamosa" in s.overrides) for _, s in with_overrides] == [
        ("a4a2b93", True, False),
        ("ba18ba6", False, True),
    ]
    assert with_overrides[-1][0] == len(steps) - 1 and all(s.overrides_note for _, s in with_overrides)
    assert replay.overrides is not None and "[[" not in replay.overrides
    # Nothing from CORLA's final reports in what earlier steps enter or say.
    assert "Audit Report" not in replay.seed_source and "final" not in replay.audit_boards_note.lower()
    for step in steps[:-1]:
        assert "HASH_MISMATCH" not in step.detail and "Saguache" not in step.detail
    # A step whose title tells what it brings shows a plain title until applied.
    replaced = next(s for s in steps if s.commit == "f46c663")
    assert replaced.preview == "Audit center update" and "replaced" in replaced.title


def test_overrides_arrive_with_their_evidence_and_problems_wait_for_their_due_time(
    tmp_path: Path, replay: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from corla_observer.datamap import build_map
    from corla_observer.due import view_for

    general = TIMELINES["2026/general"]
    steps = general.timeline("Adams")
    alias = '\n[[county_alias]]\nvalue = "Adms"\ncounty = "Adams"\nreason = "seen in CORLA\'s reports"\n'
    staged = [*steps[:-1], replace(steps[-1], overrides=alias, overrides_note="the alias, from CORLA's reports", upcoming="Audit center update")]
    monkeypatch.setitem(TIMELINES, "2026/general", replace(general, timeline=lambda county: staged))
    root = tmp_path / "walk"
    ws = init_walkthrough(root, election="2026/general", county="Adams", mirror=replay)
    with EventLog.open(root) as log:
        next_step(ws, log)  # the target list only
        datamap = build_map(log, ws)
        assert datamap.overrides_sha256 is None
        view = view_for(datamap, ws, log)
        assert view.now_source == "the walkthrough's simulated time" and view.lines == []
        assert "Manifests: 0 of 63 posted, due Fri 13 Nov 2:00 PM" in [w.text for w in view.waiting]
        for _ in range(len(staged) - 2):
            next_step(ws, log)
        assert build_map(log, ws).overrides_sha256 is None
        boards = [e for e in log.iter_events() if e.kind == "audit_boards.recorded"]
        assert boards and all("final" not in str(e.data.get("note")) for e in boards)
        _, notes = next_step(ws, log)
        assert "data map overrides applied: the alias, from CORLA's reports" in notes
        assert build_map(log, ws).county_aliases == {"Adms": "Adams"}
        assert "Adms" in (root / "overrides.toml").read_text()
