"""Fetch the Secretary of State's Audit Center and every file it links to, recording each fetch.

Two layers:

- `Fetcher.fetch` gets one URL, carefully: HTTPS only, hosts on an
  allow-list (``www.coloradosos.gov`` by default), redirects only to allowed
  URLs, a size limit, timeouts, a few retries with exponential backoff on
  network errors and on HTTP 408, 425, 429 and 5xx (honouring
  Retry-After), a descriptive User-Agent, and pacing between requests (10
  a second by default). When the server answers 429 or 503, or with a
  Cloudflare challenge page, it halves its rate (`Fetcher.slowdowns`). It returns a
  `FetchResult` with the bytes, status, final URL and the headers of
  interest (ETag, Last-Modified, Content-Type, Content-Length).
- `crawl` starts from the Audit Center page, follows links to files and
  sub-pages on an allowed host under the RLA path, fetches each, stores
  every body unmodified in the `Workspace`, and records what happened in
  the `EventLog`.

Events written by `crawl` (actor ``system``; ``data`` always has ``url``):

``fetch.crawl_started``, ``fetch.crawl_finished``
    The crawl's settings, and its counts and duplicate groups.
``fetch.file``
    A successful (2xx) fetch: ``sha256``, ``size``, ``status``,
    ``final_url``, ``headers``, ``linked_from``, ``depth``, ``attempts``;
    for HTML also ``normalized_sha256`` (see `normalize_html`). The event's
    ``file_sha256`` is the stored body's SHA-256.
``fetch.new``, ``fetch.changed``, ``fetch.unchanged``
    The comparison with the previous fetch of the same URL in the log. HTML
    is compared by ``normalized_sha256``, everything else byte for byte.
    A ``304 Not Modified`` answer to a conditional request is recorded as
    ``fetch.unchanged`` with ``not_modified`` true and no ``fetch.file``.
``fetch.missing``
    A linked URL that now fails (an HTTP error, a network error or a
    refused redirect), or a URL linked before that is no longer linked.
    Recorded once when it goes missing, not again on every crawl while it
    stays missing.
``fetch.classified``
    The `corla_observer.classify` result (``kind``, ``county``,
    ``evidence``) whenever a URL's content is new or changed (for HTML, its
    normalized content), and for any fetched URL not yet classified. The latest per URL is the
    election's mapping of what was found where; see `classified_files`.
``fetch.alert``
    Something a person should look at, with a ``reason`` and a
    ``severity`` (``high`` or ``normal``; see `corla_observer.flags`):
    ``changed`` (a previously fetched file, not a page, has new content),
    ``missing`` (a previously fetched file has gone),
    ``changed_after_roll`` (a manifest, CVR, target contest list, contest
    list or county-ID file new, changed or removed after the roll time the
    log knows of, with ``change``, ``time``, ``roll_time`` and both hashes),
    ``changed_after_round_start`` (any file changed or removed after the
    first round started), ``manifest_under_other_name`` (a manifest whose
    content names another county than its file name), or
    ``duplicate_content`` (two or more URLs serve identical content, as
    when one county's manifest was served under other counties' names;
    high severity when a manifest or CVR is among them).
    A duplicate group is alerted when it first appears, not again while it
    persists unchanged from one crawl to the next.
``fetch.blocked``
    A 2xx answer that is a firewall's refusal or challenge page, not the
    file (`is_blocked`): ``status``, ``sha256`` and ``size`` of the page,
    which is stored as evidence. The URL's last content stands, and it is
    not reported missing.
``fetch.slowed``
    The fetcher halved its request rate after a 429, a 503 or a Cloudflare
    challenge page: ``url``, ``status``, ``cause``, ``previous_min_interval_ms``
    and ``min_interval_ms``.

`fetch_urls` fetches a fixed list of URLs instead, recording the same
events; a rehearsal uses it to replay the audit center as a mirror held it
(`corla_observer.rehearse`), and then every event's data carries
``simulated``, saying where the content really came from.

By default every file is fetched in full on every crawl, because a fetcher
that skips files it already has will miss corrected files. With
``conditional=True``, a URL whose previous fetch returned an ETag or
Last-Modified is requested conditionally instead, and a ``304`` costs no
download.

`Fetcher` keeps per-instance state for pacing and is not thread-safe.

Standard library only.
"""

from __future__ import annotations

import hashlib
import http.client
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from email.message import Message
from html.parser import HTMLParser
from typing import IO, Any, Protocol

from corla_observer import __version__
from corla_observer.classify import classify_bytes, county_from_filename
from corla_observer.datamap import audit_times, election_from_url
from corla_observer.events import Event, EventLog, JSONValue
from corla_observer.flags import WATCHED_KINDS, change_rule
from corla_observer.workspace import Workspace

#: The Audit Center page.
DEFAULT_START_URL = "https://www.coloradosos.gov/pubs/elections/auditCenter.html"
#: Hosts fetched from by default.
DEFAULT_ALLOWED_HOSTS = frozenset({"www.coloradosos.gov"})
#: Links are followed only to paths under this prefix (case-insensitive).
DEFAULT_PATH_PREFIX = "/pubs/elections/RLA/"
USER_AGENT = (
    f"corla-observer/{__version__} (independent public observation of Colorado's "
    "risk-limiting audits; polite, low-rate fetcher)"
)

ACTOR = "system"

#: HTTP statuses retried as transient.
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Response headers recorded, under these keys.
HEADERS_OF_INTEREST = {
    "ETag": "etag",
    "Last-Modified": "last_modified",
    "Content-Type": "content_type",
    "Content-Length": "content_length",
}
_MAX_HEADER_LENGTH = 1024
_CHUNK = 1024 * 1024


class FetchError(Exception):
    """A URL could not be fetched."""


class FetchRefused(FetchError):
    """The URL, or a redirect, is not allowed by the policy; nothing was (or will be) requested from it."""


class FetchTooLarge(FetchError):
    """The response is larger than the policy's size limit."""


class FetchFailed(FetchError):
    """Network errors persisted through every retry."""


@dataclass(frozen=True)
class FetchPolicy:
    """What may be fetched, and how politely.

    ``allowed_hosts`` entries are host names, matched case-insensitively on
    the scheme's default port, or ``host:port`` for any other port.
    """

    allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS
    allowed_schemes: frozenset[str] = frozenset({"https"})
    #: Largest response body accepted, in bytes.
    max_bytes: int = 256 * 1024 * 1024
    #: Socket timeout per request, in seconds.
    timeout: float = 60.0
    #: Further attempts after the first, for transient failures.
    retries: int = 3
    #: Wait before the first retry, doubled for each further retry.
    backoff: float = 2.0
    #: Longest wait before a retry, including a server's Retry-After.
    max_backoff: float = 60.0
    #: Least time between the starts of two requests, in seconds (10 requests a second).
    min_interval: float = 0.1
    #: Longest interval automatic slowing down may reach, in seconds.
    max_interval: float = 30.0
    max_redirects: int = 5
    user_agent: str = USER_AGENT
    #: Whether to use proxies from the environment (``https_proxy`` etc.).
    use_env_proxies: bool = True

    def check(self, url: str) -> str:
        """Return ``url`` without its fragment if it is allowed; raise `FetchRefused` otherwise."""
        parts = urllib.parse.urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in self.allowed_schemes:
            raise FetchRefused(f"scheme {parts.scheme or '(none)'!r} not allowed: {url}")
        if parts.username is not None or parts.password is not None:
            raise FetchRefused(f"credentials in URL not allowed: {url}")
        host = (parts.hostname or "").lower()
        try:
            port = parts.port
        except ValueError:
            raise FetchRefused(f"bad port in URL: {url}") from None
        allowed = {h.lower() for h in self.allowed_hosts}
        key = host if port is None or port == _DEFAULT_PORTS.get(scheme) else f"{host}:{port}"
        if not host or key not in allowed:
            raise FetchRefused(f"host {key or '(none)'!r} not on the allow-list: {url}")
        return urllib.parse.urlunsplit(parts._replace(fragment=""))


_DEFAULT_PORTS = {"https": 443, "http": 80}

#: Statuses after which the fetcher halves its rate.
SLOW_DOWN_STATUSES = frozenset({429, 503})
_CHALLENGE_MARKERS = (b"_cf_chl_opt", b"/cdn-cgi/challenge-platform/", b"cf-browser-verification", b"<title>Just a moment...")


def is_challenge(status: int, body: bytes) -> bool:
    """Whether a response is a Cloudflare challenge page instead of the content asked for."""
    head = body[:65536]
    return status in (403, 429, 503) and any(marker in head for marker in _CHALLENGE_MARKERS)


#: Markers of a page that stands in for the content when a firewall or a
#: content delivery network refuses a request. Such a page can be served
#: with status 200 -- a web application firewall's "The Web Request Was
#: Rejected", or a Cloudflare challenge -- so a 2xx answer is not by itself
#: evidence that the file was served.
_BLOCK_MARKERS = (b"<title>The Web Request Was Rejected</title>", b"<title>Just a moment...</title>", b"_cf_chl_opt")


def is_blocked(body: bytes) -> bool:
    """Whether a 2xx response is a firewall's refusal or challenge page instead of the file asked for.

    Such a page is never taken for the file: `crawl` records it as
    ``fetch.blocked`` and neither replaces the file's last content nor
    reports it missing (and a refused start page stops the crawl from
    reporting files as no longer linked).
    """
    head = body[:8192]
    return len(body) < 65536 and any(marker in head for marker in _BLOCK_MARKERS)


@dataclass(frozen=True)
class FetchResult:
    """One HTTP response (after redirects and retries)."""

    url: str
    final_url: str
    status: int
    body: bytes
    #: The headers of interest that were present, keyed as in `HEADERS_OF_INTEREST`.
    headers: dict[str, str]
    attempts: int = 1
    #: Each URL redirected to, in order.
    redirects: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    @property
    def size(self) -> int:
        return len(self.body)


class _PolicyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to a URL the policy allows, and remember the chain."""

    def __init__(self, policy: FetchPolicy) -> None:
        super().__init__()
        self.policy = policy
        self.chain: list[str] = []

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        if len(self.chain) >= self.policy.max_redirects:
            raise FetchRefused(f"{req.full_url}: more than {self.policy.max_redirects} redirects")
        target = self.policy.check(urllib.parse.urljoin(req.full_url, newurl))
        self.chain.append(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


def _interesting_headers(headers: Message | None) -> dict[str, str]:
    result: dict[str, str] = {}
    if headers is None:
        return result
    for name, key in HEADERS_OF_INTEREST.items():
        value = headers.get(name)
        if value is not None:
            result[key] = str(value)[:_MAX_HEADER_LENGTH]
    return result


def _retry_after(headers: Message | None) -> float | None:
    value = headers.get("Retry-After") if headers is not None else None
    if value is None:
        return None
    try:
        return max(0.0, float(int(str(value).strip())))
    except ValueError:
        return None


class Fetcher:
    """Fetches URLs under a `FetchPolicy`, pacing requests.

    ``sleep`` and ``monotonic`` are injectable for tests.
    """

    def __init__(
        self,
        policy: FetchPolicy | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = policy or FetchPolicy()
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_start: float | None = None
        #: The current least time between requests; doubled by each slowdown.
        self.interval = self.policy.min_interval
        #: Seconds slept for pacing and backoff, in order (for tests and logs).
        self.waits: list[float] = []
        #: Each time the rate was halved: ``url``, ``status``, ``cause`` and the intervals, in ms.
        self.slowdowns: list[dict[str, JSONValue]] = []

    def _wait(self, seconds: float) -> None:
        if seconds > 0:
            self.waits.append(seconds)
            self._sleep(seconds)

    def _pace(self) -> None:
        if self._last_start is not None:
            self._wait(self._last_start + self.interval - self._monotonic())
        self._last_start = self._monotonic()

    def _opener(self) -> tuple[urllib.request.OpenerDirector, _PolicyRedirectHandler]:
        redirects = _PolicyRedirectHandler(self.policy)
        handlers: list[urllib.request.BaseHandler] = [redirects]
        if not self.policy.use_env_proxies:
            handlers.append(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(*handlers), redirects

    def _slow_down(self, url: str, status: int, cause: str) -> None:
        previous = self.interval
        self.interval = min(max(self.interval * 2, 0.1), self.policy.max_interval)
        self.slowdowns.append(
            {
                "url": url,
                "status": status,
                "cause": cause,
                "previous_min_interval_ms": round(previous * 1000),
                "min_interval_ms": round(self.interval * 1000),
            }
        )

    def _read(self, response: IO[bytes], headers: Message | None, url: str) -> bytes:
        limit = self.policy.max_bytes
        declared = headers.get("Content-Length") if headers is not None else None
        if declared is not None and str(declared).strip().isdigit() and int(str(declared)) > limit:
            raise FetchTooLarge(f"{url}: Content-Length {declared} exceeds the limit of {limit} bytes")
        chunks: list[bytes] = []
        total = 0
        while chunk := response.read(min(_CHUNK, limit + 1 - total) or 1):
            total += len(chunk)
            if total > limit:
                raise FetchTooLarge(f"{url}: response exceeds the limit of {limit} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    def fetch(self, url: str, *, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        """Fetch ``url``; with ``etag`` or ``last_modified``, conditionally.

        Returns the response for any HTTP status, including errors (after
        retrying transient ones) and ``304``. Raises `FetchRefused` for a
        URL or redirect the policy doesn't allow, `FetchTooLarge` for a body
        over the limit, and `FetchFailed` when network errors persist
        through every retry.
        """
        target = self.policy.check(url)
        attempts = 0
        while True:
            attempts += 1
            self._pace()
            opener, redirects = self._opener()
            request = urllib.request.Request(
                target,
                headers={"User-Agent": self.policy.user_agent, "Accept-Encoding": "identity"},
            )
            if etag:
                request.add_header("If-None-Match", etag)
            if last_modified:
                request.add_header("If-Modified-Since", last_modified)
            retry_wait: float | None = None
            try:
                with opener.open(request, timeout=self.policy.timeout) as response:
                    body = self._read(response, response.headers, target)
                    result = FetchResult(
                        url=target,
                        final_url=response.geturl(),
                        status=response.status,
                        body=body,
                        headers=_interesting_headers(response.headers),
                        attempts=attempts,
                        redirects=tuple(redirects.chain),
                    )
            except urllib.error.HTTPError as exc:
                try:
                    body = self._read(exc, exc.headers, target) if exc.fp is not None else b""
                finally:
                    exc.close()
                result = FetchResult(
                    url=target,
                    final_url=exc.geturl() or target,
                    status=exc.code,
                    body=body,
                    headers=_interesting_headers(exc.headers),
                    attempts=attempts,
                    redirects=tuple(redirects.chain),
                )
                if is_challenge(exc.code, body):
                    self._slow_down(target, exc.code, "cloudflare_challenge")
                elif exc.code in SLOW_DOWN_STATUSES:
                    self._slow_down(target, exc.code, f"HTTP {exc.code}")
                if exc.code in RETRY_STATUSES:
                    retry_wait = _retry_after(exc.headers)
                    if retry_wait is None:
                        retry_wait = self.policy.backoff * 2 ** (attempts - 1)
            except FetchError:
                raise
            except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
                if attempts > self.policy.retries:
                    raise FetchFailed(f"{target}: {exc} (after {attempts} attempts)") from exc
                self._wait(min(self.policy.backoff * 2 ** (attempts - 1), self.policy.max_backoff))
                continue
            if retry_wait is None or attempts > self.policy.retries:
                return result
            self._wait(min(retry_wait, self.policy.max_backoff))


class FetcherLike(Protocol):
    """What `crawl` and `fetch_urls` need of a fetcher: a policy and `Fetcher.fetch`."""

    policy: FetchPolicy

    def fetch(self, url: str, *, etag: str | None = None, last_modified: str | None = None) -> FetchResult: ...


def fetch(url: str, policy: FetchPolicy | None = None) -> FetchResult:
    """Fetch one URL under ``policy`` (the default policy if omitted); see `Fetcher.fetch`."""
    return Fetcher(policy).fetch(url)


# ---------------------------------------------------------------------------
# HTML


# The end tag admits attributes and whitespace, ``</script foo>``, which HTML
# treats as closing the element. Requiring a bare ``</script>`` would run the
# lazy match on to the next one and treat page content in between as script.
_SCRIPT_RE = re.compile(rb"<script\b[^>]*>.*?</script\b[^>]*>", re.IGNORECASE | re.DOTALL)
_CF_SCRIPT_MARKERS = (b"__CF$cv$params", b"/cdn-cgi/", b"_cf_chl_opt")
_ATTR_VALUE = rb"""(?:"[^"]*"|'[^']*'|[^\s>]*)"""
_CF_SUBSTITUTIONS: tuple[tuple[re.Pattern[bytes], bytes], ...] = (
    # Rocket Loader marks each element it rewrote with a per-request token.
    (re.compile(rb"\s+data-cf-modified-[0-9a-f]+-(?:=" + _ATTR_VALUE + rb")?", re.IGNORECASE), b""),
    (re.compile(rb"\s+data-cf-settings=" + _ATTR_VALUE, re.IGNORECASE), b""),
    (re.compile(rb"\b[0-9a-f]{16,64}-text/javascript", re.IGNORECASE), b"text/javascript"),
    (re.compile(rb"if \(!window\.__cfRLUnblockHandlers\) return false; ?"), b""),
    # Email obfuscation: the encoded address changes with every request.
    (re.compile(rb"(/cdn-cgi/l/email-protection)#[0-9a-f]*", re.IGNORECASE), rb"\1"),
    (re.compile(rb"(data-cfemail=)(?:\"[0-9a-f]*\"|'[0-9a-f]*')", re.IGNORECASE), rb'\1""'),
    (re.compile(rb"\s+nonce=" + _ATTR_VALUE, re.IGNORECASE), b""),
)


def normalize_html(body: bytes) -> bytes:
    """Remove the per-request tokens Cloudflare inserts into pages, for comparing them.

    Removed: scripts Cloudflare injects (its challenge and bot-management
    snippets, anything loaded from ``/cdn-cgi/``); Rocket Loader's
    ``data-cf-modified-<token>-`` and ``data-cf-settings`` attributes, its
    ``<token>-text/javascript`` script types and its ``__cfRLUnblockHandlers``
    guards; the encoded addresses of email protection; and ``nonce``
    attributes. Everything else is kept byte for byte, so any change to the
    page's own content still changes the result.
    """

    def drop_cloudflare_script(match: re.Match[bytes]) -> bytes:
        script = match.group(0)
        return b"" if any(marker in script for marker in _CF_SCRIPT_MARKERS) else script

    result = _SCRIPT_RE.sub(drop_cloudflare_script, body)
    for pattern, replacement in _CF_SUBSTITUTIONS:
        result = pattern.sub(replacement, result)
    return result


def is_html(body: bytes, content_type: str | None = None) -> bool:
    """Whether a response is an HTML page, by its Content-Type or its content."""
    if content_type and "html" in content_type.lower():
        return True
    head = body[:4096].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith((b"<!doctype html", b"<html")) or b"<html" in head[:1024]


class _LinkParser(HTMLParser):
    _LINK_ATTRS = {
        "a": "href",
        "area": "href",
        "iframe": "src",
        "frame": "src",
        "embed": "src",
        "object": "data",
        "img": "src",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.base: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "base" and values.get("href") and self.base is None:
            self.base = values["href"]
            return
        name = self._LINK_ATTRS.get(tag)
        if name and values.get(name):
            self.links.append(str(values[name]).strip())


def _decode_html(body: bytes, content_type: str | None) -> str:
    charset = None
    if content_type:
        found = re.search(r"charset=([\w.-]+)", content_type, re.IGNORECASE)
        charset = found.group(1) if found else None
    if charset is None:
        meta = re.search(rb"<meta[^>]+charset=[\"']?([\w.-]+)", body[:4096], re.IGNORECASE)
        charset = meta.group(1).decode("ascii") if meta else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def extract_links(body: bytes, page_url: str, content_type: str | None = None) -> list[str]:
    """Absolute URLs (without fragments) of the links in an HTML page, in order, deduplicated.

    Links are taken from ``a``/``area`` ``href``, ``iframe``/``frame``/
    ``embed``/``img`` ``src`` and ``object`` ``data``, resolved against the
    page's ``<base href>`` or its URL. ``mailto:``, ``javascript:`` and
    other non-HTTP links are dropped.
    """
    parser = _LinkParser()
    parser.feed(_decode_html(body, content_type))
    parser.close()
    base = urllib.parse.urljoin(page_url, parser.base) if parser.base else page_url
    seen: set[str] = set()
    links: list[str] = []
    for link in parser.links:
        absolute = urllib.parse.urldefrag(urllib.parse.urljoin(base, link)).url
        if urllib.parse.urlsplit(absolute).scheme.lower() not in ("http", "https"):
            continue
        if absolute not in seen:
            seen.add(absolute)
            links.append(absolute)
    return links


def is_followed(url: str, policy: FetchPolicy, path_prefix: str = DEFAULT_PATH_PREFIX) -> bool:
    """Whether `crawl` follows a link: the policy allows it and its path is under ``path_prefix``.

    The prefix is compared case-insensitively.
    """
    try:
        checked = policy.check(url)
    except FetchRefused:
        return False
    return urllib.parse.urlsplit(checked).path.lower().startswith(path_prefix.lower())


# ---------------------------------------------------------------------------
# Previous state, from the event log


@dataclass
class _UrlState:
    sha256: str | None = None
    compare_sha256: str | None = None
    is_html: bool = False
    etag: str | None = None
    last_modified: str | None = None
    seq: int | None = None
    linked: bool = False
    missing: bool = False
    classified_sha256: str | None = None
    #: The kind the latest classification gave.
    kind: str | None = None


@dataclass
class _PreviousState:
    urls: dict[str, _UrlState] = field(default_factory=dict)
    duplicates: set[tuple[str, tuple[str, ...]]] = field(default_factory=set)

    def get(self, url: str) -> _UrlState:
        return self.urls.setdefault(url, _UrlState())


def _str_or_none(value: JSONValue) -> str | None:
    return value if isinstance(value, str) else None


def _previous_state(log: EventLog) -> _PreviousState:
    state = _PreviousState()
    for event in log.iter_events():
        if not event.kind.startswith("fetch."):
            continue
        data = event.data
        if event.kind == "fetch.crawl_finished":
            groups = data.get("duplicates")
            state.duplicates = set()
            if isinstance(groups, list):
                for group in groups:
                    if isinstance(group, dict):
                        urls = group.get("urls")
                        sha = _str_or_none(group.get("sha256"))
                        if sha and isinstance(urls, list):
                            state.duplicates.add((sha, tuple(sorted(str(u) for u in urls))))
            continue
        url = _str_or_none(data.get("url"))
        if url is None:
            continue
        entry = state.get(url)
        if event.kind == "fetch.file":
            entry.sha256 = _str_or_none(data.get("sha256"))
            entry.is_html = data.get("normalized_sha256") is not None
            entry.compare_sha256 = _str_or_none(data.get("normalized_sha256")) or entry.sha256
            headers = data.get("headers")
            if isinstance(headers, dict):
                entry.etag = _str_or_none(headers.get("etag"))
                entry.last_modified = _str_or_none(headers.get("last_modified"))
            entry.seq = event.seq
            entry.missing = False
            if data.get("linked_from") is not None:
                entry.linked = True
        elif event.kind == "fetch.unchanged":
            entry.missing = False
        elif event.kind == "fetch.missing":
            entry.missing = True
        elif event.kind == "fetch.classified":
            entry.classified_sha256 = _str_or_none(data.get("sha256"))
            entry.kind = _str_or_none(data.get("kind"))
    return state


def classified_files(log: EventLog) -> dict[str, dict[str, JSONValue]]:
    """The latest ``fetch.classified`` data for each URL: what was found where.

    Each value has ``url``, ``sha256``, ``kind``, ``county`` and
    ``evidence``, plus ``seq``, the classifying event's position in the log.
    """
    mapping: dict[str, dict[str, JSONValue]] = {}
    for event in log.iter_events():
        if event.kind == "fetch.classified":
            url = _str_or_none(event.data.get("url"))
            if url is not None:
                mapping[url] = dict(event.data) | {"seq": event.seq}
    return mapping


# ---------------------------------------------------------------------------
# Crawling


@dataclass(frozen=True)
class CrawlEntry:
    """What happened to one URL in a crawl."""

    url: str
    #: "new", "changed", "unchanged", "not_modified", "missing" or "blocked".
    outcome: str
    status: int | None = None
    sha256: str | None = None
    size: int | None = None
    kind: str | None = None
    county: str | None = None
    reason: str | None = None


@dataclass
class CrawlReport:
    """The result of one crawl."""

    start_url: str
    entries: list[CrawlEntry] = field(default_factory=list)
    alerts: list[dict[str, JSONValue]] = field(default_factory=list)
    #: Links seen but not followed: off the allow-list or outside the path prefix.
    skipped_links: list[str] = field(default_factory=list)
    #: True if ``max_urls`` cut the crawl short (no "no longer linked" checks then).
    truncated: bool = False
    start_failed: bool = False

    def count(self, outcome: str) -> int:
        return sum(1 for e in self.entries if e.outcome == outcome)


#: Most duplicate groups, and URLs per group, kept in ``fetch.crawl_finished``.
_MAX_DUPLICATE_GROUPS = 50
_MAX_DUPLICATE_URLS = 40


class _Crawl:
    def __init__(
        self,
        workspace: Workspace,
        log: EventLog,
        fetcher: FetcherLike,
        path_prefix: str,
        conditional: bool,
        simulated: Mapping[str, JSONValue] | None = None,
    ) -> None:
        self.workspace = workspace
        self.log = log
        self.fetcher = fetcher
        self.path_prefix = path_prefix
        self.conditional = conditional
        self.simulated = dict(simulated) if simulated is not None else None
        self.previous = _previous_state(log)
        self.times = audit_times(log, workspace)
        #: Each URL's kind this crawl (or as last classified).
        self.kinds: dict[str, str | None] = {}
        self._slowdowns_seen = len(getattr(fetcher, "slowdowns", []))
        #: For each URL fetched or confirmed unchanged this crawl: its content
        #: key (the normalized hash for HTML, else the SHA-256), its size, and
        #: the SHA-256 of the stored body.
        self.current: dict[str, tuple[str, int, str]] = {}

    def append(
        self,
        kind: str,
        data: Mapping[str, Any],
        *,
        file_sha256: str | None = None,
        county: str | None = None,
    ) -> Event:
        if self.simulated is not None:
            data = dict(data) | {"simulated": self.simulated}
        return self.log.append(kind, ACTOR, data=data, file_sha256=file_sha256, county=county)

    def moment(self, event: Event) -> str:
        """When a fetch really happened: a rehearsal's recorded time, else the event's."""
        recorded = self.simulated.get("time") if self.simulated is not None else None
        return recorded if isinstance(recorded, str) else event.server_time

    def change_alert(
        self,
        report: CrawlReport,
        url: str,
        change: str,
        moment: str,
        kind: str | None,
        sha256: str | None,
        previous_sha256: str | None,
        detail: str | None = None,
    ) -> None:
        """Alert on a new, changed or missing file if `corla_observer.flags.change_rule` flags it."""
        election = election_from_url(url)
        same = self.times.election is None or election is None or election == self.times.election
        roll = self.times.roll.value if self.times.roll and same else None
        start = self.times.round_start.value if self.times.round_start and same else None
        rule = change_rule(kind, change, moment, roll, start)
        if rule is None:
            return
        reason, severity = rule
        data: dict[str, JSONValue] = {"reason": reason, "severity": severity, "url": url}
        if reason in ("changed_after_roll", "changed_after_round_start"):
            data |= {"change": change, "kind": kind, "time": moment}
            data["roll_time" if reason == "changed_after_roll" else "round_start"] = roll if reason == "changed_after_roll" else start
        if change != "missing":
            data["sha256"] = sha256
        data["previous_sha256"] = previous_sha256
        if detail is not None:
            data["detail"] = detail
        self.alert(report, data, file_sha256=sha256 or previous_sha256)

    def record_slowdowns(self) -> None:
        slowdowns = getattr(self.fetcher, "slowdowns", [])
        for slowdown in slowdowns[self._slowdowns_seen :]:
            self.append("fetch.slowed", slowdown)
        self._slowdowns_seen = len(slowdowns)

    def alert(self, report: CrawlReport, data: dict[str, JSONValue], **kwargs: Any) -> None:
        report.alerts.append(data)
        self.append("fetch.alert", data, **kwargs)

    def follows(self, url: str) -> bool:
        return is_followed(url, self.fetcher.policy, self.path_prefix)

    def missing(self, report: CrawlReport, url: str, reason: str, status: int | None = None) -> None:
        before = self.previous.urls.get(url)
        report.entries.append(CrawlEntry(url, "missing", status=status, reason=reason))
        if before is not None and before.missing:
            return
        data: dict[str, JSONValue] = {
            "url": url,
            "reason": reason,
            "status": status,
            "previous_sha256": before.sha256 if before else None,
        }
        event = self.append("fetch.missing", data, file_sha256=before.sha256 if before else None)
        if before is not None and before.sha256 is not None and not before.is_html:
            self.change_alert(report, url, "missing", self.moment(event), before.kind, None, before.sha256, reason)

    def visit(
        self, report: CrawlReport, url: str, linked_from: str | None, depth: int
    ) -> tuple[bool, FetchResult | None]:
        """Fetch and record one URL. Returns (whether it was available, the page if HTML)."""
        before = self.previous.urls.get(url)
        etag = last_modified = None
        if self.conditional and before is not None and before.sha256 and not before.missing:
            etag, last_modified = before.etag, before.last_modified
        try:
            result = self.fetcher.fetch(url, etag=etag, last_modified=last_modified)
        except FetchError as exc:
            self.missing(report, url, f"{type(exc).__name__}: {exc}")
            return False, None
        if result.not_modified and before is not None and before.sha256:
            self.append(
                "fetch.unchanged",
                {
                    "url": url,
                    "sha256": before.sha256,
                    "not_modified": True,
                    "status": result.status,
                    "headers": dict(result.headers),
                    "previous_seq": before.seq,
                },
                file_sha256=before.sha256,
            )
            self.current[url] = (before.compare_sha256 or before.sha256, 1, before.sha256)
            report.entries.append(CrawlEntry(url, "not_modified", result.status, before.sha256))
            # A page not re-fetched is not re-parsed: its links are as before.
            return True, None
        if not result.ok:
            self.missing(report, url, f"HTTP {result.status}", result.status)
            return False, None
        if is_blocked(result.body):
            blocked = self.workspace.put_bytes(result.body)
            self.append(
                "fetch.blocked",
                {"url": url, "status": result.status, "sha256": blocked.sha256, "size": blocked.size},
                file_sha256=blocked.sha256,
            )
            report.entries.append(CrawlEntry(url, "blocked", result.status, blocked.sha256, reason="refused by a firewall"))
            slow = getattr(self.fetcher, "_slow_down", None)
            if callable(slow):
                slow(url, result.status, "blocked_page")
            return False, None

        stored = self.workspace.put_bytes(result.body)
        html = is_html(result.body, result.headers.get("content_type"))
        normalized = hashlib.sha256(normalize_html(result.body)).hexdigest() if html else None
        data: dict[str, JSONValue] = {
            "url": url,
            "final_url": result.final_url,
            "status": result.status,
            "sha256": stored.sha256,
            "size": stored.size,
            "headers": dict(result.headers),
            "linked_from": linked_from,
            "depth": depth,
            "attempts": result.attempts,
        }
        if result.redirects:
            data["redirects"] = list(result.redirects)
        if normalized is not None:
            data["normalized_sha256"] = normalized
        moment = self.moment(self.append("fetch.file", data, file_sha256=stored.sha256))

        compare = normalized or stored.sha256
        self.current[url] = (compare, stored.size, stored.sha256)
        if before is None or before.sha256 is None:
            outcome = "new"
            self.append("fetch.new", {"url": url, "sha256": stored.sha256}, file_sha256=stored.sha256)
        else:
            outcome = "unchanged" if compare == before.compare_sha256 else "changed"
            comparison: dict[str, JSONValue] = {
                "url": url,
                "sha256": stored.sha256,
                "previous_sha256": before.sha256,
                "previous_seq": before.seq,
                "compared": "normalized_html" if normalized is not None else "bytes",
            }
            if normalized is not None:
                comparison["normalized_sha256"] = normalized
                comparison["previous_normalized_sha256"] = before.compare_sha256
            self.append(f"fetch.{outcome}", comparison, file_sha256=stored.sha256)

        kind = county = None
        if outcome != "unchanged" or before is None or before.classified_sha256 is None:
            classification = classify_bytes(result.body)
            kind, county = classification.kind, classification.county
            self.append(
                "fetch.classified",
                {"url": url, "sha256": stored.sha256} | classification.to_dict(),
                file_sha256=stored.sha256,
                county=county,
            )
            name_county = county_from_filename(urllib.parse.unquote(urllib.parse.urlsplit(url).path).rsplit("/", 1)[-1])
            if kind == "county_manifest" and county and name_county and name_county != county:
                self.alert(
                    report,
                    {
                        "reason": "manifest_under_other_name",
                        "severity": "high",
                        "url": url,
                        "sha256": stored.sha256,
                        "counties": [name_county, county],
                        "message": f"{url} holds {county}'s manifest, not {name_county}'s",
                    },
                    file_sha256=stored.sha256,
                )
        self.kinds[url] = kind or (before.kind if before is not None else None)
        if outcome in ("new", "changed") and not html:
            previous_kind = before.kind if before is not None else None
            watched = next((k for k in (kind, previous_kind) if k in WATCHED_KINDS), kind or previous_kind)
            self.change_alert(
                report, url, outcome, moment, watched, stored.sha256, before.sha256 if before is not None else None
            )
        report.entries.append(
            CrawlEntry(url, outcome, result.status, stored.sha256, stored.size, kind, county)
        )
        return True, (result if html else None)

    def duplicates(self, report: CrawlReport) -> list[dict[str, JSONValue]]:
        """Alert on each new group of URLs with identical content; return every group.

        Empty bodies are not compared. HTML pages are compared in normalized
        form, so ``content_sha256`` is then the normalized hash, and the
        event's ``file_sha256`` is the stored body of the group's first URL.
        """
        by_content: dict[str, list[str]] = {}
        stored: dict[str, str] = {}
        for url, (key, size, sha256) in self.current.items():
            if size > 0:
                by_content.setdefault(key, []).append(url)
                stored.setdefault(key, sha256)
        groups: list[dict[str, JSONValue]] = []
        for key, urls in sorted(by_content.items()):
            if len(urls) < 2:
                continue
            urls = sorted(urls)[:_MAX_DUPLICATE_URLS]
            groups.append({"sha256": key, "urls": list(urls)})
            if (key, tuple(urls)) not in self.previous.duplicates:
                county_files = any(self.kinds.get(u) in ("county_manifest", "cvr") for u in urls)
                self.alert(
                    report,
                    {
                        "reason": "duplicate_content",
                        "severity": "high" if county_files else "normal",
                        "content_sha256": key,
                        "compared": "bytes" if key == stored[key] else "normalized_html",
                        "urls": list(urls),
                    },
                    file_sha256=stored[key],
                )
        return groups


def crawl(
    start_url: str = DEFAULT_START_URL,
    *,
    workspace: Workspace,
    log: EventLog,
    fetcher: Fetcher | None = None,
    conditional: bool = False,
    path_prefix: str = DEFAULT_PATH_PREFIX,
    max_depth: int = 2,
    max_urls: int = 2000,
    extra_urls: Iterable[str] = (),
) -> CrawlReport:
    """Fetch the Audit Center page and what it links to; store and record everything.

    ``extra_urls`` (in scope) are fetched too, after the start page, as if
    linked from it but recorded with no ``linked_from``: the likely
    addresses of files not linked yet (`corla_observer.election`). One that
    is not there is recorded as missing once, not on every crawl, and is
    never reported as "no longer linked".

    The crawl's scope is ``start_url`` itself (which need only be allowed
    by the fetcher's policy, wherever it is on the host) plus every URL the
    policy allows whose path starts with ``path_prefix``. The Audit Center
    page lives at ``/pubs/elections/auditCenter.html`` and links to its
    files relatively (``RLA/2026/...``), so the defaults cover it.

    Follows links from HTML pages, breadth first, to in-scope URLs, up
    to ``max_depth`` links away from ``start_url`` and ``max_urls`` URLs in
    all. Every response body is stored in ``workspace`` and every outcome
    recorded in ``log``, as described in this module's documentation.
    Returns a `CrawlReport`.
    """
    fetcher = fetcher or Fetcher()
    run = _Crawl(workspace, log, fetcher, path_prefix, conditional)
    report = CrawlReport(start_url=start_url)
    extras = [u for u in dict.fromkeys(extra_urls) if u != start_url and run.follows(u)]
    run.append(
        "fetch.crawl_started",
        {
            "url": start_url,
            "conditional": conditional,
            "path_prefix": path_prefix,
            "max_depth": max_depth,
            "max_urls": max_urls,
            "extra_urls": len(extras),
            "user_agent": fetcher.policy.user_agent,
        },
    )
    queue: deque[tuple[str, str | None, int]] = deque([(start_url, None, 0)])
    seen = {start_url}
    skipped: set[str] = set()
    for extra in extras:
        seen.add(extra)
        queue.append((extra, None, 1))
    while queue:
        url, linked_from, depth = queue.popleft()
        available, page = run.visit(report, url, linked_from, depth)
        run.record_slowdowns()
        if url == start_url and not available:
            report.start_failed = True
        if page is None or depth >= max_depth:
            continue
        for link in extract_links(page.body, page.final_url, page.headers.get("content_type")):
            if link in seen or link in skipped:
                continue
            if not run.follows(link):
                skipped.add(link)
                continue
            if len(seen) >= max_urls:
                report.truncated = True
                break
            seen.add(link)
            queue.append((link, url, depth + 1))
    report.skipped_links = sorted(skipped)

    if not report.start_failed and not report.truncated:
        for url, before in sorted(run.previous.urls.items()):
            if before.linked and not before.missing and url not in seen:
                run.missing(report, url, "no longer linked")

    groups = run.duplicates(report)
    run.append(
        "fetch.crawl_finished",
        {
            "url": start_url,
            "start_failed": report.start_failed,
            "truncated": report.truncated,
            "counts": {
                outcome: report.count(outcome)
                for outcome in ("new", "changed", "unchanged", "not_modified", "missing", "blocked")
            },
            "alerts": len(report.alerts),
            "skipped_links": len(report.skipped_links),
            "duplicates": groups[:_MAX_DUPLICATE_GROUPS],
        },
    )
    return report


def fetch_urls(
    urls: Iterable[str],
    *,
    workspace: Workspace,
    log: EventLog,
    fetcher: FetcherLike,
    simulated: Mapping[str, JSONValue] | None = None,
    report_missing: bool = True,
) -> CrawlReport:
    """Fetch a fixed list of URLs, without following links, recording the same events as `crawl`.

    For re-running what an earlier fetch saw, as in a rehearsal that serves
    the audit center's files as a mirror held them at some commit
    (`corla_observer.rehearse`): ``fetcher`` is then any `FetcherLike`
    object, and ``simulated`` describes
    where the content really came from. It is added to the data of every
    event this call records, under ``simulated``, so a simulated fetch can
    never be mistaken for a real one.

    With ``report_missing``, a URL fetched before (in the log) but not in
    ``urls`` is recorded as missing ("not in this fetch"), as `crawl` does
    for a URL no longer linked.
    """
    run = _Crawl(workspace, log, fetcher, DEFAULT_PATH_PREFIX, conditional=False, simulated=simulated)
    wanted = list(dict.fromkeys(urls))
    report = CrawlReport(start_url=wanted[0] if wanted else "")
    run.append("fetch.crawl_started", {"url": report.start_url, "urls": len(wanted), "fixed_list": True})
    for url in wanted:
        run.visit(report, url, None, 0)
        run.record_slowdowns()
    if report_missing:
        listed = set(wanted)
        for url, before in sorted(run.previous.urls.items()):
            if before.sha256 and not before.missing and url not in listed:
                run.missing(report, url, "not in this fetch")
    groups = run.duplicates(report)
    run.append(
        "fetch.crawl_finished",
        {
            "url": report.start_url,
            "start_failed": False,
            "truncated": False,
            "counts": {
                outcome: report.count(outcome)
                for outcome in ("new", "changed", "unchanged", "not_modified", "missing", "blocked")
            },
            "alerts": len(report.alerts),
            "skipped_links": 0,
            "duplicates": groups[:_MAX_DUPLICATE_GROUPS],
        },
    )
    return report


def iter_alerts(log: EventLog) -> Iterable[dict[str, JSONValue]]:
    """Every ``fetch.alert`` event's data, with its ``seq`` and ``server_time``, in log order."""
    for event in log.iter_events():
        if event.kind == "fetch.alert":
            yield dict(event.data) | {"seq": event.seq, "server_time": event.server_time}
