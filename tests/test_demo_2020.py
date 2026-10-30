"""Fast tests for what the Delta 2020 demonstration needs (docs/walkthroughs/demo-delta-2020-general.md).

The 2020 general's setup (`corla_observer.election.GENERAL_2020_SETUP`), its audit center replayed
from the public mirror one update per fetch (`corla_observer.rehearse.mirror_update`), a fetch from
the mirror leaving files out (`stage_fetch`'s ``exclude``), the data map's comparison of a county's
uploads with the audit center's copy and CORLA's upload hashes, and a manifest in a round's
directory not taken for a round.
"""

from __future__ import annotations

import hashlib
import io
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from corla_observer.county_status import audit_rounds
from corla_observer.datamap import build_map, describe
from corla_observer.election import (
    GENERAL_2020_AFTER_ROLL,
    GENERAL_2020_SETUP,
    MirrorSetup,
    MirrorUpdate,
    init_setup,
    load_setup,
    parse_setup,
)
from corla_observer.events import EventLog
from corla_observer.fetch import FetchPolicy, FetchResult, fetch_urls
from corla_observer.rehearse import RehearsalError, mirror_update, set_mirror_checkout, stage_fetch
from corla_observer.schedule import default_crawl
from corla_observer.uploads import receive
from corla_observer.workspace import Workspace
from synthetic import ADAMS_MANIFEST, ELECTION_URL, ballots, election_files, make_cvr

ACTOR = "coordinator:test"
DEMO = "2020/general"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.org", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


@pytest.fixture
def mirror(tmp_path: Path) -> tuple[Path, str]:
    """A mirror holding one election under 2020/general/ in one commit, with a round 2 file posted after the roll."""
    repo = tmp_path / "mirror-checkout"
    repo.mkdir()
    git(repo, "init", "-q")
    files = election_files() | {"round_2/contest.csv": b"contest_name,audited_sample_count\r\nGovernor - DEM,9\r\n"}
    for path, data in files.items():
        target = repo / DEMO / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "snapshot after the audit")
    return repo, git(repo, "rev-parse", "HEAD")


def fetched_paths(log: EventLog) -> set[str]:
    return {str(e.data["url"]).rsplit(DEMO + "/", 1)[1] for e in log.iter_events() if e.kind == "fetch.file"}


def test_the_2020_general_setup() -> None:
    setup = parse_setup(GENERAL_2020_SETUP)
    assert setup.election == "2020/general" and setup.pilot_counties == ("Delta",)
    assert setup.crawl is False and setup.probe_urls == ()
    assert setup.results.election_id == "" and setup.results.discover_from == ()
    assert setup.mirror is not None and setup.mirror.election == "2020/general"
    (update,) = setup.mirror.updates
    assert update.commit.startswith("382a035") and update.exclude == GENERAL_2020_AFTER_ROLL
    assert "round_1/contest_selection.csv" in update.exclude and "20201116NoticeOfRandomSeed.pdf" in update.exclude
    assert setup.milestone("roll") is not None and setup.milestone("roll").time == "2020-11-16T16:00:00.000Z"  # type: ignore[union-attr]


def test_a_mirror_setup_is_checked() -> None:
    bad = GENERAL_2020_SETUP.replace('commit = "382a035fe0ff90291382a6dfb33bf583032d1605"', "")
    with pytest.raises(Exception, match="needs a commit"):
        parse_setup(bad)


def test_fetch_from_the_mirror_leaves_out_what_is_excluded(tmp_path: Path, mirror: tuple[Path, str]) -> None:
    repo, commit = mirror
    ws = Workspace.open(tmp_path / "ws")
    with EventLog.open(ws.root) as log:
        report = stage_fetch(ws, log, repo, commit, election=DEMO, exclude=("round_2/", "riskLimit"))
        assert report.count("new") == 4
        assert fetched_paths(log) == {"files/Adams.csv", "files/Boulder.csv", "files/Weld.csv", "files/targetContests.csv"}


def test_each_fetch_applies_the_next_mirror_update(tmp_path: Path, mirror: tuple[Path, str]) -> None:
    repo, commit = mirror
    ws = Workspace.open(tmp_path / "ws")
    setup = MirrorSetup(DEMO, "https://github.com/nealmcb/auditcenter", (MirrorUpdate("before the roll", commit[:10], ("round_2/",)), MirrorUpdate("round 2", commit)))
    with EventLog.open(ws.root) as log:
        with pytest.raises(RehearsalError, match="no mirror checkout"):
            mirror_update(ws, log, setup)
        set_mirror_checkout(ws, repo)
        first = mirror_update(ws, log, setup)
        assert first.count("new") == 5 and "round_2/contest.csv" not in fetched_paths(log)
        second = mirror_update(ws, log, setup)
        assert second.count("new") == 1 and "round_2/contest.csv" in fetched_paths(log)
        third = mirror_update(ws, log, setup)
        assert third.count("new") == 0 and third.count("unchanged") == 6
        updates = [e for e in log.iter_events() if e.kind == "audit_center.mirror_update"]
        assert [(e.data["index"], e.data["again"]) for e in updates] == [(0, False), (1, False), (1, True)]
        assert updates[0].data["commit"] == commit and updates[0].data["exclude"] == ["round_2/"]
        markers = [e.data["simulated"] for e in log.iter_events() if e.kind == "fetch.file"]
        assert all(isinstance(m, dict) and m["source"] == "mirror" and m["commit"] == commit for m in markers)
    assert str(repo) not in (ws.root / "events.jsonl").read_text()


def test_a_cycle_replays_the_mirror_instead_of_crawling(tmp_path: Path, mirror: tuple[Path, str]) -> None:
    repo, commit = mirror
    ws = Workspace.open(tmp_path / "ws")
    init_setup(ws, "2020/general")
    setup = load_setup(ws)
    assert setup is not None and setup.mirror is not None
    setup = parse_setup(GENERAL_2020_SETUP.replace("382a035fe0ff90291382a6dfb33bf583032d1605", commit))
    set_mirror_checkout(ws, repo)
    with EventLog.open(ws.root) as log:
        report = default_crawl(ws, log, setup)
        assert report is not None and report.count("new") == 5  # round_2/ is among the demo's exclusions
    no_mirror = parse_setup(GENERAL_2020_SETUP.split("[audit_center_mirror]")[0] + "[calendar]" + GENERAL_2020_SETUP.split("[calendar]", 1)[1])
    assert no_mirror.mirror is None and no_mirror.crawl is False
    with EventLog.open(ws.root) as log:
        assert default_crawl(ws, log, no_mirror) is None


class Files:
    def __init__(self, files: Mapping[str, bytes]) -> None:
        self.files = dict(files)
        self.policy = FetchPolicy()

    def fetch(self, url: str, *, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        body = self.files.get(url)
        return FetchResult(url, url, 200 if body is not None else 404, body or b"", {})


def fetch(ws: Workspace, log: EventLog, files: Mapping[str, bytes]) -> None:
    urls = {ELECTION_URL + path: data for path, data in files.items()}
    fetch_urls(list(urls), workspace=ws, log=log, fetcher=Files(urls))


def test_uploads_compared_with_the_audit_center_and_corla(tmp_path: Path) -> None:
    ws = Workspace.open(tmp_path / "ws")
    cvr = make_cvr(ballots(12, "Mayor", "Council"), title="2026 Adams County General Election")
    with EventLog.open(ws.root) as log:
        receive(ws, log, "Adams", "redacted_cvr", io.BytesIO(cvr), "Adams_CVR.csv", ACTOR)
        receive(ws, log, "Adams", "manifest", io.BytesIO(ADAMS_MANIFEST), "Adams manifest.csv", ACTOR)
        before = build_map(log, ws)
        assert [(c.kind, c.status_posted, c.corla, c.posted) for c in before.upload_hashes] == [
            ("manifest", False, None, ()), ("redacted_cvr", False, None, ())
        ]
        text = describe(before)
        assert "CORLA's upload hashes: not yet posted" in text and "has not posted the county's manifest yet" in text
        status = (
            "name,filename,type,status,timestamp,computed_hash\r\n"
            f"Adams,m.csv,manifest,IMPORTED,2026-11-10 10:00:00,{hashlib.sha256(ADAMS_MANIFEST).hexdigest().upper()}\r\n"
            f"Adams,c.csv,CVRs,IMPORTED,2026-11-10 09:00:00,{hashlib.sha256(cvr).hexdigest().upper()}\r\n"
            f"Boulder,c.csv,CVRs,IMPORTED,2026-11-10 09:00:00,{'0' * 64}\r\n"
        ).encode()
        fetch(ws, log, election_files(files_uploadStatus_csv=status))
        after = build_map(log, ws)
        manifest, redacted = after.upload_hashes
        assert manifest.posted == (ELECTION_URL + "files/Adams.csv",) and manifest.corla_match is True
        assert redacted.corla is not None and redacted.corla.filename == "c.csv" and redacted.corla_match is True
        text = describe(after)
        assert "byte-identical to the audit center's posted manifest (RLA/2026/general/files/Adams.csv)" in text
        assert "byte-identical to the file CORLA recorded importing (c.csv" in text
        assert redacted.to_dict()["corla_match"] is True
        # A redacted file that is not the one CORLA imported is said to differ, as expected.
        receive(ws, log, "Adams", "redacted_cvr", io.BytesIO(cvr.replace(b"\r\n", b"\n")), "Adams_CVR_redacted.csv", ACTOR)
        (_, redacted) = build_map(log, ws).upload_hashes
        assert redacted.corla_match is False
        assert "expected if the county redacted the file" in "\n".join(redacted.lines())


def test_a_manifest_in_a_round_directory_is_not_a_round(tmp_path: Path) -> None:
    ws = Workspace.open(tmp_path / "ws")
    with EventLog.open(ws.root) as log:
        fetch(ws, log, {"round_1/manifest-Adams.csv": ADAMS_MANIFEST})
        datamap = build_map(log, ws)
        assert "Adams" in datamap.manifests
        rounds = audit_rounds(datamap)
        assert rounds.election_round is None and rounds.by_county == {}


def test_web_seed_entry_predicts_and_the_home_page_shows_the_mirror(tmp_path: Path, mirror: tuple[Path, str]) -> None:
    import re

    from fastapi.testclient import TestClient

    from corla_observer.web.accounts import AccountStore
    from corla_observer.web.app import AppConfig, create_app

    repo, commit = mirror
    root = tmp_path / "web"
    ws = Workspace.open(root)
    (root / "election.toml").write_text(GENERAL_2020_SETUP.replace("382a035fe0ff90291382a6dfb33bf583032d1605", commit), encoding="utf-8")
    set_mirror_checkout(ws, repo)
    with EventLog.open(root) as log:
        AccountStore(root).add("alice", "correct horse battery", log)
        stage_fetch(ws, log, repo, commit, election=DEMO, exclude=("round_2/",))
    application = create_app(AppConfig(workspace=root, secret=b"s" * 32, crawl=lambda ws, log: None))
    with TestClient(application, base_url="https://testserver") as client:
        page = client.get("/login")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text)
        assert token
        client.post("/login", data={"csrf": token.group(1), "name": "alice", "password": "correct horse battery"})
        home = client.get("/coord").text
        assert "the public mirror's <code>2020/general</code> files" in home and "0 of 1 applied" in home
        csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/coord/seed").text)
        assert csrf
        client.post("/coord/seed/set", data={"csrf": csrf.group(1), "seed": "12345678901234567890", "source": "dice roll"})
    with EventLog.open(root) as log:
        kinds = [e.kind for e in log.iter_events()]
    assert kinds.index("prediction.made") > kinds.index("seed.entered"), "the seed's entry on the web predicts at once"
