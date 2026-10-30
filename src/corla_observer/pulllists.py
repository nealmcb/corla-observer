"""A county's pull lists: its RLA extract and its targeted extracts, side by side, for observers to switch between.

Each county has at most one **RLA sample** list: its latest extract whose
order is not ``targeted`` (`corla_observer.extract.latest_extract`), from
the county's pull list, CORLA's posted selections or the prediction. It
may also have any number of **targeted** lists (docs/TARGETING.md): each is
a working list a coordinator sent to the county as "Targeted list N"
(`corla_observer.targets.send`), a targeted extract
(`corla_observer.extract.build_extract` with ``targeted``) carrying the
list's name; the latest extract built under a name is that list's. A
list's **key** is ``rla`` for the RLA sample and
`corla_observer.extract.targeted_key` of its name for a targeted list.
Lists built by the first design under a name of their own are numbered the
same way and shown as "Targeted list N (name)".

Besides the lists, an observer may **look up** a ballot picked from a
storage box by its imprinted ID and enter it contest by contest; those
box-targeted observations (``observation.lookup_contest`` and
``observation.lookup_done``) belong to the key `LOOKUP`, which has no
extract, and are counted apart from every list.

Observers' entries say which extract they refer to: every signed payload
carries the hash of the extract the page was showing (docs/RECEIPTS.md),
and the server records it as the entry's plain ``extract_sha256``.
`entry_list` gives the list an entry belongs to (an entry for an extract
the log never built, or without a hash, counts for the RLA sample), and
`events_for` keeps a list's entries apart from every other list's, for the
progress counts, statistics, comparisons and the observer page.

A coordinator can **retire** a targeted list (`retire`), which hides it from
observers' switchers and from the public dashboard until it is restored
(`restore`) or built again under the same name.

During the audit the public dashboard names a targeted list only "Targeted
list N" and shows its counts: its name, its patterns and its ballots could
hint at how they are marked. After the targeted ballots have been examined,
a coordinator may **publish** a list's details (`publish`): its name, the
patterns of its draws and its imprinted IDs, from its stored list record,
are then written into the published log and shown on the public dashboard;
`withdraw` takes them off the dashboard (the log keeps them).

Events:

``extract.retired``, ``extract.restored``
    ``county`` set; ``data``: ``list`` (the list's key), ``list_name``.
``extract.details_published``
    ``county``; ``data``: ``list``, ``list_name``, ``details`` (``name``,
    ``patterns``: each draw's ``draw``, ``name`` and ``description``, and
    ``ids``, the imprinted IDs), ``list_record_sha256``.
``extract.details_withdrawn``
    ``county``; ``data``: ``list``, ``list_name``.

Standard library only.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from corla_observer.events import Event, EventLog, check_actor
from corla_observer.extract import DEFAULT_TARGETED_NAME, targeted_key
from corla_observer.workspace import Workspace

RLA = "rla"
RLA_NAME = "RLA sample"
#: Box-targeted lookups (no extract): the observer page's "Look up a ballot".
LOOKUP = "lookup"
LOOKUP_NAME = "Looked up from boxes"
LOOKUP_KINDS = frozenset({"observation.lookup_contest", "observation.lookup_done"})
RETIRED_KIND = "extract.retired"
RESTORED_KIND = "extract.restored"
PUBLISHED_KIND = "extract.details_published"
WITHDRAWN_KIND = "extract.details_withdrawn"
LIST_KINDS = frozenset({RETIRED_KIND, RESTORED_KIND, PUBLISHED_KIND, WITHDRAWN_KIND})


@dataclass
class PullList:
    """One of a county's pull lists, as of its latest extract."""

    county: str
    key: str
    name: str
    targeted: bool
    sha256: str
    seq: int
    built: str
    actor: str
    order: str
    ballots: int
    #: Ballots per board, by board number as a string.
    boards: dict[str, int] = field(default_factory=dict)
    retired: bool = False
    #: A targeted list's number in the county, in the order the lists were first built (1, 2, ...); 0 for the RLA sample.
    number: int = 0
    #: Every extract hash this list has had, oldest first.
    shas: list[str] = field(default_factory=list)
    #: The stored list record's hash (`corla_observer.targets.list_record`), if the build stored one.
    record_sha256: str | None = None
    #: Whether it was combined from several targeted lists.
    combined: bool = False
    #: The details a coordinator published (`publish`), shown on the public dashboard, or None.
    published: dict[str, Any] | None = None

    @property
    def label(self) -> str:
        """How observers and coordinators see it: "RLA sample", "Targeted list N", or "Targeted list N (NAME)" for a list named by the first design."""
        if not self.targeted:
            return RLA_NAME
        default = f"Targeted list {self.number}"
        return default if self.name == default else f"{default} ({self.name})"

    @property
    def public_label(self) -> str:
        """How the public dashboard names it, without the name: "RLA sample" or "Targeted list N"."""
        return f"Targeted list {self.number}" if self.targeted else RLA_NAME

    def board_ballots(self, board: int) -> int:
        return int(self.boards.get(str(board), 0))


def built_list_key(event: Event) -> str:
    """The list an ``extract.built`` event's extract belongs to."""
    if event.data.get("order") != "targeted":
        return RLA
    key = event.data.get("list")
    return str(key) if isinstance(key, str) and key else targeted_key(DEFAULT_TARGETED_NAME)


def _name(event: Event) -> str:
    name = event.data.get("list_name")
    return str(name) if isinstance(name, str) and name else DEFAULT_TARGETED_NAME


def all_lists(events: Iterable[Event]) -> dict[str, list[PullList]]:
    """Every county's pull lists, retired ones too: the RLA sample first, then the targeted lists in the order first built."""
    found: dict[str, dict[str, PullList]] = {}
    for event in events:
        county = event.county
        if not county:
            continue
        if event.kind == "extract.built" and event.file_sha256:
            key = built_list_key(event)
            lists = found.setdefault(county, {})
            before = lists.get(key)
            boards = event.data.get("boards")
            ballots = event.data.get("ballots")
            item = PullList(
                county=county,
                key=key,
                name=_name(event) if key != RLA else RLA_NAME,
                targeted=key != RLA,
                sha256=event.file_sha256,
                seq=event.seq,
                built=event.server_time,
                actor=event.actor,
                order=str(event.data.get("order") or ""),
                ballots=ballots if isinstance(ballots, int) else 0,
                boards={str(b): int(n) for b, n in boards.items() if isinstance(n, int)} if isinstance(boards, dict) else {},
                number=before.number if before else (0 if key == RLA else 1 + sum(1 for x in lists.values() if x.targeted)),
                shas=[*(before.shas if before else []), event.file_sha256],
                record_sha256=str(event.data["list_record_sha256"]) if isinstance(event.data.get("list_record_sha256"), str) else None,
                combined=bool(event.data.get("combined")),
            )
            lists[key] = item
        elif event.kind in LIST_KINDS:
            item_or_none = found.get(county, {}).get(str(event.data.get("list")))
            if item_or_none is not None and item_or_none.targeted:
                if event.kind in (RETIRED_KIND, RESTORED_KIND):
                    item_or_none.retired = event.kind == RETIRED_KIND
                elif event.kind == PUBLISHED_KIND:
                    shown = event.data.get("details")
                    item_or_none.published = dict(shown) if isinstance(shown, dict) else None
                else:
                    item_or_none.published = None
    return {county: sorted(lists.values(), key=lambda x: (x.targeted, x.number)) for county, lists in found.items()}


def county_lists(events: Iterable[Event], county: str, *, board: int | None = None, retired: bool = False) -> list[PullList]:
    """A county's pull lists (those with ballots on ``board``, if given), without retired ones unless asked."""
    lists = all_lists(e for e in events if e.county == county).get(county, [])
    return [x for x in lists if (retired or not x.retired) and (board is None or x.board_ballots(board) > 0)]


def find_list(events: Iterable[Event], county: str, key: str, *, board: int | None = None, retired: bool = False) -> PullList | None:
    return next((x for x in county_lists(events, county, board=board, retired=retired) if x.key == key), None)


def extract_lists(events: Iterable[Event]) -> dict[str, str]:
    """Each extract hash built, and the list it belongs to (`built_list_key`)."""
    return {e.file_sha256: built_list_key(e) for e in events if e.kind == "extract.built" and e.file_sha256}


def entry_extract(event: Event) -> str | None:
    """The extract an observer's event refers to: its plain ``extract_sha256``, else its signed payload's."""
    sha = event.data.get("extract_sha256")
    if not isinstance(sha, str):
        payload = event.data.get("payload")
        sha = payload.get("extract_sha256") if isinstance(payload, Mapping) else None
    return sha if isinstance(sha, str) else None


def is_list_event(event: Event) -> bool:
    """Whether the event belongs to one list: an observer's entry (``observation.*``) or a practice reading."""
    return event.kind.startswith("observation.") or event.kind in ("practice.read", "practice.read_reset")


def entry_list(event: Event, lists: Mapping[str, str]) -> str:
    """The list an observer's entry or a practice reading belongs to; `LOOKUP` for a box-targeted lookup; the RLA sample when its extract is unknown."""
    if event.kind in LOOKUP_KINDS:
        return LOOKUP
    if event.kind in ("practice.read", "practice.read_reset"):
        key = event.data.get("list")
        return str(key) if isinstance(key, str) and key else RLA
    sha = entry_extract(event)
    return lists.get(sha, RLA) if sha else RLA


def events_for(events: Iterable[Event], key: str, lists: Mapping[str, str] | None = None) -> list[Event]:
    """The events with every other list's entries and readings left out (other events kept).

    ``lists`` is `extract_lists` of the log; by default it is read from ``events``.
    """
    events = list(events)
    lists = extract_lists(events) if lists is None else lists
    return [e for e in events if not is_list_event(e) or entry_list(e, lists) == key]


def load(workspace: Workspace, item: PullList) -> dict[str, Any]:
    """A list's extract."""
    data: dict[str, Any] = json.loads(workspace.get_bytes(item.sha256))
    return data


def extract_by_sha(workspace: Workspace, events: Iterable[Event], county: str, sha: str) -> dict[str, Any] | None:
    """The county's extract with this hash, if the log built it."""
    if not any(e.kind == "extract.built" and e.county == county and e.file_sha256 == sha for e in events):
        return None
    data: dict[str, Any] = json.loads(workspace.get_bytes(sha))
    return data


def _mark(log: EventLog, kind: str, county: str, key: str, actor: str, session_id: str | None) -> PullList:
    check_actor(actor)
    item = find_list(log.iter_events(), county, key, retired=True)
    if item is None or not item.targeted:
        raise ValueError("no such targeted list")
    log.append(kind, actor, county=county, session_id=session_id, data={"list": key, "list_name": item.name})
    if kind in (RETIRED_KIND, RESTORED_KIND):
        item.retired = kind == RETIRED_KIND
    else:
        item.published = None
    return item


def record(workspace: Workspace, item: PullList) -> dict[str, Any] | None:
    """A targeted list's stored record (`corla_observer.targets.list_record`), or None."""
    if item.record_sha256 is None:
        return None
    try:
        data: dict[str, Any] = json.loads(workspace.get_bytes(item.record_sha256))
    except Exception:  # noqa: BLE001 - a record missing from the workspace: nothing to publish
        return None
    return data


def details(workspace: Workspace, item: PullList) -> dict[str, Any]:
    """What publishing a list shows: its name, its draws' patterns in words, and its imprinted IDs (from its record, else its extract)."""
    found = record(workspace, item)
    if found is not None:
        from corla_observer.targets import record_patterns

        ids = [str(b.get("imprinted_id")) for b in found.get("ballots") or []]
        patterns = record_patterns(found)
    else:
        ids = list(dict.fromkeys(str(b["imprinted_id"]) for b in load(workspace, item)["ballots"]))
        patterns = []
    return {"name": item.name, "patterns": patterns, "ids": ids}


def publish(workspace: Workspace, log: EventLog, county: str, key: str, actor: str, *, session_id: str | None = None) -> PullList:
    """Publish a targeted list's details on the public dashboard (``extract.details_published``), for after its ballots are examined.

    Raises:
        ValueError: if the county has no targeted list with this key.
    """
    check_actor(actor)
    item = find_list(log.iter_events(), county, key, retired=True)
    if item is None or not item.targeted:
        raise ValueError("no such targeted list")
    shown = details(workspace, item)
    log.append(
        PUBLISHED_KIND, actor, county=county, session_id=session_id,
        data={"list": key, "list_name": item.name, "details": shown, "list_record_sha256": item.record_sha256},
    )  # fmt: skip
    item.published = shown
    return item


def withdraw(log: EventLog, county: str, key: str, actor: str, *, session_id: str | None = None) -> PullList:
    """Take a list's published details off the public dashboard (``extract.details_withdrawn``); the log keeps them."""
    return _mark(log, WITHDRAWN_KIND, county, key, actor, session_id)


def retire(log: EventLog, county: str, key: str, actor: str, *, session_id: str | None = None) -> PullList:
    """Hide a targeted list from observers' switchers and the public dashboard (``extract.retired``).

    Raises:
        ValueError: if the county has no targeted list with this key.
    """
    return _mark(log, RETIRED_KIND, county, key, actor, session_id)


def restore(log: EventLog, county: str, key: str, actor: str, *, session_id: str | None = None) -> PullList:
    """Show a retired targeted list again (``extract.restored``)."""
    return _mark(log, RESTORED_KIND, county, key, actor, session_id)
