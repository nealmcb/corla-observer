"""Fast tests for the web interface (corla_observer.web), with FastAPI's TestClient and temporary workspaces."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from typer.testing import CliRunner

from corla_observer.cli import app as cli_app
from corla_observer.events import EventLog
from corla_observer.seeds import enter_and_confirm
from corla_observer.uploads import STAGING_DIRNAME, receive, record_audit_boards
from corla_observer.web import coordinator
from corla_observer.web.accounts import AccountError, AccountStore
from corla_observer.web.app import AppConfig, create_app
from corla_observer.web.ratelimit import Limit, RateLimiter
from corla_observer.web.sessions import SecretError, load_secret
from corla_observer.workspace import Workspace
from synthetic import ballots, make_cvr

GOOD_CVR = make_cvr(ballots(10, "Mayor", "Council") + ballots(11, "Mayor", precinct="102"))
BAD_CVR = make_cvr(ballots(10, "Mayor", "Council") + ballots(3, "Mayor", "Fire", precinct="7"))
MANIFEST = b"County,Tabulator,Batch,# of Ballots,Location\r\nBoulder,101,1,25,Box 1\r\n,101,2,30,Box 1\r\n"
SEED = "12345678901234567890"
PASSWORD = "correct horse battery"
SECRET = b"s" * 32

Factory = Callable[..., TestClient]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    store = Workspace.open(root)
    with EventLog.open(store.root) as log:
        AccountStore(store.root).add("alice", PASSWORD, log)
    return root


@pytest.fixture
def make_client(workspace: Path) -> Iterator[Factory]:
    clients: list[TestClient] = []

    def make(**overrides: object) -> TestClient:
        settings: dict[str, object] = {
            "workspace": workspace,
            "secret": SECRET,
            "counties": ("Boulder",),
            "sse_poll_interval": 0.05,
            "sse_max_seconds": 1.0,
            "crawl": lambda ws, log: None,
        }
        settings.update(overrides)
        client = TestClient(create_app(AppConfig(**settings)), base_url="https://testserver")  # type: ignore[arg-type]
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Factory) -> TestClient:
    return make_client()


def csrf_of(response: Response) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match, "no CSRF token in the page"
    return match.group(1)


def login(client: TestClient, prefix: str = "", name: str = "alice", password: str = PASSWORD) -> Response:
    page = client.get(f"{prefix}/login")
    response: Response = client.post(
        f"{prefix}/login",
        data={"csrf": csrf_of(page), "name": name, "password": password},
        follow_redirects=False,
    )
    return response


def upload(client: TestClient, data: bytes, kind: str = "redacted_cvr", county: str = "Boulder", csrf: str | None = None) -> Response:
    if csrf is None:
        csrf = csrf_of(client.get("/coord/upload"))
    # The order matters: the token, kind and county come before the file.
    response: Response = client.post(
        "/coord/upload",
        data={"csrf": csrf, "kind": kind, "county": county},
        files={"file": ("cvr.csv", data, "text/csv")},
    )
    return response


def events(workspace: Path) -> list[tuple[str, str]]:
    with EventLog.open(workspace, read_only=True) as log:
        return [(e.kind, e.actor) for e in log.iter_events()]


def all_bytes_under(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file())


def test_dashboard_renders_sample_events(workspace: Path, make_client: Factory, tmp_path: Path) -> None:
    store = Workspace.open(workspace)
    source = tmp_path / "good.csv"
    source.write_bytes(GOOD_CVR)
    with EventLog.open(workspace) as log:
        assert receive(store, log, "Boulder", "redacted_cvr", source, "good.csv", "coordinator:alice").accepted
        record_audit_boards(log, "Boulder", 3, "coordinator:alice")
        enter_and_confirm(log, SEED, "coordinator:alice", "announced at the dice roll", second_actor="coordinator:bob")
        log.append("fetch.crawl_finished", "system", data={"url": "https://www.coloradosos.gov/pubs/elections/auditCenter.html", "counts": {"new": 2}})
        head_seq, head_hash = log.head()
    client = make_client()
    response = client.get("/")
    assert response.status_code == 200
    page = response.text
    assert "Boulder" in page and "PASS" in page and SEED in page and head_hash in page
    assert f"{head_seq} events" in page
    assert "21 ballots" in page  # counts only
    assert "Mayor" not in page  # never the CVR's content
    assert "crawl finished" in page and "2 new" in page
    assert "missing 1" not in page and "pull list" not in page  # a pull list arrives only after the drawing, so it is not shown as missing
    assert "<dt>Pull list</dt>" not in page and "Still missing" not in page  # listed once, as missing
    # Every time is Colorado time with its zone, never UTC as stored.
    assert re.search(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d M[DS]T", page)
    assert not re.search(r"\d\dT\d\d:\d\d:\d\d(\.\d+)?Z", page)
    # Security and caching headers.
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"].startswith("public")
    assert "server" not in response.headers
    assert client.get("/", headers={"If-None-Match": response.headers["etag"]}).status_code == 304
    assert client.head("/").status_code == 200
    data = client.get("/dashboard.json").json()
    assert data["seed"] == SEED and data["head"] == {"seq": head_seq, "hash": head_hash}
    assert data["counties"][0]["cvr"]["verdict"] == "PASS"
    assert client.get("/fragment/dashboard").text.count("data-live") == 1
    assert "<html" not in client.get("/fragment/dashboard").text


def test_sse_emits_an_update_after_an_append(workspace: Path, make_client: Factory) -> None:
    client = make_client(sse_max_seconds=1.5)
    log: EventLog = client.app.state.web.log  # type: ignore[attr-defined]
    before = log.head()[0]

    def append_later() -> None:
        time.sleep(0.4)
        log.append("test.ping", "system")

    thread = threading.Thread(target=append_later)
    thread.start()
    response = client.get("/events/stream")
    thread.join()
    assert response.headers["content-type"].startswith("text/event-stream")
    heads = [json.loads(line[len("data: ") :]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert [h["seq"] for h in heads] == [before, before + 1]
    assert heads[1]["hash"] == log.head()[1]


def test_login_and_logout(workspace: Path, client: TestClient) -> None:
    assert login(client, password="wrong password!").status_code == 401
    assert login(client, name="nobody").status_code == 401
    response = login(client)
    assert response.status_code == 303 and response.headers["location"] == "/coord?msg=logged_in"
    cookie = next(c for c in response.headers.get_list("set-cookie") if c.startswith("corla_session="))
    for attribute in ("HttpOnly", "Secure", "SameSite=strict"):
        assert attribute.lower() in cookie.lower()
    home = client.get("/coord")
    assert home.status_code == 200 and "Log out alice" in home.text
    out = client.post("/logout", data={"csrf": csrf_of(home)}, follow_redirects=False)
    assert out.status_code == 303
    assert client.get("/coord", follow_redirects=False).status_code == 303
    kinds = events(workspace)
    assert ("coordinator.login", "coordinator:alice") in kinds
    assert ("coordinator.logout", "coordinator:alice") in kinds
    assert ("coordinator.login_failed", "system") in kinds  # the wrong password; unknown names are not logged
    assert sum(1 for k, _ in kinds if k == "coordinator.login_failed") == 1


def test_csrf_is_required(workspace: Path, client: TestClient) -> None:
    # A login form posted without the pre-session token.
    assert client.post("/login", data={"name": "alice", "password": PASSWORD}).status_code == 403
    login(client)
    before = events(workspace)
    assert client.post("/coord/audit-boards", data={"county": "Boulder", "count": "2"}).status_code == 403
    assert client.post("/coord/audit-boards", data={"csrf": "forged", "county": "Boulder", "count": "2"}).status_code == 403
    assert client.post("/coord/fetch", data={"csrf": "forged"}).status_code == 403
    assert upload(client, GOOD_CVR, csrf="forged").status_code == 403
    assert events(workspace) == before
    assert not list(Workspace.open(workspace).iter_objects())


def test_upload_stores_a_passing_cvr_and_rejects_a_failing_one(workspace: Path, client: TestClient) -> None:
    login(client)
    good = upload(client, GOOD_CVR)
    assert good.status_code == 200 and "accepted" in good.text and "PASS" in good.text
    store = Workspace.open(workspace)
    assert [store.get_bytes(sha) for sha in store.iter_objects()] == [GOOD_CVR]
    assert ("file.received", "coordinator:alice") in events(workspace)

    bad = upload(client, BAD_CVR)
    assert bad.status_code == 422 and "rejected" in bad.text and "deleted" in bad.text
    assert BAD_CVR not in all_bytes_under(workspace)
    assert not any((workspace / STAGING_DIRNAME).iterdir())
    assert ("upload.rejected", "coordinator:alice") in events(workspace)
    assert "Fire" not in bad.text  # contest names are not shown

    manifest = upload(client, MANIFEST, kind="manifest")
    assert manifest.status_code == 200
    dashboard = client.get("/").text
    assert "county copy received" in dashboard and "1 rejected" not in dashboard


def test_upload_result_lists_ballot_types_sharing_a_contest_pattern(client: TestClient) -> None:
    login(client)
    shared = make_cvr(ballots(10, "Mayor", ballot_type="03") + ballots(12, "Mayor", ballot_type="11"))
    page = upload(client, shared).text
    assert "Ballot types sharing a contest pattern" in page
    assert "[03, 11] (22 ballots: 10, 12)" in page


def upload_results(client: TestClient, data: bytes, scope: str = "state", note: str = "the fetch was blocked") -> Response:
    csrf = csrf_of(client.get("/coord/upload"))
    response: Response = client.post(
        "/coord/upload",
        data={"csrf": csrf, "kind": "results", "scope": scope, "source_url": "https://results.enr.clarityelections.com/CO/1/", "note": note},
        files={"file": ("detailxml.zip", data, "application/zip")},
    )
    return response


def detected_upload(client: TestClient, files: list[tuple[str, bytes]], **fields: str) -> Response:
    """An upload of files with nothing said about them: no kind, no county."""
    csrf = csrf_of(client.get("/coord/upload"))
    response: Response = client.post("/coord/upload", data={"csrf": csrf, **fields}, files=[("file", (name, data, "application/octet-stream")) for name, data in files])
    return response


BOULDER_CVR = make_cvr(ballots(10, "Mayor", "Council") + ballots(11, "Mayor", precinct="102"), title="2026 Boulder County General Election")


def test_upload_detects_kind_and_county(workspace: Path, client: TestClient) -> None:
    login(client)
    page = client.get("/coord/upload").text
    assert 'name="kind"' not in page and 'name="county"' not in page and "multiple" in page
    both = detected_upload(client, [("export.csv", BOULDER_CVR), ("m.csv", MANIFEST)])
    assert both.status_code == 200, both.text[:2000]
    text = both.text
    assert text.count("accepted") >= 2 and "Redacted CVR" in text and "Ballot manifest" in text and "PASS" in text
    assert "2026 Boulder County General Election" in text and "its County column names Boulder" in text
    with EventLog.open(workspace, read_only=True) as log:
        detected = [e for e in log.iter_events() if e.kind == "upload.detected"]
    assert [(e.county, e.data["kind"], e.data["chosen"]) for e in detected] == [("Boulder", "redacted_cvr", []), ("Boulder", "manifest", [])]
    assert not any((workspace / STAGING_DIRNAME).iterdir())


def test_upload_asks_only_when_it_cannot_tell(workspace: Path, client: TestClient) -> None:
    login(client)
    before = events(workspace)
    asked = detected_upload(client, [("cvr.csv", GOOD_CVR)])  # "2026 Synthetic County General Election": no county named
    assert asked.status_code == 200 and "county not known" in asked.text and 'action="/coord/upload/choose"' in asked.text
    assert events(workspace) == before  # nothing recorded until the choice
    pending = re.search(r'name="pending" value="([0-9a-f]{32})"', asked.text)
    assert pending and '<option value="Boulder">Boulder</option>' in asked.text
    csrf = csrf_of(asked)
    chosen = client.post("/coord/upload/choose", data={"csrf": csrf, "pending": pending.group(1), "county": "Boulder", "action": "choose"})
    assert chosen.status_code == 200 and "accepted" in chosen.text and "(chosen)" in chosen.text
    with EventLog.open(workspace, read_only=True) as log:
        detected = [e for e in log.iter_events() if e.kind == "upload.detected"]
    assert detected[-1].county == "Boulder" and detected[-1].data["chosen"] == ["county"]
    assert not any((workspace / STAGING_DIRNAME).iterdir())
    again = client.post("/coord/upload/choose", data={"csrf": csrf, "pending": pending.group(1), "county": "Boulder", "action": "choose"})
    assert again.status_code == 410
    # Discarded: nothing recorded, nothing kept.
    asked = detected_upload(client, [("notes.txt", b"hello world\n")])
    assert "kind not known" in asked.text
    pending = re.search(r'name="pending" value="([0-9a-f]{32})"', asked.text)
    assert pending
    before = events(workspace)
    gone = client.post("/coord/upload/choose", data={"csrf": csrf_of(asked), "pending": pending.group(1), "action": "discard"})
    assert gone.status_code == 200 and "discarded" in gone.text and events(workspace) == before
    assert not any((workspace / STAGING_DIRNAME).iterdir())
    assert client.post("/coord/upload/choose", data={"csrf": "forged", "pending": pending.group(1), "action": "discard"}).status_code == 403


def test_choosing_for_one_of_several_files_keeps_the_others(workspace: Path, client: TestClient) -> None:
    """Testland's manifest and CVR uploaded together: each waits for its county; choosing one keeps the other, with no warning."""
    from corla_observer import testcounty

    login(client)
    files = testcounty.make().files()
    asked = detected_upload(client, [(testcounty.MANIFEST_NAME, files[testcounty.MANIFEST_NAME]), (testcounty.CVR_NAME, files[testcounty.CVR_NAME])])
    pendings = re.findall(r'name="pending" value="([0-9a-f]{32})"', asked.text)
    assert len(pendings) == 2 and "not a Colorado county" not in asked.text and "a fictional test county" in asked.text
    assert '<option value="Testland">Testland</option>' in asked.text
    chosen = client.post("/coord/upload/choose", data={"csrf": csrf_of(asked), "pending": pendings[0], "county": "Testland", "action": "choose",
                                                       "others": " ".join(pendings[1:])})  # fmt: skip
    assert chosen.status_code == 200 and "accepted" in chosen.text
    assert re.findall(r'name="pending" value="([0-9a-f]{32})"', chosen.text) == [pendings[1]]
    last = client.post("/coord/upload/choose", data={"csrf": csrf_of(chosen), "pending": pendings[1], "county": "Testland", "action": "choose", "others": ""})
    assert last.status_code == 200 and "PASS" in last.text and "Contest patterns" in last.text
    home = client.get("/coord").text
    assert "Test counties (practice only)" in home and "Testland" in home.split("Test counties (practice only)")[1], home[-3000:]


def test_upload_a_results_file(workspace: Path, client: TestClient) -> None:
    from synthetic import clarity_detail

    login(client)
    detail = clarity_detail({"Governor (Vote For 1)": {"A": {"Adams": 6, "San Juan": 2}, "B": {"Adams": 4, "San Juan": 1}}}, {"Adams": 10, "San Juan": 3})
    detected = detected_upload(client, [("detailxml.zip", detail)])
    assert detected.status_code == 200 and "Election night results" in detected.text and "for the state" in detected.text
    assert "ballots cast for 2 counties" in detected.text
    good = upload_results(client, detail)
    assert good.status_code == 200 and "accepted" in good.text and "detailxml.zip" in good.text and "2026-11-04 21:00" in good.text
    assert ("results.uploaded", "coordinator:alice") in events(workspace)
    assert ("totals.checked", "system") in events(workspace)
    bad = upload_results(client, MANIFEST)
    assert bad.status_code == 422 and "not a Clarity results file" in bad.text
    assert ("results.upload_rejected", "coordinator:alice") in events(workspace)
    assert upload_results(client, detail, note="").status_code == 200  # a source URL is enough without a note
    estimates = client.get("/coord/estimates").text
    assert "uploaded by hand by coordinator:alice" in estimates and 'id="totals"' in estimates
    assert not any((workspace / STAGING_DIRNAME).iterdir())


def test_upload_limits(workspace: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    login(client)
    from corla_observer import uploads

    monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", {"redacted_cvr": 100, "manifest": 100, "pull_list": 100})
    response = upload(client, GOOD_CVR)
    assert response.status_code == 422 and "too large" in response.text
    monkeypatch.setattr(coordinator, "MAX_FILE_BYTES", 100)
    response = detected_upload(client, [("big.csv", BOULDER_CVR)])
    assert response.status_code == 422 and "too large" in response.text
    assert not list(Workspace.open(workspace).iter_objects())
    assert not any((workspace / STAGING_DIRNAME).iterdir())
    assert ("upload.rejected", "coordinator:alice") in events(workspace)
    # Anything but an upload is limited to a small body.
    assert client.post("/login", content=b"x" * 100_000, headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 413
    # Files go only through the upload route.
    assert client.post("/coord/audit-boards", files={"file": ("a", b"a")}).status_code in (403, 415)


def test_rate_limits(make_client: Factory) -> None:
    client = make_client(login_limit=Limit(2, 300))
    assert login(client, password="wrong password!").status_code == 401
    assert login(client, password="wrong password!").status_code == 401
    assert login(client).status_code == 429
    limiter = RateLimiter(Limit(1, 10), clock=lambda: 0.0)
    assert limiter.allow("k") and not limiter.allow("k") and limiter.allow("other")


def test_unauthenticated_access_is_refused(client: TestClient) -> None:
    for path in ("/coord", "/coord/upload", "/coord/map"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login"
        assert response.headers["cache-control"] == "no-store"
    assert client.post("/coord/audit-boards", data={"csrf": "x", "county": "Boulder", "count": "2"}).status_code == 403
    assert client.post("/coord/fetch", data={"csrf": "x"}).status_code == 403
    assert upload(client, GOOD_CVR, csrf="x").status_code == 403
    # A forged session cookie is no session.
    client.cookies.set("corla_session", "abc.def")
    assert client.get("/coord", follow_redirects=False).status_code == 303


def test_audit_boards_map_and_fetch(workspace: Path, make_client: Factory) -> None:
    crawled = threading.Event()

    def crawl(ws: Workspace, log: EventLog) -> None:
        crawled.set()

    client = make_client(crawl=crawl)
    login(client)
    home = client.get("/coord")
    csrf = csrf_of(home)
    response = client.post("/coord/audit-boards", data={"csrf": csrf, "county": "boulder", "count": "4", "note": "from the clerk"})
    assert response.status_code == 200 and "Recorded 4 audit boards for Boulder." in response.text
    assert client.post("/coord/audit-boards", data={"csrf": csrf, "county": "Atlantis", "count": "4"}).status_code == 400
    assert client.post("/coord/fetch", data={"csrf": csrf}).status_code == 200
    assert crawled.wait(5)
    again = client.post("/coord/fetch", data={"csrf": csrf})
    assert "less than a minute ago" in again.text or "already running" in again.text
    kinds = events(workspace)
    assert ("audit_boards.recorded", "coordinator:alice") in kinds
    assert kinds.count(("coordinator.fetch_requested", "coordinator:alice")) == 1
    page = client.get("/coord/map")
    assert page.status_code == 200 and "overrides" in page.text and 'data-copy="ai-text"' in page.text


def test_path_prefix(make_client: Factory) -> None:
    client = make_client(path_prefix="/x7Gq2")
    page = client.get("/x7Gq2/")
    assert page.status_code == 200
    assert 'href="/x7Gq2/coord"' in page.text and "/x7Gq2/static/app.js" in page.text
    assert client.get("/").status_code == 404
    assert client.get("/coord", follow_redirects=False).status_code == 404
    assert client.get("/x7Gq2", follow_redirects=False).status_code == 308
    script = client.get("/x7Gq2/static/app.js")
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]
    assert client.get("/x7Gq2/static/").status_code == 404  # no directory listing
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404
    response = login(client, prefix="/x7Gq2")
    assert response.status_code == 303 and response.headers["location"] == "/x7Gq2/coord?msg=logged_in"
    cookie = next(c for c in response.headers.get_list("set-cookie") if c.startswith("corla_session="))
    assert "Path=/x7Gq2" in cookie
    assert client.get("/x7Gq2/coord").status_code == 200


def test_accounts_and_secret(workspace: Path, tmp_path: Path) -> None:
    store = AccountStore(workspace)
    assert store.names() == ["alice"]
    assert store.authenticate("alice", PASSWORD) is not None
    assert store.authenticate("alice", PASSWORD + "x") is None
    assert store.authenticate("bob", PASSWORD) is None
    assert (store.path.stat().st_mode & 0o777) == 0o600
    assert PASSWORD.encode() not in store.path.read_bytes()
    with pytest.raises(AccountError):
        store.add("alice", PASSWORD)
    with pytest.raises(AccountError):
        store.add("bob", "short")
    with pytest.raises(AccountError):
        store.add("bad name", PASSWORD)
    secret_path = tmp_path / "config" / "secret"
    secret = load_secret(secret_path)
    assert len(secret) == 32 and load_secret(secret_path) == secret
    with pytest.raises(SecretError):
        load_secret(workspace / "secret", workspace=workspace)
    secret_path.chmod(0o644)
    with pytest.raises(SecretError):
        load_secret(secret_path)


def test_cli_user_add_and_list(tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / "ws"
    result = runner.invoke(cli_app, ["user", "add", "bob", "--workspace", str(root)], input=f"{PASSWORD}\n{PASSWORD}\n")
    assert result.exit_code == 0, result.output
    assert "added coordinator:bob" in result.output
    assert runner.invoke(cli_app, ["user", "list", "--workspace", str(root)]).output == "coordinator:bob\n"
    assert events(root) == [("account.added", "system")]
    again = runner.invoke(cli_app, ["user", "add", "bob", "--workspace", str(root)], input=f"{PASSWORD}\n{PASSWORD}\n")
    assert again.exit_code == 2


def test_flags_are_for_coordinators_and_can_be_addressed(workspace: Path, make_client: Factory, tmp_path: Path) -> None:
    from corla_observer.datamap import record_audit_time

    store = Workspace.open(workspace)
    source = tmp_path / "manifest.csv"
    source.write_bytes(MANIFEST)
    with EventLog.open(workspace) as log:
        record_audit_time(log, "roll", "2026-01-01 10:00", "coordinator:alice", "test")
        assert receive(store, log, "Boulder", "manifest", source, "manifest.csv", "coordinator:alice").accepted
    client = make_client()
    # The public sees no flags, in the page or the JSON.
    page = client.get("/").text
    assert "changed after roll" not in page and "Change flags" not in page and "open flag" not in page
    data = client.get("/dashboard.json").json()
    assert "flags" not in data and all("flags" not in row for row in data["counties"])
    assert client.get("/coord/flags", follow_redirects=False).status_code == 303
    login(client)
    flags_page = client.get("/coord/flags")
    assert flags_page.status_code == 200 and "Open (1)" in flags_page.text and "changed after roll" in flags_page.text
    assert "Addressed (0)" in flags_page.text
    key = re.search(r'name="key" value="([^"]+)"', flags_page.text)
    assert key
    csrf = csrf_of(flags_page)
    assert client.post("/coord/flags/address", data={"csrf": csrf, "key": key.group(1), "note": ""}).status_code == 400
    assert client.post("/coord/flags/address", data={"csrf": csrf, "key": "nonsense", "note": "x"}).status_code == 400
    done = client.post("/coord/flags/address", data={"csrf": csrf, "key": key.group(1), "note": "the county's copy, received before the roll"})
    assert done.status_code == 200 and "Flag marked addressed." in done.text
    assert "Open (0)" in done.text and "Addressed (1)" in done.text and "the county&#39;s copy, received before the roll" in done.text
    assert ("flag.addressed", "coordinator:alice") in events(workspace)
    home = client.get("/coord").text
    assert "0 open" in home
    reopened = client.post("/coord/flags/reopen", data={"csrf": csrf, "key": key.group(1)})
    assert reopened.status_code == 200 and "Open (1)" in reopened.text
    assert ("flag.reopened", "coordinator:alice") in events(workspace)
    assert client.post("/coord/flags/reopen", data={"csrf": csrf, "key": key.group(1)}).status_code == 400


def test_coordinators_make_list_and_revoke_links(workspace: Path, make_client: Factory) -> None:
    client = make_client(path_prefix="/p1")
    assert client.get("/p1/coord/links", follow_redirects=False).status_code == 303
    login(client, prefix="/p1")
    page = client.get("/p1/coord/links")
    assert page.status_code == 200 and "No links yet." in page.text
    csrf = csrf_of(page)
    bad = client.post("/p1/coord/links", data={"csrf": csrf, "role": "observer", "name": "a b", "county": "Boulder", "board": "1"})
    assert bad.status_code == 400 and "a name is 1 to 64" in bad.text
    assert client.post("/p1/coord/links", data={"role": "observer", "name": "ann", "county": "Boulder", "board": "1"}).status_code == 403
    made = client.post("/p1/coord/links", data={"csrf": csrf, "role": "observer", "name": "ann", "county": "boulder", "board": "2"})
    assert made.status_code == 200
    url = re.search(r'id="new-link">(https://testserver/p1/observe/[A-Za-z0-9_-]+)<', made.text)
    assert url, made.text
    assert "observer:ann" in made.text and "Boulder, 2" in made.text and re.search(r"M[DS]T</td>", made.text)
    reader = client.post("/p1/coord/links", data={"csrf": csrf, "role": "reader", "name": "bob", "county": "Boulder", "board": "2"})
    assert re.search(r'id="new-link">https://testserver/p1/read/', reader.text)
    # The link works, the list shows both links without their addresses, and revoking one ends it.
    token = url.group(1).rsplit("/", 1)[1]
    assert client.get(f"/p1/observe/{token}").status_code == 200
    listed = client.get("/p1/coord/links").text
    assert "Links made (2)" in listed and token not in listed
    digest = re.findall(r'name="id" value="([0-9a-f]{64})"', listed)
    assert len(digest) == 2
    revoked = client.post("/p1/coord/links/revoke", data={"csrf": csrf, "id": digest[0]})
    assert revoked.status_code == 200 and "Link revoked." in revoked.text and "Links made (1)" in revoked.text
    assert client.post("/p1/coord/links/revoke", data={"csrf": csrf, "id": digest[0]}).status_code == 400
    assert ("practice.link_created", "coordinator:alice") in events(workspace)
    assert ("practice.link_revoked", "coordinator:alice") in events(workspace)
    gone = [d for d in digest if d != digest[0]]
    remaining = client.get(f"/p1/observe/{token}")
    assert remaining.status_code == (200 if gone and gone[0] == hashlib.sha256(token.encode()).hexdigest() else 404)
    if remaining.status_code == 404:  # a person holding a revoked link is told what happened, and whom to ask
        assert "This link no longer works" in remaining.text and "coordinator" in remaining.text
    assert "bob (practice reader)" in reader.text and "observer:bob" not in reader.text


def test_link_addresses_follow_the_request() -> None:
    """A link is made with the scheme and host the browser used: http on this machine (a local demo), a proxy's forwarded scheme, else https."""
    from types import SimpleNamespace

    from starlette.requests import Request

    from corla_observer.web.coordinator import site_url

    def request(scheme: str, host: str, forwarded: str = "") -> Request:
        headers = [(b"host", host.encode())] + ([(b"x-forwarded-proto", forwarded.encode())] if forwarded else [])
        return Request({"type": "http", "scheme": scheme, "server": (host, 80), "path": "/", "headers": headers, "query_string": b""})

    state: Any = SimpleNamespace(config=SimpleNamespace(secure_cookies=True), prefix="/p")
    assert site_url(request("http", "127.0.0.1:8765"), state) == "http://127.0.0.1:8765/p"
    assert site_url(request("http", "localhost:8765"), state) == "http://localhost:8765/p"
    assert site_url(request("http", "observe.example.org"), state) == "https://observe.example.org/p"
    assert site_url(request("http", "observe.example.org", "http"), state) == "http://observe.example.org/p"


def test_dashboard_lists_counties_in_observation_and_counties_page_lists_all(workspace: Path, make_client: Factory, tmp_path: Path) -> None:
    from corla_observer.classify import COLORADO_COUNTIES
    from corla_observer.fetch import FetchPolicy, FetchResult, fetch_urls
    from synthetic import ELECTION_URL, election_files

    class Fetcher:
        policy = FetchPolicy()

        def __init__(self, files: dict[str, bytes]) -> None:
            self.files = files

        def fetch(self, url: str, *, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
            return FetchResult(url, url, 200, self.files[url], {})

    upload_status = (
        b"name,filename,type,status,timestamp,computed_hash\r\n"
        b"Boulder,b.csv,manifest,IMPORTED,2026-11-03 09:15:00,AA\r\nBoulder,c.csv,CVRs,IMPORTED,2026-11-03 10:20:00,BB\r\n"
        b"Adams,a.csv,manifest,IMPORTED,2026-11-03 11:00:00,CC\r\nAdams,d.csv,CVRs,IMPORTED,2026-11-03 11:30:00,DD\r\n"
    )
    files = {ELECTION_URL + p: d for p, d in election_files(files_uploadStatus_csv=upload_status).items()}
    store = Workspace.open(workspace)
    source = tmp_path / "good.csv"
    source.write_bytes(GOOD_CVR)
    with EventLog.open(workspace) as log:
        fetch_urls(list(files), workspace=store, log=log, fetcher=Fetcher(files))
        assert receive(store, log, "Boulder", "redacted_cvr", source, "good.csv", "coordinator:alice").accepted
        record_audit_boards(log, "Boulder", 3, "coordinator:alice", placeholder=True)
        log.append("practice.link_created", "coordinator:alice", county="Mesa", board="1", data={"role": "observer", "actor": "observer:ann", "board": 1})
    client = make_client(counties=("Boulder", "Adams"))
    page = client.get("/").text
    assert 'id="county-Boulder"' in page and 'id="county-Mesa"' in page
    assert 'id="county-Adams"' not in page  # uploaded to CORLA, but no redacted CVR to us and no observers
    assert "Before the drawing · manifest and CVR uploaded to CORLA · redacted CVR provided" in page
    assert "Before the drawing · manifest and CVR not yet uploaded to CORLA · redacted CVR not yet provided" in page  # Mesa
    assert "complete" not in page and "not yet known" in page
    assert 'href="/counties"' in page
    assert "open flag" not in page and "Mayor" not in page
    data = client.get("/dashboard.json").json()
    assert [c["county"] for c in data["counties"]] == ["Boulder", "Mesa"]
    assert data["counties"][0]["status"]["corla_manifest"]["time"] == "2026-11-03 09:15:00 MST"

    response = client.get("/counties")
    assert response.status_code == 200 and response.headers["cache-control"].startswith("public")
    counties = response.text
    assert all(f'id="county-{c.replace(" ", "-")}"' in counties for c in COLORADO_COUNTIES)
    assert "San Juan County counts its ballots by hand and does not take part in CORLA" in counties
    adams = counties.split('id="county-Adams"')[1].split("</tr>")[0]
    assert adams.count("imported") == 2 and "not taking part" in adams
    boulder = counties.split('id="county-Boulder"')[1].split("</tr>")[0]
    assert "taking part" in boulder and "not taking part" not in boulder
    assert "flag" not in counties.lower() and "Mayor" not in counties
    assert client.get("/counties", headers={"If-None-Match": response.headers["etag"]}).status_code == 304
    listed = client.get("/counties.json").json()
    assert len(listed["counties"]) == len(COLORADO_COUNTIES) and listed["corla_upload_status"].endswith("uploadStatus.csv")
    assert not any("flags" in c for c in listed["counties"])


def test_version_page(workspace: Path, make_client: Factory) -> None:
    """Every page's footer names the software with its commit and links to /version, which shows the commit and its log entry."""
    from corla_observer.version import current

    identity = current()
    client = make_client()
    dashboard = client.get("/").text
    assert f'<a href="/version">{identity.label}</a>' in dashboard
    page = client.get("/version")
    assert page.status_code == 200 and identity.software in page.text
    if identity.commit:
        assert identity.commit in page.text and f"https://github.com/nealmcb/corla-observer/commit/{identity.commit}" in page.text
        assert ("+dirty" in page.text) == identity.dirty
    data = client.get("/version.json").json()
    assert data["software"] == identity.software and data["commit"] == identity.commit and data["dirty"] == identity.dirty
