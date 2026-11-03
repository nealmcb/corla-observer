"""Next steps on the coordinator home page (corla_observer.nextsteps): the targeted sample and RLA sample paths."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from corla_observer.events import EventLog
from corla_observer.nextsteps import Facts, facts_for, next_steps
from corla_observer.quicksetup import populate
from corla_observer.web.accounts import AccountStore
from corla_observer.web.app import AppConfig, create_app
from corla_observer.workspace import Workspace
from test_web import SECRET, login


def _states(county: str, facts: Facts) -> dict[str, list[str]]:
    return {p.name: [s.state for s in p.steps] for p in next_steps(county, facts)}


def test_nothing_yet_then_each_path_moves_on() -> None:
    assert _states("Delta", Facts()) == {
        "Targeted sample": ["next", "later", "later", "later", "later", "later"],
        "RLA sample": ["next", "later", "later", "later", "later"],
    }
    # Before the seed: the targeted path goes on, the RLA path waits for the seed.
    before_seed = Facts(cvr=True, manifest=True, sources=True, drafts=1, observer_links=2)
    assert _states("Delta", before_seed) == {
        "Targeted sample": ["done", "done", "done", "done", "done", "next"],
        "RLA sample": ["done", "next", "later", "later", "done"],
    }
    targeted, rla = next_steps("Delta (2020 demo)", before_seed)
    assert targeted.steps[-1].path == "/coord/targets?county=Delta%20%282020%20demo%29#drafts" and "1 open draft" in targeted.steps[3].detail
    assert "2 observer links so far" in rla.steps[-1].detail and not targeted.finished
    # Sending a list straight from the working list also finishes the draft step; an RLA extract finishes the pull-list step.
    done = Facts(cvr=True, manifest=True, sources=True, sent=1, observer_links=1, seed=True, rla_extract=True)
    assert all(p.finished for p in next_steps("Delta", done))


def test_the_home_page_shows_the_remembered_or_chosen_county(tmp_path: Path) -> None:
    store = Workspace.open(tmp_path / "ws")
    with EventLog.open(store.root) as log:
        AccountStore(store.root).add("alice", "correct horse battery", log)
        made = populate(store, log, "Testland", "coordinator:ann")
        facts = facts_for(store, log, "Testland", observer_links=1)
    assert facts.cvr and facts.manifest and facts.sources and facts.sent == 1 and not facts.seed and not facts.rla_extract
    app = create_app(AppConfig(workspace=store.root, secret=SECRET, crawl=lambda ws, log: None))
    with TestClient(app, base_url="https://testserver") as client:
        assert login(client).status_code == 303
        page = client.get("/coord").text
        assert '<section id="next-steps">' in page and "Choose a county to see what to do next" in page
        page = client.get("/coord?county=Testland").text
        assert "Next steps for Testland" in page and "Targeted sample path" in page and "RLA sample path" in page
        assert "all done" in page  # Populate drew, sent and made a link
        assert '<a href="/coord/seed"><strong>The seed is rolled</strong></a>' in page
        assert made.observer_token
