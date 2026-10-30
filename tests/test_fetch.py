"""Fast tests for corla_observer.fetch, against a local HTTP server on 127.0.0.1.

The server serves a tiny synthetic Audit Center page and files. Nothing here
touches the network beyond the loopback interface, and the real Secretary
of State site is never contacted: the policy in these tests allows only the
local server, over plain HTTP.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from corla_observer.cli import app
from corla_observer.events import Event, EventLog
from corla_observer.fetch import (
    DEFAULT_ALLOWED_HOSTS,
    CrawlReport,
    FetchFailed,
    Fetcher,
    FetchPolicy,
    FetchRefused,
    FetchTooLarge,
    classified_files,
    crawl,
    extract_links,
    is_followed,
    is_html,
    iter_alerts,
    normalize_html,
)
from corla_observer.workspace import Workspace
from synthetic import SEED, state_audit_report

RLA = "/pubs/elections/RLA/"
START = RLA + "auditCenter.html"


@dataclass
class Route:
    body: bytes = b""
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    #: If set, sent as the ETag, and a matching If-None-Match gets a 304.
    etag: str | None = None
    #: Statuses to answer with first, one per request, before `status`.
    first_statuses: list[int] = field(default_factory=list)
    content_length: bool = True


class Site:
    """Routes for the local server, and a record of the requests it received."""

    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.port = 0

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def url(self, path: str) -> str:
        return f"http://{self.host}{path}"

    def paths_requested(self) -> list[str]:
        return [path for path, _ in self.requests]


def _handler(site: Site) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            site.requests.append((self.path, dict(self.headers)))
            route = site.routes.get(self.path)
            if route is None:
                self.send_response(404)
                self.send_header("Content-Length", "9")
                self.end_headers()
                self.wfile.write(b"not found")
                return
            status = route.first_statuses.pop(0) if route.first_statuses else route.status
            if route.etag and status == 200 and self.headers.get("If-None-Match") == route.etag:
                status = 304
            self.send_response(status)
            for name, value in route.headers.items():
                self.send_header(name, value)
            if route.etag:
                self.send_header("ETag", route.etag)
            body = b"" if status == 304 else route.body
            if route.content_length and status != 304:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


@pytest.fixture
def site() -> Iterator[Site]:
    site = Site()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(site))
    site.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield site
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class FakeClock:
    """A monotonic clock that only moves when slept on."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_fetcher(site: Site, clock: FakeClock | None = None, **overrides: Any) -> Fetcher:
    settings: dict[str, Any] = {
        "allowed_hosts": frozenset({site.host}),
        "allowed_schemes": frozenset({"http"}),
        "min_interval": 0.0,
        "retries": 2,
        "backoff": 1.0,
        "timeout": 5.0,
        "use_env_proxies": False,
    }
    settings.update(overrides)
    clock = clock or FakeClock()
    return Fetcher(FetchPolicy(**settings), sleep=clock.sleep, monotonic=clock.monotonic)


# ---------------------------------------------------------------------------
# The policy


def test_default_policy_allows_only_https_on_the_secretary_of_states_host() -> None:
    policy = FetchPolicy()
    assert DEFAULT_ALLOWED_HOSTS == {"www.coloradosos.gov"}
    url = "https://www.coloradosos.gov/pubs/elections/auditCenter.html#top"
    assert policy.check(url) == url.removesuffix("#top")
    assert policy.check("https://WWW.ColoradoSOS.gov:443/x") == "https://WWW.ColoradoSOS.gov:443/x"
    for refused in [
        "http://www.coloradosos.gov/pubs/elections/auditCenter.html",
        "https://coloradosos.gov/pubs/elections/auditCenter.html",
        "https://www.coloradosos.gov.example.com/",
        "https://www.coloradosos.gov:8443/",
        "https://user:pw@www.coloradosos.gov/",
        "file:///etc/passwd",
        "ftp://www.coloradosos.gov/",
        "/relative/path",
    ]:
        with pytest.raises(FetchRefused):
            policy.check(refused)


def test_policy_with_a_port() -> None:
    policy = FetchPolicy(allowed_hosts=frozenset({"127.0.0.1:8080"}), allowed_schemes=frozenset({"http"}))
    assert policy.check("http://127.0.0.1:8080/a")
    with pytest.raises(FetchRefused):
        policy.check("http://127.0.0.1/a")


def test_refused_url_is_never_requested(site: Site) -> None:
    fetcher = make_fetcher(site)
    with pytest.raises(FetchRefused):
        fetcher.fetch(f"https://{site.host}/x")
    assert site.requests == []


# ---------------------------------------------------------------------------
# Fetching one URL


def test_fetch_returns_body_status_and_headers(site: Site) -> None:
    site.routes["/f.csv"] = Route(
        b"a,b\n", headers={"Content-Type": "text/csv", "Last-Modified": "Mon, 16 Nov 2026 10:00:00 GMT",
                           "X-Other": "ignored"}, etag='"v1"'
    )  # fmt: skip
    result = make_fetcher(site).fetch(site.url("/f.csv"))
    assert result.ok and result.status == 200
    assert result.body == b"a,b\n"
    assert result.size == 4
    assert result.final_url == site.url("/f.csv")
    assert result.headers == {
        "etag": '"v1"',
        "last_modified": "Mon, 16 Nov 2026 10:00:00 GMT",
        "content_type": "text/csv",
        "content_length": "4",
    }
    _, headers = site.requests[0]
    assert headers["User-Agent"].startswith("corla-observer/")
    assert "If-None-Match" not in headers


def test_conditional_fetch_gets_not_modified(site: Site) -> None:
    site.routes["/f.csv"] = Route(b"a,b\n", etag='"v1"')
    result = make_fetcher(site).fetch(
        site.url("/f.csv"), etag='"v1"', last_modified="Mon, 16 Nov 2026 10:00:00 GMT"
    )
    assert result.not_modified
    assert result.body == b""
    _, headers = site.requests[0]
    assert headers["If-None-Match"] == '"v1"'
    assert headers["If-Modified-Since"] == "Mon, 16 Nov 2026 10:00:00 GMT"


def test_http_errors_are_returned_not_raised(site: Site) -> None:
    result = make_fetcher(site).fetch(site.url("/absent.csv"))
    assert (result.status, result.ok, result.body) == (404, False, b"not found")
    assert result.attempts == 1


def test_redirect_on_host_is_followed(site: Site) -> None:
    site.routes["/old.csv"] = Route(status=302, headers={"Location": "/new.csv"})
    site.routes["/new.csv"] = Route(b"new")
    result = make_fetcher(site).fetch(site.url("/old.csv"))
    assert (result.status, result.body) == (200, b"new")
    assert result.final_url == site.url("/new.csv")
    assert result.redirects == (site.url("/new.csv"),)


def test_redirect_off_host_is_refused(site: Site) -> None:
    site.routes["/away.csv"] = Route(status=302, headers={"Location": "https://evil.example/manifest.csv"})
    with pytest.raises(FetchRefused, match="evil.example"):
        make_fetcher(site).fetch(site.url("/away.csv"))
    assert site.paths_requested() == ["/away.csv"]


def test_redirect_loop_is_cut_short(site: Site) -> None:
    site.routes["/a"] = Route(status=302, headers={"Location": "/b"})
    site.routes["/b"] = Route(status=302, headers={"Location": "/a"})
    with pytest.raises(FetchRefused, match="redirects"):
        make_fetcher(site, max_redirects=3).fetch(site.url("/a"))
    assert len(site.requests) == 4


@pytest.mark.parametrize("content_length", [True, False])
def test_size_limit(site: Site, content_length: bool) -> None:
    site.routes["/big.csv"] = Route(b"x" * 1000, content_length=content_length)
    site.routes["/ok.csv"] = Route(b"x" * 100, content_length=content_length)
    fetcher = make_fetcher(site, max_bytes=100)
    with pytest.raises(FetchTooLarge):
        fetcher.fetch(site.url("/big.csv"))
    assert fetcher.fetch(site.url("/ok.csv")).size == 100


def test_transient_errors_are_retried_with_backoff(site: Site) -> None:
    site.routes["/flaky.csv"] = Route(b"finally", first_statuses=[503, 502])
    clock = FakeClock()
    result = make_fetcher(site, clock).fetch(site.url("/flaky.csv"))
    assert (result.status, result.body, result.attempts) == (200, b"finally", 3)
    assert clock.sleeps == [1.0, 2.0]


def test_retry_after_is_honoured_up_to_the_cap(site: Site) -> None:
    site.routes["/slow.csv"] = Route(b"ok", first_statuses=[429], headers={"Retry-After": "500"})
    clock = FakeClock()
    result = make_fetcher(site, clock, max_backoff=7.0).fetch(site.url("/slow.csv"))
    assert result.status == 200
    assert clock.sleeps == [7.0]


def test_persistent_server_errors_return_the_last_response(site: Site) -> None:
    site.routes["/down.csv"] = Route(b"down", status=503)
    result = make_fetcher(site).fetch(site.url("/down.csv"))
    assert (result.status, result.attempts) == (503, 3)


def test_client_errors_are_not_retried(site: Site) -> None:
    site.routes["/gone.csv"] = Route(status=410)
    assert make_fetcher(site).fetch(site.url("/gone.csv")).attempts == 1


def test_network_errors_are_retried_then_raised() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    clock = FakeClock()
    fetcher = Fetcher(
        FetchPolicy(
            allowed_hosts=frozenset({f"127.0.0.1:{port}"}),
            allowed_schemes=frozenset({"http"}),
            retries=2,
            backoff=0.5,
            min_interval=0.0,
            timeout=2.0,
            use_env_proxies=False,
        ),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    with pytest.raises(FetchFailed, match="after 3 attempts"):
        fetcher.fetch(f"http://127.0.0.1:{port}/x")
    assert clock.sleeps == [0.5, 1.0]


def test_requests_are_paced(site: Site) -> None:
    site.routes["/a"] = Route(b"a")
    clock = FakeClock()
    fetcher = make_fetcher(site, clock, min_interval=1.5)
    for _ in range(3):
        fetcher.fetch(site.url("/a"))
    assert clock.sleeps == [1.5, 1.5]


def test_default_rate_is_ten_requests_a_second() -> None:
    assert FetchPolicy().min_interval == 0.1


def test_429_and_503_halve_the_rate_and_retry_after_is_honoured(site: Site) -> None:
    site.routes["/busy"] = Route(b"ok", first_statuses=[429, 503], headers={"Retry-After": "3"})
    clock = FakeClock()
    fetcher = make_fetcher(site, clock, min_interval=0.1)
    result = fetcher.fetch(site.url("/busy"))
    assert result.status == 200 and result.attempts == 3
    assert [s["cause"] for s in fetcher.slowdowns] == ["HTTP 429", "HTTP 503"]
    assert [s["min_interval_ms"] for s in fetcher.slowdowns] == [200, 400]
    assert fetcher.interval == pytest.approx(0.4)
    assert 3.0 in clock.sleeps, "Retry-After is still honoured"


def test_cloudflare_challenge_halves_the_rate(site: Site) -> None:
    challenge = b"<html><head><title>Just a moment...</title></head><body><script>window._cf_chl_opt={}</script></body></html>"
    site.routes["/a"] = Route(challenge, status=403)
    fetcher = make_fetcher(site, FakeClock(), min_interval=0.1, max_interval=0.3)
    for _ in range(3):
        assert fetcher.fetch(site.url("/a")).status == 403
    assert [s["cause"] for s in fetcher.slowdowns] == ["cloudflare_challenge"] * 3
    assert fetcher.interval == pytest.approx(0.3), "capped at max_interval"
    site.routes["/b"] = Route(b"forbidden", status=403)
    fetcher.fetch(site.url("/b"))
    assert len(fetcher.slowdowns) == 3, "an ordinary 403 is not a challenge"


def test_crawl_records_slowdowns(election: Site, store: Store) -> None:
    election.routes[RLA + "2026/general/files/Adams.csv"].first_statuses = [429]
    events, _ = run(election, store)
    slowed = kinds(events, "fetch.slowed")
    assert len(slowed) == 1 and slowed[0].data["status"] == 429
    assert slowed[0].data["url"] == election.url(RLA + "2026/general/files/Adams.csv")


# ---------------------------------------------------------------------------
# HTML


def cloudflare_page(token: str, body: str = "<p>Round 1 files</p>", email: str = "1a2b3c") -> bytes:
    return f"""<!DOCTYPE html>
<html><head><title>Audit Center</title>
<script type="{token}-text/javascript" nonce="n{token}">var x = 1;</script></head>
<body><button onclick="if (!window.__cfRLUnblockHandlers) return false; go()" data-cf-modified-{token}-="">Go</button>
{body}
<a href="/cdn-cgi/l/email-protection#{email}" class="__cf_email__" data-cfemail="{email}">[email&#160;protected]</a>
<script src="/cdn-cgi/scripts/7d0fa10a/cloudflare-static/rocket-loader.min.js" data-cf-settings="{token}-|49" defer=""></script>
<script>(function(){{window['__CF$cv$params']={{r:'{token[:16]}',t:'MTc4'}};}})();</script>
</body></html>""".encode()


def test_normalize_html_strips_cloudflare_tokens_only() -> None:
    a = cloudflare_page("54362e442f4f84375efe9194", email="aa11")
    b = cloudflare_page("61fc872608ef829e8cc7a8dd", email="bb22")
    assert a != b
    assert normalize_html(a) == normalize_html(b)
    normalized = normalize_html(a)
    for token in (
        b"54362e442f4f84375efe9194",
        b"__CF$cv$params",
        b"__cfRLUnblockHandlers",
        b"aa11",
        b"nonce",
    ):
        assert token not in normalized
    assert b'<script type="text/javascript">var x = 1;</script>' in normalized
    assert b"<p>Round 1 files</p>" in normalized
    changed = cloudflare_page("61fc872608ef829e8cc7a8dd", body="<p>Round 2 files</p>")
    assert normalize_html(changed) != normalize_html(a)


def test_is_html() -> None:
    assert is_html(b"anything", "text/html; charset=utf-8")
    assert is_html(b"\xef\xbb\xbf\n<!DOCTYPE html><html></html>")
    assert not is_html(b"County,Tabulator\n", "text/csv")


def test_extract_links() -> None:
    page = b"""<html><head><base href="https://h.example/pubs/elections/RLA/"></head><body>
    <a href="2026/files/Adams.csv">a</a> <a href="2026/files/Adams.csv#x">again</a>
    <a href="/pubs/elections/RLA/2026/riskLimit.html">r</a> <img src="2026/dice.png">
    <a href="mailto:someone@example.com">m</a> <a href="javascript:void(0)">j</a> <a>none</a>
    <iframe src="https://other.example/frame.html"></iframe> <a href="2026/Garc&iacute;a.csv">e</a>
    </body></html>"""
    assert extract_links(page, "https://h.example/pubs/elections/auditCenter.html") == [
        "https://h.example/pubs/elections/RLA/2026/files/Adams.csv",
        "https://h.example/pubs/elections/RLA/2026/riskLimit.html",
        "https://h.example/pubs/elections/RLA/2026/dice.png",
        "https://other.example/frame.html",
        "https://h.example/pubs/elections/RLA/2026/García.csv",
    ]


def test_default_start_page_with_relative_rla_links(site: Site, store: Store) -> None:
    """As on the live site: the page outside RLA/, linking its files as RLA/...; only those are followed."""
    start = "/pubs/elections/auditCenter.html"
    site.routes[start] = Route(
        audit_center(
            "54362e442f4f84375efe9194",
            [
                "RLA/2026/general/files/Adams.csv",
                "RLA/2026/general/riskLimit.html",
                "main.html",
                "/pubs/info_center/contact.html",
                "https://other.example/RLA/x.csv",
                "RLA/../other/escape.csv",
            ],
        )
    )
    site.routes[RLA + "2026/general/files/Adams.csv"] = Route(ADAMS)
    site.routes[RLA + "2026/general/riskLimit.html"] = Route(b"<html><body>risk</body></html>")
    for path in (
        "/pubs/elections/main.html",
        "/pubs/info_center/contact.html",
        "/pubs/elections/other/escape.csv",
    ):
        site.routes[path] = Route(b"must not be fetched")
    report = crawl(site.url(start), workspace=store.workspace, log=store.log, fetcher=make_fetcher(site))
    assert not report.start_failed
    assert sorted(site.paths_requested()) == sorted(
        [start, RLA + "2026/general/files/Adams.csv", RLA + "2026/general/riskLimit.html"]
    )
    assert {e.url: e.kind for e in report.entries if e.outcome == "new"}[
        site.url(RLA + "2026/general/files/Adams.csv")
    ] == "county_manifest"
    assert sorted(report.skipped_links) == sorted(
        [
            site.url("/pubs/elections/main.html"),
            site.url("/pubs/info_center/contact.html"),
            "https://other.example/RLA/x.csv",
            site.url("/pubs/elections/other/escape.csv"),
            site.url("/cdn-cgi/l/email-protection"),
        ]
    )


def test_default_start_url() -> None:
    from corla_observer.fetch import DEFAULT_START_URL

    assert DEFAULT_START_URL == "https://www.coloradosos.gov/pubs/elections/auditCenter.html"
    assert FetchPolicy().check(DEFAULT_START_URL) == DEFAULT_START_URL
    assert not is_followed(DEFAULT_START_URL, FetchPolicy())  # in scope only as the start page
    assert is_followed(
        "https://www.coloradosos.gov/pubs/elections/RLA/2026/primary/riskLimit.html", FetchPolicy()
    )


def test_is_followed() -> None:
    policy = FetchPolicy()
    assert is_followed("https://www.coloradosos.gov/pubs/elections/RLA/2026/files/Adams.csv", policy)
    assert is_followed("https://www.coloradosos.gov/pubs/elections/rla/faqs.html", policy)
    assert not is_followed("https://www.coloradosos.gov/pubs/elections/main.html", policy)
    assert not is_followed("http://www.coloradosos.gov/pubs/elections/RLA/x.csv", policy)
    assert not is_followed("https://coloradosos.gov/pubs/elections/RLA/x.csv", policy)
    assert is_followed("https://www.coloradosos.gov/pubs/x.csv", policy, path_prefix="/pubs/")


# ---------------------------------------------------------------------------
# Crawling

MANIFEST_HEADER = b"County,Tabulator ID,Batch,# of Ballot Cards,Location\r\n"
ADAMS = MANIFEST_HEADER + b"Adams,101,1,100,Box 1\r\nAdams,101,2,98,Box 1\r\n"
BOULDER = MANIFEST_HEADER + b"Boulder,1,1,50,Bin 1\r\n"
CONTESTS = b"county_id,county_name,contest_name,contest_id\r\n1,Adams,Mayor,10\r\n7,Boulder,Mayor,11\r\n"


def audit_center(token: str, links: list[str]) -> bytes:
    anchors = "".join(f'<li><a href="{link}">{link}</a></li>' for link in links)
    return cloudflare_page(token, body=f"<ul>{anchors}</ul>")


DEFAULT_LINKS = [
    "2026/general/files/Adams.csv",
    "2026/general/files/Boulder.csv",
    "2026/general/finalReports/ContestIDsByCounty.csv",
    "2026/general/finalReports/StateAuditReport.xlsx",
    "2026/general/riskLimit.html",
    "https://other.example/elsewhere.csv",
    "/pubs/elections/other.html",
    "mailto:public.elections@example.com",
]


@pytest.fixture
def election(site: Site) -> Site:
    site.routes[START] = Route(audit_center("54362e442f4f84375efe9194", DEFAULT_LINKS),
                               headers={"Content-Type": "text/html; charset=utf-8"})  # fmt: skip
    site.routes[RLA + "2026/general/files/Adams.csv"] = Route(ADAMS, etag='"adams-1"')
    site.routes[RLA + "2026/general/files/Boulder.csv"] = Route(BOULDER, etag='"boulder-1"')
    site.routes[RLA + "2026/general/finalReports/ContestIDsByCounty.csv"] = Route(CONTESTS)
    site.routes[RLA + "2026/general/finalReports/StateAuditReport.xlsx"] = Route(state_audit_report())
    site.routes[RLA + "2026/general/riskLimit.html"] = Route(
        b"<!DOCTYPE html><html><head><title>Risk limit</title></head><body>"
        b'<a href="files/targetContests.csv">targets</a></body></html>'
    )
    site.routes[RLA + "2026/general/files/targetContests.csv"] = Route(
        b"County,Contest,Diluted Margin,Risk Limit\r\nAdams,Mayor,0.1,0.03\r\n"
    )
    return site


@dataclass
class Store:
    workspace: Workspace
    log: EventLog


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    log = EventLog.open(tmp_path / "log")
    try:
        yield Store(Workspace.open(tmp_path / "ws"), log)
    finally:
        log.close()


def run(site: Site, store: Store, **kwargs: Any) -> tuple[list[Event], CrawlReport]:
    """Crawl the local site; return the events this crawl appended, and the report."""
    before = len(store.log)
    report = crawl(
        site.url(START), workspace=store.workspace, log=store.log, fetcher=make_fetcher(site), **kwargs
    )
    return list(store.log.iter_events(after_seq=before)), report


def kinds(events: list[Event], kind: str) -> list[Event]:
    return [e for e in events if e.kind == kind]


def by_url(events: list[Event], kind: str) -> dict[str, Event]:
    return {str(e.data["url"]): e for e in kinds(events, kind)}


def test_first_crawl_fetches_stores_classifies_and_logs(election: Site, store: Store) -> None:
    events, report = run(election, store)
    assert events[0].kind == "fetch.crawl_started"
    assert events[-1].kind == "fetch.crawl_finished"
    assert {e.actor for e in events} == {"system"}
    files = by_url(events, "fetch.file")
    expected = (
        {election.url(START)}
        | {election.url(RLA + link) for link in DEFAULT_LINKS[:5]}
        | {election.url(RLA + "2026/general/files/targetContests.csv")}
    )
    assert set(files) == expected
    adams = files[election.url(RLA + "2026/general/files/Adams.csv")]
    assert adams.file_sha256 == adams.data["sha256"]
    assert store.workspace.get_bytes(str(adams.data["sha256"])) == ADAMS
    assert adams.data["headers"] == {"etag": '"adams-1"', "content_length": str(len(ADAMS))}
    assert adams.data["linked_from"] == election.url(START)
    assert adams.data["depth"] == 1
    assert "normalized_sha256" not in adams.data
    page = files[election.url(START)]
    assert page.data["linked_from"] is None
    assert page.data["normalized_sha256"] != page.data["sha256"]
    assert set(by_url(events, "fetch.new")) == expected
    assert kinds(events, "fetch.changed") == kinds(events, "fetch.unchanged") == []

    classified = {
        url: (e.data["kind"], e.data["county"]) for url, e in by_url(events, "fetch.classified").items()
    }
    assert classified == {
        election.url(START): ("html_page", None),
        election.url(RLA + "2026/general/files/Adams.csv"): ("county_manifest", "Adams"),
        election.url(RLA + "2026/general/files/Boulder.csv"): ("county_manifest", "Boulder"),
        election.url(RLA + "2026/general/finalReports/ContestIDsByCounty.csv"): (
            "contest_ids_by_county",
            None,
        ),
        election.url(RLA + "2026/general/finalReports/StateAuditReport.xlsx"): ("state_audit_report", None),
        election.url(RLA + "2026/general/riskLimit.html"): ("html_page", None),
        election.url(RLA + "2026/general/files/targetContests.csv"): ("target_contests", "Adams"),
    }
    report_event = by_url(events, "fetch.classified")[
        election.url(RLA + "2026/general/finalReports/StateAuditReport.xlsx")
    ]
    evidence = report_event.data["evidence"]
    assert isinstance(evidence, dict) and evidence["audit_random_seed"] == SEED
    assert (
        by_url(events, "fetch.classified")[election.url(RLA + "2026/general/files/Adams.csv")].county
        == "Adams"
    )

    assert report.skipped_links == sorted(
        [
            "https://other.example/elsewhere.csv",
            election.url("/pubs/elections/other.html"),
            election.url("/cdn-cgi/l/email-protection"),
        ]
    )
    assert kinds(events, "fetch.alert") == []
    assert store.log.verify().ok
    mapping = classified_files(store.log)
    assert mapping[election.url(RLA + "2026/general/files/Boulder.csv")]["county"] == "Boulder"


def test_unchanged_crawl_ignores_cloudflare_tokens(election: Site, store: Store) -> None:
    run(election, store)
    election.routes[START].body = audit_center("61fc872608ef829e8cc7a8dd", DEFAULT_LINKS)
    events, report = run(election, store)
    assert kinds(events, "fetch.changed") == []
    unchanged = by_url(events, "fetch.unchanged")
    assert len(unchanged) == 7
    page = unchanged[election.url(START)]
    assert page.data["compared"] == "normalized_html"
    assert page.data["sha256"] != page.data["previous_sha256"]
    assert unchanged[election.url(RLA + "2026/general/files/Adams.csv")].data["compared"] == "bytes"
    # The page's new raw bytes are stored and fetched as usual, but not reclassified.
    assert len(kinds(events, "fetch.file")) == 7
    assert kinds(events, "fetch.classified") == []
    assert kinds(events, "fetch.alert") == []
    # Re-fetched in full by default: no conditional headers sent.
    assert all("If-None-Match" not in headers for _, headers in election.requests)


def test_changed_file_is_recorded_alerted_and_reclassified(election: Site, store: Store) -> None:
    run(election, store)
    path = RLA + "2026/general/files/Adams.csv"
    election.routes[path].body = ADAMS + b"Adams,101,3,12,Box 2\r\n"
    events, _ = run(election, store)
    changed = by_url(events, "fetch.changed")
    assert list(changed) == [election.url(path)]
    assert changed[election.url(path)].data["previous_sha256"] != changed[election.url(path)].data["sha256"]
    alerts = kinds(events, "fetch.alert")
    assert [(a.data["reason"], a.data["url"]) for a in alerts] == [("changed", election.url(path))]
    assert list(by_url(events, "fetch.classified")) == [election.url(path)]


def test_changed_page_is_recorded_but_not_alerted(election: Site, store: Store) -> None:
    run(election, store)
    election.routes[START].body = audit_center("61fc872608ef829e8cc7a8dd", DEFAULT_LINKS + ["#new"])
    election.routes[START].body = election.routes[START].body.replace(b"<ul>", b"<p>Round 2</p><ul>")
    events, _ = run(election, store)
    assert list(by_url(events, "fetch.changed")) == [election.url(START)]
    assert kinds(events, "fetch.alert") == []


def test_unlinked_file_is_missing_once(election: Site, store: Store) -> None:
    run(election, store)
    links = [link for link in DEFAULT_LINKS if "Boulder" not in link]
    election.routes[START].body = audit_center("61fc872608ef829e8cc7a8dd", links)
    events, report = run(election, store)
    boulder = election.url(RLA + "2026/general/files/Boulder.csv")
    missing = by_url(events, "fetch.missing")
    assert list(missing) == [boulder]
    assert missing[boulder].data["reason"] == "no longer linked"
    assert [(a.data["reason"], a.data["url"]) for a in kinds(events, "fetch.alert")] == [("missing", boulder)]
    assert report.count("missing") == 1
    # Still missing next time: not recorded again.
    events, _ = run(election, store)
    assert kinds(events, "fetch.missing") == []
    assert kinds(events, "fetch.alert") == []
    # Linked again: fetched and compared with what it was before it went missing.
    election.routes[START].body = audit_center("0123456789abcdef01234567", DEFAULT_LINKS)
    events, _ = run(election, store)
    assert list(by_url(events, "fetch.unchanged")).count(boulder) == 1


def test_failing_file_is_missing(election: Site, store: Store) -> None:
    run(election, store)
    path = RLA + "2026/general/finalReports/ContestIDsByCounty.csv"
    del election.routes[path]
    events, _ = run(election, store)
    missing = by_url(events, "fetch.missing")
    assert list(missing) == [election.url(path)]
    assert (missing[election.url(path)].data["reason"], missing[election.url(path)].data["status"]) == (
        "HTTP 404",
        404,
    )
    assert [a.data["reason"] for a in kinds(events, "fetch.alert")] == ["missing"]


def test_newly_linked_url_that_fails_is_missing_but_not_alerted(election: Site, store: Store) -> None:
    election.routes[START].body = audit_center(
        "54362e442f4f84375efe9194", DEFAULT_LINKS + ["2026/absent.csv"]
    )
    events, _ = run(election, store)
    assert list(by_url(events, "fetch.missing")) == [election.url(RLA + "2026/absent.csv")]
    assert kinds(events, "fetch.alert") == []


def test_redirect_off_host_in_a_crawl_is_missing(election: Site, store: Store) -> None:
    path = RLA + "2026/general/files/Boulder.csv"
    election.routes[path] = Route(status=302, headers={"Location": "https://evil.example/Boulder.csv"})
    events, _ = run(election, store)
    missing = by_url(events, "fetch.missing")[election.url(path)]
    assert str(missing.data["reason"]).startswith("FetchRefused")


def test_duplicate_content_is_alerted_once(election: Site, store: Store) -> None:
    election.routes[RLA + "2026/general/files/Boulder.csv"].body = ADAMS
    events, _ = run(election, store)
    alerts = kinds(events, "fetch.alert")
    assert sorted(str(a.data["reason"]) for a in alerts) == ["duplicate_content", "manifest_under_other_name"]
    other = next(a for a in alerts if a.data["reason"] == "manifest_under_other_name")
    assert other.data["severity"] == "high" and other.data["counties"] == ["Boulder", "Adams"]
    alert = next(a for a in alerts if a.data["reason"] == "duplicate_content")
    assert alert.data["severity"] == "high"
    assert alert.data["compared"] == "bytes"
    assert alert.data["urls"] == sorted(
        [
            election.url(RLA + "2026/general/files/Adams.csv"),
            election.url(RLA + "2026/general/files/Boulder.csv"),
        ]
    )
    assert alert.file_sha256 == alert.data["content_sha256"]
    assert kinds(events, "fetch.crawl_finished")[0].data["duplicates"] == [
        {"sha256": alert.data["content_sha256"], "urls": alert.data["urls"]}
    ]
    # Unchanged next crawl: no new alert. Fixed, then broken again: alerted again.
    events, _ = run(election, store)
    assert kinds(events, "fetch.alert") == []
    election.routes[RLA + "2026/general/files/Boulder.csv"].body = BOULDER
    events, _ = run(election, store)
    assert [a.data["reason"] for a in kinds(events, "fetch.alert")] == ["changed"]
    election.routes[RLA + "2026/general/files/Boulder.csv"].body = ADAMS
    events, _ = run(election, store)
    assert sorted(str(a.data["reason"]) for a in kinds(events, "fetch.alert")) == [
        "changed",
        "duplicate_content",
        "manifest_under_other_name",
    ]
    assert [a["reason"] for a in iter_alerts(store.log) if a["reason"] != "manifest_under_other_name"] == [
        "duplicate_content", "changed", "changed", "duplicate_content",
    ]  # fmt: skip


def test_conditional_crawl_skips_unchanged_downloads(election: Site, store: Store) -> None:
    run(election, store)
    election.requests.clear()
    events, report = run(election, store, conditional=True)
    not_modified = by_url(events, "fetch.unchanged")
    adams = election.url(RLA + "2026/general/files/Adams.csv")
    assert not_modified[adams].data["not_modified"] is True
    assert not_modified[adams].data["status"] == 304
    assert adams not in by_url(events, "fetch.file")
    sent = dict(election.requests)
    assert sent[RLA + "2026/general/files/Adams.csv"]["If-None-Match"] == '"adams-1"'
    assert "If-None-Match" not in sent[RLA + "2026/general/finalReports/ContestIDsByCounty.csv"]
    assert report.count("not_modified") == 2
    # A changed ETag means a real fetch.
    election.routes[RLA + "2026/general/files/Adams.csv"] = Route(
        ADAMS + b"Adams,1,9,1,B\r\n", etag='"adams-2"'
    )
    events, _ = run(election, store, conditional=True)
    assert adams in by_url(events, "fetch.changed")


def test_failed_start_page_does_not_mark_everything_missing(election: Site, store: Store) -> None:
    run(election, store)
    election.routes[START] = Route(status=500)
    events, report = run(election, store)
    assert report.start_failed
    assert list(by_url(events, "fetch.missing")) == [election.url(START)]
    assert kinds(events, "fetch.alert") == []
    assert kinds(events, "fetch.crawl_finished")[0].data["start_failed"] is True


def test_max_depth_and_max_urls(election: Site, store: Store) -> None:
    events, _ = run(election, store, max_depth=1)
    assert election.url(RLA + "2026/general/files/targetContests.csv") not in by_url(events, "fetch.file")
    _, report = run(election, store, max_urls=3)
    assert report.truncated


def test_cli_fetch_refuses_plain_http_without_network(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "fetch",
            "http://www.coloradosos.gov/pubs/elections/auditCenter.html",
            "--workspace",
            str(tmp_path / "ws"),
            "--rate",
            "5",
        ],
    )
    assert result.exit_code == 1
    with EventLog.open(tmp_path / "ws", read_only=True) as log:
        missing = [e for e in log.iter_events() if e.kind == "fetch.missing"]
    assert len(missing) == 1
    assert str(missing[0].data["reason"]).startswith("FetchRefused")


REJECTED = (
    b"<html><head><title>The Web Request Was Rejected</title></head><body>We are sorry for the inconvenience, "
    b"but the requested URL was rejected due to a possible web violation.<br>Your support ID is: 1234</body></html>"
)


def test_a_firewall_refusal_with_status_200_is_not_taken_for_the_page(election: Site, store: Store) -> None:
    run(election, store)
    election.routes[START] = Route(REJECTED, headers={"Content-Type": "text/html"})
    election.routes[RLA + "2026/general/files/Adams.csv"] = Route(REJECTED)
    events, report = run(election, store)
    kinds = [e.kind for e in events]
    assert "fetch.blocked" in kinds and "fetch.missing" not in kinds
    assert report.start_failed and report.count("blocked") == 1
    blocked = next(e for e in events if e.kind == "fetch.blocked")
    assert blocked.data["status"] == 200 and store.workspace.get_bytes(str(blocked.data["sha256"])) == REJECTED
    # A file refused the same way keeps its last content.
    from corla_observer.fetch import is_blocked

    assert is_blocked(REJECTED) and not is_blocked(ADAMS)


def test_probe_urls_are_fetched_and_missing_ones_recorded_once(election: Site, store: Store) -> None:
    probe = election.url(RLA + "2026/general/files/uploadStatus.csv")
    absent = election.url(RLA + "2026/general/finalReports/ContestsListRound1.csv")
    election.routes[RLA + "2026/general/files/uploadStatus.csv"] = Route(b"x,y\r\n1,2\r\n")
    events, report = run(election, store, extra_urls=[probe, absent])
    assert probe in by_url(events, "fetch.file") and absent in by_url(events, "fetch.missing")
    assert by_url(events, "fetch.file")[probe].data["linked_from"] is None
    events, _ = run(election, store, extra_urls=[probe, absent])
    assert absent not in by_url(events, "fetch.missing")
    # A probe no longer requested is not reported as no longer linked.
    events, _ = run(election, store)
    assert probe not in by_url(events, "fetch.missing")
