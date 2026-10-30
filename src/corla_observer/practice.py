"""Practice extracts, observer and reader links, observation entries, and usability statistics.

This is the first version of live observation (docs/PLAN.md, component 9),
built so a few people can try following an audit board's read-out on their
phones, from public data, and so we can measure whether they keep up.

**Practice extract.** `build_extract` builds the county's extract
(`corla_observer.extract.build_extract`, the same structure a live audit
uses) for a practice: from the county's pull list if one was uploaded, and
otherwise from the latest prediction in a rehearsal workspace
(`corla_observer.rehearse`), every ballot drawn in the county for any
targeted contest once, in pull order, split into contiguous runs for
``boards`` audit boards, with each ballot's expected votes from the county's
redacted CVR. It is recorded as ``extract.built`` with ``purpose``
``practice``.

**Links.** `LinkStore` keeps observer and reader links in the workspace's
private ``accounts/links.json``, by the SHA-256 of their random token only.
A link names one person (their actor ``observer:NAME``), one county and one
board, and a role: ``observer`` (follows the read-out and records entries)
or ``reader`` (acts as the audit board in practice). Creating one records
``practice.link_created`` (never the token), with the coordinator who made
it; `LinkStore.links` lists them by token hash, and `LinkStore.revoke`
removes one, recording ``practice.link_revoked``. A link's URL is shown
only when it is made, since only its token's hash is kept.

**Two taps for a match.** An observer taps once on hearing the first audit
board member read the ballot ("First reading"), and again on hearing the
second reading ("Confirmed match"); only the second tap records the match.
`record_first_reading` records the first tap as an
``observation.first_reading`` event (the ballot's position and imprinted ID,
``elapsed_ms`` from the ballot being shown, and the device's sequence
number); `practice_stats` reports the time to the first reading and from it
to the confirmation.

**Entries.** `record_entry` records one observer entry as an
``observation.entry`` event: the ballot's position and imprinted ID,
``match`` (the one-tap "matches") and ``note`` (what was heard instead, free
text), whether it is an ``edit`` of an earlier entry, the time from the
ballot being shown to the entry (``elapsed_ms``, for usability), and the
device's ID and per-device sequence number (``device_seq``), with the
device's clock time as ``client_time``. Entries resent after a network
failure are recognized by their device ID and sequence number and recorded
once.

**Reader.** `next_read` moves a reader link to the next ballot of its board
and records what is to be read aloud as a ``practice.read`` event. With an
injection rate above zero it sometimes reads something deliberately
different (`DEVIATIONS`: a different vote in one contest, a wrong imprinted
ID, or a skipped ballot), recorded in the event, so that practice entries
can be scored (`board_stats`).

**Pull lists.** A county may have targeted lists besides its RLA sample
(`corla_observer.pulllists`). Each entry belongs to the list of the extract
it refers to, and each reading to the list it was read from (its ``list``),
so the reader's place, the progress counts and the statistics are kept per
list (``list`` on `ObserverStats` and `BoardProgress`).

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import secrets
import statistics
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from corla_observer.classify import known_county
from corla_observer.contests import MODE_LABELS, latest_mode, observer_modes
from corla_observer.contests import fold as fold_contests
from corla_observer.events import Event, EventLog, JSONValue
from corla_observer.extract import (
    ExpectedBallot,
    ExtractError,
    board_ballots,
    contest_label,
    expected_votes,
    latest_extract,
    pull_order_key,
)
from corla_observer.extract import build_extract as build_county_extract
from corla_observer.pulllists import LOOKUP, LOOKUP_NAME, RLA, RLA_NAME, all_lists, entry_list, extract_lists
from corla_observer.workspace import Workspace

__all__ = [
    "ExpectedBallot",
    "board_ballots",
    "contest_label",
    "expected_votes",
    "latest_extract",
    "pull_order_key",
]

LINKS_PATH = Path("accounts") / "links.json"
ROLES = ("observer", "reader")
DEVIATIONS = ("different_vote", "wrong_ballot", "skipped_ballot")
MAX_NOTE = 500
MAX_BOARDS = 20
#: Events of contest-by-contest entry (`corla_observer.contests`): one contest's outcome in one
#: reading, a ballot marked done, and the observer's choice of entry mode.
CONTEST_KINDS = frozenset(
    {"observation.contest", "observation.ballot_done", "observation.entry_mode", "observation.acknowledged", "observation.list_switched",
     "observation.lookup_contest", "observation.lookup_done"}
)  # fmt: skip
#: Events an observer's device sends, each with its ``device_seq``.
OBSERVER_KINDS = frozenset(
    {"observation.entry", "observation.first_reading", "observation.out_of_order", "observation.id_not_on_list",
     "observation.other_board"}
) | CONTEST_KINDS  # fmt: skip
#: Event kinds that settle a ballot for an observer: an entry, or "skipped: other board" for a boundary ballot.
SETTLING_KINDS = frozenset({"observation.entry", "observation.other_board"})

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_DEVICE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class PracticeError(ValueError):
    """A practice extract, link or entry can't be made as asked."""


# ---------------------------------------------------------------------------
# Expected votes


def build_extract(
    workspace: Workspace, log: EventLog, county: str, *, boards: int = 1, actor: str = "system"
) -> tuple[dict[str, Any], str, int]:
    """Build, store and record a county's practice extract; return it, its SHA-256 and the event's seq."""
    if not 1 <= boards <= MAX_BOARDS:
        raise PracticeError(f"boards must be 1 to {MAX_BOARDS}")
    try:
        built = build_county_extract(workspace, log, county, boards=boards, purpose="practice", actor=actor)
    except ExtractError as exc:
        raise PracticeError(str(exc)) from None
    return built.extract, built.sha256, built.seq


# ---------------------------------------------------------------------------
# Links


@dataclass(frozen=True)
class Link:
    role: str
    name: str
    county: str
    board: int
    created: str

    @property
    def actor(self) -> str:
        return f"observer:{self.name}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LinkStore:
    """Observer and reader links, kept by token hash in the workspace's private ``accounts/``."""

    def __init__(self, workspace_root: Path | str) -> None:
        self.path = Path(workspace_root) / LINKS_PATH

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data: dict[str, dict[str, Any]] = json.loads(self.path.read_text(encoding="utf-8")).get("links", {})
        except FileNotFoundError:
            return {}
        return data

    def _save(self, links: Mapping[str, Mapping[str, Any]]) -> None:
        directory = self.path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".links-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"format": 1, "links": dict(links)}, f, indent=1, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def links(self) -> list[tuple[str, Link]]:
        """Every link, as ``(token hash, link)``, by county, board, role and name."""
        found = [
            (digest, Link(e["role"], e["name"], e["county"], int(e["board"]), e["created"]))
            for digest, e in self._load().items()
        ]
        return sorted(found, key=lambda x: (x[1].county, x[1].board, x[1].role, x[1].name, x[1].created))

    def revoke(self, digest: str, log: EventLog | None = None, *, actor: str = "system") -> Link | None:
        """Remove the link whose token hash is ``digest``; return it, or None if there is none."""
        links = self._load()
        entry = links.pop(digest, None)
        if entry is None:
            return None
        self._save(links)
        link = Link(entry["role"], entry["name"], entry["county"], int(entry["board"]), entry["created"])
        if log is not None:
            log.append(
                "practice.link_revoked",
                actor,
                county=link.county,
                board=str(link.board),
                data={"role": link.role, "actor": link.actor, "board": link.board, "created": link.created},
            )
        return link

    def create(
        self, role: str, name: str, county: str, board: int, log: EventLog | None = None, *, actor: str = "system"
    ) -> tuple[str, Link]:
        if role not in ROLES:
            raise PracticeError(f"role must be one of {', '.join(ROLES)}")
        if not _NAME_RE.match(name):
            raise PracticeError("a name is 1 to 64 letters, digits, '.', '_' or '-'")
        canonical = known_county(county)
        if canonical is None:
            raise PracticeError(f"not a Colorado county: {county!r}")
        if not 1 <= board <= MAX_BOARDS:
            raise PracticeError(f"board must be 1 to {MAX_BOARDS}")
        token = secrets.token_urlsafe(24)
        link = Link(role, name, canonical, board, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        links = self._load()
        links[token_hash(token)] = {"role": role, "name": name, "county": canonical, "board": board, "created": link.created}
        self._save(links)
        if log is not None:
            log.append(
                "practice.link_created",
                actor,
                county=canonical,
                board=str(board),
                data={"role": role, "actor": link.actor, "board": board},
            )
        return token, link

    def get(self, token: str) -> Link | None:
        if not token or len(token) > 128:
            return None
        entry = self._load().get(token_hash(token))
        if entry is None:
            return None
        return Link(entry["role"], entry["name"], entry["county"], int(entry["board"]), entry["created"])


# ---------------------------------------------------------------------------
# Entries


def _check_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    def need(cond: bool, what: str) -> None:
        if not cond:
            raise PracticeError(f"invalid entry: {what}")

    position, seq = entry.get("position"), entry.get("device_seq")
    need(isinstance(position, int) and not isinstance(position, bool) and 1 <= position <= 100_000, "position")
    need(isinstance(seq, int) and not isinstance(seq, bool) and 0 <= seq <= 2**53 - 1, "device_seq")
    imprinted = entry.get("imprinted_id")
    need(isinstance(imprinted, str) and 0 < len(imprinted) <= 64, "imprinted_id")
    device = entry.get("device_id")
    need(isinstance(device, str) and bool(_DEVICE_RE.match(device)), "device_id")
    note = entry.get("note", "")
    need(isinstance(note, str), "note")
    match = entry.get("match")
    need(isinstance(match, bool), "match")
    elapsed = entry.get("elapsed_ms")
    need(elapsed is None or (isinstance(elapsed, int) and not isinstance(elapsed, bool) and 0 <= elapsed <= 86_400_000), "elapsed_ms")
    client_time = entry.get("client_time")
    if not (isinstance(client_time, str) and _TIME_RE.match(client_time)):
        client_time = None
    note = "".join(ch for ch in note if ch.isprintable()).strip()[:MAX_NOTE]
    need(match or bool(note), "an entry is a match or a note")
    return {
        "position": position,
        "imprinted_id": imprinted,
        "match": match,
        "note": note,
        "edit": bool(entry.get("edit", False)),
        "elapsed_ms": elapsed,
        "device_id": device,
        "device_seq": seq,
        "client_time": client_time,
    }


def record_entry(
    log: EventLog,
    link: Link,
    entry: Mapping[str, Any],
    seen: set[tuple[str, int]],
    *,
    signature: str | None = None,
    extra: Mapping[str, JSONValue] | None = None,
) -> int | None:
    """Record one observer entry; return its seq, or None if this device already sent it.

    ``seen`` holds the ``(device_id, device_seq)`` pairs already recorded for
    the link (`seen_entries`) and is updated. A device-signed entry
    (`corla_observer.signed`) passes its ``signature`` for the event's
    field, and its payload and the payload's hash as ``extra`` data.

    Raises:
        PracticeError: for an invalid entry, or a link that isn't an observer's.
    """
    if link.role != "observer":
        raise PracticeError("only an observer link records entries")
    checked = _check_entry(entry)
    key = (checked["device_id"], checked["device_seq"])
    if key in seen:
        return None
    data: dict[str, JSONValue] = {
        k: checked[k] for k in ("position", "imprinted_id", "match", "note", "edit", "elapsed_ms", "device_seq")
    }
    data |= dict(extra or {})
    event = log.append(
        "observation.entry",
        link.actor,
        county=link.county,
        board=str(link.board),
        device_id=checked["device_id"],
        client_time=checked["client_time"],
        signature=signature,
        data=data,
    )
    seen.add(key)
    return event.seq


def record_first_reading(
    log: EventLog,
    link: Link,
    item: Mapping[str, Any],
    seen: set[tuple[str, int]],
    *,
    signature: str | None = None,
    extra: Mapping[str, JSONValue] | None = None,
) -> int | None:
    """Record an observer's first tap on a ballot, on hearing its first reading; return the event's seq.

    None if this device already sent it. ``item`` has the ballot's
    ``position`` and ``imprinted_id``, ``elapsed_ms``, ``device_id``,
    ``device_seq`` and ``client_time``; ``signature`` and ``extra`` are as
    for `record_entry`.

    Raises:
        PracticeError: for an invalid item, or a link that isn't an observer's.
    """
    if link.role != "observer":
        raise PracticeError("only an observer link records entries")
    checked = _check_entry(dict(item) | {"match": True, "note": ""})
    key = (checked["device_id"], checked["device_seq"])
    if key in seen:
        return None
    data: dict[str, JSONValue] = {k: checked[k] for k in ("position", "imprinted_id", "elapsed_ms", "device_seq")}
    data |= dict(extra or {})
    event = log.append(
        "observation.first_reading",
        link.actor,
        county=link.county,
        board=str(link.board),
        device_id=checked["device_id"],
        client_time=checked["client_time"],
        signature=signature,
        data=data,
    )
    seen.add(key)
    return event.seq


def record_observation(
    log: EventLog,
    link: Link,
    kind: str,
    item: Mapping[str, Any],
    fields: Mapping[str, JSONValue],
    seen: set[tuple[str, int]],
    *,
    signature: str | None = None,
    extra: Mapping[str, JSONValue] | None = None,
) -> int | None:
    """Record one of the `CONTEST_KINDS` from an observer's device; return the event's seq.

    None if this device already sent it. ``item`` has the ballot's
    ``position`` and ``imprinted_id``, ``device_id``, ``device_seq`` and
    ``client_time``, as for `record_first_reading`; ``fields`` are the
    event's plain fields besides them (``contest``, ``reading``, ``outcome``
    and so on, from the signed payload, `corla_observer.contests`), and
    ``signature`` and ``extra`` are as for `record_entry`.

    Raises:
        PracticeError: for an invalid item, an unknown kind, or a link that isn't an observer's.
    """
    if link.role != "observer":
        raise PracticeError("only an observer link records entries")
    if kind not in CONTEST_KINDS:
        raise PracticeError(f"not a contest-by-contest kind: {kind}")
    checked = _check_entry(dict(item) | {"match": True, "note": ""})
    key = (checked["device_id"], checked["device_seq"])
    if key in seen:
        return None
    data: dict[str, JSONValue] = {k: checked[k] for k in ("position", "imprinted_id", "device_seq")}
    data |= dict(fields) | dict(extra or {})
    event = log.append(
        kind,
        link.actor,
        county=link.county,
        board=str(link.board),
        device_id=checked["device_id"],
        client_time=checked["client_time"],
        signature=signature,
        data=data,
    )
    seen.add(key)
    return event.seq


# ---------------------------------------------------------------------------
# Matching an imprinted ID heard or typed by an observer
#
# Mirrored exactly in static/idmatch.js, which the observer page uses to jump
# at once; the server matches again with this function for the record.

_ID_TEXT_RE = re.compile(r"^[0-9]+(?:[\s-]+[0-9]+)*$")
#: Fewest digits for text without separators to count as an imprinted ID.
MIN_ID_DIGITS = 3


@dataclass(frozen=True)
class IdMatch:
    """``status`` is ``found`` (with ``imprinted_id``), ``ambiguous`` (with
    ``candidates``), ``not_found`` (looks like an ID, not on the list) or
    ``not_id`` (an ordinary note)."""

    status: str
    imprinted_id: str | None = None
    candidates: tuple[str, ...] = ()


def id_groups(text: str) -> list[str]:
    """The digit groups of an ID, without leading zeros: ``"102-046-5"`` is ``["102", "46", "5"]``."""
    return [g.lstrip("0") or "0" for g in re.findall(r"[0-9]+", text)]


def looks_like_id(text: str) -> bool:
    """Digits separated by dashes or spaces, or at least `MIN_ID_DIGITS` digits alone."""
    text = text.strip()
    if not _ID_TEXT_RE.match(text):
        return False
    return len(id_groups(text)) > 1 or len(text) >= MIN_ID_DIGITS


def match_imprinted_id(text: str, ids: Sequence[str]) -> IdMatch:
    """Which of ``ids`` an observer meant by ``text``.

    In order, stopping at the first rule that matches anything: the exact
    text; the same digit groups (ignoring spaces, dashes and leading zeros);
    the same digits with every separator removed; and the last digit groups
    (a suffix such as ``46-5`` for ``102-46-5``, when the text has at least
    two groups). One match is ``found``, several ``ambiguous``.
    """
    text = text.strip()
    if not looks_like_id(text):
        return IdMatch("not_id")
    if text in ids:
        return IdMatch("found", text)
    groups = id_groups(text)
    digits = re.sub(r"[^0-9]", "", text)
    rules: list[Callable[[str], bool]] = [
        lambda i: id_groups(i) == groups,
        lambda i: re.sub(r"[^0-9]", "", i) == digits,
    ]
    if len(groups) > 1:
        rules.append(lambda i: len(id_groups(i)) > len(groups) and id_groups(i)[-len(groups):] == groups)
    for rule in rules:
        found = sorted({i for i in ids if rule(i)}, key=pull_order_key)
        if len(found) == 1:
            return IdMatch("found", found[0])
        if found:
            return IdMatch("ambiguous", candidates=tuple(found))
    return IdMatch("not_found")


def record_heard_id(
    log: EventLog,
    link: Link,
    item: Mapping[str, Any],
    seen: set[tuple[str, int]],
    ballots: Sequence[Mapping[str, Any]],
    *,
    signature: str | None = None,
    extra: Mapping[str, JSONValue] | None = None,
) -> tuple[int, str, int | None] | None:
    """Record an imprinted ID the observer heard instead of the expected ballot's.

    The ID (``heard``) is matched against the board's ballots with
    `match_imprinted_id`. If it names one of them, an
    ``observation.out_of_order`` event records that the audit board read
    that ballot out of order (``expected_position``,
    ``expected_imprinted_id``, ``heard``, ``found_position``,
    ``found_imprinted_id``); otherwise an ``observation.id_not_on_list``
    event records the heard text against the expected ballot, with the match
    status and any candidates. Returns ``(seq, event kind, found position)``,
    or None if this device already sent it. ``signature`` and ``extra`` are
    as for `record_entry`.

    Raises:
        PracticeError: for an invalid item, or a link that isn't an observer's.
    """
    if link.role != "observer":
        raise PracticeError("only an observer link records entries")
    heard = item.get("heard")
    if not isinstance(heard, str) or not heard.strip() or len(heard) > 64:
        raise PracticeError("invalid entry: heard")
    checked = _check_entry(dict(item) | {"match": False, "note": heard})
    key = (checked["device_id"], checked["device_seq"])
    if key in seen:
        return None
    by_id = {str(b["imprinted_id"]): b for b in ballots}
    result = match_imprinted_id(heard, list(by_id))
    data: dict[str, JSONValue] = {
        "expected_position": checked["position"],
        "expected_imprinted_id": checked["imprinted_id"],
        "heard": checked["note"],
        "elapsed_ms": checked["elapsed_ms"],
        "device_seq": checked["device_seq"],
    }
    found_position: int | None = None
    if result.status == "found" and result.imprinted_id is not None:
        kind = "observation.out_of_order"
        found_position = int(by_id[result.imprinted_id]["position"])
        data |= {"found_position": found_position, "found_imprinted_id": result.imprinted_id}
    else:
        kind = "observation.id_not_on_list"
        data |= {"match": result.status, "candidates": list(result.candidates[:10])}
    data |= dict(extra or {})
    event = log.append(
        kind,
        link.actor,
        county=link.county,
        board=str(link.board),
        device_id=checked["device_id"],
        client_time=checked["client_time"],
        signature=signature,
        data=data,
    )
    seen.add(key)
    return event.seq, kind, found_position


def seen_entries(log: EventLog, link: Link) -> set[tuple[str, int]]:
    seen: set[tuple[str, int]] = set()
    for event in log.iter_events():
        if event.kind in OBSERVER_KINDS and event.actor == link.actor and event.device_id:
            seq = event.data.get("device_seq")
            if isinstance(seq, int):
                seen.add((event.device_id, seq))
    return seen


def latest_entries(events: Iterable[Event], link: Link) -> dict[int, dict[str, Any]]:
    """The observer's latest entry for each position, as recorded."""
    latest: dict[int, dict[str, Any]] = {}
    for event in events:
        if event.kind in SETTLING_KINDS and event.actor == link.actor and event.county == link.county and event.board == str(link.board):
            position = event.data.get("position")
            if isinstance(position, int):
                latest[position] = dict(event.data) | {"seq": event.seq, "server_time": event.server_time}
    return latest


def record_other_board(
    log: EventLog,
    link: Link,
    item: Mapping[str, Any],
    seen: set[tuple[str, int]],
    *,
    signature: str | None = None,
    extra: Mapping[str, JSONValue] | None = None,
) -> int | None:
    """Record that a boundary ballot was not pulled by the observer's board (it is another board's); return the seq.

    One tap on the observer page ("Skipped: other board"), for a ballot the
    extract lists on two boards because CORLA's split of the ballots among
    boards is not known before the pull list (docs/EXTRACT.md). The ballot
    then counts as settled, neither missing nor read out of order. ``item``
    is as for `record_first_reading`.

    Raises:
        PracticeError: for an invalid item, or a link that isn't an observer's.
    """
    if link.role != "observer":
        raise PracticeError("only an observer link records entries")
    checked = _check_entry(dict(item) | {"match": True, "note": ""})
    key = (checked["device_id"], checked["device_seq"])
    if key in seen:
        return None
    data: dict[str, JSONValue] = {k: checked[k] for k in ("position", "imprinted_id", "elapsed_ms", "device_seq")}
    data |= {"other_board": True} | dict(extra or {})
    event = log.append(
        "observation.other_board",
        link.actor,
        county=link.county,
        board=str(link.board),
        device_id=checked["device_id"],
        client_time=checked["client_time"],
        signature=signature,
        data=data,
    )
    seen.add(key)
    return event.seq


# ---------------------------------------------------------------------------
# The practice reader


def _read_list(event: Event) -> str:
    key = event.data.get("list")
    return str(key) if isinstance(key, str) and key else RLA


def _reads(events: Iterable[Event], county: str, board: int, list_key: str = RLA) -> list[Event]:
    """The board's ``practice.read`` events of a pull list since its last reset."""
    reads: list[Event] = []
    for event in events:
        if event.county != county or event.board != str(board) or _read_list(event) != list_key:
            continue
        if event.kind == "practice.read_reset":
            reads = []
        elif event.kind == "practice.read":
            reads.append(event)
    return reads


def current_read(log: EventLog, county: str, board: int, list_key: str = RLA) -> dict[str, Any] | None:
    reads = _reads(log.iter_events(), county, board, list_key)
    return dict(reads[-1].data) if reads else None


def _deviate(ballot: Mapping[str, Any], kind: str, extract: Mapping[str, Any], rng: random.Random) -> tuple[str, list[list[str]], dict[str, JSONValue]] | None:
    votes = [list(v) for v in ballot["votes"]]
    if kind == "different_vote":
        options = [
            (i, [c for c in extract["choices"].get(v[0], []) if c != v[1]])
            for i, v in enumerate(votes)
            if v[1] not in ("redacted",)
        ]
        options = [(i, alts + (["no vote"] if votes[i][1] != "no vote" else [])) for i, alts in options]
        options = [(i, alts) for i, alts in options if alts]
        if not options:
            return None
        i, alts = rng.choice(options)
        new = rng.choice(alts)
        detail: dict[str, JSONValue] = {"contest": votes[i][0], "expected": votes[i][1], "read": new}
        votes[i][1] = new
        return ballot["imprinted_id"], votes, detail
    if kind == "wrong_ballot":
        parts = str(ballot["imprinted_id"]).split("-")
        if not parts[-1].isdigit():
            return None
        parts[-1] = str(int(parts[-1]) + rng.choice((1, 2, 3)))
        wrong = "-".join(parts)
        return wrong, votes, {"expected": ballot["imprinted_id"], "read": wrong}
    return None


def next_read(
    log: EventLog,
    link: Link,
    extract: Mapping[str, Any],
    *,
    inject_rate: float = 0.0,
    rng: random.Random | None = None,
    list_key: str = RLA,
    extract_sha256: str | None = None,
) -> list[int]:
    """Record the board's next read-out (and a skipped ballot, if one is injected); return the events' seqs.

    ``list_key`` is the pull list read from (`corla_observer.pulllists`), and
    ``extract_sha256`` its extract's hash: a targeted list's readings are
    kept apart from the RLA sample's, and recorded with ``list`` and
    ``extract_sha256``.

    Raises:
        PracticeError: for a link that isn't a reader's, or at the end of the board's ballots.
    """
    if link.role != "reader":
        raise PracticeError("only a reader link reads ballots")
    rng = rng or random.SystemRandom()
    ballots = board_ballots(extract, link.board)
    current = current_read(log, link.county, link.board, list_key)
    index = 0 if current is None else int(current["board_position"])
    if index >= len(ballots):
        raise PracticeError("that was the board's last ballot")
    seqs: list[int] = []

    def append(ballot: Mapping[str, Any], imprinted: str | None, votes: list[list[str]], deviation: dict[str, JSONValue] | None) -> None:
        data: dict[str, JSONValue] = {
            "position": ballot["position"],
            "board_position": ballot["board_position"],
            "of": len(ballots),
            "imprinted_id_read": imprinted,
            "votes_read": [[str(x) for x in v] for v in votes],
            "redacted": ballot.get("status") == "redacted",
            "deviation": deviation,
        }
        if list_key != RLA or extract_sha256:
            data |= {"list": list_key, "extract_sha256": extract_sha256}
        seqs.append(log.append("practice.read", link.actor, county=link.county, board=str(link.board), data=data).seq)

    ballot = ballots[index]
    if inject_rate > 0 and rng.random() < inject_rate:
        kinds = list(DEVIATIONS)
        if index + 1 >= len(ballots):
            kinds.remove("skipped_ballot")
        kind = rng.choice(kinds)
        if kind == "skipped_ballot":
            append(ballot, None, [], {"type": "skipped_ballot"})
            ballot = ballots[index + 1]
            append(ballot, ballot["imprinted_id"], ballot["votes"], None)
            return seqs
        changed = _deviate(ballot, kind, extract, rng)
        if changed is not None:
            imprinted, votes, detail = changed
            deviation: dict[str, JSONValue] = {"type": kind}
            deviation.update(detail)
            append(ballot, imprinted, votes, deviation)
            return seqs
    append(ballot, ballot["imprinted_id"], ballot["votes"], None)
    return seqs


def reset_reads(log: EventLog, link: Link, list_key: str = RLA) -> int:
    if link.role != "reader":
        raise PracticeError("only a reader link reads ballots")
    data: dict[str, JSONValue] = {"list": list_key} if list_key != RLA else {}
    return log.append("practice.read_reset", link.actor, county=link.county, board=str(link.board), data=data).seq


# ---------------------------------------------------------------------------
# Statistics


@dataclass
class ObserverStats:
    actor: str
    county: str
    board: str
    #: The pull list (`corla_observer.pulllists`): ``rla``, or a targeted list's key.
    list_key: str = RLA
    list_label: str = RLA_NAME
    entries: int = 0
    edits: int = 0
    matches: int = 0
    notes: int = 0
    positions: int = 0
    median_ms: int | None = None
    slowest_ms: int | None = None
    #: First taps ("First reading"), the median time from a ballot being
    #: shown to its first reading, and from the first reading to the
    #: confirmed match.
    first_readings: int = 0
    first_reading_median_ms: int | None = None
    confirm_median_ms: int | None = None
    #: Scoring against injected deviations, where the board had a reader.
    deviations: int = 0
    caught: int = 0
    missed: int = 0
    false_alarms: int = 0
    #: Ballots the observer heard read out of order, and IDs heard that are not on the board's list.
    out_of_order: int = 0
    not_on_list: int = 0
    #: Boundary ballots marked "skipped: other board".
    other_board: int = 0
    #: Entry modes used (`corla_observer.contests`): "two taps", "contest by contest", or both.
    modes: list[str] = field(default_factory=list)
    #: Contest by contest: outcomes entered (each contest, each reading), corrections among them,
    #: and contests whose final outcome was heard instead, not heard clearly, or differed between readings.
    contest_marks: int = 0
    contest_corrections: int = 0
    contest_heard: int = 0
    contest_unclear: int = 0
    contest_differs: int = 0
    #: Ballots entered contest by contest that are done.
    contest_ballots: int = 0

    @property
    def mode_text(self) -> str:
        # No mode recorded and no entries to show one: only older records, from when two taps was the
        # default and the page recorded a mode only when the observer changed it (`corla_observer.contests`).
        return ", ".join(MODE_LABELS.get(m, m) for m in self.modes) or MODE_LABELS["two_tap"]


@dataclass
class BoardProgress:
    county: str
    board: str
    ballots: int | None = None
    #: The pull list (`corla_observer.pulllists`): ``rla``, or a targeted list's key, with its label for coordinators
    #: ("Targeted: NAME") and for the public ("Targeted list N").
    list_key: str = RLA
    list_label: str = RLA_NAME
    public_label: str = RLA_NAME
    done: int = 0
    matches: int = 0
    notes: int = 0
    observers: list[str] = field(default_factory=list)
    read: int = 0
    #: Ballots settled as another board's (boundary ballots).
    other_board: int = 0
    #: Observers who entered contest by contest (some or all of the time).
    per_contest: list[str] = field(default_factory=list)
    #: The imprinted IDs of the ballots done, for counting a targeted list's ballots the RLA sample also holds (docs/TARGETING.md).
    done_ids: list[str] = field(default_factory=list)

    @property
    def targeted(self) -> bool:
        return self.list_key != RLA

    @property
    def lookup(self) -> bool:
        """Box-targeted lookups (`corla_observer.pulllists.LOOKUP`), which have no extract and no ballot count."""
        return self.list_key == LOOKUP


def practice_stats(events: Sequence[Event]) -> tuple[list[ObserverStats], list[BoardProgress]]:
    """Per observer: entries, matches, notes, entry times and scoring; per board: progress.

    A ballot entered contest by contest (`corla_observer.contests`) counts as
    entered once it is done (marked done, or every contest has an outcome in
    the second reading): as a match if every contest was confirmed, and
    otherwise as a note naming the contests heard instead, not heard clearly,
    or heard differently in the two readings. Where an observer entered a
    ballot both ways, the later entry counts.
    """
    lists = extract_lists(events)
    labels: dict[tuple[str, str], tuple[str, str]] = {}
    for county, found in all_lists(events).items():
        for item in found:
            labels[(county, item.key)] = (item.label, item.public_label)
    sizes: dict[tuple[str, str, str], int] = {}
    entries: dict[tuple[str, str, str, str], list[Event]] = {}
    reads: dict[tuple[str, str, str], list[Event]] = {}
    for event in events:
        if event.kind in ("extract.built", "practice.extract_built") and event.county:
            counts = event.data.get("boards")
            built = lists.get(event.file_sha256 or "", RLA)
            if isinstance(counts, dict):
                for old in [k for k in sizes if k[0] == event.county and k[2] == built]:
                    del sizes[old]  # a rebuilt list's boards are its latest extract's
                for board, n in counts.items():
                    if isinstance(n, int):
                        sizes[(event.county, board, built)] = n
        elif event.kind in OBSERVER_KINDS and event.county and event.board:
            entries.setdefault((event.actor, event.county, event.board, entry_list(event, lists)), []).append(event)
        elif event.kind == "practice.read_reset" and event.county and event.board:
            reads[(event.county, event.board, _read_list(event))] = []
        elif event.kind == "practice.read" and event.county and event.board:
            reads.setdefault((event.county, event.board, _read_list(event)), []).append(event)

    def label(county: str, key: str) -> tuple[str, str]:
        if key == LOOKUP:
            return (LOOKUP_NAME, LOOKUP_NAME)
        return labels.get((county, key), (RLA_NAME, RLA_NAME) if key == RLA else ("Targeted", "Targeted list"))

    def progress_for(county: str, board: str, key: str, ballots: int | None = None) -> BoardProgress:
        names = label(county, key)
        return BoardProgress(county, board, ballots=ballots, list_key=key, list_label=names[0], public_label=names[1])

    observers: list[ObserverStats] = []
    boards: dict[tuple[str, str, str], BoardProgress] = {k: progress_for(k[0], k[1], k[2], n) for k, n in sizes.items()}
    board_latest: dict[tuple[str, str, str], dict[int, list[Mapping[str, JSONValue]]]] = {}
    for (actor, county, board, list_key), evs in sorted(entries.items()):
        stats = ObserverStats(actor, county, board, list_key, label(county, list_key)[0])
        stats.modes = observer_modes(evs).get((actor, county, board), [])
        if not stats.modes:
            # Nothing entered on this list yet: the mode the observer is in, chosen on another list.
            chosen = latest_mode(events, actor, county, board)
            stats.modes = [chosen] if chosen else []
        latest: dict[int, Mapping[str, JSONValue]] = {}
        latest_seq: dict[int, int] = {}
        times = []
        first_times: list[int] = []
        confirm_times: list[int] = []
        #: The latest first reading's elapsed time, per device and position.
        pending_first: dict[tuple[str | None, int], int] = {}
        for e in evs:
            if e.kind == "observation.first_reading":
                stats.first_readings += 1
                position, elapsed = e.data.get("position"), e.data.get("elapsed_ms")
                if isinstance(position, int) and isinstance(elapsed, int):
                    first_times.append(elapsed)
                    pending_first[(e.device_id, position)] = elapsed
                continue
            if e.kind == "observation.out_of_order":
                stats.out_of_order += 1
                continue
            if e.kind == "observation.id_not_on_list":
                stats.not_on_list += 1
                continue
            if e.kind in CONTEST_KINDS:
                continue
            if e.kind == "observation.other_board":
                position = e.data.get("position")
                if isinstance(position, int):
                    latest[position] = e.data
                    latest_seq[position] = e.seq
                continue
            position = e.data.get("position")
            if not isinstance(position, int):
                continue
            latest[position] = e.data
            latest_seq[position] = e.seq
            if e.data.get("edit"):
                stats.edits += 1
            else:
                stats.entries += 1
                elapsed = e.data.get("elapsed_ms")
                if isinstance(elapsed, int):
                    times.append(elapsed)
            first = pending_first.pop((e.device_id, position), None)
            elapsed = e.data.get("elapsed_ms")
            if first is not None and e.data.get("match") and isinstance(elapsed, int) and elapsed >= first:
                confirm_times.append(elapsed - first)
        for position, rows in fold_contests(evs).get((actor, county, board), {}).items():
            for contest_row in rows.rows.values():
                stats.contest_marks += contest_row.marks
                stats.contest_corrections += contest_row.corrections
                final = contest_row.final
                if contest_row.differs:
                    stats.contest_differs += 1
                elif final is not None and final.outcome == "heard_instead":
                    stats.contest_heard += 1
                elif final is not None and final.outcome == "unclear":
                    stats.contest_unclear += 1
            if not rows.done:
                continue
            stats.contest_ballots += 1
            if rows.last_seq > latest_seq.get(position, 0):
                latest[position] = {"match": rows.match, "note": rows.note(), "per_contest": True, "imprinted_id": rows.imprinted_id}
                latest_seq[position] = rows.last_seq
        stats.positions = len(latest)
        stats.other_board = sum(1 for d in latest.values() if d.get("other_board"))
        stats.matches = sum(1 for d in latest.values() if d.get("match") and not d.get("note") and not d.get("other_board"))
        stats.notes = stats.positions - stats.matches - stats.other_board
        if times:
            stats.median_ms = int(statistics.median(times))
            stats.slowest_ms = max(times)
        if first_times:
            stats.first_reading_median_ms = int(statistics.median(first_times))
        if confirm_times:
            stats.confirm_median_ms = int(statistics.median(confirm_times))
        deviations = {
            int(r.data["position"]): r.data.get("deviation")  # type: ignore[arg-type]
            for r in reads.get((county, board, list_key), [])
            if isinstance(r.data.get("position"), int)
        }
        for position, deviation in deviations.items():
            noted = position in latest and bool(latest[position].get("note"))
            if deviation:
                stats.deviations += 1
                if noted:
                    stats.caught += 1
                else:
                    stats.missed += 1
            elif noted:
                stats.false_alarms += 1
        observers.append(stats)
        progress = boards.setdefault((county, board, list_key), progress_for(county, board, list_key))
        progress.observers.append(actor)
        if "per_contest" in stats.modes:
            progress.per_contest.append(actor)
        per_position = board_latest.setdefault((county, board, list_key), {})
        for position, d in latest.items():
            per_position.setdefault(position, []).append(d)
    for bkey, per_position in board_latest.items():
        progress = boards[bkey]
        progress.done = len(per_position)
        progress.done_ids = sorted({str(d["imprinted_id"]) for ds in per_position.values() for d in ds if d.get("imprinted_id")})
        progress.notes = sum(1 for ds in per_position.values() if any(d.get("note") for d in ds))
        progress.other_board = sum(1 for ds in per_position.values() if all(d.get("other_board") for d in ds))
        progress.matches = progress.done - progress.notes - progress.other_board
    for rkey, rs in reads.items():
        boards.setdefault(rkey, progress_for(*rkey)).read = len({r.data.get("position") for r in rs})
    numbers = {(c, x.key): x.number for c, found in all_lists(events).items() for x in found}

    def order(k: tuple[str, str, str]) -> tuple[str, int, int]:
        return (k[0], numbers.get((k[0], k[2]), 0 if k[2] == RLA else 999), int(k[1]) if k[1].isdigit() else 0)

    return observers, [boards[k] for k in sorted(boards, key=order)]
