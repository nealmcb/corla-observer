"""Several coordinators at once against one real server: fetches, uploads and draws in parallel.

A uvicorn server runs in a thread on a free port, as in the browser tests, and
two coordinators, each with their own session, send requests from many threads
released together by a barrier. The tests check what the code promises:

- **Fetches**: at most one cycle runs at a time, however many coordinators ask;
  every request that started one is in the log, and the others are told a fetch
  is running.
- **Uploads**: different CVRs for one county, uploaded at once, are all kept;
  each but the first records which file it replaced, and those form one chain in
  log order, so the latest file is never ambiguous.
- **Draws**: draws of one pattern at once get distinct, consecutive numbers; a
  draw that lost the race is refused with "draw again", never recorded twice.

The event log's hash chain verifies after each.
"""

from __future__ import annotations

import re
import threading
from functools import partial
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from corla_observer.events import EventLog
from corla_observer.web.accounts import AccountStore
from corla_observer.web.app import AppConfig, create_app
from corla_observer.workspace import Workspace
from synthetic import ballots, make_cvr
from test_browser_per_contest import free_port
from test_targets import COUNTY, PASSWORD, SENATE, _workspace, contest

import corla_observer.targets as tg

THREADS = 8


def _csrf(text: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', text)
    assert match, "no CSRF token in the page"
    return match.group(1)


def _session(base: str, name: str) -> httpx.Client:
    client = httpx.Client(base_url=base, timeout=60, follow_redirects=False)
    page = client.get("/login")
    response = client.post("/login", data={"csrf": _csrf(page.text), "name": name, "password": PASSWORD})
    assert response.status_code == 303, response.text[:300]
    return client


def _together(calls: Sequence[Callable[[], Any]]) -> list[Any]:
    """Run the calls in threads released at the same moment; their results, in order."""
    barrier = threading.Barrier(len(calls))
    results: list[Any] = [None] * len(calls)

    def run(i: int) -> None:
        barrier.wait()
        try:
            results[i] = calls[i]()
        except Exception as exc:  # reported by the assertions below
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(calls))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    return results


class _Crawl:
    """A slow stand-in for the cycle that counts how many run at once."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = self.most = self.calls = 0

    def __call__(self, ws: Workspace, log: EventLog) -> None:
        with self.lock:
            self.active += 1
            self.calls += 1
            self.most = max(self.most, self.active)
        time.sleep(0.3)
        with self.lock:
            self.active -= 1


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[str, Path, _Crawl]]:
    _, root = _workspace(tmp_path)
    with EventLog.open(root) as log:
        AccountStore(root).add("bob", PASSWORD, log)
    crawl = _Crawl()
    app = create_app(AppConfig(workspace=root, secret=b"s" * 32, secure_cookies=False, crawl=crawl, fetch_interval=0))
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}", root, crawl
    finally:
        srv.should_exit = True
        thread.join(timeout=10)


def _events(root: Path, kind: str) -> list[Any]:
    with EventLog.open(root, read_only=True) as log:
        return [e for e in log.iter_events() if e.kind == kind]


def _verified(root: Path) -> bool:
    with EventLog.open(root, read_only=True) as log:
        return log.verify().ok


def test_parallel_fetches_run_one_cycle_at_a_time(server: tuple[str, Path, _Crawl]) -> None:
    base, root, crawl = server
    clients = [_session(base, "alice"), _session(base, "bob")]
    tokens = [_csrf(c.get("/coord").text) for c in clients]
    for _ in range(3):  # a few rounds, each of simultaneous requests from both coordinators
        calls = [partial(clients[i % 2].post, "/coord/fetch", data={"csrf": tokens[i % 2]}) for i in range(THREADS)]
        responses = _together(calls)
        assert all(isinstance(r, httpx.Response) and r.status_code == 303 for r in responses), responses
        started = [r for r in responses if "msg=fetch_started" in r.headers["location"]]
        assert started and all("msg=fetch_started" in r.headers["location"] or "msg=fetch_running" in r.headers["location"] for r in responses)
        deadline = time.monotonic() + 10
        while crawl.active and time.monotonic() < deadline:
            time.sleep(0.05)
    assert crawl.most == 1, f"{crawl.most} cycles ran at once"
    assert len(_events(root, "coordinator.fetch_requested")) == crawl.calls
    assert _verified(root)


def test_parallel_uploads_of_different_cvrs_chain_their_replacements(server: tuple[str, Path, _Crawl]) -> None:
    base, root, _ = server
    clients = [_session(base, "alice"), _session(base, "bob")]
    tokens = [_csrf(c.get("/coord/upload").text) for c in clients]
    cvrs = [make_cvr(ballots(10 + i, "Mayor", "Council") + ballots(11, "Mayor", precinct="102")) for i in range(6)]

    def send(i: int) -> httpx.Response:
        return clients[i % 2].post(
            "/coord/upload", data={"csrf": tokens[i % 2], "kind": "redacted_cvr", "county": "Boulder"},
            files={"file": (f"cvr-{i}.csv", cvrs[i], "text/csv")},
        )  # fmt: skip

    responses = _together([partial(send, i) for i in range(len(cvrs))])
    assert all(isinstance(r, httpx.Response) and r.status_code == 200 and "accepted" in r.text for r in responses), responses
    received = [e for e in _events(root, "file.received") if e.county == "Boulder"]
    assert len(received) == len(cvrs) and len({e.file_sha256 for e in received}) == len(cvrs)
    replaced = [e for e in _events(root, "upload.warning") if e.county == "Boulder" and e.data["reason"] == "replaces_earlier"]
    # Each arrival after the first names the arrival just before it in the log: one chain, no gaps, no forks.
    # The warnings are recorded after each file's check, so their order can differ from the arrivals'.
    assert sorted((e.file_sha256, e.data["replaced_sha256"]) for e in replaced) == sorted(
        (later.file_sha256, earlier.file_sha256) for earlier, later in zip(received, received[1:], strict=False)
    )
    assert _verified(root)


def test_parallel_draws_get_distinct_consecutive_numbers(server: tuple[str, Path, _Crawl], tmp_path: Path) -> None:
    base, root, _ = server
    clients = [_session(base, "alice"), _session(base, "bob")]
    pages = [c.get("/coord/targets", params={"county": COUNTY}) for c in clients]
    tokens = [_csrf(p.text) for p in pages]
    votes = tg.load_votes(tmp_path / "files" / "cvr.csv")
    counted = clients[0].get("/coord/targets/count", params={"county": COUNTY, "v": f"{contest(votes, SENATE).index}:0"}).json()
    form = {"county": COUNTY, "selection": counted["selection"], "seed": "parallel", "size": "1"}
    calls = [partial(clients[i % 2].post, "/coord/targets/draw", data={"csrf": tokens[i % 2], **form}) for i in range(THREADS)]
    responses = _together(calls)
    assert all(isinstance(r, httpx.Response) for r in responses), responses
    won = [r for r in responses if r.status_code == 303]
    lost = [r for r in responses if r.status_code != 303]
    assert won, [r.text[:300] for r in responses]
    assert all("draw again" in r.text for r in lost), [r.text[:300] for r in lost]
    numbers = sorted(int(re.search(r"drawn=(\d+)", r.headers["location"]).group(1)) for r in won)  # type: ignore[union-attr]
    assert numbers == list(range(1, len(won) + 1))
    assert sorted(e.data["draw"] for e in _events(root, "targets.drawn")) == numbers
    assert _verified(root)
