"""Login, logout and the coordinator pages.

Routes (under the app's path prefix):

``GET /login``, ``POST /login``
    The login form, protected by a pre-session CSRF token, rate-limited per
    address and per account name. A successful login starts a new session
    and records ``coordinator.login``; a wrong password for an existing
    account records ``coordinator.login_failed`` (actor ``system``). Unknown
    names are not recorded, only rate-limited.
``POST /logout``
    Ends the session and records ``coordinator.logout``.
``GET /coord``
    Each county's files, audit boards and what is missing, with the
    uploaders, warnings and rejections; the audit board form; the fetch
    button and the state of the last fetch.
``GET /coord/upload``, ``POST /coord/upload``
    Upload one or more files (up to `corla_observer.intake.MAX_FILES`),
    streamed to the staging directory and rate-limited per account and per
    address. Nothing but the files is asked for: each file's kind
    (redacted CVR, manifest, pull list, or a results file downloaded by
    hand from Clarity's ENR) and county are detected from its content
    (`corla_observer.intake.take`), shown on the result page with the
    evidence, and the file goes to `corla_observer.uploads.receive` (with
    the redaction check) or `corla_observer.enr.receive_results` (after
    which the totals check, flags and estimates are updated at once). The
    form's optional ``kind`` and ``county`` (or ``scope``) fields override
    the detection; the page does not show them.
``POST /coord/upload/choose``
    For a file whose kind or county could not be told, which waits in the
    staging directory (`corla_observer.intake.PendingStore`): the
    coordinator's choice among the candidates, or discarding it.
``POST /coord/audit-boards``
    Record a county's audit board count (`corla_observer.uploads.record_audit_boards`).
``GET /coord/map``
    The data map as ``map show`` prints it, its problems, and the
    ``map show --for-ai`` text with a copy button.
``GET /coord/flags``
    The change flags, which only coordinators see: the open ones, most
    severe first, each with its findings expandable and a form to mark it
    addressed with a note, and the addressed ones separately, with who
    addressed them, when, and how.
``GET /coord/links``, ``POST /coord/links``, ``POST /coord/links/revoke``
    Make an observer link or a practice reader link for a person, a county
    and a board, shown once with its full URL to copy (only the token's
    hash is kept), list the links made, with their observer IDs, and revoke
    one (`corla_observer.practice.LinkStore`).
``POST /coord/flags/address``, ``POST /coord/flags/reopen``
    Mark a flag addressed, with a note (``flag.addressed``), or reopen it
    (``flag.reopened``); see `corla_observer.flags.record_addressed`.
``POST /coord/fetch``
    Start a crawl of the audit center in the background, recording
    ``coordinator.fetch_requested`` first; one at a time, and at most one
    per ``fetch_interval``.

Every page here needs a session: a GET without one is redirected to the
login page, and a POST without one, or without the session's CSRF token, is
refused with 403. Every action is recorded in the event log with the
coordinator's actor and the session's public label as ``session_id``.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from functools import partial
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from starlette.datastructures import FormData

from corla_observer.classify import COLORADO_COUNTIES, known_county
from corla_observer.contests import BallotComparison, compare_board
from corla_observer.enr import MAX_RESULTS_UPLOAD_BYTES
from corla_observer.flags import record_addressed, record_reopened
from corla_observer.intake import (
    KIND_LABELS,
    KINDS,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    Intake,
    PendingStore,
    known_counties,
    take,
)
from corla_observer.practice import MAX_BOARDS as MAX_LINK_BOARDS
from corla_observer.practice import ROLES, LinkStore, PracticeError
from corla_observer.uploads import (
    MAX_AUDIT_BOARDS,
    MAX_UPLOAD_BYTES,
    STAGING_DIRNAME,
    UploadError,
    record_audit_boards,
)
from corla_observer.web.accounts import AccountError, check_name
from corla_observer.web.app import (
    PRESESSION_COOKIE,
    SESSION_COOKIE,
    WebState,
    get_state,
)
from corla_observer.web.forms import FormError, parse_upload
from corla_observer.web.sessions import SESSION_LIFETIME, Session
from corla_observer.web.views import map_text

router = APIRouter()

#: Messages shown after a redirect, chosen by a fixed code (never echoed from the request).
MESSAGES = {
    "boards": "Audit boards recorded.",
    "fetch_started": "Fetch started. Its events appear on the dashboard as it runs.",
    "fetch_running": "A fetch is already running.",
    "fetch_too_soon": "A fetch was requested less than a minute ago; try again shortly.",
    "logged_in": "Logged in.",
    "addressed": "Flag marked addressed.",
    "reopened": "Flag reopened.",
}


class CsrfError(FormError):
    pass


def _address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def current_session(request: Request) -> Session | None:
    return get_state(request).sessions.get(request.cookies.get(SESSION_COOKIE))


def _login_redirect(state: WebState) -> RedirectResponse:
    return RedirectResponse(state.url("/login"), status_code=303)


def _forbidden() -> HTTPException:
    return HTTPException(status_code=403, detail="Forbidden: log in again, from this site's own form.")


async def small_form(request: Request) -> FormData:
    """A URL-encoded form (never multipart: files go only through the upload route)."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="Expected a URL-encoded form.")
    return await request.form(max_files=0, max_fields=16, max_part_size=8192)


async def checked_form(request: Request) -> tuple[Session, FormData]:
    """The session and the form of a coordinator POST, refusing it without both and a valid CSRF token."""
    session = current_session(request)
    if session is None:
        raise _forbidden()
    form = await small_form(request)
    token = form.get("csrf")
    if not isinstance(token, str) or not get_state(request).sessions.check_csrf(session, token):
        raise _forbidden()
    return session, form


def _set_cookie(state: WebState, response: Response, name: str, value: str, max_age: int | None = None) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path=state.prefix or "/",
        httponly=True,
        secure=state.config.secure_cookies,
        samesite="strict",
    )


def _clear_cookie(state: WebState, response: Response, name: str) -> None:
    response.delete_cookie(
        name, path=state.prefix or "/", secure=state.config.secure_cookies, httponly=True, samesite="strict"
    )


# ---------------------------------------------------------------------------
# Login and logout


def _login_page(request: Request, error: str | None = None, status_code: int = 200) -> Response:
    state = get_state(request)
    nonce = request.cookies.get(PRESESSION_COOKIE)
    if not nonce or len(nonce) > 128:
        nonce = state.sessions.new_presession()
    response = state.render(
        request, "login.html", status_code, csrf=state.sessions.presession_csrf(nonce), error=error, session=None
    )
    _set_cookie(state, response, PRESESSION_COOKIE, nonce)
    return response


@router.get("/login")
async def login_form(request: Request) -> Response:
    if current_session(request) is not None:
        return RedirectResponse(get_state(request).url("/coord"), status_code=303)
    return _login_page(request)


@router.post("/login")
async def login(request: Request) -> Response:
    state = get_state(request)
    form = await small_form(request)
    token, name, password = form.get("csrf"), form.get("name"), form.get("password")
    if not state.sessions.check_presession(request.cookies.get(PRESESSION_COOKIE), token if isinstance(token, str) else None):
        raise _forbidden()
    name = name.strip() if isinstance(name, str) else ""
    password = password if isinstance(password, str) else ""
    if not (state.login_limiter.allow("address:" + _address(request)) and state.login_limiter.allow("name:" + name[:64])):
        return _login_page(request, "Too many attempts. Wait a few minutes and try again.", 429)
    account = await run_in_threadpool(state.accounts.authenticate, name, password)
    if account is None:
        if _valid_name(name) and state.accounts.get(name) is not None:
            state.log.append("coordinator.login_failed", "system", data={"name": name, "reason": "wrong_password"})
        return _login_page(request, "That name and password were not accepted.", 401)
    cookie, session = state.sessions.create(account.name, account.actor)
    state.log.append("coordinator.login", account.actor, session_id=session.label, data={"name": account.name})
    response = RedirectResponse(state.url("/coord?msg=logged_in"), status_code=303)
    # Kept when the browser is closed, for the session's lifetime (it still ends on logging out, after
    # SESSION_IDLE without use, or when the server restarts, since sessions are held in its memory).
    _set_cookie(state, response, SESSION_COOKIE, cookie, max_age=SESSION_LIFETIME)
    _clear_cookie(state, response, PRESESSION_COOKIE)
    return response


def _valid_name(name: str) -> bool:
    try:
        check_name(name)
    except AccountError:
        return False
    return True


@router.post("/logout")
async def logout(request: Request) -> Response:
    state = get_state(request)
    session, _ = await checked_form(request)
    state.sessions.end(request.cookies.get(SESSION_COOKIE))
    state.log.append("coordinator.logout", session.actor, session_id=session.label, data={"name": session.account})
    response = RedirectResponse(state.url("/"), status_code=303)
    _clear_cookie(state, response, SESSION_COOKIE)
    return response


# ---------------------------------------------------------------------------
# Coordinator pages


def _common(state: WebState, session: Session) -> dict[str, Any]:
    return {
        "session": session,
        "csrf": session.csrf,
        "kinds": KINDS,
        "kind_labels": KIND_LABELS,
        "max_bytes": MAX_UPLOAD_BYTES,
        "max_boards": MAX_AUDIT_BOARDS,
        "max_results_bytes": MAX_RESULTS_UPLOAD_BYTES,
        "max_files": MAX_FILES,
        "max_total_bytes": MAX_TOTAL_BYTES,
        "all_counties": COLORADO_COUNTIES,
    }


@router.get("/coord")
async def coord_home(request: Request) -> Response:
    state = get_state(request)
    session = current_session(request)
    if session is None:
        return _login_redirect(state)
    snapshot = await run_in_threadpool(state.cache.get)
    message = MESSAGES.get(request.query_params.get("msg", ""))
    if request.query_params.get("msg") == "boards":
        county, count = known_county(request.query_params.get("county", "")), request.query_params.get("count", "")
        if county and count.isdigit():
            message = f"Recorded {count} audit board{'' if count == '1' else 's'} for {county}."
    return state.render(
        request, "coord_home.html", snap=snapshot, message=message, fetch=state.fetch, crawl_off=_crawl_off(state), **_common(state, session)
    )


def _crawl_off(state: WebState) -> dict[str, Any] | None:
    """How the election setup replaces crawling the Audit Center, if it does: ``mirror`` (a past election
    replayed from the public mirror, with its updates applied and the next one) or ``off``."""
    from corla_observer.election import SetupError, load_setup
    from corla_observer.rehearse import mirror_updates_applied

    try:
        setup = load_setup(state.workspace)
    except SetupError:
        return None
    if setup is None:
        return None
    if setup.mirror is not None:
        done = mirror_updates_applied(state.log)
        updates = setup.mirror.updates
        return {
            "mode": "mirror", "election": setup.mirror.election, "repository": setup.mirror.repository,
            "applied": min(done, len(updates)), "total": len(updates),
            "next": updates[done].title if done < len(updates) else None, "last": updates[min(done, len(updates)) - 1].title if done else None,
        }  # fmt: skip
    return None if setup.crawl else {"mode": "off"}


@router.get("/coord/upload")
async def upload_form(request: Request) -> Response:
    state = get_state(request)
    session = current_session(request)
    if session is None:
        return _login_redirect(state)
    return state.render(request, "coord_upload.html", error=None, **_common(state, session))


def _upload_page(request: Request, state: WebState, session: Session, status_code: int, error: str) -> Response:
    return state.render(request, "coord_upload.html", status_code, error=error, **_common(state, session))


def _results_page(request: Request, state: WebState, session: Session, intakes: list[Intake], notes: list[str]) -> Response:
    status_code = 422 if any(i.rejected for i in intakes) else 200
    reports = [i.upload.report.to_dict() if i.upload and i.upload.report else None for i in intakes]
    pending_ids = [i.pending for i in intakes if i.pending]
    return state.render(
        request, "coord_upload_result.html", status_code, items=list(zip(intakes, reports, strict=True)), notes=notes,
        pending_ids=pending_ids, **_common(state, session),
    )  # fmt: skip


def _still_waiting(store: PendingStore, others: str, actor: str, done: str) -> list[Intake]:
    """The other files of the same upload still waiting for a choice, so that choosing for one doesn't lose the rest."""
    waiting = []
    for ident in dict.fromkeys(others.split()):
        if ident == done:
            continue
        item = store.get(ident, actor)
        if item is not None:
            size = item.path.stat().st_size if item.path.exists() else 0
            waiting.append(Intake(item.original_name, size, item.detection, pending=ident))
    return waiting


def _after(state: WebState, intakes: list[Intake]) -> list[str]:
    from corla_observer.schedule import update_after_upload

    if any(i.results is not None and i.results.accepted for i in intakes):
        return update_after_upload(state.workspace, state.log)
    return []


@router.post("/coord/upload")
async def upload(request: Request) -> Response:
    """One or more files, each detected (`corla_observer.intake`): no kind or county is asked for, unless it cannot be told."""
    state = get_state(request)
    session = current_session(request)
    if session is None:
        raise _forbidden()
    if not (state.upload_limiter.allow("actor:" + session.actor) and state.upload_limiter.allow("address:" + _address(request))):
        return _upload_page(request, state, session, 429, "Too many uploads; wait and try again.")

    def before_file(fields: dict[str, str]) -> int:
        if not state.sessions.check_csrf(session, fields.get("csrf")):
            raise CsrfError("missing or wrong CSRF token")
        return MAX_FILE_BYTES

    try:
        form = await parse_upload(
            request.stream(),
            request.headers.get("content-type", ""),
            state.workspace.root / STAGING_DIRNAME,
            file_field="file",
            before_file=before_file,
            max_files=MAX_FILES,
            max_total=MAX_TOTAL_BYTES,
        )
    except CsrfError:
        raise _forbidden() from None
    except FormError as exc:
        return _upload_page(request, state, session, 400, f"Upload refused: {exc}.")
    try:
        fields = form.fields
        if not state.sessions.check_csrf(session, fields.get("csrf")):
            raise _forbidden()
        if form.too_large:
            last = form.files[-1]
            name = _original_name(last.name)
            state.log.append(
                "upload.rejected", session.actor, session_id=session.label,
                data={"kind": None, "original_name": name, "reason": "too_large", "size_limit": last.limit if last.too_large else MAX_TOTAL_BYTES,
                      "sha256": None, "size": None},
            )  # fmt: skip
            limit = f"{last.limit // (1024 * 1024)} MB per file" if last.too_large else f"{MAX_TOTAL_BYTES // (1024 * 1024)} MB for all the files together"
            return _upload_page(request, state, session, 422, f"Upload refused: {name} is too large (the limit is {limit}); nothing was kept.")
        if not form.files:
            return _upload_page(request, state, session, 400, "Choose a file to upload.")
        # Optional overrides (the command line's --kind and --county); the page itself never asks for them.
        kind = fields.get("kind", "").strip() or None
        county = (fields.get("county", "") or fields.get("scope", "")).strip() or None
        extra = {"source_url": fields.get("source_url", ""), "note": fields.get("note", ""), "file_time": fields.get("file_time", "")}
        known = known_counties(state.workspace, state.log, state.config.counties)
        intakes: list[Intake] = []
        pending = PendingStore(state.workspace)
        for item in form.files:
            assert item.path is not None
            intake = await run_in_threadpool(
                partial(
                    take, state.workspace, state.log, item.path, item.name, session.actor, kind=kind, county=county, known=known,
                    source_url=extra["source_url"], note=extra["note"], file_time=extra["file_time"] or None, session_id=session.label,
                )
            )  # fmt: skip
            if not intake.detection.complete:
                intake.pending = pending.put(item.path, intake.original_name, session.actor, intake.detection, extra)
                item.path = None
            intakes.append(intake)
    except UploadError as exc:
        return _upload_page(request, state, session, 400, f"Upload refused: {exc}.")
    finally:
        form.discard()
    notes = await run_in_threadpool(_after, state, intakes)
    return _results_page(request, state, session, intakes, notes)


@router.post("/coord/upload/choose")
async def upload_choose(request: Request) -> Response:
    """The coordinator's choice of kind and county for a file whose content did not say, or its discarding."""
    state = get_state(request)
    session, form = await checked_form(request)
    store = PendingStore(state.workspace)
    ident = str(form.get("pending") or "")
    waiting = store.get(ident, session.actor)
    if waiting is None:
        return _upload_page(request, state, session, 410, "That file is no longer waiting for a choice (files wait two hours): upload it again.")
    if form.get("action") == "discard":
        store.discard(ident)
        rest = _still_waiting(store, str(form.get("others") or ""), session.actor, ident)
        if rest:
            return _results_page(request, state, session, rest, [f"{waiting.original_name} was discarded; nothing was recorded or kept."])
        return _upload_page(request, state, session, 200, f"{waiting.original_name} was discarded; nothing was recorded or kept.")
    kind = str(form.get("kind") or "").strip() or None
    county = str(form.get("county") or "").strip() or None
    try:
        intake = await run_in_threadpool(
            lambda: take(
                state.workspace, state.log, waiting.path, waiting.original_name, session.actor, kind=kind, county=county,
                detection=waiting.detection, source_url=waiting.extra.get("source_url", ""), note=waiting.extra.get("note", ""),
                file_time=waiting.extra.get("file_time") or None, session_id=session.label,
            )
        )  # fmt: skip
    except UploadError as exc:
        return _upload_page(request, state, session, 400, f"Upload refused: {exc}.")
    if intake.detection.complete:
        store.discard(ident)
    else:
        intake.pending = ident
    notes = await run_in_threadpool(_after, state, [intake])
    return _results_page(request, state, session, [intake, *_still_waiting(store, str(form.get("others") or ""), session.actor, ident)], notes)


def _original_name(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    return ("".join(ch for ch in base if ch.isprintable()).strip() or "(unnamed)")[:255]


@router.post("/coord/audit-boards")
async def audit_boards(request: Request) -> Response:
    state = get_state(request)
    session, form = await checked_form(request)
    county, count, note = form.get("county"), form.get("count"), form.get("note")
    try:
        number = int(str(count).strip())
    except ValueError:
        number = 0
    try:
        await run_in_threadpool(
            lambda: record_audit_boards(
                state.log,
                str(county or ""),
                number,
                session.actor,
                placeholder=form.get("placeholder") == "yes",
                note=str(note or ""),
            )
        )
    except UploadError as exc:
        snapshot = await run_in_threadpool(state.cache.get)
        return state.render(
            request, "coord_home.html", 400, snap=snapshot, message=None, error=str(exc), fetch=state.fetch,
            crawl_off=_crawl_off(state), **_common(state, session),
        )  # fmt: skip
    named = known_county(str(county or "")) or ""
    return RedirectResponse(state.url(f"/coord?msg=boards&county={quote(named)}&count={number}"), status_code=303)


@router.get("/coord/map")
async def coord_map(request: Request) -> Response:
    state = get_state(request)
    session = current_session(request)
    if session is None:
        return _login_redirect(state)
    snapshot = await run_in_threadpool(state.cache.get)
    text = await run_in_threadpool(map_text, snapshot, for_ai=False)
    ai_text = await run_in_threadpool(map_text, snapshot, for_ai=True)
    return state.render(request, "coord_map.html", snap=snapshot, map_text=text, ai_text=ai_text, **_common(state, session))


@router.post("/coord/fetch")
async def start_fetch(request: Request) -> Response:
    state = get_state(request)
    session, _ = await checked_form(request)
    job = state.fetch
    with job.lock:
        if job.running:
            return RedirectResponse(state.url("/coord?msg=fetch_running"), status_code=303)
        if job.last_request and time.monotonic() - job.last_request < state.config.fetch_interval:
            return RedirectResponse(state.url("/coord?msg=fetch_too_soon"), status_code=303)
        job.last_request = time.monotonic()
        state.log.append("coordinator.fetch_requested", session.actor, session_id=session.label, data={"source": "web"})
        job.started, job.finished, job.error = _now(), None, None

        def run() -> None:
            try:
                state.config.crawl(state.workspace, state.log)
            except Exception as exc:
                job.error = f"{type(exc).__name__}: {exc}"[:500]
            finally:
                job.finished = _now()

        job.thread = threading.Thread(target=run, name="corla-observer-fetch", daemon=True)
        job.thread.start()
    return RedirectResponse(state.url("/coord?msg=fetch_started"), status_code=303)


@router.get("/coord/stats")
async def coord_stats(request: Request) -> Response:
    state = get_state(request)
    session = current_session(request)
    if session is None:
        return _login_redirect(state)
    snapshot = await run_in_threadpool(state.cache.get)

    def comparisons() -> list[tuple[str, str, list[BallotComparison], int, str | None]]:
        # Every board's ballots that some observer recorded a difference on, or where observers disagree,
        # with contest-by-contest findings (which name the contest) before two-tap notes; each pull list apart.
        from corla_observer.pulllists import events_for, extract_lists

        events = list(state.log.iter_events())
        lists = extract_lists(events)
        by_list = {}
        found = []
        for b in [*snapshot.boards, *snapshot.test_boards]:
            if b.list_key not in by_list:
                by_list[b.list_key] = events_for(events, b.list_key, lists)
            compared = compare_board(by_list[b.list_key], b.county, b.board)
            found.append((b.county, b.board, [c for c in compared if c.verdict != "match"], len(compared), b.list_label if b.targeted else None))
        return found

    compared = await run_in_threadpool(comparisons)
    return state.render(request, "coord_stats.html", snap=snapshot, compared=compared, **_common(state, session))


@router.get("/coord/flags")
async def coord_flags(request: Request) -> Response:
    state = get_state(request)
    session = current_session(request)
    if session is None:
        return _login_redirect(state)
    snapshot = await run_in_threadpool(state.cache.get)
    message = MESSAGES.get(request.query_params.get("msg", ""))
    return state.render(request, "coord_flags.html", snap=snapshot, message=message, error=None, **_common(state, session))


@router.post("/coord/flags/address")
async def address_flag(request: Request) -> Response:
    state = get_state(request)
    session, form = await checked_form(request)
    key, note = str(form.get("key") or ""), str(form.get("note") or "")
    snapshot = await run_in_threadpool(state.cache.get)
    flag = next((f for f in snapshot.flag_objects if f.key == key), None)
    error = None
    if flag is None:
        error = "That flag is no longer listed; reload the page."
    else:
        try:
            await run_in_threadpool(lambda: record_addressed(state.log, flag, session.actor, note, session_id=session.label))
        except ValueError as exc:
            error = str(exc).capitalize() + "."
    if error:
        return state.render(request, "coord_flags.html", 400, snap=snapshot, message=None, error=error, **_common(state, session))
    return RedirectResponse(state.url("/coord/flags?msg=addressed"), status_code=303)


@router.post("/coord/flags/reopen")
async def reopen_flag(request: Request) -> Response:
    state = get_state(request)
    session, form = await checked_form(request)
    key = str(form.get("key") or "")
    snapshot = await run_in_threadpool(state.cache.get)
    if key not in snapshot.addressed:
        return state.render(
            request, "coord_flags.html", 400, snap=snapshot, message=None, error="That flag is not marked addressed.", **_common(state, session)
        )
    await run_in_threadpool(lambda: record_reopened(state.log, key, session.actor, str(form.get("note") or ""), session_id=session.label))
    return RedirectResponse(state.url("/coord/flags?msg=reopened"), status_code=303)


# ---------------------------------------------------------------------------
# Observer and reader links


def site_url(request: Request, state: WebState) -> str:
    """The site's address with its path prefix, as the browser reached it.

    The scheme is the request's: a proxy's ``X-Forwarded-Proto`` if it sent
    one, else the request's own on this machine (a local demo on plain
    http), else https when cookies are Secure (behind an https front end).
    """
    host = request.headers.get("host") or request.url.netloc
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    hostname = host.rsplit(":", 1)[0].strip("[]") if not host.endswith("]") else host.strip("[]")
    if forwarded in ("http", "https"):
        scheme = forwarded
    elif hostname in ("localhost", "127.0.0.1", "::1") or hostname.startswith("127."):
        scheme = request.url.scheme
    else:
        scheme = "https" if state.config.secure_cookies else request.url.scheme
    return f"{scheme}://{host}{state.prefix}"


def _links_page(
    request: Request, state: WebState, session: Session, status_code: int = 200, **context: Any
) -> Response:
    context.setdefault("made", None)
    context.setdefault("error", None)
    context.setdefault("message", None)
    links = LinkStore(state.workspace.root).links()
    return state.render(
        request,
        "coord_links.html",
        status_code,
        links=links,
        roles=ROLES,
        max_link_boards=MAX_LINK_BOARDS,
        **context,
        **_common(state, session),
    )


@router.get("/coord/links")
async def links_page(request: Request) -> Response:
    state = get_state(request)
    session = current_session(request)
    if session is None:
        return _login_redirect(state)
    message = "Link revoked." if request.query_params.get("msg") == "revoked" else None
    return _links_page(request, state, session, message=message)


@router.post("/coord/links")
async def make_link(request: Request) -> Response:
    state = get_state(request)
    session, form = await checked_form(request)
    role, name, county = str(form.get("role") or ""), str(form.get("name") or "").strip(), str(form.get("county") or "")
    try:
        board = int(str(form.get("board") or "").strip())
    except ValueError:
        board = 0
    try:
        token, link = await run_in_threadpool(
            lambda: LinkStore(state.workspace.root).create(role, name, county, board, state.log, actor=session.actor)
        )
    except PracticeError as exc:
        return _links_page(request, state, session, 400, error=str(exc))
    path = "observe" if link.role == "observer" else "read"
    made = {"link": link, "url": f"{site_url(request, state)}/{path}/{token}"}
    return _links_page(request, state, session, made=made)


@router.post("/coord/links/revoke")
async def revoke_link(request: Request) -> Response:
    state = get_state(request)
    session, form = await checked_form(request)
    digest = str(form.get("id") or "")
    revoked = await run_in_threadpool(lambda: LinkStore(state.workspace.root).revoke(digest, state.log, actor=session.actor))
    if revoked is None:
        return _links_page(request, state, session, 400, error="No such link.")
    return RedirectResponse(state.url("/coord/links?msg=revoked"), status_code=303)
