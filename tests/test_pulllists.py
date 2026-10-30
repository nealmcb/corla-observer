"""A county's pull lists side by side: the RLA sample and sent targeted lists, observers switching, entries kept apart, box lookups, the dashboards."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from corla_observer import pulllists
from corla_observer import targets as tg
from corla_observer.events import EventLog
from corla_observer.extract import build_extract, county_manifest, latest_extract, targeted_key
from corla_observer.observer_record import check_record
from corla_observer.practice import LinkStore, practice_stats
from corla_observer.web.app import AppConfig, create_app
from corla_observer.web.views import lookup_summaries, targeted_summaries
from corla_observer.workspace import Workspace
from devices import Device
from test_targets import ACTOR, COUNTY, _workspace
from test_web import csrf_of, login

LIST_1 = targeted_key("Targeted list 1")
LIST_2 = targeted_key("Targeted list 2")
TIME = "2026-11-17T16:05:41.950Z"


def _send(log: EventLog, root: Path, ids: list[str], note: str = "") -> tg.SentList:
    """Enter ``ids`` by hand and send the working list to the county (`corla_observer.targets.send`)."""
    store = Workspace.open(root)
    votes = tg.load_votes(root.parent / "files" / "cvr.csv")
    tg.enter_ids(store, log, COUNTY, ids, ACTOR, note=note)
    manifest = county_manifest(store, log, COUNTY)
    return tg.send(store, log, COUNTY, ACTOR, votes=votes, locations=manifest.locations, sizes=manifest.sizes)


def _setup(tmp_path: Path) -> tuple[Any, Path, Any, Any, Any]:
    store, root = _workspace(tmp_path)
    with EventLog.open(root) as log:
        rla = build_extract(store, log, COUNTY, actor=ACTOR, sample_count=6)
        a = _send(log, root, ["1-2-1", "1-1-3"], "first")
        b = _send(log, root, ["1-3-1", "1-1-3"], "second")
    return store, root, rla, a, b


def test_lists_side_by_side_and_retired(tmp_path: Path) -> None:
    store, root, rla, a, b = _setup(tmp_path)
    with EventLog.open(root) as log:
        found = latest_extract(store, log, COUNTY)
        assert found is not None and found[1] == rla.sha256  # targeted extracts never replace the RLA extract
        lists = pulllists.county_lists(log.iter_events(), COUNTY)
        assert [(x.key, x.label, x.number) for x in lists] == [("rla", "RLA sample", 0), (LIST_1, "Targeted list 1", 1), (LIST_2, "Targeted list 2", 2)]
        assert lists[1].public_label == "Targeted list 1" and lists[1].sha256 == a.extract_sha256 and lists[2].sha256 == b.extract_sha256
        # The second list holds 1-1-3 too: a sent list is a frozen snapshot, each ballot once.
        assert sorted(e.imprinted_id for e in b.entries) == ["1-1-3", "1-3-1"]
        # Rebuilding the RLA extract keeps the lists.
        again = build_extract(store, log, COUNTY, actor=ACTOR, sample_count=5)
        lists = pulllists.county_lists(log.iter_events(), COUNTY)
        assert [x.sha256 for x in lists] == [again.sha256, a.extract_sha256, b.extract_sha256]
        pulllists.retire(log, COUNTY, LIST_2, ACTOR)
        assert [x.name for x in pulllists.county_lists(log.iter_events(), COUNTY)] == ["RLA sample", "Targeted list 1"]
        assert pulllists.county_lists(log.iter_events(), COUNTY, retired=True)[2].retired
        pulllists.restore(log, COUNTY, LIST_2, ACTOR)
        assert len(pulllists.county_lists(log.iter_events(), COUNTY)) == 3
        # The next list is 3 even with a list retired.
        assert tg.next_list_number(log, COUNTY) == (3, "Targeted list 3")


def test_observer_switches_lists_and_entries_never_mix(tmp_path: Path) -> None:
    store, root, rla, a, _ = _setup(tmp_path)
    key = LIST_1
    with EventLog.open(root) as log:
        token, link = LinkStore(root).create("observer", "ann", COUNTY, 1, log)
    app = create_app(AppConfig(workspace=root, secret=b"s" * 32, crawl=lambda ws, log: None))
    with TestClient(app, base_url="https://testserver") as client:
        page = client.get(f"/observe/{token}")
        assert page.status_code == 200 and 'id="obs-list"' in page.text and "Targeted list 1 (2)" in page.text
        assert 'data-list="rla"' in page.text and "<strong>TARGETED (NOT RLA)</strong>" not in page.text
        assert "Look up a ballot (from a box) (0)" in page.text and 'id="obs-lookup-link"' in page.text  # the county has a CVR
        targeted = client.get(f"/observe/{token}?list={key}")
        assert 'data-list="' + key + '"' in targeted.text and "<strong>TARGETED (NOT RLA)</strong>" in targeted.text and f"list={key}" in targeted.text
        data = client.get(f"/observe/{token}/data.json?list={key}").json()
        assert data["extract_sha256"] == a.extract_sha256 and data["list_label"] == "Targeted list 1" and len(data["ballots"]) == 2
        assert rla.sha256 in data["other_shas"] and data["list_shas"] == [a.extract_sha256]
        rla_data = client.get(f"/observe/{token}/data.json").json()
        assert rla_data["extract_sha256"] == rla.sha256 and rla_data["list"] == "rla"

        on_rla = Device("observer:ann", COUNTY, "1", rla.sha256)
        first = rla_data["ballots"][0]
        sent = client.post(f"/observe/{token}/entries", json={"entries": [on_rla.registration(), on_rla.entry(first["position"], first["id"])]}).json()
        assert not sent["errors"]
        on_rla.extract = a.extract_sha256
        t1 = data["ballots"][0]
        items = [on_rla.list_switch(key, "Targeted list 1", t1["position"], t1["id"]), on_rla.entry(t1["position"], t1["id"], False, "heard Bob")]
        items.append(on_rla.heard(t1["position"], t1["id"], data["ballots"][1]["id"]))  # matched against the targeted list's ballots
        sent = client.post(f"/observe/{token}/entries", json={"entries": items}).json()
        assert not sent["errors"] and sent["heard"][str(on_rla.seq)]["kind"] == "observation.out_of_order"

        # Each list's page shows only its own entries.
        assert list(client.get(f"/observe/{token}/data.json").json()["entries"]) == [str(first["position"])]
        theirs = client.get(f"/observe/{token}/data.json?list={key}").json()["entries"]
        assert list(theirs) == [str(t1["position"])] and theirs[str(t1["position"])]["note"] == "heard Bob"

        # A retired list is gone from the switcher, and its address goes back to the RLA sample.
        with EventLog.open(root) as log:
            pulllists.retire(log, COUNTY, key, ACTOR)
        assert client.get(f"/observe/{token}?list={key}", follow_redirects=False).status_code == 303
        assert 'id="obs-list"' in client.get(f"/observe/{token}").text  # Targeted list 2 is still there

    with EventLog.open(root) as log:
        events = list(log.iter_events())
        switched = [e for e in events if e.kind == "observation.list_switched"]
        assert switched and switched[0].data["list"] == key and switched[0].data["extract_sha256"] == a.extract_sha256
        observers, boards = practice_stats(events)
        by_list = {(b.list_key, b.board): b for b in boards}
        assert by_list[("rla", "1")].done == 1 and by_list[("rla", "1")].matches == 1 and by_list[("rla", "1")].done_ids == [first["id"]]
        assert by_list[(key, "1")].done == 1 and by_list[(key, "1")].notes == 1 and by_list[(key, "1")].ballots == 2
        assert {(o.list_key, o.entries) for o in observers} == {("rla", 1), (key, 1)}
        summary = next(t for t in targeted_summaries(events, boards, observers, store) if t.key == key)
        assert summary.done == 1 and summary.notes == 1 and summary.observers == ["observer:ann"] and summary.retired
        public = summary.public_dict()
        assert public["label"] == "Targeted list 1" and "first" not in json.dumps(public) and public["details"] is None

        # The record's check counts the entries per list, and still passes.
        record = {
            "format": "corla-observer-record", "version": 1, "observer": on_rla.observer, "county": COUNTY, "board": "1",
            "device_id": on_rla.device_id, "public_key": on_rla.public_key,
            "entries": [{"payload": i["payload"], "signature": i["signature"], "receipt": None, "refused": None} for i in on_rla.items],
        }  # fmt: skip
        report = check_record(record, root / "events.jsonl")
        assert not [f for f in report.problems if f.kind != "missing"]
        assert report.lists == {f"RLA sample (extract {rla.sha256[:8]})": 1, f"Targeted: Targeted list 1 (extract {a.extract_sha256[:8]})": 3}


def test_a_targeted_ballot_also_in_the_rla_sample_counts_once(tmp_path: Path) -> None:
    """A sent list's ballot the RLA sample holds: marked on the list, and its RLA entry counts for the list."""
    store, root = _workspace(tmp_path)
    with EventLog.open(root) as log:
        rla = build_extract(store, log, COUNTY, actor=ACTOR, sample_count=6)
        sampled = [b["imprinted_id"] for b in rla.extract["ballots"]]
        a = _send(log, root, [sampled[0], sampled[1], "1-2-1"])
        assert a.in_rla >= 2
        token, _ = LinkStore(root).create("observer", "ann", COUNTY, 1, log)
    app = create_app(AppConfig(workspace=root, secret=b"s" * 32, crawl=lambda ws, log: None))
    with TestClient(app, base_url="https://testserver") as client:
        data = client.get(f"/observe/{token}/data.json?list={LIST_1}").json()
        marked = [b for b in data["ballots"] if b["in_rla"]]
        assert len(marked) == a.in_rla and marked[0]["in_rla"].startswith("ALSO IN THE RLA SAMPLE")
        device = Device("observer:ann", COUNTY, "1", rla.sha256)
        rla_data = client.get(f"/observe/{token}/data.json").json()
        first = next(b for b in rla_data["ballots"] if b["id"] == sampled[0])
        sent = client.post(f"/observe/{token}/entries", json={"entries": [device.registration(), device.entry(first["position"], first["id"])]}).json()
        assert not sent["errors"]
    with EventLog.open(root) as log:
        events = list(log.iter_events())
        observers, boards = practice_stats(events)
        summary = next(t for t in targeted_summaries(events, boards, observers, store) if t.key == LIST_1)
        assert summary.in_rla == a.in_rla and summary.done_on_rla == 1 and summary.done == 1 and summary.public_dict()["in_rla"] == a.in_rla


def test_box_lookups_are_recorded_apart_from_every_list(tmp_path: Path) -> None:
    """"Look up a ballot": a ballot picked from a box, looked up by ID, entered contest by contest, counted apart from the RLA sample and the lists."""
    store, root, rla, a, _ = _setup(tmp_path)
    with EventLog.open(root) as log:
        token, _ = LinkStore(root).create("observer", "ann", COUNTY, 1, log)
        reader, _ = LinkStore(root).create("reader", "rita", COUNTY, 1, log)
        from corla_observer.uploads import status

        cvr_sha = status(log, [COUNTY])[COUNTY].latest["redacted_cvr"].sha256
    app = create_app(AppConfig(workspace=root, secret=b"s" * 32, crawl=lambda ws, log: None))
    with TestClient(app, base_url="https://testserver") as client:
        page = client.get(f"/observe/{token}?list=lookup")
        assert page.status_code == 200 and 'data-list="lookup"' in page.text and 'id="obs-lookup-form"' in page.text
        assert "LOOKED UP (NOT RLA)" in page.text and 'value="lookup" selected' in page.text and "Provenance" not in page.text.split("obs-more-title")[1]
        assert client.get(f"/read/{reader}?list=lookup", follow_redirects=False).status_code == 200  # the reader has no lookups: the RLA sample
        empty = client.get(f"/observe/{token}/data.json?list=lookup").json()
        assert empty["list"] == "lookup" and empty["lookup"] and empty["ballots"] == [] and empty["extract_sha256"] == cvr_sha
        assert empty["list_label"] == "Looked up from boxes" and "CVR " + cvr_sha[:8] in empty["ident"]
        looked = client.get(f"/observe/{token}/lookup.json?id=1-1-5&position=1").json()
        assert looked["found"] and looked["in_manifest"] and looked["location"] == "Box 10" and looked["extract_sha256"] == cvr_sha
        ballot = looked["ballot"]
        assert ballot["position"] == 1 and ballot["id"] == "1-1-5" and ballot["qr"] == [] and ballot["location"] == "Box 10"
        assert ballot["votes"] and any("Senat" in v[0] for v in ballot["votes"])
        assert client.get(f"/observe/{token}/lookup.json?id=junk&position=1").status_code == 400
        missing = client.get(f"/observe/{token}/lookup.json?id=2-1-1&position=2")
        assert missing.status_code == 404 and "not in" in missing.json()["error"]
        beyond = client.get(f"/observe/{token}/lookup.json?id=1-9-9&position=2").json()  # in the manifest, not in the CVR
        assert not beyond["found"] and beyond["in_manifest"] and beyond["ballot"]["missing"]

        # The entries: lookup_contest and lookup_done payloads, on the redacted CVR's hash.
        device = Device("observer:ann", COUNTY, "1", cvr_sha)
        rows = len(ballot["votes"])
        items = [device.registration()]
        for reading in (1, 2):
            for n, vote in enumerate(ballot["votes"], start=1):
                content = {"contest": vote[0], "row": n, "rows": rows, "expected": vote[1], "reading": reading, "outcome": "confirmed" if n > 1 else "heard_instead",
                           "heard": "" if n > 1 else "Bob Senate", "correction": False, "elapsed_ms": 100}  # fmt: skip
                items.append(device._chain(device.sign(device.payload("lookup_contest", 1, "1-1-5", content, TIME))))
        items.append(device._chain(device.sign(device.payload("lookup_done", 1, "1-1-5", {"reading": 2, "rows": rows, "unresolved": 0, "elapsed_ms": 900}, TIME))))
        sent = client.post(f"/observe/{token}/entries", json={"entries": items}).json()
        assert not sent["errors"] and len(sent["recorded"]) == len(items)
        # Restored on a reload, and nowhere else.
        data = client.get(f"/observe/{token}/data.json?list=lookup").json()
        assert [b["id"] for b in data["ballots"]] == ["1-1-5"] and data["contests"]["1"]["done"] and data["entries"]["1"]["per_contest"]
        assert "Bob Senate" in data["entries"]["1"]["note"]
        assert client.get(f"/observe/{token}/data.json").json()["entries"] == {}
        assert client.get(f"/observe/{token}/data.json?list={LIST_1}").json()["entries"] == {}
        assert "Look up a ballot (from a box) (1)" in client.get(f"/observe/{token}").text
        # The statistics and the dashboards: "Targeted from boxes", apart.
        assert login(client).status_code == 303
        stats = client.get("/coord/stats").text
        assert "Targeted from boxes: 1 observed by 1 observer" in stats and "Targeted by CVR: 2 lists, 4 ballots, 0 observed" in stats
        assert "Looked up from boxes" in stats
    with EventLog.open(root) as log:
        events = list(log.iter_events())
        kinds = {e.kind for e in events}
        assert {"observation.lookup_contest", "observation.lookup_done"} <= kinds and "observation.contest" not in kinds
        observers, boards = practice_stats(events)
        lookups = [b for b in boards if b.lookup]
        assert len(lookups) == 1 and lookups[0].done == 1 and lookups[0].notes == 1 and lookups[0].ballots is None and lookups[0].list_label == "Looked up from boxes"
        assert all(b.done == 0 for b in boards if not b.lookup)
        assert [(x.county, x.done, x.observers) for x in lookup_summaries(boards)] == [(COUNTY, 1, ["observer:ann"])]
        assert [o.list_key for o in observers] == ["lookup"] and observers[0].mode_text == "contest by contest"
        record = {
            "format": "corla-observer-record", "version": 1, "observer": device.observer, "county": COUNTY, "board": "1",
            "device_id": device.device_id, "public_key": device.public_key,
            "entries": [{"payload": i["payload"], "signature": i["signature"], "receipt": None, "refused": None} for i in device.items],
        }  # fmt: skip
        report = check_record(record, root / "events.jsonl")
        assert not [f for f in report.problems if f.kind != "missing"]
        assert report.lists == {f"Looked up from boxes (redacted CVR {cvr_sha[:8]})": len(items) - 1}


def test_links_open_on_the_targeted_list_until_there_is_an_rla_sample(tmp_path: Path) -> None:
    """Targeted lists and no RLA sample yet (no seed): a link opens on the first targeted list, the page says so and shows the switcher."""
    store, root = _workspace(tmp_path)
    with EventLog.open(root) as log:
        token, _ = LinkStore(root).create("observer", "ann", COUNTY, 1, log)
        reader, _ = LinkStore(root).create("reader", "bob", COUNTY, 1, log)
    app = create_app(AppConfig(workspace=root, secret=b"s" * 32, crawl=lambda ws, log: None))
    notice = f"No RLA sample for {COUNTY} board 1 yet (the seed hasn't been rolled). Showing the targeted list: Targeted list 1."
    shown = notice.replace("'", "&#39;")  # as the pages escape it
    with TestClient(app, base_url="https://testserver") as client:
        # No list at all yet: the page loads and the data says why (the script shows it, and never stays at "Loading").
        data = client.get(f"/observe/{token}/data.json").json()
        assert data["error"] == f"no extract for {COUNTY} yet" and [x["key"] for x in data["lists"]] == ["lookup"] and "default" not in data
        page = client.get(f"/observe/{token}")
        assert page.status_code == 200 and 'id="obs-rla-missing" class="notice obs-rla-missing" role="note" hidden' in page.text
        assert 'id="obs-list"' not in page.text and 'id="obs-lookup-link"' in page.text  # one list at most: the lookup is under More
        assert "No extract for" in client.get(f"/read/{reader}").text

        with EventLog.open(root) as log:
            p1 = _send(log, root, ["1-2-1", "1-1-3"])
        key = LIST_1
        data = client.get(f"/observe/{token}/data.json").json()
        assert data["list"] == key and data["extract_sha256"] == p1.extract_sha256 and data["rla_missing"] and data["notice"] == notice
        assert [x["key"] for x in data["lists"]] == [key, "lookup"] and len(data["ballots"]) == 2
        page = client.get(f"/observe/{token}")
        assert f'data-list="{key}"' in page.text and 'id="obs-list"' in page.text and "Targeted list 1 (2)" in page.text and shown in page.text
        assert "<strong>TARGETED (NOT RLA)</strong>" in page.text and f"list={key}" in page.text  # the printout and provenance links
        # The RLA sample asked for by name: no ballots, but the lists and the default, which the page then opens.
        asked = client.get(f"/observe/{token}/data.json?list=rla").json()
        assert asked["error"].startswith("no RLA sample for") and asked["default"] == key and [x["key"] for x in asked["lists"]] == [key, "lookup"]
        assert client.get(f"/observe/{token}/pdf").headers["content-type"] == "application/pdf"  # the targeted list's
        assert client.get(f"/observe/{token}/provenance").status_code == 200
        reading = client.get(f"/read/{reader}")
        assert reading.status_code == 200 and shown in reading.text and 'name="list"' in reading.text and "TARGETED (NOT RLA)" in reading.text
        assert client.post(f"/read/{reader}/next", data={"rate": "0"}, follow_redirects=False).headers["location"].endswith(f"list={key}")

        # A second list: still the first. The RLA sample built: links open on it again, without the notice, both lists in the switcher.
        with EventLog.open(root) as log:
            _send(log, root, ["1-3-1"])
        assert client.get(f"/observe/{token}/data.json").json()["list"] == key
        with EventLog.open(root) as log:
            rla = build_extract(store, log, COUNTY, actor=ACTOR, sample_count=6)
        data = client.get(f"/observe/{token}/data.json").json()
        assert data["list"] == "rla" and data["extract_sha256"] == rla.sha256 and not data["rla_missing"] and data["notice"] is None
        page = client.get(f"/observe/{token}")
        assert 'data-list="rla"' in page.text and shown not in page.text and page.text.count("<option value=") == 4  # three lists and the lookup


def test_coordinator_sent_lists_allowed_total_and_publish(tmp_path: Path) -> None:
    _, root, _, a, b = _setup(tmp_path)
    app = create_app(AppConfig(workspace=root, secret=b"s" * 32, crawl=lambda ws, log: None))
    with TestClient(app, base_url="https://testserver") as client:
        assert login(client).status_code == 303
        page = client.get(f"/coord/targets?county={COUNTY}")
        assert page.status_code == 200 and "Targeted list 1" in page.text and "Publish details" in page.text and "Combine" not in page.text
        assert "2 lists active, 4 ballots (3 different)" in page.text and "first" in page.text  # the note of the entry
        csrf = csrf_of(page)
        assert client.post("/coord/targets/allowed", data={"csrf": csrf, "county": COUNTY, "allowed": "2"}, follow_redirects=False).status_code == 303
        assert "more than the 2" in client.get(f"/coord/targets?county={COUNTY}").text
        # Per-list printouts on Extracts, and publishing a list's details.
        extracts = client.get("/coord/extract").text
        assert "Targeted lists (not RLA)" in extracts and f"list={LIST_1}" in extracts and "Targeted list 2" in extracts
        assert client.get(f"/coord/extract/pdf?county={COUNTY}&board=1&layout=portrait-2&list={LIST_1}").content.startswith(b"%PDF")
        stats = client.get("/coord/stats").text
        assert "Targeted by CVR: 2 lists, 4 ballots" in stats and "public: Targeted list 2" in stats
        assert client.post("/coord/targets/lists", data={"csrf": csrf, "county": COUNTY, "list": LIST_2, "action": "publish"}, follow_redirects=False).status_code == 303
        with EventLog.open(root) as log:
            published = [e for e in log.iter_events() if e.kind == "extract.details_published"][0]
            details: Any = published.data["details"]
            assert details["name"] == "Targeted list 2" and sorted(details["ids"]) == ["1-1-3", "1-3-1"] and details["patterns"] == []
            # Nothing of a note or an ID in the events the sends recorded.
            assert all("1-3-1" not in json.dumps(e.data) for e in log.iter_events() if e.kind in ("extract.built", "targets.sent"))


def test_public_dashboard_names_targeted_lists_neutrally(tmp_path: Path) -> None:
    store, root = _workspace(tmp_path)
    from corla_observer.uploads import receive

    real = "Delta"
    with EventLog.open(root) as log:
        assert receive(store, log, real, "redacted_cvr", tmp_path / "files" / "cvr.csv", "cvr.csv", ACTOR).accepted
        votes = tg.load_votes(tmp_path / "files" / "cvr.csv")
        tg.enter_ids(store, log, real, ["1-2-1", "1-1-3"], ACTOR, note="Split Pattern X")
        built = tg.send(store, log, real, ACTOR, votes=votes, locations={}, sizes={})
    app = create_app(AppConfig(workspace=root, secret=b"s" * 32, counties=(real,), crawl=lambda ws, log: None))
    with TestClient(app, base_url="https://testserver") as client:
        shown = client.get("/dashboard.json").json()["targeted_lists"]
        assert [(t["county"], t["label"], t["ballots"], t["extract"], t["done"]) for t in shown] == [(real, "Targeted list 1", 2, built.extract_sha256[:8], 0)]
        assert client.get("/dashboard.json").json()["targeted_from_boxes"] == []
        page = client.get("/").text
        assert "Targeted ballots (not RLA)" in page and "Targeted list 1" in page
        assert "Split Pattern X" not in page and "Split Pattern X" not in json.dumps(shown) and "1-2-1" not in json.dumps(shown)
        assert login(client).status_code == 303
        assert "Split Pattern X" in client.get(f"/coord/targets?county={real}").text
        csrf = csrf_of(client.get(f"/coord/targets?county={real}"))
        client.post("/coord/targets/lists", data={"csrf": csrf, "county": real, "list": LIST_1, "action": "publish"})
        details = client.get("/dashboard.json").json()["targeted_lists"][0]["details"]
        assert details["name"] == "Targeted list 1" and sorted(details["ids"]) == ["1-1-3", "1-2-1"]
        assert "1-2-1" in client.get("/").text
        client.post("/coord/targets/lists", data={"csrf": csrf, "county": real, "list": LIST_1, "action": "retire"})
        assert client.get("/dashboard.json").json()["targeted_lists"] == []
        stats = client.get("/coord/stats").text
        assert "Targeted list 1" in stats and "retired" in stats


def test_reader_keeps_its_place_per_list_and_switch_payloads_are_checked(tmp_path: Path) -> None:
    import pytest

    from corla_observer.practice import current_read
    from corla_observer.receipts import PayloadError, check_payload

    _, root, _, a, _ = _setup(tmp_path)
    key = LIST_1
    with EventLog.open(root) as log:
        token, _ = LinkStore(root).create("reader", "rita", COUNTY, 1, log)
    app = create_app(AppConfig(workspace=root, secret=b"s" * 32, crawl=lambda ws, log: None))
    with TestClient(app, base_url="https://testserver") as client:
        page = client.get(f"/read/{token}?list={key}")
        assert "TARGETED (NOT RLA)" in page.text and "Targeted list 1 (2)" in page.text and "Press Next" in page.text
        assert "Look up a ballot" not in page.text  # the reader reads lists only
        client.post(f"/read/{token}/next", data={"rate": "0", "list": key})
        client.post(f"/read/{token}/next", data={"rate": "0", "list": key})
        client.post(f"/read/{token}/next", data={"rate": "0"})
    with EventLog.open(root) as log:
        targeted = current_read(log, COUNTY, 1, key)
        rla = current_read(log, COUNTY, 1)
        assert targeted is not None and targeted["board_position"] == 2 and targeted["extract_sha256"] == a.extract_sha256
        assert rla is not None and rla["board_position"] == 1
    device = Device("observer:ann", COUNTY, "1", a.extract_sha256)
    device.registration()
    assert check_payload(device.list_switch(key, "Targeted list 1")["payload"])["type"] == "list_switch"
    assert check_payload(device.list_switch("lookup", "Look up a ballot")["payload"])["content"]["list"] == "lookup"
    with pytest.raises(PayloadError, match="list"):
        check_payload(device.list_switch("pattern-a", "Targeted: Pattern A")["payload"])


def test_a_targeted_list_not_yet_entered_shows_the_mode_chosen(tmp_path: Path) -> None:
    _, root, rla, a, _ = _setup(tmp_path)
    key = LIST_1
    with EventLog.open(root) as log:
        token, _ = LinkStore(root).create("observer", "ann", COUNTY, 1, log)
    app = create_app(AppConfig(workspace=root, secret=b"s" * 32, crawl=lambda ws, log: None))
    with TestClient(app, base_url="https://testserver") as client:
        rla_data = client.get(f"/observe/{token}/data.json").json()
        device = Device("observer:ann", COUNTY, "1", rla.sha256)
        first = rla_data["ballots"][0]
        sent = client.post(f"/observe/{token}/entries", json={"entries": [device.registration(), device.entry_mode("per_contest", first["position"], first["id"])]}).json()
        assert not sent["errors"]
        device.extract = a.extract_sha256
        t1 = client.get(f"/observe/{token}/data.json?list={key}").json()["ballots"][0]
        sent = client.post(f"/observe/{token}/entries", json={"entries": [device.list_switch(key, "Targeted list 1", t1["position"], t1["id"])]}).json()
        assert not sent["errors"]
    with EventLog.open(root) as log:
        observers, _ = practice_stats(list(log.iter_events()))
    assert {o.list_key: o.mode_text for o in observers} == {"rla": "contest by contest", key: "contest by contest"}


def test_sent_lists_are_evidence(tmp_path: Path) -> None:
    """A sent list of a real county is in the evidence set, under its name; a test county's never is."""
    from corla_observer.timestamp import collect_evidence
    from corla_observer.uploads import receive

    store, root = _workspace(tmp_path)
    real = "Delta"
    with EventLog.open(root) as log:
        assert receive(store, log, real, "redacted_cvr", tmp_path / "files" / "cvr.csv", "cvr.csv", ACTOR).accepted
        votes = tg.load_votes(tmp_path / "files" / "cvr.csv")
        tg.enter_ids(store, log, real, ["1-2-1"], ACTOR)
        sent = tg.send(store, log, real, ACTOR, votes=votes, locations={}, sizes={})
        _send(log, root, ["1-1-3"])  # the demonstration county's: not evidence
        evidence = collect_evidence(store, log)
    paths = {f.path: f.sha256 for f in evidence.files}
    assert paths.get("derived/extract-Delta-targeted-list-1.json") == sent.extract_sha256
    assert any(p.startswith("derived/extract-Delta-targeted-list-1-board1-") for p in paths)
    assert not any("2020" in p for p in paths)
