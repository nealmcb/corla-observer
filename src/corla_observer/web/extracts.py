"""Coordinator pages for the seed's entry and for the committed extract.

Routes (under the app's path prefix; every one needs a coordinator session,
and every POST its CSRF token, as in `corla_observer.web.coordinator`):

``GET /coord/seed``
    Where seed entry stands: the seed in effect, who entered it, any
    matching second entry, and any mismatches and changes; the forms to
    enter the seed and to check it with a second entry.
``POST /coord/seed/set``
    An entry (`corla_observer.seeds.enter_seed`), in effect at once, with a
    free-text note of where the seed came from. A different value from the
    seed in effect is recorded as a change, and flagged. The selections are
    then predicted from it at once (`corla_observer.schedule.update_after_upload`),
    so a real county's extract can be built without waiting for a cycle.
``POST /coord/seed/confirm``
    An optional second, independent entry
    (`corla_observer.seeds.confirm_seed`), compared with the seed in effect.
    By the coordinator who entered it, it is recorded as a re-entry. A
    mismatch is shown, and flagged (`corla_observer.flags`).
``GET /coord/extract``
    Each county with a redacted CVR: its pull list and audit boards, its
    RLA extract with its hash, order and pages, links to download its
    printout (a PDF per layout), its plain text, provenance and CSV, and the
    form to build it (without one, the page says what the RLA sample is
    waiting for, the seed first of all, and that observers' links open on a
    targeted list meanwhile); then its targeted lists
    (`corla_observer.pulllists`), each with the same downloads and the
    choice to hide it from observers.
    Every download below takes ``list=KEY`` for a targeted list's extract.
``POST /coord/extract/build``
    Build and record a county's extract (`corla_observer.extract.build_extract`).
``GET /coord/extract/pdf?county=C&board=B&layout=L``
    One board's printout, a PDF (`corla_observer.pdfextract`), as a
    download (`corla_observer.pdfextract.printout`): the one recorded when
    the extract was built (its hash is in the ``extract.built`` event), else
    drawn anew, which gives the same bytes. ``layout`` is one of `corla_observer.extract.LAYOUTS`.
``GET /coord/extract/provenance?county=C&board=B``
    The board's provenance page alone (`corla_observer.extract.provenance`):
    the seed, the extract's hash, every input's hash and the page index's hash.
``GET /coord/extract/text?county=C&board=B``
    The board's extract as plain text.
``GET /coord/extract/csv?county=C[&board=B]``
    The extract (one board, or the whole county) as CSV for a spreadsheet,
    escaped against formula injection.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from corla_observer.classify import known_county
from corla_observer.extract import (
    DEFAULT_LAYOUT,
    MAX_BOARDS,
    ExtractError,
    board_pages,
    build_extract,
    default_layout,
    extract_boards,
    extract_pdfs,
    extract_software,
    latest_extract,
    layouts,
    render_provenance_html,
    render_text,
    screen_line,
    to_csv,
)
from corla_observer.pdfextract import pdf_filename, printout
from corla_observer.pulllists import RLA, county_lists
from corla_observer.pulllists import load as load_list
from corla_observer.printlayout import LAYOUTS as PRINT_LAYOUTS
from corla_observer.seeds import SeedError, confirm_seed, enter_seed, seed_state
from corla_observer.uploads import status as upload_status
from corla_observer.testcounty import SAMPLE_COUNT as TEST_SAMPLE_COUNT
from corla_observer.web.app import WebState, get_state
from corla_observer.web.coordinator import _common, _login_redirect, checked_form, current_session
from corla_observer.web.sessions import Session

router = APIRouter()

SEED_MESSAGES = {
    "entered": "Seed entered and in effect. Another coordinator may enter it independently to check it.",
    "confirmed": "The second entry matches the seed in effect: the seed is confirmed.",
    "mismatch": "The second entry does NOT match the seed in effect. Check the announced seed and enter the right one.",
    "reentered": "Seed entered again, unchanged.",
    "changed": "Seed entered again with a DIFFERENT value. It is in effect, and flagged as a change.",
}
EXTRACT_MESSAGES = {
    "built": "Extract built and recorded.",
    "retired": "Targeted list retired: hidden from observers' switchers and the public dashboard.",
    "restored": "Targeted list restored.",
    "published": "The list's details are published on the public dashboard.",
    "withdrawn": "The list's details are off the public dashboard (the log keeps them).",
}


# ---------------------------------------------------------------------------
# Seed


def _seed_page(state: WebState, request: Request, session: Session, status_code: int = 200, error: str | None = None) -> Response:
    seeds = seed_state(state.log.iter_events())
    message = SEED_MESSAGES.get(request.query_params.get("msg", ""))
    return state.render(
        request,
        "coord_seed.html",
        status_code,
        seeds=seeds,
        message=message,
        mismatch=request.query_params.get("msg") in ("mismatch", "changed"),
        error=error,
        own_entry=seeds.current is not None and seeds.current.actor == session.actor,
        **_common(state, session),
    )


@router.get("/coord/seed")
async def seed_page(request: Request) -> Response:
    state = get_state(request)
    session = current_session(request)
    if session is None:
        return _login_redirect(state)
    return await run_in_threadpool(_seed_page, state, request, session)


def _enter_seed(state: WebState, seed: str, actor: str, source: str) -> bool:
    """Enter the seed, then predict the selections from it (as a cycle would); whether it changed the seed in effect."""
    from corla_observer.schedule import update_after_upload

    previous = seed_state(state.log.iter_events()).seed
    enter_seed(state.log, seed, actor, source)
    if not state.walkthrough:
        update_after_upload(state.workspace, state.log)
    return previous is not None and previous != seed_state(state.log.iter_events()).seed


@router.post("/coord/seed/set")
async def seed_set(request: Request) -> Response:
    state = get_state(request)
    session, form = await checked_form(request)
    seed, source = str(form.get("seed") or ""), str(form.get("source") or "")
    try:
        changed = await run_in_threadpool(_enter_seed, state, seed, session.actor, source)
    except SeedError as exc:
        return await run_in_threadpool(_seed_page, state, request, session, 400, str(exc))
    return RedirectResponse(state.url(f"/coord/seed?msg={'changed' if changed else 'entered'}"), status_code=303)


@router.post("/coord/seed/confirm")
async def seed_confirm(request: Request) -> Response:
    state = get_state(request)
    session, form = await checked_form(request)
    seed, source = str(form.get("seed") or ""), str(form.get("source") or "")
    try:
        outcome = await run_in_threadpool(confirm_seed, state.log, seed, session.actor, source)
    except SeedError as exc:
        return await run_in_threadpool(_seed_page, state, request, session, 400, str(exc))
    if outcome.reentry:
        msg = "reentered" if outcome.matched else "changed"
    else:
        msg = "confirmed" if outcome.matched else "mismatch"
    return RedirectResponse(state.url(f"/coord/seed?msg={msg}"), status_code=303)


# ---------------------------------------------------------------------------
# Extract


def _extract_rows(state: WebState) -> list[dict[str, Any]]:
    rows = []
    for county, entry in upload_status(state.log, state.config.counties).items():
        if "redacted_cvr" not in entry.latest:
            continue
        found = latest_extract(state.workspace, state.log, county)
        row: dict[str, Any] = {"county": county, "status": entry, "extract": None, "targeted": []}
        for item in county_lists(state.log.iter_events(), county, retired=True):
            if not item.targeted:
                continue
            extract = load_list(state.workspace, item)
            row["targeted"].append(
                {"list": item, "layouts": layouts(extract), "extract_in_rla": len(extract.get("checks", {}).get("in_rla") or []), "boards": [
                    (b, [len(board_pages(extract, item.sha256, b, layout)) for layout in layouts(extract)], extract["boards"][str(b)])
                    for b in extract_boards(extract)
                ]}
            )  # fmt: skip
        if found is not None:
            extract, sha = found
            row["extract"] = extract
            row["sha256"] = sha
            row["ident"] = screen_line(extract, sha)
            row["layouts"] = layouts(extract)
            row["boards"] = [
                (b, [len(board_pages(extract, sha, b, layout)) for layout in layouts(extract)], extract["boards"][str(b)])
                for b in extract_boards(extract)
            ]
        rows.append(row)
    return rows


def _extract_page(state: WebState, request: Request, session: Session, status_code: int = 200, error: str | None = None) -> Response:
    return state.render(
        request,
        "coord_extract.html",
        status_code,
        rows=_extract_rows(state),
        has_seed=seed_state(state.log.iter_events()).seed is not None,
        message=EXTRACT_MESSAGES.get(request.query_params.get("msg", "")),
        error=error,
        max_extract_boards=MAX_BOARDS,
        test_sample_count=TEST_SAMPLE_COUNT,
        layout_label=layout_label,
        **_common(state, session),
    )


@router.get("/coord/extract")
async def extract_page(request: Request) -> Response:
    state = get_state(request)
    session = current_session(request)
    if session is None:
        return _login_redirect(state)
    return await run_in_threadpool(_extract_page, state, request, session)


@router.post("/coord/extract/build")
async def extract_build(request: Request) -> Response:
    state = get_state(request)
    session, form = await checked_form(request)
    county = known_county(str(form.get("county") or ""))
    boards_text = str(form.get("boards") or "").strip()
    count_text = str(form.get("sample_count") or "").strip()
    try:
        if county is None:
            raise ExtractError("choose a Colorado county")
        boards = int(boards_text) if boards_text else None
        count = int(count_text) if count_text else None
        await run_in_threadpool(
            lambda: build_extract(state.workspace, state.log, county, boards=boards, actor=session.actor, sample_count=count)
        )
    except ValueError as exc:  # ExtractError, or an unreadable board count
        return await run_in_threadpool(_extract_page, state, request, session, 400, str(exc))
    return RedirectResponse(state.url("/coord/extract?msg=built"), status_code=303)


def _chosen(state: WebState, request: Request, board_required: bool = True) -> tuple[dict[str, Any], str, int | None]:
    county = known_county(request.query_params.get("county", ""))
    key = request.query_params.get("list") or RLA
    found = None
    if county and key == RLA:
        found = latest_extract(state.workspace, state.log, county)
    elif county:
        item = next((x for x in county_lists(state.log.iter_events(), county, retired=True) if x.key == key), None)
        found = (load_list(state.workspace, item), item.sha256) if item is not None else None
    if found is None:
        raise HTTPException(status_code=404, detail="No extract for that county.")
    extract, sha = found
    text = request.query_params.get("board", "")
    if not text and not board_required:
        return extract, sha, None
    try:
        board = int(text)
    except ValueError:
        raise HTTPException(status_code=404, detail="No such audit board.") from None
    if board not in extract_boards(extract):
        raise HTTPException(status_code=404, detail="No such audit board.")
    return extract, sha, board


def _filename(extract: dict[str, Any], sha: str, board: int | None, suffix: str) -> str:
    county = "".join(ch if ch.isalnum() else "-" for ch in str(extract["county"]))
    part = f"-board-{board}" if board is not None else ""
    return f"extract-{county}{part}-{sha[:12]}.{suffix}"


def layout_label(layout: str) -> str:
    """How a layout is named on a download link: "portrait, one column"."""
    lay = PRINT_LAYOUTS.get(layout)
    label = lay.description if lay else layout
    return label + (", the default" if layout == DEFAULT_LAYOUT else "")


def pdf_response(body: bytes, filename: str) -> Response:
    """A PDF as a download, with a file name a phone keeps."""
    return Response(
        body,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
    )


def _layout(request: Request, extract: dict[str, Any]) -> str:
    layout = request.query_params.get("layout") or default_layout(extract)
    if layout not in layouts(extract):
        raise HTTPException(status_code=404, detail="No such layout for this extract.")
    return layout


def _pdf(state: WebState, request: Request) -> Response:
    extract, sha, board = _chosen(state, request)
    assert board is not None
    layout = _layout(request, extract)
    return pdf_response(printout(state.workspace, state.log, extract, sha, board, layout), pdf_filename(extract, sha, board, layout))


@router.get("/coord/extract/pdf")
async def extract_pdf(request: Request) -> Response:
    state = get_state(request)
    if current_session(request) is None:
        return _login_redirect(state)
    return await run_in_threadpool(_pdf, state, request)


def _provenance_view(state: WebState, request: Request) -> str:
    extract, sha, board = _chosen(state, request)
    assert board is not None
    return render_provenance_html(
        extract,
        sha,
        board,
        stylesheet=state.static_url("extract.css"),
        integrity=state.integrity["extract.css"],
        links=[("Back", state.url("/coord/extract"))],
        software=extract_software(state.log, sha),
    )


@router.get("/coord/extract/provenance")
async def extract_provenance(request: Request) -> Response:
    state = get_state(request)
    if current_session(request) is None:
        return _login_redirect(state)
    return HTMLResponse(await run_in_threadpool(_provenance_view, state, request))


@router.get("/coord/extract/text")
async def extract_text(request: Request) -> Response:
    state = get_state(request)
    if current_session(request) is None:
        return _login_redirect(state)
    extract, sha, board = await run_in_threadpool(_chosen, state, request)
    assert board is not None
    text = await run_in_threadpool(lambda: render_text(extract, sha, board, software=extract_software(state.log, sha)))
    return PlainTextResponse(text, headers={"Content-Disposition": f'inline; filename="{_filename(extract, sha, board, "txt")}"'})


@router.get("/coord/extract/csv")
async def extract_csv(request: Request) -> Response:
    state = get_state(request)
    if current_session(request) is None:
        return _login_redirect(state)
    extract, sha, board = await run_in_threadpool(_chosen, state, request, False)
    body = await run_in_threadpool(lambda: to_csv(extract, sha, board, extract_pdfs(state.log, sha)))
    return Response(
        body.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{_filename(extract, sha, board, "csv")}"'},
    )
