"""Fast tests for corla_observer.schedule: the schedule, the cycle, sprints and the loop."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from corla_observer.cli import app
from corla_observer.election import ElectionSetup, init_setup
from corla_observer.events import EventLog, format_server_time
from corla_observer.schedule import (
    GENERAL_2026_SCHEDULE,
    ScheduleError,
    active_sprint,
    cycle_lock,
    decide,
    init_schedule,
    parse_interval,
    parse_schedule,
    request_sprint,
    run_cycle,
    run_loop,
    stop_sprint,
)
from corla_observer.seeds import enter_seed
from corla_observer.workspace import Workspace
from synthetic import ELECTION_URL, election_files
from test_datamap import FakeFetcher

SEED = "12345678901234567890"


def utc(text: str) -> str:
    from corla_observer.datamap import parse_time

    return parse_time(text)


def test_the_2026_general_schedule() -> None:
    schedule = parse_schedule(GENERAL_2026_SCHEDULE)
    assert [w.name for w in schedule.windows][:2] == ["target list", "early postings"]
    assert schedule.window_at(utc("2026-11-06 18:59")) is None
    first = schedule.window_at(utc("2026-11-06 19:00"))
    assert first is not None and first.results
    roll = schedule.window_at(utc("2026-11-16 09:30"))
    assert roll is not None and roll.every_seconds == 180 and not roll.results
    assert schedule.next_start(utc("2026-10-26 12:00")) == utc("2026-11-06 19:00")
    assert parse_interval("1h30m") == 5400 and parse_interval("3m") == 180
    with pytest.raises(ValueError):
        parse_interval("soon")
    with pytest.raises(ScheduleError) as raised:
        parse_schedule('format = 1\nkeep_awake = "maybe"\n[[window]]\nstart = "2026-11-06 17:00"\nend = "2026-11-06 16:00"\nevery = "1h"\n')
    assert len(raised.value.errors) == 2


def test_decide() -> None:
    schedule = parse_schedule(GENERAL_2026_SCHEDULE)
    idle = decide(schedule, None, None, utc("2026-10-26 12:00"))
    assert not idle.due and "next starts" in idle.reason
    assert decide(schedule, None, None, utc("2026-11-06 19:01")).due
    assert not decide(schedule, None, utc("2026-11-06 19:00"), utc("2026-11-06 23:00")).due
    # The first cycle of a window, even if a cycle ran shortly before it began.
    assert decide(schedule, None, utc("2026-11-12 17:50"), utc("2026-11-12 18:00")).due
    hourly = decide(schedule, None, utc("2026-11-12 18:00"), utc("2026-11-12 18:59"))
    assert not hourly.due and hourly.wait_seconds <= 60
    assert decide(schedule, None, utc("2026-11-12 18:00"), utc("2026-11-12 19:00")).due
    # Missed while asleep: one cycle on waking.
    assert decide(schedule, None, utc("2026-11-14 00:00"), utc("2026-11-15 11:00")).due
    assert not decide(schedule, None, None, utc("2026-12-01 12:00")).due


def setup_workspace(tmp_path: Path) -> Workspace:
    ws = Workspace.open(tmp_path / "ws")
    init_setup(ws, "2026/general")
    init_schedule(ws, "2026/general")
    return ws


def fake_crawl(files: dict[str, bytes]) -> Any:
    from corla_observer.fetch import fetch_urls

    urls = {ELECTION_URL + p: d for p, d in files.items()}

    def crawl(workspace: Workspace, log: EventLog, setup: ElectionSetup | None) -> Any:
        return fetch_urls(list(urls), workspace=workspace, log=log, fetcher=FakeFetcher(urls))

    return crawl


def no_results(*args: Any) -> Any:
    return None


def test_cycle_records_everything_and_rebuilds_nothing_unchanged(tmp_path: Path) -> None:
    ws = setup_workspace(tmp_path)
    crawl = fake_crawl(election_files())
    with EventLog.open(ws.root) as log:
        report = run_cycle(ws, log, trigger="test", crawl_fn=crawl, results_fn=no_results)
        assert report.ran and report.crawl["new"] == 5 and not report.errors
        assert report.estimate_seq is not None and report.prediction_seq is None  # no seed yet
        again = run_cycle(ws, log, trigger="test", crawl_fn=crawl, results_fn=no_results)
        assert again.estimate_seq is None  # unchanged estimates are not recorded again
        enter_seed(log, SEED, "coordinator:c1", "the roll")
        seeded = run_cycle(ws, log, trigger="test", crawl_fn=crawl, results_fn=no_results)
        assert seeded.prediction_seq is not None, seeded.errors
        assert run_cycle(ws, log, trigger="test", crawl_fn=crawl, results_fn=no_results).prediction_seq is None
        kinds = [e.kind for e in log.iter_events()]
    assert kinds.count("schedule.cycle") == 4 and kinds.count("election.setup_recorded") == 1


def test_a_cycle_is_skipped_while_another_runs(tmp_path: Path) -> None:
    ws = setup_workspace(tmp_path)
    with EventLog.open(ws.root) as log, cycle_lock(ws) as taken:
        assert taken
        report = run_cycle(ws, log, trigger="test", crawl_fn=fake_crawl({}), results_fn=no_results)
        assert not report.ran and [e.kind for e in log.iter_events()][-1] == "schedule.cycle_skipped"


def test_sprints(tmp_path: Path) -> None:
    ws = setup_workspace(tmp_path)
    now = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
    with EventLog.open(ws.root) as log:
        request_sprint(log, "coordinator:c1", minutes=60, every_seconds=10, now=now)
        sprint = active_sprint(log, format_server_time(now + timedelta(minutes=5)))
        assert sprint is not None and sprint.every_seconds == 30
        assert active_sprint(log, format_server_time(now + timedelta(minutes=61))) is None
        decision = decide(None, sprint, format_server_time(now), format_server_time(now + timedelta(seconds=31)))
        assert decision.due and not decision.results
        stop_sprint(log, "coordinator:c1")
        assert active_sprint(log, format_server_time(now + timedelta(minutes=5))) is None


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def test_loop_is_idle_before_the_first_window_then_runs(tmp_path: Path) -> None:
    ws = setup_workspace(tmp_path)
    clock = FakeClock(datetime(2026, 11, 7, 1, 59, 0, tzinfo=UTC))  # 6:59 PM MST
    lines: list[str] = []
    ticks = iter(range(10))

    run_loop(ws.root, clock=clock, sleep=clock.sleep, stop=lambda: next(ticks, None) is None,
             crawl_fn=fake_crawl(election_files()), results_fn=no_results, echo=lines.append)  # fmt: skip
    with EventLog.open(ws.root) as log:
        cycles = [e for e in log.iter_events() if e.kind == "schedule.cycle"]
    assert len(cycles) == 1 and cycles[0].data["window"] == "target list"
    assert any("idle" in line for line in lines)


def test_cli(tmp_path: Path) -> None:
    runner = CliRunner()
    root = str(tmp_path / "ws")
    assert runner.invoke(app, ["schedule", "init", "--workspace", root]).exit_code == 0
    shown = runner.invoke(app, ["schedule", "show", "--workspace", root])
    assert shown.exit_code == 0 and "roll day" in shown.output and "idle" in shown.output
    asked = runner.invoke(app, ["schedule", "sprint", "--workspace", root, "--actor", "coordinator:c1", "--minutes", "30"])
    assert asked.exit_code == 0 and "sprint requested" in asked.output
    assert "sprint until" in runner.invoke(app, ["schedule", "show", "--workspace", root]).output
    assert "stopped" in runner.invoke(app, ["schedule", "stop", "--workspace", root, "--actor", "coordinator:c1"]).output
