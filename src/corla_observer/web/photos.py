"""The "Check a page" pages: a photo of a printed extract page, or its typed short hashes, checked against the committed extract.

The check is `corla_observer.photocheck.check_photo` (docs/PHOTO_CHECK.md).
Routes (under the app's path prefix):

``GET /coord/check[?extract=X&page=P]``, ``POST /coord/check``
    For coordinators (a session; the POST's CSRF token comes before the
    photo, so it is checked before any of the photo is read). The GET shows
    the form (a photo, from a file or the phone's camera, or the short
    hashes), and with ``extract`` and ``page``, the pages they locate, each
    as committed, with its commitment's proofs. The POST checks the photo
    and shows the verdict, the photo beside the committed page as text,
    each line's result and the proofs (for a line unread or differing:
    what was read in each pass, why it was not accepted, and a crop of it,
    inline as a ``data:`` image, the page's CSP allowing that, only where
    the photo is kept); every extract in the workspace can be checked.
``GET /observe/TOKEN/check``, ``POST /observe/TOKEN/check`` (and ``/read/TOKEN/check``)
    The same for whoever holds an observer's or a practice reader's link,
    for their own county's extracts only, recorded against the link (a
    paper observer's photos of their marked pages arrive the same way).
``GET /check``, ``POST /check``
    The same for anyone, only when the app is configured with
    ``public_check`` (``corla-observer serve --public-check``), which is
    off by default: during the audit the pages' content is kept out of
    sight of the audit boards. The public check never stores the photo.
``GET /coord/check/photo/SHA``, ``GET /observe/TOKEN/check/photo/SHA``, ``GET /read/TOKEN/check/photo/SHA``
    A stored photo (without its metadata), to a coordinator, or to the link
    that uploaded it. Photos are never served publicly.

Every check is recorded as ``photo.checked`` (`corla_observer.photocheck.record_check`)
with the photo stored under its hash in the workspace, except on the
public route. Photos are limited in size (`MAX_PHOTO_BYTES`; a larger
request is refused by `corla_observer.web.app.BodyLimitMiddleware` with
a page saying so) and type (JPEG, PNG, WebP, TIFF, recognized by
content); the form's script (``static/photoshrink.js``) shrinks a large
photo in the browser before it is sent; checks are rate-limited
per account, link or address, and at most two run at once
(``WebState.photo_slots``).
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool

from corla_observer.photocheck import (
    CheckResult,
    PageFinder,
    Verdict,
    check_photo,
    lookup_short,
    page_text,
    photo_checked_by,
    record_check,
)
from corla_observer.photoimage import MAX_PHOTO_BYTES, PhotoError, ToolMissingError, load_photo, too_large_message
from corla_observer.practice import Link, LinkStore
from corla_observer.uploads import STAGING_DIRNAME
from corla_observer.web.app import CONTENT_SECURITY_POLICY, WebState, get_state
from corla_observer.web.coordinator import CsrfError, _address, _common, _forbidden, _login_redirect, current_session
from corla_observer.web.forms import FormError, parse_upload
from corla_observer.web.sessions import Session

router = APIRouter()

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
#: Each verdict's CSS class and symbol (the words are always shown too, never the colour alone).
VERDICT_STYLE = {
    Verdict.MATCH: ("match", "✓"),
    Verdict.UNREAD: ("unread", "?"),
    Verdict.MISMATCH: ("mismatch", "✗"),
    Verdict.UNKNOWN: ("unknown", "∅"),
}
LINE_STYLE = {"matched": ("ok", "✓", "matched"), "unread": ("warning", "?", "unread"), "differs": ("fail", "✗", "differs")}


@dataclass(frozen=True)
class Checker:
    """Who is checking, and what they may see."""

    via: str
    actor: str
    #: The counties whose extracts may be searched; None for all.
    counties: tuple[str, ...] | None
    #: The page's own URL (form action), and the stored photos' URL prefix (None: photos are not stored or shown).
    action: str
    photos: str | None
    session: Session | None = None
    link: Link | None = None


def _coordinator(state: WebState, session: Session) -> Checker:
    return Checker("coordinator", session.actor, None, state.url("/coord/check"), state.url("/coord/check/photo/"), session=session)


def _linked(state: WebState, token: str, kind: str) -> Checker:
    link = LinkStore(state.workspace.root).get(token)
    role = "observer" if kind == "observe" else "reader"
    if link is None or link.role != role:
        raise HTTPException(status_code=404, detail="Not Found")
    base = f"/{kind}/{token}/check"
    return Checker(role, link.actor, (link.county,), state.url(base), state.url(base + "/photo/"), link=link)


def _public(state: WebState) -> Checker:
    if not state.config.public_check:
        raise HTTPException(status_code=404, detail="Not Found")
    return Checker("public", "system", None, state.url("/check"), None)


def _context(state: WebState, who: Checker) -> dict[str, Any]:
    context: dict[str, Any] = {
        "who": who,
        "max_photo_bytes": MAX_PHOTO_BYTES,
        "verdict_style": VERDICT_STYLE,
        "line_style": LINE_STYLE,
        "session": who.session,
        "csrf": who.session.csrf if who.session else None,
    }
    if who.session is not None:
        context.update(_common(state, who.session))
    return context


def _render(state: WebState, request: Request, who: Checker, status_code: int = 200, **extra: Any) -> Response:
    response = state.render(request, "check.html", status_code, **_context(state, who), **extra)
    response.headers["Cache-Control"] = "no-store"
    return response


def _lookup(state: WebState, who: Checker, extract: str, page: str) -> dict[str, Any]:
    with_index = lookup_short(state.workspace, state.log, extract, page, counties=who.counties)
    finder = PageFinder(state.workspace, state.log)
    found = [{"location": loc, "commitments": commits, "lines": page_text(finder, loc)} for loc, commits in with_index.pages]
    return {"lookup": found, "lookup_error": with_index.error, "typed": {"extract": extract, "page": page}}


async def _get(request: Request, who: Checker) -> Response:
    state = get_state(request)
    extract = request.query_params.get("extract", "").strip()[:64]
    page = request.query_params.get("page", "").strip()[:64]
    extra: dict[str, Any] = {}
    if page:
        extra = await run_in_threadpool(_lookup, state, who, extract, page)
    return _render(state, request, who, **extra)


async def _post(request: Request, who: Checker) -> Response:
    state = get_state(request)
    limiter_key = f"{who.via}:{who.actor}"
    if not (state.photo_limiter.allow(limiter_key) and state.photo_limiter.allow("address:" + _address(request))):
        return _render(state, request, who, 429, error="Too many checks; wait a while and try again.")

    def before_file(fields: dict[str, str]) -> int:
        if who.session is not None and not state.sessions.check_csrf(who.session, fields.get("csrf")):
            raise CsrfError("missing or wrong CSRF token")
        return MAX_PHOTO_BYTES

    try:
        form = await parse_upload(
            request.stream(), request.headers.get("content-type", ""), state.workspace.root / STAGING_DIRNAME, file_field="photo", before_file=before_file
        )
    except CsrfError:
        raise _forbidden() from None
    except FormError as exc:
        return _render(state, request, who, 400, error=f"Photo refused: {exc}.")
    try:
        if form.too_large:
            return _render(state, request, who, 413, error=too_large_message(None))
        if form.file_path is None or form.file_size == 0:
            return _render(state, request, who, 400, error="Choose a photo of a printed page, or type its short hashes.")
        data = form.file_path.read_bytes()
    finally:
        form.discard()

    def run() -> tuple[CheckResult, int | None, list[Any]]:
        with state.photo_slots:  # decoding a large photo takes memory too, so it waits its turn
            photo = load_photo(data)
            result = check_photo(state.workspace, state.log, photo, counties=who.counties, work_parent=state.workspace.root / STAGING_DIRNAME)
        store = who.photos is not None
        session_id = who.session.label if who.session else None
        event = record_check(state.workspace, state.log, result, photo, actor=who.actor, via=who.via, store=store, session_id=session_id)
        finder = PageFinder(state.workspace, state.log)
        lines = page_text(finder, result.location) if result.location else []
        return result, event.seq, lines

    try:
        result, seq, committed = await run_in_threadpool(run)
    except PhotoError as exc:
        return _render(state, request, who, 400, error=f"Photo refused: {exc}.")
    except ToolMissingError as exc:
        return _render(state, request, who, 503, error=f"The photo check cannot run on this server: {exc}.")
    by_key = {line.key: line for line in result.lines}
    rows = [(e, by_key.get(e.key)) for e in committed] if committed else [(None, line) for line in result.lines]
    extras = [line for line in result.lines if line.kind == "extra"]
    if committed and result.kind == "page":
        rows += [(None, line) for line in extras]
    # The unmatched lines' crops of the photo, inline, only to whoever may see the stored photo; never stored or recorded.
    crops = {key: "data:image/png;base64," + base64.b64encode(png).decode("ascii") for key, png in result.crops.items()} if who.photos else {}
    response = _render(
        state, request, who, result=result, rows=rows, event_seq=seq, crops=crops,
        photo_url=(who.photos + result.photo_sha256) if who.photos and result.photo_sha256 else None,
    )  # fmt: skip
    if crops:
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY.replace("img-src 'self'", "img-src 'self' data:")
    return response


def _photo(state: WebState, who: Checker, sha: str) -> Response:
    if not _SHA_RE.match(sha):
        raise HTTPException(status_code=404, detail="Not Found")
    actor = None if who.via == "coordinator" else who.actor
    event = photo_checked_by(state.log, sha, actor)
    if event is None:
        raise HTTPException(status_code=404, detail="Not Found")
    try:
        body = state.workspace.get_bytes(sha)
    except OSError:
        raise HTTPException(status_code=404, detail="Not Found") from None
    media = str(event.data.get("media_type") or "image/jpeg")
    if media not in ("image/jpeg", "image/png"):
        media = "application/octet-stream"
    return Response(body, media_type=media, headers={"Cache-Control": "private, no-store", "Content-Disposition": "inline"})


# ---------------------------------------------------------------------------
# Coordinators


@router.get("/coord/check")
async def coord_check(request: Request) -> Response:
    state = get_state(request)
    session = current_session(request)
    if session is None:
        return _login_redirect(state)
    return await _get(request, _coordinator(state, session))


@router.post("/coord/check")
async def coord_check_post(request: Request) -> Response:
    state = get_state(request)
    session = current_session(request)
    if session is None:
        raise _forbidden()
    return await _post(request, _coordinator(state, session))


@router.get("/coord/check/photo/{sha}")
async def coord_check_photo(request: Request, sha: str) -> Response:
    state = get_state(request)
    session = current_session(request)
    if session is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return await run_in_threadpool(_photo, state, _coordinator(state, session), sha)


# ---------------------------------------------------------------------------
# Link holders


@router.get("/observe/{token}/check")
async def observe_check(request: Request, token: str) -> Response:
    return await _get(request, _linked(get_state(request), token, "observe"))


@router.post("/observe/{token}/check")
async def observe_check_post(request: Request, token: str) -> Response:
    return await _post(request, _linked(get_state(request), token, "observe"))


@router.get("/observe/{token}/check/photo/{sha}")
async def observe_check_photo(request: Request, token: str, sha: str) -> Response:
    state = get_state(request)
    return await run_in_threadpool(_photo, state, _linked(state, token, "observe"), sha)


@router.get("/read/{token}/check")
async def read_check(request: Request, token: str) -> Response:
    return await _get(request, _linked(get_state(request), token, "read"))


@router.post("/read/{token}/check")
async def read_check_post(request: Request, token: str) -> Response:
    return await _post(request, _linked(get_state(request), token, "read"))


@router.get("/read/{token}/check/photo/{sha}")
async def read_check_photo(request: Request, token: str, sha: str) -> Response:
    state = get_state(request)
    return await run_in_threadpool(_photo, state, _linked(state, token, "read"), sha)


# ---------------------------------------------------------------------------
# Anyone, after the audit


@router.get("/check")
async def public_check(request: Request) -> Response:
    return await _get(request, _public(get_state(request)))


@router.post("/check")
async def public_check_post(request: Request) -> Response:
    return await _post(request, _public(get_state(request)))

