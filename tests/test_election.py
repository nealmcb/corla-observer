"""Fast tests for corla_observer.election: the election setup file."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from corla_observer.cli import app
from corla_observer.election import (
    GENERAL_2026_SETUP,
    SETUP_FILENAME,
    SetupError,
    init_setup,
    load_setup,
    parse_setup,
    record_setup,
)
from corla_observer.events import EventLog
from corla_observer.workspace import Workspace


def test_the_2026_general_default() -> None:
    setup = parse_setup(GENERAL_2026_SETUP)
    assert setup.election == "2026/general"
    assert setup.pilot_counties == ("La Plata", "Weld", "Morgan")
    assert all("/RLA/2026/general/" in u for u in setup.probe_urls)
    assert any(u.endswith("files/targetContestSelections.xlsx") for u in setup.probe_urls)
    assert len(setup.probe_urls) == len(set(setup.probe_urls)) >= 15
    roll = setup.milestone("roll")
    assert roll is not None and roll.time == "2026-11-16T17:00:00.000Z"  # 10:00 MST
    assert setup.milestone("targets_deadline").time == "2026-11-07T00:00:00.000Z"  # type: ignore[union-attr]
    assert setup.cards_for("Denver")[0] == 2.99 and setup.cards_for("La Plata")[0] == 1.0
    assert "San Juan" not in setup.cards_per_ballot
    assert setup.cards_for("San Juan") == (1.0, "the setup's default_cards_per_ballot")
    assert setup.results.election_id == "" and setup.results.min_interval == 1.0
    assert not setup.results_final("2026-11-13T20:59:59.000Z") and setup.results_final("2026-11-13T21:00:00.000Z")


def test_errors_are_all_listed() -> None:
    bad = (
        'format = 1\nelection = ""\nprobe_urls = ["https://example.org/x"]\npilot_counties = ["Atlantis"]\n'
        '[calendar]\nroll = "soon"\n[results]\nelection_id = "abc"\n[cards_per_ballot]\nAdams = 0\n'
    )
    with pytest.raises(SetupError) as raised:
        parse_setup(bad)
    assert len(raised.value.errors) == 6


def test_init_load_and_record(tmp_path: Path) -> None:
    ws = Workspace.open(tmp_path / "ws")
    assert load_setup(ws) is None
    init_setup(ws, "2026/general")
    with pytest.raises(SetupError, match="exists"):
        init_setup(ws, "2026/general")
    with pytest.raises(SetupError, match="no default"):
        init_setup(ws, "2027/general", force=True)
    setup = load_setup(ws)
    assert setup is not None
    with EventLog.open(ws.root) as log:
        assert record_setup(ws, log, setup) is not None
        assert record_setup(ws, log, setup) is None
        (ws.root / SETUP_FILENAME).write_text(GENERAL_2026_SETUP.replace('election_id = ""', 'election_id = "130000"'))
        changed = load_setup(ws)
        assert changed is not None and changed.results.election_id == "130000"
        assert record_setup(ws, log, changed) is not None
        recorded = [e for e in log.iter_events() if e.kind == "election.setup_recorded"]
    assert len(recorded) == 2 and ws.get_bytes(str(recorded[1].file_sha256)).decode() == changed.text


def test_cli(tmp_path: Path) -> None:
    runner = CliRunner()
    root = str(tmp_path / "ws")
    made = runner.invoke(app, ["election", "init", "--workspace", root, "--election", "2026/general"])
    assert made.exit_code == 0, made.output
    shown = runner.invoke(app, ["election", "show", "--workspace", root])
    assert shown.exit_code == 0, shown.output
    assert "2026/general" in shown.output and "roll" in shown.output and "MST" in shown.output
