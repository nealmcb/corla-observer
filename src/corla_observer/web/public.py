"""The public dashboard: no login, read-only, the same for everyone, so a CDN can cache it.

Routes (under the app's path prefix):

``GET /``
    The election overview: the event log's head and count, the seed when
    entered, and each county taking part in observation (one that provided
    a redacted CVR, or has observer links or entries) with its status in
    one line (`corla_observer.county_status`) and in detail (CORLA's
    imports of its manifest and CVR, its files here, a redacted CVR with
    its redaction verdict and counts only, audit boards, observation
    progress), the data map's problems, and recent fetch activity. Not the change flags, many of which
    are addressed before the public would see them: they, and the data map
    problems and fetch alerts that report the same findings, are on the
    coordinator pages only (``/coord/flags``).
``GET /counties``, ``GET /counties.json``
    Every Colorado county, with its status: CORLA's imports of its manifest
    and CVR where its upload status records them, its round or stage, and
    whether it takes part in observation; San Juan County with a note that
    it counts by hand and does not take part in CORLA's audit.
``GET /fragment/dashboard``
    The same content without the page around it, which ``app.js`` swaps in
    when the log's head moves.
``GET /dashboard.json``
    The same content as JSON.
``GET /head``
    The log's head: ``{"seq": ..., "hash": ...}``.
``GET /receipt-key``, ``GET /receipt-key.pem``
    The public half of the server's receipt key (docs/RECEIPTS.md): as JSON
    (``algorithm``, ``public_key`` in base64, ``key_id``), and as PEM.
``GET /version``, ``GET /version.json``
    The running software's identity (`corla_observer.version`): its
    version, full commit hash and whether the tree is dirty, the commit's
    git log entry, and a link to the commit on GitHub. The footer of every
    page links here.
``GET /events/stream``
    Server-sent events: a ``head`` event with the log's head when the
    stream opens and whenever it moves, checked every
    ``sse_poll_interval`` seconds (one indexed query), and a comment every
    15 seconds to keep the connection open. A stream ends after
    ``sse_max_seconds`` and the browser reconnects.

Responses carry an ETag (the log head's position and hash) and ``Cache-Control:
public, no-cache``, so a cached copy is always revalidated.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import anyio
from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

from corla_observer.classify import is_test_county
from corla_observer.version import current
from corla_observer.web.app import WebState, get_state
from corla_observer.web.views import Snapshot

router = APIRouter()

#: Shared caches may keep a copy but must revalidate it (cheaply, by ETag) on every use.
PUBLIC_CACHE = "public, no-cache"
KEEPALIVE_SECONDS = 15.0


def _etag(snapshot: Snapshot, variant: str) -> str:
    return f'"{variant}-{snapshot.head_seq}-{snapshot.head_hash[:16]}"'


async def _snapshot(state: WebState) -> Snapshot:
    return await run_in_threadpool(state.cache.get)


def _not_modified(request: Request, etag: str) -> Response | None:
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": PUBLIC_CACHE})
    return None


@router.api_route("/", methods=["GET", "HEAD"])
async def dashboard(request: Request) -> Response:
    state = get_state(request)
    snapshot = await _snapshot(state)
    etag = _etag(snapshot, "page")
    cached = _not_modified(request, etag)
    if cached:
        return cached
    response = state.render(request, "dashboard.html", snap=snapshot)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = PUBLIC_CACHE
    return response


@router.api_route("/fragment/dashboard", methods=["GET", "HEAD"])
async def dashboard_fragment(request: Request) -> Response:
    state = get_state(request)
    snapshot = await _snapshot(state)
    etag = _etag(snapshot, "fragment")
    cached = _not_modified(request, etag)
    if cached:
        return cached
    response = state.render(request, "_dashboard_body.html", snap=snapshot)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = PUBLIC_CACHE
    return response


def public_dict(snapshot: Snapshot) -> dict[str, Any]:
    """A snapshot's public content as JSON values: the counties taking part in observation only."""
    return {
        "head": {"seq": snapshot.head_seq, "hash": snapshot.head_hash},
        "built_at": snapshot.built_at,
        "election": snapshot.election,
        "seed": snapshot.seed,
        "seed_source": snapshot.seed_source,
        "roll_time": snapshot.roll_time,
        "counties": [
            {k: v for k, v in dataclasses.asdict(row).items() if k != "flags"} for row in snapshot.active_counties
        ],
        # Targeted lists (not RLA) under neutral labels, with counts only, unless a coordinator published a list's details.
        "targeted_lists": [t.public_dict() for t in snapshot.public_targeted],
        # Box-targeted lookups, counted apart (docs/TARGETING.md), and the RLA sample's ballots that earlier targeted lists hold.
        "targeted_from_boxes": [x.public_dict() for x in snapshot.public_lookups],
        "previously_targeted": {c: n for c, n in snapshot.previously_targeted.items() if not is_test_county(c)},
        "problems": snapshot.public_problems,
        "recent_fetches": snapshot.public_fetches,
        "manifests_in_map": snapshot.manifests_in_map,
        "contests": snapshot.contests,
        "errors": snapshot.errors,
    }


@router.api_route("/dashboard.json", methods=["GET", "HEAD"])
async def dashboard_json(request: Request) -> Response:
    state = get_state(request)
    snapshot = await _snapshot(state)
    etag = _etag(snapshot, "json")
    cached = _not_modified(request, etag)
    if cached:
        return cached
    return JSONResponse(public_dict(snapshot), headers={"ETag": etag, "Cache-Control": PUBLIC_CACHE})


@router.api_route("/counties", methods=["GET", "HEAD"])
async def counties(request: Request) -> Response:
    state = get_state(request)
    snapshot = await _snapshot(state)
    etag = _etag(snapshot, "counties")
    cached = _not_modified(request, etag)
    if cached:
        return cached
    response = state.render(request, "counties.html", snap=snapshot)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = PUBLIC_CACHE
    return response


def counties_dict(snapshot: Snapshot) -> dict[str, Any]:
    """Every county's status as JSON values."""
    return {
        "head": {"seq": snapshot.head_seq, "hash": snapshot.head_hash},
        "election": snapshot.election,
        "corla_upload_status": snapshot.corla.source,
        "counties": [dataclasses.asdict(status) for status in snapshot.statuses.values()],
    }


@router.api_route("/counties.json", methods=["GET", "HEAD"])
async def counties_json(request: Request) -> Response:
    state = get_state(request)
    snapshot = await _snapshot(state)
    etag = _etag(snapshot, "counties-json")
    cached = _not_modified(request, etag)
    if cached:
        return cached
    return JSONResponse(counties_dict(snapshot), headers={"ETag": etag, "Cache-Control": PUBLIC_CACHE})


@router.api_route("/head", methods=["GET", "HEAD"])
async def head(request: Request) -> Response:
    seq, digest = get_state(request).log.head()
    return JSONResponse({"seq": seq, "hash": digest}, headers={"Cache-Control": "no-cache"})


@router.api_route("/receipt-key", methods=["GET", "HEAD"])
async def receipt_key(request: Request) -> Response:
    return JSONResponse(get_state(request).receipts.public_info(), headers={"Cache-Control": PUBLIC_CACHE})


@router.api_route("/receipt-key.pem", methods=["GET", "HEAD"])
async def receipt_key_pem(request: Request) -> Response:
    return Response(
        get_state(request).receipts.public_pem, media_type="application/x-pem-file", headers={"Cache-Control": PUBLIC_CACHE}
    )


@router.api_route("/version", methods=["GET", "HEAD"])
async def version_page(request: Request) -> Response:
    state = get_state(request)
    response = state.render(request, "version.html", identity=current())
    response.headers["Cache-Control"] = "no-cache"
    return response


@router.api_route("/version.json", methods=["GET", "HEAD"])
async def version_json(request: Request) -> Response:
    return JSONResponse(current().to_dict(), headers={"Cache-Control": "no-cache"})


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


@router.get("/events/stream")
async def event_stream(request: Request) -> Response:
    state = get_state(request)
    config = state.config
    if state.sse_clients >= config.sse_max_clients:
        return Response(
            "Too many live connections; reload later.\n",
            status_code=503,
            media_type="text/plain",
            headers={"Retry-After": "30"},
        )

    async def stream() -> AsyncIterator[str]:
        # Counted only once the stream runs, so the count is released whatever happens.
        with state.sse_lock:
            state.sse_clients += 1
        try:
            started = last_sent = time.monotonic()
            last: tuple[int, str] | None = None
            yield "retry: 5000\n\n"
            while True:
                current = state.log.head()
                now = time.monotonic()
                if current != last:
                    last = current
                    last_sent = now
                    yield _sse("head", {"seq": current[0], "hash": current[1]})
                elif now - last_sent >= KEEPALIVE_SECONDS:
                    last_sent = now
                    yield ": keepalive\n\n"
                if now - started >= config.sse_max_seconds or await request.is_disconnected():
                    return
                await anyio.sleep(config.sse_poll_interval)
        finally:
            with state.sse_lock:
                state.sse_clients -= 1

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
