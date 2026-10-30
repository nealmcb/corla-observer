"""Coordinator pages for running an election live: the estimates, the crawl schedule, and a walkthrough.

``GET /coord/estimates``
    Each targeted contest's estimated round-one count, central and low,
    with the margin's and the denominator's sources and the cards per
    ballot used (`corla_observer.estimate`), how each changed (from the
    ``estimate.made`` events), CORLA's count once published, and the
    inputs supplied by hand in the overrides file, each with whether a
    published source has replaced it; the results file used per scope
    (fetched or uploaded by hand), and the latest totals check
    (`corla_observer.totals`), one line per difference, the flagged ones
    first.
``GET /coord/schedule``
    The crawl schedule's windows, what the scheduler would do now, any
    sprint, the latest cycles, and when each audit center file was first
    seen (`corla_observer.schedule`); forms to start and stop a sprint
    (``POST /coord/schedule/sprint``, ``/coord/schedule/stop``).
``GET /coord/walkthrough``
    In a walkthrough workspace (`corla_observer.walkthrough`), its steps
    with their simulated times, and a button that applies the next step
    (``POST /coord/walkthrough/next``).

Every page needs a coordinator's login; every form its CSRF token.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, Response

from corla_observer.events import format_server_time
from corla_observer.web.app import get_state
from corla_observer.web.coordinator import _common, _login_redirect, checked_form, current_session

router = APIRouter()


def _estimates_view(state: Any) -> dict[str, Any]:
    from corla_observer.datamap import build_map
    from corla_observer.election import SetupError, load_setup
    from corla_observer.enr import load_results
    from corla_observer.estimate import estimate_contests, estimate_history, hand_inputs

    try:
        setup = load_setup(state.workspace)
        setup_error = None
    except SetupError as exc:
        setup, setup_error = None, str(exc)
    datamap = build_map(state.log, state.workspace)
    results = load_results(state.workspace, state.log)
    estimates = estimate_contests(datamap, setup=setup, results=results, log=state.log)
    hand = hand_inputs(datamap, state.log)
    from corla_observer.enr import results_by_scope
    from corla_observer.totals import last_totals_check

    totals = last_totals_check(state.log)
    listed = totals[2].get("differences") if totals else None
    differences = [d for d in listed if isinstance(d, dict)] if isinstance(listed, list) else []
    return {
        "totals": totals,
        "differences": sorted(differences, key=lambda d: not d.get("flag")),
        "files_on_record": {scope: len(items) for scope, items in results_by_scope(state.log, state.workspace).items()},
        "estimates": estimates,
        "history": estimate_history(state.log),
        "results": sorted(results.items()),
        "hand": hand,
        "setup": setup,
        "setup_error": setup_error,
    }


@router.get("/coord/estimates")
async def coord_estimates(request: Request) -> Response:
    state = get_state(request)
    session = current_session(request)
    if session is None:
        return _login_redirect(state)
    view = await run_in_threadpool(_estimates_view, state)
    return state.render(request, "coord_estimates.html", message=request.query_params.get("msg"), error=None, **view, **_common(state, session))


def _schedule_view(state: Any) -> dict[str, Any]:
    from corla_observer.schedule import ScheduleError, active_sprint, decide, last_cycle_start, load_schedule

    now = format_server_time(datetime.now(UTC))
    try:
        schedule, error = load_schedule(state.workspace), None
    except ScheduleError as exc:
        schedule, error = None, str(exc)
    sprint = active_sprint(state.log, now)
    first_seen: dict[str, str] = {}
    cycles = []
    for event in state.log.iter_events():
        if event.kind == "fetch.file":
            url = str(event.data.get("url"))
            simulated = event.data.get("simulated")
            moment = str(simulated["time"]) if isinstance(simulated, dict) and isinstance(simulated.get("time"), str) else event.server_time
            first_seen.setdefault(url, moment)
        elif event.kind == "schedule.cycle":
            cycles.append(event)
    return {
        "schedule": schedule,
        "schedule_error": error,
        "sprint": sprint,
        "decision": decide(schedule, sprint, last_cycle_start(state.log), now),
        "cycles": cycles[-12:][::-1],
        "first_seen": sorted(first_seen.items(), key=lambda kv: kv[1]),
    }


@router.get("/coord/schedule")
async def coord_schedule(request: Request) -> Response:
    state = get_state(request)
    session = current_session(request)
    if session is None:
        return _login_redirect(state)
    view = await run_in_threadpool(_schedule_view, state)
    return state.render(request, "coord_schedule.html", message=request.query_params.get("msg"), **view, **_common(state, session))


@router.post("/coord/schedule/sprint")
async def start_sprint(request: Request) -> Response:
    from corla_observer.schedule import request_sprint

    state = get_state(request)
    session, form = await checked_form(request)
    if state.walkthrough:
        return RedirectResponse(state.url("/coord/schedule?msg=A+walkthrough+never+crawls."), status_code=303)
    try:
        minutes = int(str(form.get("minutes") or "180"))
        every = int(str(form.get("every") or "60"))
        await run_in_threadpool(lambda: request_sprint(state.log, session.actor, minutes=minutes, every_seconds=every))
    except ValueError as exc:
        return RedirectResponse(state.url(f"/coord/schedule?msg={str(exc)[:80]}"), status_code=303)
    return RedirectResponse(state.url("/coord/schedule?msg=Sprint+requested:+the+scheduler+picks+it+up+within+20+seconds."), status_code=303)


@router.post("/coord/schedule/stop")
async def end_sprint(request: Request) -> Response:
    from corla_observer.schedule import stop_sprint

    state = get_state(request)
    session, _ = await checked_form(request)
    await run_in_threadpool(lambda: stop_sprint(state.log, session.actor))
    return RedirectResponse(state.url("/coord/schedule?msg=Sprint+stopped."), status_code=303)


@router.get("/coord/walkthrough")
async def coord_walkthrough(request: Request) -> Response:
    from corla_observer.walkthrough import state as walk_state
    from corla_observer.walkthrough import steps_table

    state = get_state(request)
    session = current_session(request)
    if session is None:
        return _login_redirect(state)
    if not state.walkthrough:
        return state.render(request, "coord_walkthrough.html", walk=None, rows=[], notes=[], **_common(state, session))
    walk = await run_in_threadpool(walk_state, state.workspace, state.log)
    notes = [n for n in (request.query_params.get("notes") or "").split("|") if n]
    return state.render(request, "coord_walkthrough.html", walk=walk, rows=steps_table(walk), notes=notes, **_common(state, session))


@router.post("/coord/walkthrough/next")
async def walkthrough_next(request: Request) -> Response:
    from urllib.parse import quote

    from corla_observer.walkthrough import WalkthroughError, next_step

    state = get_state(request)
    await checked_form(request)
    if not state.walkthrough:
        return RedirectResponse(state.url("/coord/walkthrough"), status_code=303)
    with state.walkthrough_lock:
        try:
            _, notes = await run_in_threadpool(next_step, state.workspace, state.log)
        except WalkthroughError as exc:
            notes = [f"error: {exc}"]
    return RedirectResponse(state.url("/coord/walkthrough?notes=" + quote("|".join(notes)[:1500])), status_code=303)
