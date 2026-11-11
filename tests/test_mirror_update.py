"""Fast tests for corla_observer.mirror: the mirror update after each crawl, and publishing it.

Each test makes a bare repository standing in for GitHub's ``main`` (``origin``), a clone of it standing in
for the server's (``server``), and, where a person's own clone matters, a second clone (``person``). The
crawls are recorded in the event log as a crawl records them, with the bodies stored in the workspace.
Nothing touches the network.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from corla_observer.cli import app
from corla_observer.events import EventLog
from corla_observer.fetch import normalize_html
from corla_observer.mirror import (
    HEADING,
    MirrorError,
    after_crawl,
    current_files,
    mirror_status,
    publish_mirror,
    snapshot_name,
    update_mirror,
    waiting_commits,
)
from corla_observer.workspace import Workspace

RLA = "https://www.coloradosos.gov/pubs/elections/RLA/"
AUDIT_CENTER = "https://www.coloradosos.gov/pubs/elections/auditCenter.html"
OLD_SNAPSHOT = "auditCenters/auditCenter_2026-11-06T17:24:08-07:00.html"
TARGETS = "2026/general/files/2026GeneralRLAContestList.xlsx"


def page(text: str, token: str = "1a2b3c4d5e6f7a8b") -> bytes:
    """An Audit Center page as Cloudflare serves it: with a per-response token in a script."""
    return (f"<!DOCTYPE html><html><head><script>window.__CF$cv$params={{r:'{token}'}};"
            f"</script></head><body>{text}</body></html>").encode()  # fmt: skip


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def configure(repo: Path, name: str = "Server") -> None:
    git(repo, "config", "user.name", name)
    git(repo, "config", "user.email", f"{name.lower()}@example.org")
    git(repo, "config", "commit.gpgsign", "false")


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """GitHub's stand-in: a bare repository whose main holds a README, an older 2026 general file and a snapshot."""
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--quiet", "--bare", "-b", "main", str(bare)], check=True
    )
    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "clone", "--quiet", str(bare), str(seed)],
        check=True,
        capture_output=True,
    )
    configure(seed, "Seed")
    git(seed, "checkout", "--quiet", "-b", "main")
    (seed / "README.md").write_text("mirror\n")
    (seed / "2026/general/files").mkdir(parents=True)
    (seed / "2026/general/riskLimit.html").write_bytes(
        page("<p>3%</p>", "0000000000000000")
    )
    (seed / "auditCenters").mkdir()
    (seed / OLD_SNAPSHOT).write_bytes(
        page("<p>The 2026 general</p>", "9999999999999999")
    )
    (seed / "2024").mkdir()
    (seed / "2024/old.csv").write_text("old\n")
    git(seed, "add", ".")
    git(seed, "commit", "--quiet", "-m", "The mirror so far")
    git(seed, "push", "--quiet", "origin", "main")
    return bare


def clone(
    origin: Path, path: Path, *, name: str = "Server", sparse: bool = False
) -> Path:
    args = [
        "git",
        "clone",
        "--quiet",
        *(["--sparse"] if sparse else []),
        str(origin),
        str(path),
    ]
    subprocess.run(args, check=True, capture_output=True)
    configure(path, name)
    return path


@pytest.fixture
def server(origin: Path, tmp_path: Path) -> Path:
    return clone(origin, tmp_path / "server")


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace.open(tmp_path / "ws")


@pytest.fixture
def log(ws: Workspace) -> Iterator[EventLog]:
    with EventLog.open(ws.root) as log:
        yield log


def record_crawl(
    ws: Workspace, log: EventLog, files: dict[str, bytes], missing: tuple[str, ...] = ()
) -> int:
    """Record a crawl that fetched ``files`` (URL to body) and found ``missing`` gone; return its finished event's seq."""
    log.append("fetch.crawl_started", "system", data={"url": AUDIT_CENTER})
    for url, body in files.items():
        stored = ws.put_bytes(body)
        data: dict[str, Any] = {
            "url": url,
            "status": 200,
            "sha256": stored.sha256,
            "size": stored.size,
        }
        if url.endswith(".html"):
            data["normalized_sha256"] = hashlib.sha256(normalize_html(body)).hexdigest()
        log.append("fetch.file", "system", file_sha256=stored.sha256, data=data)
    for url in missing:
        log.append("fetch.missing", "system", data={"url": url, "reason": "HTTP 404"})
    state = hashlib.sha256(f"{len(log)}".encode()).hexdigest()
    return log.append(
        "fetch.crawl_finished",
        "system",
        data={"url": AUDIT_CENTER, "state_sha256": state},
    ).seq


def events(log: EventLog, kind: str) -> list[dict[str, Any]]:
    return [dict(e.data) for e in log.iter_events() if e.kind == kind]


def message(repo: Path, rev: str = "HEAD") -> str:
    return git(repo, "log", "-1", "--format=%B", rev)


def first_crawl(ws: Workspace, log: EventLog) -> dict[str, bytes]:
    files = {
        AUDIT_CENTER: page("<p>The 2026 general: targets posted</p>"),
        RLA + TARGETS: b"PK targets v1",
        RLA + "2026/general/riskLimit.html": page(
            "<p>3%</p>", "5555555555555555"
        ),  # the same page, another token
        RLA + "faqs.html": page("<p>FAQ</p>"),
    }
    record_crawl(ws, log, files)
    return files


def test_update_writes_changed_files_at_their_mirror_paths_and_commits_once(
    ws: Workspace, log: EventLog, server: Path, origin: Path
) -> None:
    files = first_crawl(ws, log)
    before = git(origin, "rev-parse", "main")
    result = update_mirror(ws, log, server)
    assert (
        result.commit is not None
        and result.heading is not None
        and result.heading.startswith(HEADING + " 20")
    )
    assert result.heading.endswith(("MST", "MDT"))
    written = {f.path: f.change for f in result.files}
    snapshot = snapshot_name(current_files(log)[AUDIT_CENTER].fetched)
    # The risk limit page differs only by Cloudflare's token: not written again. The FAQ, at the top of RLA/, is.
    assert written == {TARGETS: "new", "faqs.html": "new", snapshot: "new"}
    assert (server / TARGETS).read_bytes() == files[RLA + TARGETS] and (
        server / snapshot
    ).read_bytes() == files[AUDIT_CENTER]
    text = message(server)
    assert (
        text.startswith(result.heading + "\n\n")
        and f"New: {TARGETS}" in text
        and "New: faqs.html" in text
    )
    assert (
        f"the latest before it is {OLD_SNAPSHOT}" in text
        and "the crawl is event" in text
    )
    assert git(server, "status", "--porcelain") == ""
    # Waiting: not pushed.
    assert git(origin, "rev-parse", "main") == before
    assert [c.heading for c in waiting_commits(server)] == [result.heading]
    assert events(log, "mirror.updated") == []
    # Nothing changed since: no commit; a page differing only by its token gets no new snapshot.
    record_crawl(
        ws,
        log,
        {
            AUDIT_CENTER: page(
                "<p>The 2026 general: targets posted</p>", "ffffffffffffffff"
            )
        },
    )
    again = update_mirror(ws, log, server)
    assert again.commit is None and len(waiting_commits(server)) == 1


def test_a_changed_file_names_its_previous_version_and_nothing_is_ever_deleted(
    ws: Workspace, log: EventLog, server: Path
) -> None:
    first_crawl(ws, log)
    first = update_mirror(ws, log, server)
    record_crawl(
        ws,
        log,
        {
            RLA + TARGETS: b"PK targets v2",
            RLA + "2026/general/riskLimit.html": page("<p>4%</p>"),
        },
        missing=(RLA + "faqs.html",),
    )
    second = update_mirror(ws, log, server)
    assert {f.path: f.change for f in second.files} == {
        TARGETS: "changed",
        "2026/general/riskLimit.html": "changed",
    }
    text = message(server)
    # Still waiting, the earlier commit is named by its heading (its ID changes if it is replayed); a published one by ID.
    assert f'previous version: in the commit "{first.heading}"' in text
    seed_commit = git(server, "rev-parse", "origin/main").strip()
    assert f"previous version: commit {seed_commit}" in text
    # The FAQ went missing: it stays in the mirror; so does every older file.
    assert (
        (server / "faqs.html").is_file()
        and (server / "2024/old.csv").is_file()
        and (server / OLD_SNAPSHOT).is_file()
    )
    assert [c.heading for c in waiting_commits(server)] == [
        first.heading,
        second.heading,
    ]


def test_a_sparse_clone_gets_the_folders_it_writes_to(
    ws: Workspace, log: EventLog, origin: Path, tmp_path: Path
) -> None:
    sparse = clone(origin, tmp_path / "sparse", sparse=True)
    assert not (sparse / "2026").exists()
    first_crawl(ws, log)
    result = update_mirror(ws, log, sparse)
    assert result.sparse_added == ["2026/general", "auditCenters"]
    assert (sparse / TARGETS).is_file() and not (sparse / "2024").exists()
    assert git(sparse, "status", "--porcelain") == ""
    assert set(git(sparse, "show", "--name-only", "--format=", "HEAD").split()) == {
        TARGETS,
        "faqs.html",
        *(f.path for f in result.files if f.path.startswith("auditCenters/")),
    }


def test_publish_pushes_the_waiting_commits_and_only_then_records_them(
    ws: Workspace, log: EventLog, server: Path, origin: Path
) -> None:
    first_crawl(ws, log)
    update_mirror(ws, log, server)
    record_crawl(ws, log, {RLA + TARGETS: b"PK targets v2"})
    update_mirror(ws, log, server)
    waiting = waiting_commits(server)
    result = publish_mirror(log, server, actor="coordinator:alice")
    assert not result.replayed and [c.commit for c in result.commits] == [
        c.commit for c in waiting
    ]
    assert (
        git(origin, "rev-parse", "main") == git(server, "rev-parse", "HEAD")
        and waiting_commits(server) == []
    )
    [recorded] = events(log, "mirror.updated")
    assert (
        recorded["published_from"] == "this server"
        and recorded["repository"] == "a local repository"
    )
    assert recorded["pushed"] == {"remote": "origin", "transport": "local", "url": None}
    assert [c["files"] for c in recorded["commits"]][1] == [
        {"path": TARGETS, "change": "changed"}
    ]
    # Nothing waiting: nothing pushed or recorded.
    assert (
        publish_mirror(log, server).commits == []
        and len(events(log, "mirror.updated")) == 1
    )


def test_publish_replays_onto_a_newer_main_and_resolves_the_same_file(
    ws: Workspace, log: EventLog, server: Path, origin: Path, tmp_path: Path
) -> None:
    files = first_crawl(ws, log)
    update_mirror(ws, log, server)
    record_crawl(
        ws, log, {RLA + "2026/general/countyManifest.html": page("<p>manifests</p>")}
    )
    update_mirror(ws, log, server)
    headings = [c.heading for c in waiting_commits(server)]
    # Meanwhile someone pushes from their own clone: the same target list, the FAQ with other content, the
    # county manifest page differing only by its token, and a file of their own.
    person = clone(origin, tmp_path / "person", name="Person")
    (person / "2026/general/files").mkdir(parents=True, exist_ok=True)
    (person / TARGETS).write_bytes(files[RLA + TARGETS])
    (person / "faqs.html").write_bytes(page("<p>FAQ, as the scraper saved it</p>"))
    (person / "2026/general/countyManifest.html").write_bytes(
        page("<p>manifests</p>", "7777777777777777")
    )
    (person / "2020").mkdir()
    (person / "2020/past.csv").write_text("past\n")
    git(person, "add", ".")
    git(person, "commit", "--quiet", "-m", "Scraped by hand")
    git(person, "push", "--quiet", "origin", "main")
    theirs = git(person, "rev-parse", "HEAD").strip()

    result = publish_mirror(log, server)
    assert result.replayed and [c.heading for c in result.commits] == [headings[0]]
    assert sorted(result.kept_github_version) == [
        "2026/general/countyManifest.html",
        TARGETS,
    ]
    assert result.both_versions == [{"path": "faqs.html", "github_commit": theirs}]
    assert result.dropped == [headings[1]]  # left with nothing to add
    log_lines = git(origin, "log", "--format=%s", "main").splitlines()
    assert log_lines[:2] == [headings[0], "Scraped by hand"]
    pushed = git(origin, "rev-parse", "main").strip()
    assert (
        f"GitHub's main already held another version of faqs.html, in commit {theirs}"
        in message(origin, pushed)
    )
    assert git(origin, "show", "main:faqs.html").encode() == page("<p>FAQ</p>")
    assert (
        git(server, "rev-parse", "HEAD").strip() == pushed
        and (server / "2020/past.csv").is_file()
    )
    [recorded] = events(log, "mirror.updated")
    assert recorded["replayed"] is True and recorded["dropped"] == [headings[1]]
    assert {f["path"] for f in recorded["commits"][0]["files"]} == {
        "faqs.html",
        snapshot_name(current_files(log)[AUDIT_CENTER].fetched),
    }


def test_commits_published_from_another_machine_are_recorded_as_published(
    ws: Workspace, log: EventLog, server: Path, origin: Path, tmp_path: Path
) -> None:
    first_crawl(ws, log)
    made = update_mirror(ws, log, server)
    # Neal's machine fetches the server's clone, which brings the waiting commit, and pushes it with his own key.
    person = clone(origin, tmp_path / "person", name="Neal")
    git(person, "fetch", "--quiet", str(server), "main")
    git(person, "merge", "--quiet", "--ff-only", "FETCH_HEAD")
    git(person, "push", "--quiet", "origin", "main")
    record_crawl(ws, log, {})
    result = update_mirror(ws, log, server)
    assert [c.commit for c in result.published_elsewhere] == [made.commit]
    [recorded] = events(log, "mirror.updated")
    assert recorded["published_from"] == "elsewhere" and recorded["pushed"] is None
    assert waiting_commits(server) == []
    # Once only.
    update_mirror(ws, log, server)
    assert len(events(log, "mirror.updated")) == 1


def test_a_failed_push_leaves_the_commits_waiting_and_is_recorded_once(
    ws: Workspace, log: EventLog, server: Path, origin: Path, tmp_path: Path
) -> None:
    from corla_observer.schedule import parse_schedule

    first_crawl(ws, log)
    update_mirror(ws, log, server)
    git(
        server,
        "remote",
        "set-url",
        "--push",
        "origin",
        str(tmp_path / "no-such-repository.git"),
    )
    with pytest.raises(MirrorError, match="push failed"):
        publish_mirror(log, server)
    assert len(waiting_commits(server)) == 1 and events(log, "mirror.updated") == []
    # From the scheduler, in a window that publishes: never raises, recorded once while it keeps failing.
    (ws.root / "schedule.toml").write_text(
        f'format = 1\nmirror_clone = "{server}"\n', encoding="utf-8"
    )
    window = parse_schedule(
        'format = 1\n[[window]]\nstart = "2026-11-16 07:00"\nend = "2026-11-16 23:59"\nevery = "3m"\npublish_mirror = true\n'
    ).windows[0]
    for _ in range(2):
        summary = after_crawl(ws, log, window=window)
        assert (
            summary is not None
            and summary["waiting"] == 1
            and summary["published"] == []
        )
        assert any(str(e).startswith("publish: push failed") for e in summary["errors"])
    assert [e["step"] for e in events(log, "mirror.error")] == ["publish"]
    status = mirror_status(ws, log)
    assert (
        status["configured"]
        and len(status["waiting"]) == 1
        and status["errors"][0]["step"] == "publish"
    )
    # Fixed: published, and the error cleared.
    git(server, "remote", "set-url", "--push", "origin", str(origin))
    summary = after_crawl(ws, log, window=window)
    assert (
        summary is not None
        and len(summary["published"]) == 1
        and summary["waiting"] == 0
    )
    assert events(log, "mirror.error")[-1] == {"step": "publish", "error": None}
    assert mirror_status(ws, log)["errors"] == []


def test_a_broken_clone_never_fails_the_step(
    ws: Workspace, log: EventLog, tmp_path: Path
) -> None:
    (ws.root / "schedule.toml").write_text(
        f'format = 1\nmirror_clone = "{tmp_path / "nowhere"}"\n', encoding="utf-8"
    )
    first_crawl(ws, log)
    summary = after_crawl(ws, log)
    assert (
        summary is not None
        and summary["committed"] is None
        and str(summary["errors"][0]).startswith("update:")
    )
    assert events(log, "mirror.error")[0]["step"] == "update"
    # No clone named: no mirror step at all.
    (ws.root / "schedule.toml").write_text("format = 1\n", encoding="utf-8")
    assert after_crawl(ws, log) is None


def test_the_cycle_updates_the_mirror_and_publishes_in_a_window_that_says_so(
    tmp_path: Path, origin: Path
) -> None:
    from corla_observer.election import init_setup
    from corla_observer.schedule import parse_schedule, run_cycle
    from test_schedule import fake_crawl, no_results

    clone(origin, tmp_path / "server")
    ws = Workspace.open(tmp_path / "ws")
    init_setup(ws, "2026/general")
    crawl = fake_crawl({"files/2026GeneralRLAContestList.xlsx": b"PK targets"})
    with EventLog.open(ws.root) as log:
        # No clone named: the cycle is as before.
        assert (
            run_cycle(
                ws, log, trigger="test", crawl_fn=crawl, results_fn=no_results
            ).mirror
            is None
        )
        (ws.root / "schedule.toml").write_text(
            'format = 1\nmirror_clone = "../server"\n', encoding="utf-8"
        )
        report = run_cycle(
            ws, log, trigger="test", crawl_fn=crawl, results_fn=no_results
        )
        assert (
            report.mirror is not None
            and report.mirror["files"] == [TARGETS]
            and report.mirror["waiting"] == 1
        )
        assert report.mirror["published"] == [] and not report.errors
        cycle = [e for e in log.iter_events() if e.kind == "schedule.cycle"][-1]
        assert (
            cycle.data["mirror"] == report.mirror
            and cycle.data["crawl_incomplete"] is False
        )
        window = parse_schedule(
            'format = 1\n[[window]]\nstart = "2026-11-16 07:00"\nend = "2026-11-16 23:59"\nevery = "3m"\npublish_mirror = true\n'
        ).windows[0]
        published = run_cycle(
            ws,
            log,
            trigger="test",
            window=window,
            crawl_fn=crawl,
            results_fn=no_results,
        )
        assert published.mirror is not None and len(published.mirror["published"]) == 1
    assert git(origin, "show", f"main:{TARGETS}") == "PK targets"


def test_a_replayed_election_never_writes_to_the_mirror(
    tmp_path: Path, origin: Path
) -> None:
    from corla_observer.election import init_setup
    from corla_observer.schedule import run_cycle
    from test_schedule import fake_crawl, no_results

    clone(origin, tmp_path / "server")
    ws = Workspace.open(tmp_path / "ws")
    init_setup(ws, "2020/general")
    (ws.root / "schedule.toml").write_text(
        'format = 1\nmirror_clone = "../server"\n', encoding="utf-8"
    )
    with EventLog.open(ws.root) as log:
        report = run_cycle(
            ws,
            log,
            trigger="test",
            crawl_fn=fake_crawl({"files/x.csv": b"x"}),
            results_fn=no_results,
        )
    assert report.mirror is None


def test_simulated_fetches_are_never_mirrored(ws: Workspace, log: EventLog) -> None:
    stored = ws.put_bytes(b"from the mirror")
    log.append(
        "fetch.file",
        "system",
        data={
            "url": RLA + TARGETS,
            "sha256": stored.sha256,
            "simulated": {"repository": "x"},
        },
    )
    assert current_files(log) == {}


def test_the_schedule_settings() -> None:
    from corla_observer.schedule import (
        GENERAL_2026_SCHEDULE,
        ScheduleError,
        parse_schedule,
    )

    default = parse_schedule(GENERAL_2026_SCHEDULE)
    assert default.mirror_clone is None and [
        w.name for w in default.windows if w.publish_mirror
    ] == ["roll day", "roll day, seed meeting", "roll day, afternoon"]
    named = parse_schedule('format = 1\nmirror_clone = "~/auditcenter"\n')
    assert named.mirror_clone == "~/auditcenter"
    with pytest.raises(ScheduleError) as raised:
        parse_schedule(
            'format = 1\nmirror_clone = 3\n[[window]]\nstart = "2026-11-16 07:00"\nend = "2026-11-16 23:59"\nevery = "3m"\npublish_mirror = "yes"\n'
        )
    assert len(raised.value.errors) == 2


def test_cli_status_update_and_publish(
    ws: Workspace, server: Path, origin: Path
) -> None:
    with EventLog.open(ws.root) as log:
        first_crawl(ws, log)
    (ws.root / "schedule.toml").write_text(
        f'format = 1\nmirror_clone = "{server}"\n', encoding="utf-8"
    )
    runner = CliRunner()
    args = ["--workspace", str(ws.root)]
    updated = runner.invoke(app, ["mirror", "update", *args])
    assert (
        updated.exit_code == 0
        and HEADING in updated.output
        and TARGETS in updated.output
    ), updated.output
    status = runner.invoke(app, ["mirror", "status", *args])
    assert status.exit_code == 0 and "1 mirror commit waiting" in status.output, (
        status.output
    )
    published = runner.invoke(
        app, ["mirror", "publish", *args, "--actor", "coordinator:alice"]
    )
    assert published.exit_code == 0 and "published 1 commit" in published.output, (
        published.output
    )
    assert git(origin, "rev-parse", "main") == git(server, "rev-parse", "HEAD")
    (ws.root / "schedule.toml").write_text("format = 1\n", encoding="utf-8")
    assert runner.invoke(app, ["mirror", "update", *args]).exit_code == 2


def test_names_are_kept_as_published_spaces_and_all() -> None:
    """The mirror keeps the Audit Center's own file names (``Adams Audit Report.pdf``), never a rewritten copy."""
    from corla_observer.timestamp import mirror_path

    base = "https://www.coloradosos.gov/pubs/elections/RLA/2026/primary/finalReports/"
    assert mirror_path(base + "Adams%20Audit%20Report.pdf") == "2026/primary/finalReports/Adams Audit Report.pdf"
    assert mirror_path(base + "La Plata.csv") == "2026/primary/finalReports/La Plata.csv"
    assert mirror_path("https://www.coloradosos.gov/pubs/elections/RLA/faqs.html") == "faqs.html"
    for bad in ("..%2F..%2Fetc%2Fpasswd", "a%5Cb.csv", "x%0Ay.csv", "dir/./x.csv"):
        assert mirror_path(base + bad) is None, bad
    assert mirror_path(base) is None and mirror_path("https://www.coloradosos.gov/pubs/elections/auditCenter.html") is None
