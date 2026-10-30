"""A realistic committed extract in a workspace, for the photo check's tests.

`realistic_extract` builds an extract structure (docs/EXTRACT.md, format 2)
directly, without the CVR and prediction pipeline, with the contest and
candidate names of a Colorado primary: long contest names that the print
shortens, a multi-word candidate name printed with initials, a ballot
question, no votes, an overvote, a redacted ballot, one missing from the
CVR, POSSIBLE and BOUNDARY ballots, and two audit boards with several pages
each. `store_extract` stores it and its page index in a workspace and
records ``extract.built``; `commit_evidence_record` records an evidence
commitment covering it, as ``commit-evidence`` would, without git or the
proof tools.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any

from corla_observer.events import EventLog, canonical_json
from corla_observer.extract import page_index
from corla_observer.workspace import Workspace

ACTOR = "coordinator:alice"
SEED = "49006417086137856424"

#: Contest name, its choices (in CVR column order), and how often a ballot has it.
CONTESTS: tuple[tuple[str, tuple[str, ...], float], ...] = (
    ("United States Senator - DEM", ("John Hickenlooper", "Julie Gonzales", "Karen Breslin"), 1.0),
    ("Representative to the 120th United States Congress - District 8 - DEM", ("Manny Rutinel", "Shannon Bird", "Yadira Caraveo"), 1.0),
    ("Governor - DEM", ("Michael Bennet", "Phil Weiser"), 1.0),
    ("State Representative - District 48 - DEM", ("Dez Packard", "Kelley Anne Dennison"), 0.6),
    ("Regent of the University of Colorado - Congressional District 8 - DEM", ("Jessica Buckley", "Ilana Spiegel"), 0.5),
    ("County Commissioner - District 2 - DEM", ("Lynette Peppler", "Scott James"), 0.5),
    ("Weld County Ballot Issue 1A (Statutory)", ("Yes/For", "No/Against"), 0.4),
)


def realistic_extract(county: str = "Weld", *, ballots: int = 90, boards: int = 2, seed: int = 7, order: str = "predicted") -> dict[str, Any]:
    """An extract of ``ballots`` ballots split among ``boards`` boards; deterministic for a given ``seed``."""
    rng = random.Random(seed)
    ids: list[str] = []
    seen: set[str] = set()
    while len(ids) < ballots:
        imprinted = f"{rng.choice((101, 102, 104, 107))}-{rng.randint(1, 60)}-{rng.randint(1, 250)}"
        if imprinted not in seen:
            seen.add(imprinted)
            ids.append(imprinted)
    ids.sort(key=lambda i: tuple(int(p) for p in i.split("-")))
    per_board = -(-ballots // boards)
    out: list[dict[str, Any]] = []
    for n, imprinted in enumerate(ids, start=1):
        board = min(boards, (n - 1) // per_board + 1)
        votes: list[list[str]] = []
        for contest, choices, share in CONTESTS:
            if contest.startswith(("United States", "Representative", "Governor")) or rng.random() < share:
                roll = rng.random()
                if roll < 0.06:
                    value = "no vote"
                elif n == 11 and contest.startswith("Governor"):
                    value = f"overvote: {choices[0]} + {choices[1]}"
                else:
                    value = rng.choice(choices)
                votes.append([contest, value])
        status = "votes"
        if n == 17:
            status, votes = "redacted", []
        elif n == 29:
            status, votes = "not_in_cvr", []
        ballot = {
            "position": n,
            "board": board,
            "board_position": 0,
            "imprinted_id": imprinted,
            "location": f"Bin # {1 + n // 20}",
            "status": status,
            "votes": votes,
            "picks": [["United States Senator - DEM", "statewide"]],
            "predicted": True,
            "certain": n not in (8, 40),
            "boundary": [],
        }
        out.append(ballot)
    if boards > 1:  # the first board's last ballot may be the second board's
        last = max(b["position"] for b in out if b["board"] == 1)
        at = next(i for i, b in enumerate(out) if b["position"] == last)
        out[at] = dict(out[at], boundary=[2])
        out.insert(at + 1, dict(out[at], board=2, boundary=[1]))
    counts: dict[int, int] = {}
    for b in out:
        counts[b["board"]] = counts.get(b["board"], 0) + 1
        b["board_position"] = counts[b["board"]]
    return {
        "format": 2,
        "kind": "observer_extract",
        "purpose": "audit",
        "election": "2026/general",
        "county": county,
        "seed": SEED,
        "order": order,
        "order_note": "Synthetic extract for the photo check's tests.",
        "board_split": "uncertain",
        "inputs": {
            "pull_list": None,
            "redacted_cvr": hashlib.sha256(b"cvr" + county.encode()).hexdigest(),
            "county_manifest": hashlib.sha256(b"manifest" + county.encode()).hexdigest(),
            "manifests": hashlib.sha256(b"manifests").hexdigest(),
            "overrides": None,
            "prediction": hashlib.sha256(b"prediction").hexdigest(),
            "seed": SEED,
            "selection_list": None,
            "cvr_comparison": None,
        },
        "boards": {str(k): v for k, v in sorted(counts.items())},
        "contests": {c: {"scope": "county", "counties": [county]} for c, _, _ in CONTESTS},
        "choices": {c: list(ch) for c, ch, _ in CONTESTS},
        "checks": {"not_predicted": [], "predicted_not_listed": []},
        "ballots": out,
    }


def store_extract(ws: Workspace, log: EventLog, extract: dict[str, Any], *, actor: str = ACTOR) -> tuple[str, str]:
    """Store ``extract`` and its page index, record ``extract.built``, and return (extract hash, page index hash)."""
    stored = ws.put_bytes(canonical_json(extract))
    index = ws.put_bytes(canonical_json(page_index(extract, stored.sha256)))
    log.append(
        "extract.built",
        actor,
        county=str(extract["county"]),
        file_sha256=stored.sha256,
        data={
            "sha256": stored.sha256,
            "size": stored.size,
            "format": 2,
            "purpose": str(extract.get("purpose", "audit")),
            "order": str(extract["order"]),
            "page_index_sha256": index.sha256,
            "software": "corla-observer 0.1.0",
        },
    )
    return stored.sha256, index.sha256


def commit_evidence_record(
    ws: Workspace, log: EventLog, files: dict[str, str], *, version: int = 1, ots: str = "pending", rekor: int | None = 4242, bitcoin: int | None = None
) -> str:
    """Record an evidence commitment listing ``files`` (path to SHA-256), as ``commit-evidence`` would; return the SUMS file's hash.

    With ``bitcoin``, also record the proof's upgrade to that Bitcoin block.
    """
    name = "SHA256SUMS.txt" if version == 1 else f"SHA256SUMS.v{version}.txt"
    lines = [f"# version: {version}", "# time: 2026-11-15T20:00:00Z", *(f"{sha}  {path}" for path, sha in sorted(files.items()))]
    sums = ws.put_bytes(("\n".join(lines) + "\n").encode())
    subdir = "2026/general/observerfiles/evidence"
    log.append(
        "evidence.committed",
        ACTOR,
        file_sha256=sums.sha256,
        data={
            "election": "2026/general",
            "sums_name": name,
            "sums_sha256": sums.sha256,
            "version": version,
            "subdir": subdir,
            "files": len(files),
            "steps": [
                {"name": "opentimestamps", "status": ots, "detail": "stamped"},
                {"name": "cosign", "status": "done" if rekor is not None else "skipped", "detail": "signed"},
                {"name": "git_commit", "status": "done", "detail": "a" * 40},
                {"name": "push", "status": "done", "detail": "pushed"},
            ],
            "proofs": [name + ".ots", name + ".cosign-bundle"],
            "rekor_log_index": rekor,
            "commit": "a" * 40,
            "pushed": True,
        },
    )
    if bitcoin is not None:
        rel = f"{subdir}/{name}.ots"
        log.append(
            "evidence.proofs_upgraded",
            ACTOR,
            data={"upgraded": [rel], "still_pending": [], "failed": [], "skipped": None, "commit": "b" * 40, "pushed": True, "bitcoin_blocks": {rel: bitcoin}},
        )
    return sums.sha256
