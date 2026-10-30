"""Coordinator pages for targeted ballots (`corla_observer.targets`, docs/TARGETING.md): find ballots that show a pattern of votes, draw some at random, keep the working list, and send it to the county.

Routes (under the app's path prefix; every one needs a coordinator session,
and every POST its CSRF token, as in `corla_observer.web.coordinator`; the
counts are never on a public page):

``GET /coord/targets?county=C[&v=I:K...][&add=NAME...][&q=TEXT]``
    The pattern grid: the county's statewide and US House contests, and any
    contest added by name (``add``), each tightest first by the results in
    hand, with a check box per candidate (``v`` = contest index : choice
    index), the number of ballots matching the boxes checked (and, beside
    each box, if it were checked too), the split-ticket presets, the
    pattern row (pattern · count · Seed · Ballots · Draw); ``q`` searches
    the CVR's contest names to add one. Then the working list: its sources
    with their include boxes, its ballots in pull order, the manual entry
    form, the text for an email and "Send to county"; then the sent lists.
    ``static/targets.js`` updates the counts as boxes change; without it
    the form's button does.
``GET /coord/targets/count?county=C&v=...[&add=...]``
    The counts as JSON, for the script.
``POST /coord/targets/draw``
    Draw from the boxes checked (``selection``, ``seed``, ``size``): the draw
    is recorded and becomes an included source of the working list.
``POST /coord/targets/draw/again``
    Another draw of a source's pattern, seed and size (``source``).
``GET /coord/targets/draw?county=C&n=N[&votes=1]``, ``.../draw/csv``, ``.../draw/record``
    A draw's ballots, with the text for an email (the expected votes only
    with ``votes=1``); as CSV; and its stored record (JSON), whose hash is
    in its ``targets.drawn`` event.
``POST /coord/targets/list/add``
    Imprinted IDs entered by hand (``ids``, ``note``), each looked up in the
    CVR and the manifest, as one source; what each was found as is shown
    inline. One found in neither is not added.
``POST /coord/targets/include``
    Include a source in the working list or exclude it (``source``, ``included``).
``GET /coord/targets/list/csv?county=C``
    The working list as CSV.
``POST /coord/targets/send``
    Send the working list to the county (`corla_observer.targets.send`):
    Targeted list N, with its extract, printouts, CSV and email text; the
    sources sent leave the working list. Sends run one at a time.
``GET /coord/targets/sent/csv?county=C&list=KEY``, ``.../sent/record``
    A sent list as CSV, and its stored list record.
``POST /coord/targets/lists``
    Retire or restore a sent list (hidden from observers' switchers and the
    public dashboard), or publish or withdraw its details on the public
    dashboard (for after its ballots are examined).
``POST /coord/targets/allowed``
    The most targeted ballots the county allows in all, shown beside the
    running total of its sent lists, with a warning above it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse

from corla_observer import pulllists
from corla_observer import targets as tg
from corla_observer.classify import DEMO_COUNTIES, is_test_county, known_county
from corla_observer.cvr import CvrReader
from corla_observer.datamap import contest_key
from corla_observer.enr import Results, contest_label, latest_details, read_results
from corla_observer.extract import MAX_BOARDS, ExtractError, county_manifest, layouts
from corla_observer.seeds import seed_state
from corla_observer.uploads import status as upload_status
from corla_observer.web.app import WebState, get_state
from corla_observer.web.coordinator import _common, _login_redirect, checked_form, current_session
from corla_observer.web.extracts import layout_label
from corla_observer.web.sessions import Session

router = APIRouter()

MESSAGES = {
    "drawn": "Drawn and added to the working list.",
    "included": "Source included in the working list.",
    "excluded": "Source excluded from the working list (kept on record).",
    "retired": "Targeted list retired: hidden from observers' switchers and the public dashboard.",
    "restored": "Targeted list restored.",
    "published": "The list's details are published on the public dashboard.",
    "withdrawn": "The list's details are off the public dashboard (the log keeps them).",
    "allowed": "Allowed total recorded.",
}
MAX_ADDED = 12

_STATE_RESULTS: dict[str, Results] = {}


def _state_results(state: WebState, county: str) -> Results | None:
    """The state's election night results in use, for a real county (a test county's contests are not the state's)."""
    if is_test_county(county):
        return None
    item = latest_details(state.log, state.workspace).get("state")
    if item is None:
        return None
    if item.sha256 not in _STATE_RESULTS:
        try:
            _STATE_RESULTS.clear()
            _STATE_RESULTS[item.sha256] = read_results(state.workspace.get_bytes(item.sha256), "state")
        except Exception:
            return None
    return _STATE_RESULTS[item.sha256]


def _corla_totals(state: WebState, county: str) -> dict[str, tg.CorlaTotal]:
    """CORLA's posted margins for the county's contests, from the data map's contest list (none for a test county)."""
    if is_test_county(county):
        return {}
    found = {}
    datamap = state.cache.get().datamap
    if datamap is None:
        return {}
    for spec in datamap.contests:
        if spec.margin is not None and (spec.statewide or county in spec.counties):
            found[spec.name] = tg.CorlaTotal(spec.margin, spec.ballot_card_count)
    return found


def _cvr_sha(state: WebState, county: str) -> str | None:
    entry = upload_status(state.log, [county]).get(county)
    received = entry.latest.get("redacted_cvr") if entry else None
    return received.sha256 if received else None


@dataclass
class _County:
    county: str
    cvr_sha256: str
    votes: tg.CvrVotes
    shown: list[tg.ShownContest]
    results: Results | None
    corla: dict[str, tg.CorlaTotal]
    locations: dict[tuple[str, str], str] = field(default_factory=dict)
    sizes: dict[tuple[str, str], int] = field(default_factory=dict)
    manifest_sha256: str | None = None
    #: Contests added to the grid by name, as the CVR names them.
    added: list[str] = field(default_factory=list)

    @property
    def has_results(self) -> bool:
        return self.results is not None or bool(self.corla)


def _load(state: WebState, county: str, added: Sequence[str] = ()) -> _County:
    sha = _cvr_sha(state, county)
    if sha is None:
        raise HTTPException(status_code=404, detail=f"No redacted CVR from {county}.")
    results = _state_results(state, county)
    header = CvrReader(state.workspace.path_for(sha)).header
    groups = dict(tg.candidate_contests(header.contests, results))
    votes = tg.cached_votes(state.workspace, sha)
    names = {contest_key(contest_label(n)[0]): n for n in header.contests}
    extra = []
    for name in added:
        found = names.get(contest_key(contest_label(name)[0]))
        if found is not None and found not in groups and found not in extra:
            extra.append(found)
    for name in extra[:MAX_ADDED]:
        groups[name] = "added"
    corla = _corla_totals(state, county)
    shown = tg.target_contests(votes, results, groups, corla)
    snapshot = state.cache.get()
    manifest = county_manifest(state.workspace, state.log, county, snapshot.datamap)
    return _County(county, sha, votes, shown, results, corla, manifest.locations, manifest.sizes, manifest.sha256, extra[:MAX_ADDED])


def _county(request: Request, value: str | None = None) -> str | None:
    return known_county(value if value is not None else request.query_params.get("county", ""))


def _selection(request: Request, votes: tg.CvrVotes) -> dict[int, list[int]]:
    return tg.parse_selection(request.query_params.getlist("v") + request.query_params.getlist("s"), votes)


def _added(request: Request) -> list[str]:
    return [a for a in request.query_params.getlist("add") if a.strip()][:MAX_ADDED]


def _seed_in_effect(state: WebState) -> str:
    current = seed_state(state.log.iter_events()).current
    return current.seed if current else ""


def _pct(x: float | None) -> str:
    return "" if x is None else f"{100 * x:.1f}%"


def _url(state: WebState, path: str, **params: Any) -> str:
    query = urlencode([(k, v) for k, vs in params.items() for v in (vs if isinstance(vs, list) else [vs]) if v is not None and v != ""])
    return state.url(path + ("?" + query if query else ""))


@dataclass
class SentView:
    """A sent list as the page shows it: the list, its record, its ballots and its email text."""

    item: pulllists.PullList
    entries: list[tg.Entry]
    record: dict[str, Any] | None
    email: str
    layouts: tuple[str, ...]
    sources: list[dict[str, Any]]


def _sent_views(state: WebState, county: str, lists: list[pulllists.PullList]) -> list[SentView]:
    views = []
    for item in lists:
        record = pulllists.record(state.workspace, item)
        extract = pulllists.load(state.workspace, item)
        if record is not None:
            entries = tg.sent_entries(record)
            by_id = {e.imprinted_id: e for e in entries}
            for b in extract["ballots"]:
                entry = by_id.get(str(b["imprinted_id"]))
                if entry is not None:
                    entry.ballot_type = entry.ballot_type or None
                    entry.in_rla = entry.in_rla or bool(b.get("in_rla"))
        else:
            seen: set[int] = set()
            entries = []
            for b in extract["ballots"]:
                if b["position"] not in seen:
                    seen.add(b["position"])
                    entries.append(tg.Entry(str(b["imprinted_id"]), location=b.get("location"), in_rla=bool(b.get("in_rla"))))
        details = [f"Extract {item.sha256[:12]}" + (f"; record {item.record_sha256[:16]}" if item.record_sha256 else "")]
        email = tg.list_email(county, item.label, entries, f"sent {item.built[:10]}", details)
        sources = list((record or {}).get("sources") or []) if record and int(record.get("format") or 1) >= 2 else [
            {"kind": "draw", "description": f"{p.get('name')}: {p.get('description')}", "draw": p.get("draw")} for p in (record or {}).get("patterns") or []
        ]
        views.append(SentView(item, entries, record, email, layouts(extract), sources))
    return views


def _page(state: WebState, request: Request, session: Session, county: str | None, status_code: int = 200, error: str | None = None, **extra: Any) -> Response:
    with_cvr = [c for c, e in upload_status(state.log).items() if "redacted_cvr" in e.latest]
    if county is None:
        return state.render(request, "coord_targets.html", status_code, county=None, counties=with_cvr, error=error, message=None, **_common(state, session))
    added = _added(request)
    data = _load(state, county, added)
    selection = _selection(request, data.votes)
    matching, clean = data.votes.count(selection)
    grid = data.votes.grid_counts(selection, [c.index for c in data.shown])
    query = request.query_params.get("q", "").strip()[:80]
    shown_keys = {contest_key(c.name) for c in data.shown}
    search = [cv for cv in tg.search_contests(data.votes, query) if contest_key(cv.name) not in shown_keys] if query else []
    found = tg.sources(state.workspace, state.log, county)
    working = tg.working_sources(found)
    rla = tg.rla_ids(state.workspace, state.log, county)
    entries = tg.working_list(found, data.votes, data.locations, data.sizes, rla)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    every = pulllists.county_lists(state.log.iter_events(), county, retired=True)
    lists = [x for x in every if x.targeted]
    active = [x for x in lists if not x.retired]
    sent = _sent_views(state, county, lists)
    just_sent_key = request.query_params.get("list", "")
    just_sent = next((v for v in sent if v.item.key == just_sent_key), None) if request.query_params.get("msg") == "sent" else None
    distinct = {e.imprinted_id for v in sent if not v.item.retired for e in v.entries}
    allowed = tg.allowed_total(state.log, county)
    presets = [
        (p.label, _url(state, "/coord/targets", county=county, add=added, v=[f"{c}:{k}" for c, ks in p.requirement.items() for k in ks]))
        for p in tg.presets(data.shown)
    ]
    number, _ = tg.next_list_number(state.log, county)
    return state.render(
        request,
        "coord_targets.html",
        status_code,
        county=county,
        counties=with_cvr,
        data=data,
        selection=selection,
        selection_text=tg.selection_text(selection),
        description=tg.describe(tg.pattern_definition(selection, data.votes)),
        matching=matching,
        clean=clean,
        grid=grid,
        presets=presets,
        added=added,
        query=query,
        search=search,
        sources=working,
        source_by_id={s.id: s for s in found},
        entries=entries,
        in_rla=sum(1 for e in entries if e.in_rla),
        list_email=tg.list_email(county, "Working targeted list (not yet sent)", entries, today) if entries else "",
        sent=sent,
        just_sent=just_sent,
        next_number=number,
        has_rla=any(not x.targeted for x in every),
        layout_label=layout_label,
        active_total=sum(x.ballots for x in active),
        active_distinct=len(distinct),
        allowed=allowed,
        over_allowed=allowed is not None and len(distinct) > allowed,
        max_draw=tg.MAX_DRAW,
        max_seed=tg.MAX_SEED,
        seed=_seed_in_effect(state),
        pct=_pct,
        result_sources=tg.RESULT_SOURCES,
        max_extract_boards=MAX_BOARDS,
        demo_notes=DEMO_COUNTIES,
        draw_size=tg.DRAW_SIZE,
        message=MESSAGES.get(request.query_params.get("msg", "")),
        error=error,
        **extra,
        **_common(state, session),
    )


def warm(state: WebState) -> None:
    """Read the test and demonstration counties' CVRs into the cache (`corla_observer.targets.cached_votes`), so their first page is quick."""
    try:
        counties = [c for c, e in upload_status(state.log).items() if "redacted_cvr" in e.latest and is_test_county(c)]
        for county in counties[: tg.CACHE_SIZE]:
            sha = _cvr_sha(state, county)
            if sha is not None:
                tg.cached_votes(state.workspace, sha)
    except Exception:  # noqa: BLE001 - only a warm-up: the page reads the CVR itself if this fails
        pass


@router.get("/coord/targets")
async def targets_page(request: Request) -> Response:
    state = get_state(request)
    session = current_session(request)
    if session is None:
        return _login_redirect(state)
    county = _county(request)
    drawn = request.query_params.get("drawn")

    def page() -> Response:
        text = None
        if county and drawn and drawn.isdigit():
            record = next((r for r, _ in tg.draws(state.workspace, state.log, county) if r.draw == int(drawn)), None)
            if record is not None:
                skipped = f"; {len(record.collisions)} already listed or sampled, skipped" if record.collisions else ""
                text = f"Draw {record.draw}: {len(record.ballots)} ballots added to the working list{skipped}."
        return _page(state, request, session, county, 200, None, added_message=text)

    return await run_in_threadpool(page)


@router.get("/coord/targets/count")
async def targets_count(request: Request) -> Response:
    state = get_state(request)
    if current_session(request) is None:
        return JSONResponse({"error": "log in"}, status_code=403)
    county = _county(request)
    if county is None:
        return JSONResponse({"error": "no such county"}, status_code=404)

    def count() -> dict[str, Any]:
        data = _load(state, county, _added(request))
        selection = _selection(request, data.votes)
        matching, clean = data.votes.count(selection)
        return {
            "matching": matching,
            "clean": clean,
            "grid": data.votes.grid_counts(selection, [c.index for c in data.shown]),
            "selection": tg.selection_text(selection),
            "description": tg.describe(tg.pattern_definition(selection, data.votes)),
        }

    return JSONResponse(await run_in_threadpool(count))


def _form_county(form: Any) -> str:
    county = known_county(str(form.get("county") or ""))
    if county is None:
        raise HTTPException(status_code=404, detail="No such county.")
    return county


def make_draw(
    state: WebState, county: str, definition: list[dict[str, Any]], *, seed: str, size_text: str, actor: str, session_id: str | None = None
) -> tg.DrawRecord | str:
    """Draw from a pattern (``seed`` blank: the seed in effect), skipping ballots already held, and record the draw; returns its record, or what is wrong."""
    size_text = size_text.strip() or str(tg.DRAW_SIZE)
    if not size_text.isdigit() or not 1 <= int(size_text) <= tg.MAX_DRAW:
        return f"the number of ballots to draw is 1 to {tg.MAX_DRAW}"
    data = _load(state, county)
    in_effect = _seed_in_effect(state)
    chosen = seed.strip() or in_effect
    if not chosen:
        return "enter a seed for the draw (any text; no RLA seed has been entered yet)"
    source = "the RLA seed in effect" if chosen == in_effect else "entered for this draw"
    try:
        pattern = tg.pattern_of(county, definition)
        found = tg.sources(state.workspace, state.log, county)
        record = tg.draw(
            data.votes, county=county, cvr_sha256=data.cvr_sha256, pattern=pattern, seed=chosen, seed_source=source,
            number=tg.draw_count(state.log, county) + 1, locations=data.locations, manifest_sha256=data.manifest_sha256, actor=actor,
            size=int(size_text), exclude=tg.holders(state.workspace, state.log, county, found),
        )  # fmt: skip
        record, _ = tg.record_draw(state.workspace, state.log, record, session_id=session_id)
    except tg.DrawError as exc:
        return str(exc)
    return record


def _problem(state: WebState, request: Request, session: Session, county: str, text: str) -> Response:
    return _page(state, request, session, county, 400, text[0].upper() + text[1:] + ".")


@router.post("/coord/targets/draw")
async def draw_from_grid(request: Request) -> Response:
    state = get_state(request)
    session, form = await checked_form(request)
    county = _form_county(form)

    def run() -> tg.DrawRecord | str:
        data = _load(state, county)
        selection = tg.parse_selection(str(form.get("selection") or ""), data.votes)
        return make_draw(state, county, tg.pattern_definition(selection, data.votes), seed=str(form.get("seed") or ""),
                         size_text=str(form.get("size") or ""), actor=session.actor, session_id=session.label)  # fmt: skip

    result = await run_in_threadpool(run)
    if isinstance(result, str):
        return await run_in_threadpool(_problem, state, request, session, county, result)
    return RedirectResponse(_url(state, "/coord/targets", county=county, drawn=str(result.draw)) + "#list", status_code=303)


@router.post("/coord/targets/draw/again")
async def draw_again(request: Request) -> Response:
    state = get_state(request)
    session, form = await checked_form(request)
    county = _form_county(form)
    source_id = str(form.get("source") or "")

    def run() -> tg.DrawRecord | str:
        source = next((s for s in tg.sources(state.workspace, state.log, county) if s.id == source_id), None)
        if source is None or source.draw is None:
            return "no such draw"
        return make_draw(state, county, source.draw.definition, seed=str(form.get("seed") or source.draw.seed),
                         size_text=str(form.get("size") or source.draw.size), actor=session.actor, session_id=session.label)  # fmt: skip

    result = await run_in_threadpool(run)
    if isinstance(result, str):
        return await run_in_threadpool(_problem, state, request, session, county, result)
    return RedirectResponse(_url(state, "/coord/targets", county=county, drawn=str(result.draw)) + "#list", status_code=303)


def _draw(state: WebState, request: Request) -> tuple[str, tg.DrawRecord, str]:
    county = _county(request)
    number = request.query_params.get("n", "")
    if county is None or not number.isdigit():
        raise HTTPException(status_code=404, detail="No such draw.")
    found = next(((r, s) for r, s in tg.draws(state.workspace, state.log, county) if r.draw == int(number)), None)
    if found is None:
        raise HTTPException(status_code=404, detail="No such draw.")
    return county, found[0], found[1]


def _expected(state: WebState, request: Request, record: tg.DrawRecord) -> dict[str, str] | None:
    if request.query_params.get("votes") != "1":
        return None
    data = _load(state, record.county)
    if data.cvr_sha256 != record.cvr_sha256:
        votes = tg.load_votes(state.workspace.path_for(record.cvr_sha256), [str(d["contest"]) for d in record.definition])
    else:
        votes = data.votes
    return tg.expected_for([str(b["imprinted_id"]) for b in record.ballots], record.definition, votes)


@router.get("/coord/targets/draw")
async def show_draw(request: Request) -> Response:
    state = get_state(request)
    session = current_session(request)
    if session is None:
        return _login_redirect(state)

    def view() -> Response:
        county, record, sha = _draw(state, request)
        expected = _expected(state, request, record)
        source = next((s for s in tg.sources(state.workspace, state.log, county) if s.id == record.source_id), None)
        return state.render(
            request,
            "coord_target_draw.html",
            county=county,
            record=record,
            sha256=sha,
            expected=expected,
            email=tg.draw_email(record, sha, expected),
            description=tg.describe(record.definition),
            source=source,
            max_draw=tg.MAX_DRAW,
            max_seed=tg.MAX_SEED,
            **_common(state, session),
        )

    return await run_in_threadpool(view)


def _csv_response(body: str, filename: str) -> Response:
    return Response(
        body.encode("utf-8-sig"), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


def _safe_name(county: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in county)


@router.get("/coord/targets/draw/csv")
async def draw_csv(request: Request) -> Response:
    state = get_state(request)
    if current_session(request) is None:
        return _login_redirect(state)

    def body() -> Response:
        county, record, _ = _draw(state, request)
        text = tg.to_csv(county, f"draw {record.draw} of {record.size}", record.ballots, _expected(state, request, record))
        return _csv_response(text, f"targeted-{_safe_name(county)}-draw-{record.draw}.csv")

    return await run_in_threadpool(body)


@router.get("/coord/targets/draw/record")
async def draw_record(request: Request) -> Response:
    state = get_state(request)
    if current_session(request) is None:
        return _login_redirect(state)
    county, record, sha = await run_in_threadpool(_draw, state, request)
    return Response(
        state.workspace.get_bytes(sha),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="targeted-{_safe_name(county)}-draw-{record.draw}-{sha[:12]}.json"'},
    )


@router.post("/coord/targets/list/add")
async def list_add(request: Request) -> Response:
    state = get_state(request)
    session, form = await checked_form(request)
    county = _form_county(form)
    text = str(form.get("ids") or "")[:4000]
    note = str(form.get("note") or "")[:tg.MAX_NOTE]

    def run() -> tuple[int, list[str]] | str:
        notes: list[str] = []
        data = _load(state, county)
        ids: list[str] = []
        for part in re.split(r"[\s,;]+", text):
            if not part:
                continue
            normalized = tg.normalize_id(part)
            if normalized is None:
                notes.append(f"{part[:20]}: not an imprinted ID")
                continue
            entry = tg.lookup(normalized, data.votes, data.locations, data.sizes)
            if not entry.found:
                notes.append(f"{normalized}: not found in the CVR or the manifest, not added")
                continue
            where = "in the CVR" if entry.cvr_status else "not in the CVR"
            where += ", in the manifest" if entry.in_manifest else ", not in the manifest"
            where += f", stored at {entry.location}" if entry.location else ", no storage location"
            notes.append(f"{entry.imprinted_id}: {where}")
            ids.append(entry.imprinted_id)
        if not ids and not notes:
            return "enter an imprinted ID, such as 101-12-5"
        if not ids:
            return "; ".join(notes)
        try:
            tg.enter_ids(state.workspace, state.log, county, ids, session.actor, note=note, session_id=session.label)
        except tg.DrawError as exc:
            return str(exc)
        return len(ids), notes

    result = await run_in_threadpool(run)
    if isinstance(result, str):
        return await run_in_threadpool(_problem, state, request, session, county, result)
    added, notes = result
    message = f"Added {added} to the working list (one source, which can be excluded)."
    return await run_in_threadpool(lambda: _page(state, request, session, county, 200, None, added_message=message, lookup_notes=notes))


@router.post("/coord/targets/include")
async def include_source(request: Request) -> Response:
    state = get_state(request)
    session, form = await checked_form(request)
    county = _form_county(form)
    source = str(form.get("source") or "")
    included = str(form.get("included") or "") == "1"
    if not re.fullmatch(r"(draw|entry) [0-9]{1,9}", source):
        raise HTTPException(status_code=400, detail="No such source.")
    try:
        await run_in_threadpool(lambda: tg.set_included(state.log, county, source, included, session.actor, session_id=session.label))
    except tg.DrawError as exc:
        return await run_in_threadpool(_problem, state, request, session, county, str(exc))
    return RedirectResponse(_url(state, "/coord/targets", county=county, msg="included" if included else "excluded") + "#list", status_code=303)


@router.get("/coord/targets/list/csv")
async def list_csv(request: Request) -> Response:
    state = get_state(request)
    if current_session(request) is None:
        return _login_redirect(state)
    county = _county(request)
    if county is None:
        raise HTTPException(status_code=404, detail="No such county.")

    def body() -> Response:
        data = _load(state, county)
        found = tg.sources(state.workspace, state.log, county)
        entries = tg.working_list(found, data.votes, data.locations, data.sizes, tg.rla_ids(state.workspace, state.log, county))
        return _csv_response(tg.to_csv(county, "working list (not yet sent)", [e.row() for e in entries]), f"targeted-{_safe_name(county)}-working.csv")

    return await run_in_threadpool(body)


def _boards(text: str) -> int:
    boards = int(text)
    if not 1 <= boards <= MAX_BOARDS:
        raise ValueError
    return boards


@router.post("/coord/targets/send")
async def send_list(request: Request) -> Response:
    state = get_state(request)
    session, form = await checked_form(request)
    county = _form_county(form)
    boards_text = str(form.get("boards") or "1").strip()

    def run() -> tg.SentList | str:
        try:
            boards = _boards(boards_text)
        except ValueError:
            return f"the audit boards must be 1 to {MAX_BOARDS}"
        data = _load(state, county)
        try:
            return tg.send(state.workspace, state.log, county, session.actor, votes=data.votes, locations=data.locations, sizes=data.sizes,
                           boards=boards, session_id=session.label)  # fmt: skip
        except (tg.DrawError, ExtractError) as exc:
            return str(exc)

    outcome = await run_in_threadpool(run)
    if isinstance(outcome, str):
        return await run_in_threadpool(_problem, state, request, session, county, outcome)
    return RedirectResponse(_url(state, "/coord/targets", county=county, msg="sent", list=outcome.key) + "#sent", status_code=303)


def _sent_list(state: WebState, request: Request) -> tuple[str, pulllists.PullList]:
    county = _county(request)
    key = request.query_params.get("list", "")
    if county is None or not re.fullmatch(r"t-[0-9a-f]{12}", key):
        raise HTTPException(status_code=404, detail="No such list.")
    item = pulllists.find_list(state.log.iter_events(), county, key, retired=True)
    if item is None or not item.targeted:
        raise HTTPException(status_code=404, detail="No such list.")
    return county, item


@router.get("/coord/targets/sent/csv")
async def sent_csv(request: Request) -> Response:
    state = get_state(request)
    if current_session(request) is None:
        return _login_redirect(state)

    def body() -> Response:
        county, item = _sent_list(state, request)
        (view,) = _sent_views(state, county, [item])
        return _csv_response(tg.to_csv(county, item.label, [e.row() for e in view.entries]), f"targeted-{_safe_name(county)}-list-{item.number}.csv")

    return await run_in_threadpool(body)


@router.get("/coord/targets/sent/record")
async def sent_record(request: Request) -> Response:
    state = get_state(request)
    if current_session(request) is None:
        return _login_redirect(state)
    county, item = await run_in_threadpool(_sent_list, state, request)
    if item.record_sha256 is None:
        raise HTTPException(status_code=404, detail="This list has no stored record.")
    return Response(
        state.workspace.get_bytes(item.record_sha256),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="targeted-{_safe_name(county)}-list-{item.number}-{item.record_sha256[:12]}.json"'},
    )


LIST_ACTIONS = {"retire": "retired", "restore": "restored", "publish": "published", "withdraw": "withdrawn"}


@router.post("/coord/targets/lists")
async def targeted_lists(request: Request) -> Response:
    state = get_state(request)
    session, form = await checked_form(request)
    county = _form_county(form)
    key, action = str(form.get("list") or ""), str(form.get("action") or "")
    if not re.fullmatch(r"t-[0-9a-f]{12}", key) or action not in LIST_ACTIONS:
        raise HTTPException(status_code=400, detail="No such list or action.")

    def run() -> None:
        if action == "publish":
            pulllists.publish(state.workspace, state.log, county, key, session.actor, session_id=session.label)
        else:
            {"retire": pulllists.retire, "restore": pulllists.restore, "withdraw": pulllists.withdraw}[action](state.log, county, key, session.actor, session_id=session.label)

    try:
        await run_in_threadpool(run)
    except ValueError:
        raise HTTPException(status_code=404, detail="No such targeted list.") from None
    back = str(form.get("back") or "")
    target = "/coord/extract" if back == "extract" else "/coord/targets"
    return RedirectResponse(_url(state, target, county=county, msg=LIST_ACTIONS[action]) + "#sent", status_code=303)


@router.post("/coord/targets/allowed")
async def targeted_allowed(request: Request) -> Response:
    state = get_state(request)
    session, form = await checked_form(request)
    county = _form_county(form)
    text = str(form.get("allowed") or "").strip()
    if text and not text.isdigit():
        return await run_in_threadpool(lambda: _page(state, request, session, county, 400, "The allowed total is a number of ballots, or blank."))
    try:
        await run_in_threadpool(lambda: tg.set_allowed(state.log, county, int(text) if text else None, session.actor, session_id=session.label))
    except tg.DrawError as exc:
        problem = str(exc).capitalize() + "."
        return await run_in_threadpool(lambda: _page(state, request, session, county, 400, problem))
    return RedirectResponse(_url(state, "/coord/targets", county=county, msg="allowed") + "#sent", status_code=303)

