"""The FastAPI application: a public dashboard and coordinator pages, under an optional path prefix.

`create_app` builds the app from an `AppConfig`. Pages are rendered on the
server (Jinja2, autoescaped); the only JavaScript is ``static/app.js``,
served from this package with subresource integrity, which follows the
event log's head by server-sent events and refreshes the dashboard, and
copies text to the clipboard (the observer pages and the photo check's
form have their own scripts: ``static/photoshrink.js`` shrinks a large
photo in the browser before it is sent).

Routers, each in its own module:

- `corla_observer.web.public`: the dashboard (``/``), its live fragment,
  its JSON, the log head and the server-sent event stream. No login; never
  personalized, so it can be cached by a CDN.
- `corla_observer.web.coordinator`: login and logout, and the coordinator
  pages (``/coord/...``): status, uploads, audit boards, the data map,
  fetching and observation statistics.
- `corla_observer.web.observe`: the observer entry page and the practice
  reader page, reached by per-person links.
- `corla_observer.web.extracts`: the seed's entry and a second
  coordinator's confirmation, and building, viewing, printing and
  downloading the committed extract (``/coord/seed``, ``/coord/extract``).
- `corla_observer.web.targets`: targeted ballots: ballots that show a
  pattern of votes, drawn at random, and the targeted pull list and its
  extract (``/coord/targets``).
- `corla_observer.web.photos`: "Check a page", a photo or the short hashes
  of a printed extract page checked against the committed extract, for
  coordinators, for link holders in their own county, and for anyone only
  if ``public_check`` is set (``/coord/check``, ``/observe/TOKEN/check``,
  ``/read/TOKEN/check``, ``/check``).

Every route is under ``config.path_prefix`` (empty, or ``/something``), so
the app can run under an unguessable prefix; nothing is served outside it,
and the framework's own documentation pages are turned off.

Security, applied to every response by `SecurityHeadersMiddleware`: a
Content-Security-Policy with no inline script or style and
``frame-ancestors 'none'``, ``X-Content-Type-Options: nosniff``,
``Referrer-Policy: no-referrer`` and ``Cache-Control: no-store`` unless a
route says otherwise (pages reached without login say ``no-cache``, so they
are always revalidated and never refer to stale script versions). `BodyLimitMiddleware` refuses request bodies larger
than the route allows (the upload route allows the largest upload kind, the
photo check routes the largest photo). Uploaded files are never served back
at all, and checked photos only to a coordinator or the link that sent them.
"""

from __future__ import annotations

import base64
import hashlib
import re
import threading
from collections.abc import Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from corla_observer import __version__
from corla_observer.version import current as software_identity
from corla_observer.classify import COLORADO_COUNTIES, TEST_COUNTIES
from corla_observer.events import EventLog
from corla_observer.localtime import colorado_time
from corla_observer.photoimage import MAX_PHOTO_BYTES, too_large_message
from corla_observer.receipts import ReceiptSigner
from corla_observer.signed import DeviceRegistry
from corla_observer.intake import MAX_TOTAL_BYTES
from corla_observer.web.accounts import AccountStore
from corla_observer.web.ratelimit import Limit, RateLimiter
from corla_observer.web.sessions import SessionStore
from corla_observer.web.views import Snapshot, SnapshotCache, build_snapshot, missing_label, short_ref
from corla_observer.workspace import Workspace

PACKAGE_DIR = Path(__file__).parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

#: Every script and stylesheet the pages load.
STATIC_FILES = (
    "app.css", "app.js", "contestlogic.js", "extract.css", "idmatch.js", "observe.js", "percontest.js", "photoshrink.js",
    "signing.js", "targets.js",
)  # fmt: skip

SESSION_COOKIE = "corla_session"
PRESESSION_COOKIE = "corla_pre"

#: Largest request body for anything but an upload.
MAX_FORM_BYTES = 64 * 1024
#: Allowance for the multipart framing and the text fields of an upload.
UPLOAD_OVERHEAD = 1024 * 1024

_PREFIX_RE = re.compile(r"^(/[A-Za-z0-9._~-]+)*$")

ASGIApp = Callable[
    [MutableMapping[str, Any], Callable[[], Awaitable[MutableMapping[str, Any]]], Callable[[MutableMapping[str, Any]], Awaitable[None]]],
    Awaitable[None],
]
Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


def default_crawl(workspace: Workspace, log: EventLog) -> object:
    """One full cycle (`corla_observer.schedule.run_cycle`): the crawl, the results, the flags, estimates, prediction and interim extracts.

    In a walkthrough workspace (`corla_observer.walkthrough`), nothing: a
    walkthrough never fetches from the real sites.
    """
    from corla_observer.schedule import run_cycle
    from corla_observer.walkthrough import is_walkthrough

    if is_walkthrough(workspace):
        return None
    return run_cycle(workspace, log, trigger="web")


def check_prefix(prefix: str) -> str:
    """Return a path prefix (``""`` or ``/a/b``, no trailing slash) if valid."""
    prefix = prefix.rstrip("/")
    if not _PREFIX_RE.match(prefix):
        raise ValueError("a path prefix is empty or /segments of letters, digits, '.', '_', '~' or '-'")
    return prefix


@dataclass
class AppConfig:
    workspace: Path
    #: The server secret (see `corla_observer.web.sessions.load_secret`).
    secret: bytes
    #: The key that signs observers' receipts (see
    #: `corla_observer.receipts.load_receipt_key`). ``corla-observer serve``
    #: always passes one; without one (in tests) a new key is made for the
    #: app's lifetime.
    receipt_key: ReceiptSigner | None = None
    path_prefix: str = ""
    #: Counties listed on the dashboard even before anything arrives from them.
    counties: tuple[str, ...] = ()
    #: Runs one crawl of the audit center; replaced in tests.
    crawl: Callable[[Workspace, EventLog], object] = default_crawl
    #: Cookies are always Secure in production; tests use https URLs.
    secure_cookies: bool = True
    sse_poll_interval: float = 1.0
    #: A stream ends after this long; browsers reconnect by themselves.
    sse_max_seconds: float = 300.0
    sse_max_clients: int = 200
    login_limit: Limit = field(default_factory=lambda: Limit(10, 300))
    upload_limit: Limit = field(default_factory=lambda: Limit(60, 3600))
    #: Least time between two fetches requested from the web.
    fetch_interval: float = 60.0
    #: Photo checks per account, link or address.
    photo_limit: Limit = field(default_factory=lambda: Limit(60, 3600))
    #: Whether anyone may check a page at ``/check``, without a login or a link: only after the audit.
    public_check: bool = False


class FetchJob:
    """At most one crawl at a time, run in a background thread."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.started: str | None = None
        self.finished: str | None = None
        self.error: str | None = None
        self.last_request = 0.0

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


class WebState:
    """Everything the routes share, kept on ``app.state.web``."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.prefix = check_prefix(config.path_prefix)
        self.workspace = Workspace.open(config.workspace)
        self.log = EventLog.open(self.workspace.root)
        self.accounts = AccountStore(self.workspace.root)
        self.sessions = SessionStore(config.secret)
        self.login_limiter = RateLimiter(config.login_limit)
        self.upload_limiter = RateLimiter(config.upload_limit)
        self.photo_limiter = RateLimiter(config.photo_limit)
        #: At most this many photo checks at once: each runs tesseract several times.
        self.photo_slots = threading.BoundedSemaphore(2)
        self.fetch = FetchJob()
        from corla_observer.walkthrough import is_walkthrough

        #: Whether this workspace is a walkthrough (`corla_observer.walkthrough`): then the pages say so, with its simulated time.
        self.walkthrough = is_walkthrough(self.workspace)
        self.walkthrough_lock = threading.Lock()
        self.cache = SnapshotCache(self._build, self.log.head)
        self.sse_clients = 0
        #: Per observer link (by token hash), the (device, sequence number) pairs recorded.
        self.observer_seen: dict[str, set[tuple[str, int]]] = {}
        self.receipts = config.receipt_key or ReceiptSigner.generate()
        #: Observers' registered device keys and signed payloads, from the log.
        self.devices = DeviceRegistry(self.log)
        self.sse_lock = threading.Lock()
        self.templates = Environment(
            loader=FileSystemLoader(TEMPLATES_DIR),
            autoescape=select_autoescape(default=True, default_for_string=True),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        # Each script and stylesheet is versioned by its content's hash, and
        # carries the matching integrity hash, both computed once here so they
        # always agree with each other.
        contents = {name: (STATIC_DIR / name).read_bytes() for name in STATIC_FILES}
        self.static_versions = {name: hashlib.sha256(data).hexdigest()[:16] for name, data in contents.items()}
        self.integrity = {
            name: "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode() for name, data in contents.items()
        }
        self.templates.globals.update(
            url=self.url,
            static=self.static_url,
            integrity=self.integrity,
            version=software_identity().label,
            prefix=self.prefix,
            missing_label=missing_label,
            short_ref=short_ref,
            counties=COLORADO_COUNTIES,
            test_counties=TEST_COUNTIES,
            receipt_key=self.receipts.public_info(),
        )
        self.templates.filters.update(
            h12=lambda s: (s or "")[:12], size=_size, label=lambda s: str(s).replace("_", " "), local=colorado_time,
            missing_label=missing_label,
        )

    def _build(self, previous: Snapshot | None) -> Snapshot:
        return build_snapshot(self.log, self.workspace, self.config.counties, previous=previous)

    def static_url(self, name: str) -> str:
        """A static file's URL, versioned by its content's hash (a file not in `STATIC_FILES` is an error)."""
        return f"{self.prefix}/static/{name}?v={self.static_versions[name]}"

    def url(self, path: str = "/") -> str:
        return f"{self.prefix}{path}"

    def walkthrough_banner(self) -> str | None:
        """In a walkthrough, the banner: that it is one, and its simulated time."""
        if not self.walkthrough:
            return None
        from corla_observer.walkthrough import WalkthroughError, state

        try:
            walk = state(self.workspace, self.log)
        except WalkthroughError:
            return None
        when = colorado_time(walk.now, seconds=False) if walk.now else "before the first step"
        upcoming = walk.next
        nxt = f"; next: {upcoming.title}" if upcoming else "; the replay is over"
        return f"WALKTHROUGH of the {walk.election} audit ({walk.county}), replayed from the public mirror. Simulated time: {when}{nxt}."

    def render(self, request: Request, template: str, status_code: int = 200, **context: Any) -> HTMLResponse:
        html = self.templates.get_template(template).render(request=request, walkthrough_banner=self.walkthrough_banner(), **context)
        return HTMLResponse(html, status_code=status_code)

    def close(self) -> None:
        self.log.close()


def _size(n: object) -> str:
    if not isinstance(n, int):
        return ""
    value = float(n)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{n} bytes" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} bytes"


def get_state(request: Request) -> WebState:
    state: WebState = request.app.state.web
    return state


# ---------------------------------------------------------------------------
# Middleware


CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
)


class SecurityHeadersMiddleware:
    """Add the security headers to every response, and ``Cache-Control: no-store`` unless set."""

    def __init__(self, app: ASGIApp, static_prefix: str) -> None:
        self.app = app
        self.static_prefix = static_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_static = str(scope.get("path", "")).startswith(self.static_prefix)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"server"]
                names = {k.lower() for k, _ in headers}
                headers.extend(h for h in SECURITY_HEADERS if h[0] not in names)
                if b"cache-control" not in names:
                    headers.append((b"cache-control", b"public, max-age=3600" if is_static else b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


class _BodyTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """Refuse a request body larger than its route allows, by Content-Length and as it streams."""

    def __init__(
        self,
        app: ASGIApp,
        upload_path: str,
        photo_path: re.Pattern[str] | None = None,
        photo_page: Callable[[str, int | None], str] | None = None,
    ) -> None:
        self.app = app
        #: The page for a photo refused as too large (its route's path and the declared size, if known), instead of plain text.
        self.photo_page = photo_page
        self.upload_path = upload_path
        self.upload_limit = MAX_TOTAL_BYTES + UPLOAD_OVERHEAD
        self.photo_path = photo_path
        self.photo_limit = MAX_PHOTO_BYTES + UPLOAD_OVERHEAD

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        photo = False
        if path == self.upload_path:
            limit = self.upload_limit
        elif self.photo_path is not None and self.photo_path.fullmatch(path):
            limit = self.photo_limit
            photo = True
        else:
            limit = MAX_FORM_BYTES
        render = self.photo_page if photo else None
        page = (lambda size: render(path, size)) if render is not None else None
        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    declared: int | None = int(value)
                except ValueError:
                    declared = None
                if declared is None or declared > limit:
                    await _too_large(send, page(declared) if page else None)
                    return
        received = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await _too_large(send, page(None) if page else None)


async def _too_large(send: Send, html: str | None = None) -> None:
    body = html.encode() if html is not None else b"Request body too large.\n"
    kind = b"text/html; charset=utf-8" if html is not None else b"text/plain; charset=utf-8"
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", kind), (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")],
        }
    )
    await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------
# The app


#: Modules with a ``router`` (a FastAPI ``APIRouter``) included under the prefix, in order.
ROUTER_MODULES = (
    "corla_observer.web.public",
    "corla_observer.web.coordinator",
    "corla_observer.web.observe",
    "corla_observer.web.extracts",
    "corla_observer.web.live",
    "corla_observer.web.photos",
    "corla_observer.web.targets",
)


def create_app(config: AppConfig) -> FastAPI:
    """The web app for one workspace; see the module documentation."""
    import importlib

    state = WebState(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        from corla_observer.web.targets import warm

        threading.Thread(target=warm, args=(state,), name="corla-observer-targets-warm", daemon=True).start()
        try:
            yield
        finally:
            state.close()

    app = FastAPI(
        title="corla-observer",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.web = state
    if state.prefix:

        @app.get(state.prefix, include_in_schema=False)
        async def prefix_root() -> RedirectResponse:
            return RedirectResponse(f"{state.prefix}/", status_code=308)

    for name in ROUTER_MODULES:
        module = importlib.import_module(name)
        app.include_router(module.router, prefix=state.prefix)
    app.mount(f"{state.prefix}/static", StaticFiles(directory=STATIC_DIR, html=False), name="static")
    photo_path = re.compile(re.escape(state.prefix) + r"/(coord/check|check|(observe|read)/[^/]+/check)")

    def photo_page(path: str, size: int | None) -> str:
        return state.templates.get_template("photo_too_large.html").render(
            message=too_large_message(size), back=path, walkthrough_banner=state.walkthrough_banner()
        )

    app.add_middleware(BodyLimitMiddleware, upload_path=f"{state.prefix}/coord/upload", photo_path=photo_path, photo_page=photo_page)
    app.add_middleware(SecurityHeadersMiddleware, static_prefix=f"{state.prefix}/static/")
    return app
