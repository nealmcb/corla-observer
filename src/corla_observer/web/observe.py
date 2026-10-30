"""Live observation: the observer entry page and the practice reader page, reached by link only.

Routes (under the app's path prefix; TOKEN is a link's random token, see
`corla_observer.practice.LinkStore`):

``GET /observe/TOKEN[?list=KEY]``
    The observer's page, for one of the county's pull lists
    (`corla_observer.pulllists`): the RLA sample by default, or, with
    ``list``, a targeted list (marked "TARGETED (NOT RLA)"); a switcher in
    the page's header lists the board's lists. Until the board has an RLA
    sample (no seed yet, say), a link opens on the first targeted list
    instead (`default_key`), the switcher shown and a notice saying so. A
    list no longer available (retired, say) redirects to the default. One
    ballot at a time, its imprinted ID large, the
    expected votes in a compact list, a "Match" button and a box for what
    was heard instead. Driven by ``static/observe.js``, which keeps every
    entry on the device and queues it while offline.
``GET /observe/TOKEN?list=lookup``, ``GET /observe/TOKEN/lookup.json?id=ID&position=N``
    "Look up a ballot": a ballot picked from a storage box (docs/TARGETING.md,
    "Two kinds of targeted observation"), looked up by imprinted ID in the
    county's redacted CVR (its expected votes) and manifest (its storage
    location), shown as ballot N of the device's lookups and entered contest
    by contest; the entries are ``lookup_contest`` and ``lookup_done``
    payloads, recorded as ``observation.lookup_contest`` and
    ``observation.lookup_done`` on the list key ``lookup``
    (`corla_observer.pulllists.LOOKUP`), apart from every pull list and
    never merged into one. ``data.json?list=lookup`` returns the observer's
    earlier lookups on the board, so a reload restores them; it has no
    extract, so ``extract_sha256`` is the redacted CVR's hash.
``GET /observe/TOKEN/data.json[?list=KEY]``
    The board's ballots from the list's committed extract
    (`corla_observer.extract`, recorded by ``extract.built``; by default
    the RLA sample's, or the first targeted list's until there is one, with
    ``notice`` saying so), with the board's lists, the
    extract's hash, seed and input hashes, each contest's choices, and the
    observer's latest entry for each ballot, their contest-by-contest
    outcomes (`corla_observer.contests`) and their latest choice of entry
    mode, counting only entries on this list. The page shows the extract's short identification (county,
    board and the extract's hash, first 8 hex digits) with a link to its
    provenance, and each ballot's position on the board ("Ballot 41 of 127").
``GET /observe/TOKEN/provenance``, ``GET /read/TOKEN/provenance``
    The provenance of the extract the link's board follows
    (`corla_observer.extract.provenance`): the seed, the extract's hash,
    every input's hash and the page index's hash, as on the printed provenance
    page.
``GET /observe/TOKEN/pdf?layout=L[&list=KEY]``, ``GET /read/TOKEN/pdf?layout=L[&list=KEY]``
    The link's board's printout, a PDF (`corla_observer.pdfextract`), as a
    download, from the list's extract
    (`corla_observer.pdfextract.printout`).
``POST /observe/TOKEN/entries``
    A JSON batch of device-signed items (``{"entries": [{"payload": ...,
    "signature": ...}, ...]}``, docs/RECEIPTS.md), each checked and recorded
    by `corla_observer.signed.process_item`: a device's registration as
    ``observer.device_registered``, an entry as ``observation.entry``, and an
    imprinted ID the observer heard instead of the expected one as
    ``observation.out_of_order`` or ``observation.id_not_on_list`` after
    matching it against the board's ballots in the extract it refers to. Each recorded item is answered
    with a receipt signed by the server's receipt key (``receipts``, by
    device sequence number). Items already recorded (same device and
    sequence number, same payload) get the first recording's receipt again.
    Unsigned or unverifiable items are refused, listed in ``errors`` with a
    ``code``, and recorded as ``observation.rejected``.
``GET /read/TOKEN[?list=KEY]``, ``POST /read/TOKEN/next``, ``POST /read/TOKEN/restart``
    The practice reader, for one of the county's pull lists as above (its
    place kept per list): the next ballot to read aloud, as the audit board
    would, twice (a first reading, then, with ``?step=2``, a second reading,
    as the board's two members do and observers' two taps follow),
    optionally with deliberate differences injected and recorded
    (`corla_observer.practice.next_read`).

The token in the path is the credential: there is no login. POSTs to the
entry API must be JSON, which another site can't send without the
token anyway.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from corla_observer.contests import fold as fold_contests
from corla_observer.contests import latest_mode, page_state
from corla_observer.extract import (
    IN_RLA_MARK,
    CountyManifest,
    ExpectedBallot,
    boundary_note,
    county_manifest,
    default_layout,
    expected_votes,
    extract_software,
    id_key,
    layouts,
    print_lines,
    print_names,
    qr_rows,
    render_provenance_html,
    screen_line,
    screen_qr_payload,
    targeted_in_note,
)
from corla_observer.targets import normalize_id
from corla_observer.uploads import status as upload_status
from corla_observer.pdfextract import pdf_filename, printout
from corla_observer.seeds import seed_state
from corla_observer.pulllists import LOOKUP, LOOKUP_KINDS, LOOKUP_NAME, RLA, PullList, county_lists, events_for, extract_by_sha
from corla_observer.pulllists import load as load_list
from corla_observer.practice import (
    Link,
    LinkStore,
    PracticeError,
    board_ballots,
    current_read,
    latest_entries,
    latest_extract,
    next_read,
    reset_reads,
    seen_entries,
    token_hash,
)
from corla_observer.signed import process_item
from corla_observer.web.app import WebState, get_state
from corla_observer.web.extracts import layout_label, pdf_response
from corla_observer.web.ratelimit import Limit, RateLimiter

router = APIRouter()

MAX_BATCH = 100
INJECT_RATES = (("0", "none"), ("0.1", "about 1 in 10"), ("0.2", "about 1 in 5"), ("0.33", "about 1 in 3"))

_ABBREVIATIONS = (
    ("United States Senator", "US Senator"),
    ("State Board of Education Member", "State Board of Ed"),
    ("Congressional District", "CD"),
    ("State Representative", "State Rep"),
    ("County Commissioner", "Commissioner"),
    (" - District ", " D"),
)

_entry_limiter = RateLimiter(Limit(2000, 600))
_lookup_limiter = RateLimiter(Limit(300, 600))
SEPARATOR = " \N{MIDDLE DOT} "


def short_contest(name: str) -> str:
    """A contest name shortened for a phone screen (``US Rep D3 - DEM``)."""
    name = re.sub(r"Representative to the \d+(?:st|nd|rd|th) United States Congress", "US Rep", name)
    for long, short in _ABBREVIATIONS:
        name = name.replace(long, short)
    return name


def _link(state: WebState, token: str, role: str | None = None) -> Link:
    link = LinkStore(state.workspace.root).get(token)
    if link is None or (role is not None and link.role != role):
        raise HTTPException(status_code=404, detail="Not Found")
    return link


def _gone(state: WebState, request: Request) -> Response:
    """A plain page for a link that is unknown or revoked: what happened, and whom to ask."""
    response = state.render(request, "link_gone.html", 404, session=None)
    response.headers["Cache-Control"] = "no-store"
    return response


def _seen(state: WebState, token: str, link: Link) -> set[tuple[str, int]]:
    cache = state.observer_seen
    key = token_hash(token)
    if key not in cache:
        cache[key] = seen_entries(state.log, link)
    return cache[key]


def _asked_key(request: Request, form: dict[str, str] | None = None, *, lookup: bool = False) -> str | None:
    """The list asked for (``list`` in the form, else the query), or None for the default; an unknown form of key counts as the RLA sample.

    ``lookup`` allows the observer page's ``lookup`` key (box-targeted lookups), which the reader has no use for.
    """
    key = (form or {}).get("list") or request.query_params.get("list")
    if not key:
        return None
    if key == LOOKUP:
        return LOOKUP if lookup else RLA
    return key if re.fullmatch(r"rla|t-[0-9a-f]{12}", key) else RLA


def default_key(lists: list[PullList]) -> str:
    """The list a link opens on when none is asked for: the RLA sample, or, until the board has one, its first targeted list."""
    return RLA if not lists or any(x.key == RLA for x in lists) else lists[0].key


@dataclass
class _Chosen:
    """The link's board's pull lists, and the one asked for or the default (None if it isn't one of them)."""

    lists: list[PullList]
    chosen: PullList | None
    key: str
    events: list[Any]

    def extract(self, state: WebState) -> tuple[dict[str, Any], str] | None:
        return (load_list(state.workspace, self.chosen), self.chosen.sha256) if self.chosen is not None else None

    @property
    def rla_missing(self) -> bool:
        """No RLA sample for this board yet, though it has targeted lists: the page shows one of them, and says so."""
        return bool(self.lists) and not any(x.key == RLA for x in self.lists)

    @property
    def show_switcher(self) -> bool:
        """Whether the page's header lists the board's lists: several, or only targeted ones (so the observer sees which is shown)."""
        return len(self.lists) > 1 or self.rla_missing

    def notice(self, link: Link) -> str | None:
        """What to say when the board has no RLA sample and a targeted list is shown instead."""
        if not self.rla_missing or self.chosen is None or not self.chosen.targeted:
            return None
        why = "the seed hasn't been rolled" if seed_state(self.events).seed is None else "its extract hasn't been built"
        return f"No RLA sample for {link.county} board {link.board} yet ({why}). Showing the targeted list: {self.chosen.label}."


def _choose(state: WebState, link: Link, key: str | None) -> _Chosen:
    """The board's lists and the one with ``key``, or the default (`default_key`) when ``key`` is None."""
    events = list(state.log.iter_events())
    lists = county_lists(events, link.county, board=link.board)
    chosen_key = default_key(lists) if key is None else key
    return _Chosen(lists, next((x for x in lists if x.key == chosen_key), None), chosen_key, events)


def _list_query(key: str, sep: str = "?") -> str:
    return "" if key == RLA else f"{sep}list={key}"


def _switcher(lists: list[PullList], link: Link, lookups: int | None = None) -> list[dict[str, Any]]:
    """The lists in the page's header; with ``lookups`` (how many so far), "Look up a ballot" too, for an observer whose county has a CVR.

    The header shows the switcher when the board has several lists or only
    targeted ones, or on the lookup page; otherwise "Look up a ballot" is
    under More, so a board with the RLA sample alone keeps its plain header.
    """
    found = [{"key": x.key, "label": x.label, "ballots": x.board_ballots(link.board), "targeted": x.targeted} for x in lists]
    if lookups is not None:
        found.append({"key": LOOKUP, "label": "Look up a ballot (from a box)", "ballots": lookups, "targeted": True})
    return found


# ---------------------------------------------------------------------------
# Box-targeted lookups: a ballot picked from a storage box, by imprinted ID (docs/TARGETING.md)


@dataclass
class _LookupSource:
    """What a lookup reads: the county's redacted CVR and its manifest."""

    cvr_sha256: str
    cvr_path: Any
    manifest: CountyManifest


def _lookup_source(state: WebState, county: str) -> _LookupSource | None:
    entry = upload_status(state.log, [county]).get(county)
    received = entry.latest.get("redacted_cvr") if entry else None
    if received is None:
        return None
    return _LookupSource(received.sha256, state.workspace.path_for(received.sha256), county_manifest(state.workspace, state.log, county))


def _lookup_ballot(position: int, imprinted: str, found: ExpectedBallot | None, source: _LookupSource, board: int) -> dict[str, Any]:
    """A looked-up ballot in the extract's shape, with ``location``, ``in_manifest`` and ``lookup`` besides."""
    parts = imprinted.split("-")
    key = (id_key(parts[0]), id_key(parts[1])) if len(parts) == 3 else None
    size = source.manifest.sizes.get(key) if key else None
    return {
        "position": position,
        "board": board,
        "board_position": position,
        "imprinted_id": imprinted if found is None else found.imprinted_id,
        "location": source.manifest.locations.get(key) if key else None,
        "in_manifest": size is not None and len(parts) == 3 and parts[2].isdigit() and 1 <= int(parts[2]) <= size,
        "status": "not_in_cvr" if found is None else "redacted" if found.redacted else "votes",
        "votes": [[c, v] for c, v in found.votes] if found else [],
        "picks": [],
        "predicted": False,
        "certain": True,
        "boundary": [],
        "lookup": True,
    }


def _lookups_so_far(events: list[Any], link: Link) -> dict[int, str]:
    """This observer's lookups on the board, by position: the imprinted ID first recorded for each."""
    found: dict[int, str] = {}
    for e in events:
        if e.kind in LOOKUP_KINDS and e.actor == link.actor and e.county == link.county and e.board == str(link.board):
            position = e.data.get("position")
            if isinstance(position, int) and position not in found:
                found[position] = str(e.data.get("imprinted_id") or "")
    return found


def _lookup_extract(state: WebState, link: Link, events: list[Any]) -> tuple[dict[str, Any], str] | None:
    """A pseudo-extract of the observer's lookups so far (no extract is built for lookups), and the redacted CVR's hash."""
    source = _lookup_source(state, link.county)
    if source is None:
        return None
    so_far = _lookups_so_far(events, link)
    expected, choices = expected_votes(source.cvr_path, so_far.values()) if so_far else ({}, {})
    ballots = [_lookup_ballot(position, imprinted, expected.get(imprinted), source, link.board) for position, imprinted in sorted(so_far.items())]
    used = {c for b in ballots for c, _ in b["votes"]}
    extract = {
        "county": link.county, "order": "lookup", "seed": None, "boards": {str(link.board): len(ballots)}, "ballots": ballots,
        "choices": {c: list(choices.get(c, [])) for c in sorted(used)}, "inputs": {"redacted_cvr": source.cvr_sha256},
    }  # fmt: skip
    return extract, source.cvr_sha256


def _lookup_line(link: Link, cvr_sha256: str) -> str:
    return SEPARATOR.join([link.county, f"Board {link.board}", "LOOKED UP (NOT RLA)", f"CVR {cvr_sha256[:8]}"])


def _screen_ballots(extract: dict[str, Any], sha: str, board: int) -> list[dict[str, Any]]:
    """The board's ballots as the observer page shows them: the expected votes laid out for a screen, the marks and the QR code."""
    found = board_ballots(extract, board)
    lookup = extract.get("order") == "lookup"
    ballots = [
        {
            "position": b["position"],
            "n": b["board_position"],
            "id": b["imprinted_id"],
            "votes": [[c, v, c, []] for c, v in b["votes"]],
            "redacted": b["status"] == "redacted",
            "missing": b["status"] == "not_in_cvr",
            "possible": not b.get("certain", True),
            "boundary": boundary_note(b["boundary"]) if b.get("boundary") else "",
            "source": str(b.get("source_list") or ""),
            "in_rla": IN_RLA_MARK if b.get("in_rla") else "",
            "targeted_in": targeted_in_note(str(b["targeted_in"])) if b.get("targeted_in") else "",
            "location": b.get("location") or "",
            "qr": [] if lookup else qr_rows(screen_qr_payload(sha, board, int(b["board_position"]))),
        }
        for b in found
    ]
    # Each contest as shown (`corla_observer.printnames`, layout "screen"): its short name, and its choices one per line.
    names = print_names(extract, "screen")
    for ballot, b in zip(ballots, found, strict=True):
        for line in print_lines(b, names) if b["status"] == "votes" else []:
            vote = ballot["votes"][line.row]
            if not line.continuation:
                vote[0] = line.contest
            vote[3].append(line.value)
    return ballots


@router.get("/observe/{token}")
async def observe_page(request: Request, token: str) -> Response:
    state = get_state(request)
    link = LinkStore(state.workspace.root).get(token)
    asked = _asked_key(request, lookup=True)
    if link is not None and link.role == "reader":
        return RedirectResponse(state.url(f"/read/{token}" + _list_query(asked if asked and asked != LOOKUP else RLA)), status_code=303)
    if link is None:
        return _gone(state, request)
    chosen = await run_in_threadpool(_choose, state, link, None if asked == LOOKUP else asked)
    if chosen.chosen is None and asked is not None and asked != LOOKUP:
        return RedirectResponse(state.url(f"/observe/{token}"), status_code=303)  # retired, or not on this board: the default
    lookups = await run_in_threadpool(lambda: _lookup_extract(state, link, chosen.events))
    if asked == LOOKUP and lookups is None:
        return RedirectResponse(state.url(f"/observe/{token}"), status_code=303)  # no redacted CVR to look ballots up in
    key = LOOKUP if asked == LOOKUP else chosen.key
    found = lookups if asked == LOOKUP else await run_in_threadpool(chosen.extract, state)
    ident: dict[str, Any] | None = None
    if found is not None:
        extract, sha = found
        if key == LOOKUP:
            ident = {"line": _lookup_line(link, sha), "order": "lookup", "layouts": ()}
        else:
            ident = {"line": screen_line(extract, sha, link.board), "order": extract.get("order"), "layouts": layouts(extract)}
    switcher = _switcher(chosen.lists, link, len(lookups[0]["ballots"]) if lookups is not None else None)
    response = state.render(
        request, "observe.html", link=link, token=token, ident=ident, layout_label=layout_label, session=None,
        lists=switcher, list_key=key, list_query=_list_query(key), list_amp=_list_query(key, "&"),
        show_switcher=chosen.show_switcher or key == LOOKUP, rla_notice=None if key == LOOKUP else chosen.notice(link),
        lookup=key == LOOKUP, can_lookup=lookups is not None,
    )  # fmt: skip
    response.headers["Cache-Control"] = "no-cache"
    return response


@router.get("/observe/{token}/data.json")
async def observe_data(request: Request, token: str) -> Response:
    state = get_state(request)
    link = _link(state, token, "observer")
    asked = _asked_key(request, lookup=True)

    def load() -> dict[str, Any]:
        chosen = _choose(state, link, None if asked == LOOKUP else asked)
        lookups = _lookup_extract(state, link, chosen.events)
        switcher = _switcher(chosen.lists, link, len(lookups[0]["ballots"]) if lookups is not None else None)
        if asked == LOOKUP:
            if lookups is None:
                return {"error": f"no redacted CVR from {link.county} to look ballots up in", "lists": switcher, "default": default_key(chosen.lists)}
            key = LOOKUP
            extract, sha = lookups
            label, shas, ident = LOOKUP_NAME, [sha], _lookup_line(link, sha)
        else:
            key = chosen.key
            found = chosen.extract(state)
            if found is None:
                # No such list (asked for): "default" names the one the page should show instead; or no extract at all for the county.
                if chosen.lists:
                    what = f"no RLA sample for {link.county} board {link.board} yet" if key == RLA else "no such list"
                    return {"error": f"{what}: choose a list above", "lists": switcher, "default": default_key(chosen.lists)}
                return {"error": f"no extract for {link.county} yet", "lists": switcher}
            extract, sha = found
            assert chosen.chosen is not None
            label, shas, ident = chosen.chosen.label, list(chosen.chosen.shas), screen_line(extract, sha, link.board)
        ballots = _screen_ballots(extract, sha, link.board)
        mode_events = chosen.events
        # Only this list's entries: a targeted list's never mix with the RLA sample's, nor lookups with either (corla_observer.pulllists).
        events = events_for(mode_events, key)
        entries = {
            str(p): {"match": e.get("match"), "note": e.get("note"), "other_board": bool(e.get("other_board")), "seq": e.get("seq")}
            for p, e in latest_entries(events, link).items()
        }
        # What each ballot's entry was before its latest, if it was changed: shown as "corrected", the earlier one on tap.
        history: dict[int, list[Any]] = {}
        not_on_list: list[dict[str, Any]] = []
        for e in events:
            if e.actor != link.actor or e.county != link.county or e.board != str(link.board):
                continue
            position = e.data.get("position")
            if e.kind in ("observation.entry", "observation.other_board") and isinstance(position, int):
                history.setdefault(position, []).append(e)
            elif e.kind == "observation.id_not_on_list":
                not_on_list.append({"heard": e.data.get("heard"), "at": e.data.get("expected_position"), "seq": e.seq,
                                    "device_seq": e.data.get("device_seq"), "device_id": e.device_id})  # fmt: skip
        for position, evs in history.items():
            entry = entries.get(str(position))
            if entry is not None and len(evs) > 1:
                before = evs[-2]
                entry["corrected"] = len(evs) - 1
                entry["previous"] = {"match": before.data.get("match"), "note": before.data.get("note"),
                                     "other_board": before.kind == "observation.other_board", "seq": before.seq}  # fmt: skip
        board = str(link.board)
        contests = fold_contests(events).get((link.actor, link.county, board), {})
        for position, rows in contests.items():
            # A ballot done contest by contest counts as entered in either mode, if it is the later entry.
            earlier = entries.get(str(position))
            if rows.done and (earlier is None or int(earlier["seq"] or 0) < rows.last_seq):
                entries[str(position)] = {"match": rows.match, "note": rows.note(), "other_board": False,
                                          "per_contest": True, "seq": rows.last_seq}  # fmt: skip
        used = {v[2] for b in ballots for v in b["votes"]}
        choices = extract.get("choices") or {}
        return {
            "observer": link.actor,
            "county": link.county,
            "board": link.board,
            "extract_sha256": sha,
            "list": key,
            "list_label": label,
            "lists": switcher,
            "rla_missing": chosen.rla_missing and key != LOOKUP,
            "notice": None if key == LOOKUP else chosen.notice(link),
            "list_shas": shas,
            "other_shas": sorted({h for x in chosen.lists if x.key != key for h in x.shas}),
            "ident": ident,
            "lookup": key == LOOKUP,
            "not_on_list": not_on_list,
            "seed": extract.get("seed"),
            "order": extract.get("order"),
            "inputs": extract.get("inputs"),
            "election": extract.get("election"),
            "receipt_key": state.receipts.public_info(),
            "ballots": ballots,
            "choices": {c: list(choices.get(c, [])) for c in sorted(used)},
            "entries": entries,
            "contests": page_state(contests),
            "mode": latest_mode(mode_events, link.actor, link.county, board),
        }

    return JSONResponse(await run_in_threadpool(load))


@router.get("/observe/{token}/lookup.json")
async def observe_lookup(request: Request, token: str) -> Response:
    """One ballot looked up by imprinted ID for the observer's "Look up a ballot" (docs/TARGETING.md), as a ballot of the lookup list."""
    state = get_state(request)
    link = _link(state, token, "observer")
    if not _lookup_limiter.allow(token_hash(token)):
        raise HTTPException(status_code=429, detail="Too many lookups; wait a moment.")
    imprinted = normalize_id(request.query_params.get("id", "")[:64])
    position_text = request.query_params.get("position", "")
    if imprinted is None:
        return JSONResponse({"error": "Type the ballot's imprinted ID as tabulator-batch-position, such as 101-12-5."}, status_code=400)
    if not position_text.isdigit() or not 1 <= int(position_text) <= 100_000:
        return JSONResponse({"error": "position"}, status_code=400)

    def look() -> dict[str, Any]:
        source = _lookup_source(state, link.county)
        if source is None:
            return {"error": f"no redacted CVR from {link.county} to look the ballot up in"}
        expected, choices = expected_votes(source.cvr_path, [imprinted])
        found = expected.get(imprinted)
        ballot = _lookup_ballot(int(position_text), imprinted, found, source, link.board)
        if found is None and not ballot["in_manifest"]:
            return {"error": f"{imprinted} is not in {link.county}'s redacted CVR or its manifest. Check the ID, or enter it anyway below."}
        extract: dict[str, Any] = {"county": link.county, "order": "lookup", "boards": {str(link.board): 1}, "ballots": [ballot], "choices": choices}
        (shown,) = _screen_ballots(extract, source.cvr_sha256, link.board)
        used = {v[2] for v in shown["votes"]}
        return {
            "ballot": shown,
            "found": found is not None,
            "in_manifest": ballot["in_manifest"],
            "location": ballot["location"],
            "extract_sha256": source.cvr_sha256,
            "choices": {c: list(choices.get(c, [])) for c in sorted(used)},
            "ident": _lookup_line(link, source.cvr_sha256),
        }

    result = await run_in_threadpool(look)
    return JSONResponse(result, status_code=404 if "error" in result else 200)


@router.post("/observe/{token}/entries")
async def observe_entries(request: Request, token: str) -> Response:
    state = get_state(request)
    link = _link(state, token, "observer")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="Expected JSON.")
    try:
        body = json.loads(await request.body())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON.") from None
    entries = body.get("entries") if isinstance(body, dict) else None
    if not isinstance(entries, list) or len(entries) > MAX_BATCH:
        raise HTTPException(status_code=400, detail=f"Expected up to {MAX_BATCH} entries.")
    if not _entry_limiter.allow(token_hash(token)):
        raise HTTPException(status_code=429, detail="Too many entries; wait a moment.")

    def record() -> dict[str, Any]:
        recorded: list[int] = []
        duplicates: list[int] = []
        errors: list[dict[str, Any]] = []
        heard: dict[str, Any] = {}
        receipts: dict[str, Any] = {}
        ballots: dict[str, list[dict[str, Any]]] = {}

        def board(sha: str) -> list[dict[str, Any]]:
            # The board's ballots in the extract the entry refers to; the RLA sample's for an extract the log never built.
            if sha not in ballots:
                extract = extract_by_sha(state.workspace, state.log.iter_events(), link.county, sha)
                if extract is None:
                    found = latest_extract(state.workspace, state.log, link.county)
                    extract = found[0] if found else None
                ballots[sha] = board_ballots(extract, link.board) if extract else []
            return ballots[sha]

        registry = state.devices
        with registry.lock:
            registry.refresh()
            seen = _seen(state, token, link)
            for item in entries:
                outcome = process_item(
                    state.log, link, item, registry=registry, signer=state.receipts, seen=seen, ballots=board
                )
                if outcome.status == "rejected":
                    errors.append({"device_seq": outcome.device_seq, "error": outcome.error, "code": outcome.code})
                    continue
                assert outcome.device_seq is not None
                (recorded if outcome.status == "recorded" else duplicates).append(outcome.device_seq)
                receipts[str(outcome.device_seq)] = outcome.receipt
                if outcome.heard is not None:
                    heard[str(outcome.device_seq)] = outcome.heard
        return {
            "recorded": recorded,
            "duplicates": duplicates,
            "errors": errors,
            "heard": heard,
            "receipts": receipts,
            "head": state.log.head()[0],
        }

    return JSONResponse(await run_in_threadpool(record))


def _board_extract(state: WebState, link: Link, key: str | None = None) -> tuple[dict[str, Any], str]:
    found = _choose(state, link, key).extract(state)
    if found is None or link.board not in [int(b) for b in found[0]["boards"]]:
        raise HTTPException(status_code=404, detail="No extract for this board yet.")
    return found


def _provenance(state: WebState, link: Link, back: str, key: str | None = None) -> str:
    extract, sha = _board_extract(state, link, key)
    return render_provenance_html(
        extract,
        sha,
        link.board,
        stylesheet=state.static_url("extract.css"),
        integrity=state.integrity["extract.css"],
        links=[("Back", back)],
        software=extract_software(state.log, sha),
    )


def _pdf(state: WebState, link: Link, layout: str | None, key: str | None = None) -> Response:
    extract, sha = _board_extract(state, link, key)
    layout = layout or default_layout(extract)
    if layout not in layouts(extract):
        raise HTTPException(status_code=404, detail="No such layout for this extract.")
    body = printout(state.workspace, state.log, extract, sha, link.board, layout)
    return pdf_response(body, pdf_filename(extract, sha, link.board, layout))


@router.get("/observe/{token}/pdf")
async def observe_pdf(request: Request, token: str) -> Response:
    state = get_state(request)
    link = _link(state, token, "observer")
    return await run_in_threadpool(_pdf, state, link, request.query_params.get("layout"), _asked_key(request))


@router.get("/read/{token}/pdf")
async def reader_pdf(request: Request, token: str) -> Response:
    state = get_state(request)
    link = _link(state, token, "reader")
    return await run_in_threadpool(_pdf, state, link, request.query_params.get("layout"), _asked_key(request))


@router.get("/observe/{token}/provenance")
async def observe_provenance(request: Request, token: str) -> Response:
    state = get_state(request)
    link = _link(state, token, "observer")
    asked = _asked_key(request)
    response = HTMLResponse(await run_in_threadpool(_provenance, state, link, state.url(f"/observe/{token}" + _list_query(asked or RLA)), asked))
    response.headers["Cache-Control"] = "no-cache"
    return response


@router.get("/read/{token}/provenance")
async def reader_provenance(request: Request, token: str) -> Response:
    state = get_state(request)
    link = _link(state, token, "reader")
    asked = _asked_key(request)
    response = HTMLResponse(await run_in_threadpool(_provenance, state, link, state.url(f"/read/{token}" + _list_query(asked or RLA)), asked))
    response.headers["Cache-Control"] = "no-cache"
    return response


# ---------------------------------------------------------------------------
# The practice reader


@router.get("/read/{token}")
async def reader_page(request: Request, token: str) -> Response:
    state = get_state(request)
    found_link = LinkStore(state.workspace.root).get(token)
    if found_link is None or found_link.role != "reader":
        return _gone(state, request)
    link = found_link
    asked = _asked_key(request)
    chosen = await run_in_threadpool(_choose, state, link, asked)
    if chosen.chosen is None and asked is not None:
        return RedirectResponse(state.url(f"/read/{token}"), status_code=303)
    key = chosen.key
    found = await run_in_threadpool(chosen.extract, state)
    current = await run_in_threadpool(current_read, state.log, link.county, link.board, key)
    total = len(board_ballots(found[0], link.board)) if found else 0
    ident = screen_line(found[0], found[1], link.board) if found else None
    print_layouts = layouts(found[0]) if found and link.board in [int(b) for b in found[0]["boards"]] else ()
    rate = request.query_params.get("rate", "0")
    response = state.render(
        request,
        "reader.html",
        link=link,
        token=token,
        current=current,
        total=total,
        has_extract=found is not None,
        rates=INJECT_RATES,
        rate=rate if rate in dict(INJECT_RATES) else "0",
        second=request.query_params.get("step") == "2",
        short_contest=short_contest,
        ident=ident,
        order=found[0].get("order") if found else None,
        print_layouts=print_layouts,
        layout_label=layout_label,
        lists=_switcher(chosen.lists, link),
        list_key=key,
        list_amp=_list_query(key, "&"),
        show_switcher=chosen.show_switcher,
        rla_notice=chosen.notice(link),
        session=None,
    )
    response.headers["Cache-Control"] = "no-cache"
    return response


async def _small_form(request: Request) -> dict[str, str]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="Expected a URL-encoded form.")
    form = await request.form(max_files=0, max_fields=8, max_part_size=1024)
    return {k: v for k, v in form.items() if isinstance(v, str)}


@router.post("/read/{token}/next")
async def reader_next(request: Request, token: str) -> Response:
    state = get_state(request)
    link = _link(state, token, "reader")
    form = await _small_form(request)
    rate = form.get("rate", "0")
    inject = float(rate) if rate in dict(INJECT_RATES) else 0.0
    chosen = await run_in_threadpool(_choose, state, link, _asked_key(request, form))
    key = chosen.key
    found = await run_in_threadpool(chosen.extract, state)
    if found is None:
        raise HTTPException(status_code=409, detail="No practice extract for this county yet.")
    try:
        await run_in_threadpool(
            lambda: next_read(state.log, link, found[0], inject_rate=inject, rng=random.SystemRandom(), list_key=key, extract_sha256=found[1])
        )
    except PracticeError:
        pass  # the last ballot: the page says so
    return RedirectResponse(state.url(f"/read/{token}?rate={rate}" + _list_query(key, "&")), status_code=303)


@router.post("/read/{token}/restart")
async def reader_restart(request: Request, token: str) -> Response:
    state = get_state(request)
    link = _link(state, token, "reader")
    form = await _small_form(request)
    key = (await run_in_threadpool(_choose, state, link, _asked_key(request, form))).key
    await run_in_threadpool(reset_reads, state.log, link, key)
    return RedirectResponse(state.url(f"/read/{token}?rate={form.get('rate', '0')}" + _list_query(key, "&")), status_code=303)
