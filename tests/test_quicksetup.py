"""Populate test data (corla_observer.quicksetup) and its testing-mode button on the coordinator home page."""

from __future__ import annotations

import re
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from corla_observer import pulllists
from corla_observer.events import EventLog
from corla_observer.practice import LinkStore
from corla_observer.quicksetup import SIZE, PopulateError, bundle_counties, choices, populate
from corla_observer.web.accounts import AccountStore
from corla_observer.web.app import AppConfig, create_app
from corla_observer.workspace import Workspace
from synthetic import ballots, make_cvr
from test_web import PASSWORD, SECRET, csrf_of, login

ACTOR = "coordinator:alice"
DELTA = Path(__file__).parent / "fixtures" / "delta-2020"
MANIFEST = b"County,Tabulator,Batch,# of Ballots,Location\r\nMesa,101,1,13,Box 1\r\n"


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """A small test data bundle: Delta's real files, and a Mesa whose CVR fails the redaction check."""
    root = tmp_path / "bundle"
    (root / "Delta").mkdir(parents=True)
    shutil.copyfile(DELTA / "Modified 2020 General Ballot Manifest.csv", root / "Delta" / "manifest.csv")
    shutil.copyfile(DELTA / "Modified CVR_Export_20201113093409.csv", root / "Delta" / "cvr.csv")
    (root / "Mesa").mkdir()
    (root / "Mesa" / "manifest.csv").write_bytes(MANIFEST)
    (root / "Mesa" / "cvr.csv").write_bytes(make_cvr(ballots(10, "Mayor", "Council") + ballots(3, "Mayor", "Fire", precinct="7")))
    (root / "Atlantis").mkdir()  # not a county: ignored
    return root


def test_the_choices(bundle: Path) -> None:
    assert bundle_counties(bundle) == ["Delta (2020 demo)", "Mesa (2020 demo)"]
    assert choices(bundle) == ["Testland", "Delta (2020 demo)", "Mesa (2020 demo)"] and choices(None) == ["Testland"]


def test_testland_from_nothing_and_never_twice(tmp_path: Path) -> None:
    store = Workspace.open(tmp_path / "ws")
    with EventLog.open(store.root) as log:
        made = populate(store, log, "testland", ACTOR)
        assert made.county == "Testland" and len(made.uploaded) == 2 and made.rejected is None
        assert len(made.ids) == SIZE and made.list_name == "Targeted list 1"
        drawn = next(e for e in log.iter_events() if e.kind == "targets.drawn")
        assert drawn.actor == ACTOR and drawn.data["ballots"] == SIZE
        assert [i.key for i in pulllists.county_lists(log.iter_events(), "Testland") if i.targeted] == [made.list_key]
        links = LinkStore(store.root)
        assert links.get(made.observer_token).actor == "observer:alice"  # type: ignore[union-attr]
        assert links.get(made.reader_token).role == "reader"  # type: ignore[union-attr]
        # A county that has a CVR is left as it is.
        before = len(log)
        with pytest.raises(PopulateError, match="already has a redacted CVR"):
            populate(store, log, "Testland", ACTOR)
        assert len(log) == before and log.verify().ok


def test_a_demonstration_county_from_the_bundle(tmp_path: Path, bundle: Path) -> None:
    store = Workspace.open(tmp_path / "ws")
    with EventLog.open(store.root) as log:
        made = populate(store, log, "Delta (2020 demo)", ACTOR, test_data=bundle)
        assert made.county == "Delta (2020 demo)" and made.rejected is None and len(made.ids) == SIZE
        assert made.uploaded == ("manifest-Delta.csv", "Delta 2020 CVR.csv")
        # A CVR that fails the redaction check stops after its upload, with the report.
        failed = populate(store, log, "Mesa (2020 demo)", ACTOR, test_data=bundle)
        assert failed.rejected is not None and failed.rejected.rare_group_count == 1 and failed.list_name == ""
        assert not any(e.kind == "targets.drawn" and e.county == "Mesa (2020 demo)" for e in log.iter_events())


def test_what_cannot_be_populated(tmp_path: Path, bundle: Path) -> None:
    store = Workspace.open(tmp_path / "ws")
    with EventLog.open(store.root) as log:
        with pytest.raises(PopulateError, match="not a test county"):
            populate(store, log, "Boulder", ACTOR, test_data=bundle)
        with pytest.raises(PopulateError, match="no files for Weld"):
            populate(store, log, "Weld (2020 demo)", ACTOR, test_data=bundle)
        assert len(log) == 0


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    Workspace.open(root)
    with EventLog.open(root) as log:
        AccountStore(root).add("alice", PASSWORD, log)
    return root


def _client(root: Path, **overrides: object) -> Iterator[TestClient]:
    app = create_app(AppConfig(workspace=root, secret=SECRET, crawl=lambda ws, log: None, **overrides))  # type: ignore[arg-type]
    with TestClient(app, base_url="https://testserver") as client:
        yield client


def test_no_button_on_a_real_deployment(root: Path) -> None:
    for client in _client(root):
        login(client)
        home = client.get("/coord")
        assert 'action="/coord/populate"' not in home.text
        assert client.post("/coord/populate", data={"csrf": csrf_of(home), "county": "Testland"}).status_code == 404


def test_the_button_populates_and_the_observer_link_opens(root: Path, bundle: Path) -> None:
    for client in _client(root, reset_command=(sys.executable, "-c", "pass"), test_data=bundle):
        login(client)
        home = client.get("/coord")
        assert "Testing mode: populate test data" in home.text and '<option value="" selected>Choose a county</option>' in home.text
        assert '<option value="Delta (2020 demo)">' in home.text and '<option value="Testland">' in home.text
        made = client.post("/coord/populate", data={"csrf": csrf_of(home), "county": "Testland"})
        assert made.status_code == 200 and "Targeted list 1" in made.text
        observer = re.search(r'href="https://testserver(/observe/[^"]+)" id="observer-link"', made.text)
        assert observer, made.text[:2000]
        assert len(client.get(observer.group(1) + "/data.json").json()["ballots"]) == SIZE
        # Now Testland is offered no more, and asking for it anyway is refused.
        home = client.get("/coord")
        assert '<option value="Testland" disabled>Testland (has a CVR here)</option>' in home.text
        again = client.post("/coord/populate", data={"csrf": csrf_of(home), "county": "Testland"})
        assert again.status_code == 400 and "already has a redacted CVR" in again.text
        rejected = client.post("/coord/populate", data={"csrf": csrf_of(home), "county": "Mesa (2020 demo)"})
        assert rejected.status_code == 200 and "rejected by the redaction check" in rejected.text
