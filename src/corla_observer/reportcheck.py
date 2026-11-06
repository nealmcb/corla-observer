"""Comparing observation reports: with each other, and with CORLA's own discrepancy report.

Written 2026-11-05 for the software-independence review (docs/si-review.md). The check that matters
at the end of an audit is not that every screen or page was right but that the audit's report of
its discrepancies agrees with what observers saw. An observation report (`corla_observer.obsreport`)
can come from any of the three ways of observing: online entries (the server's signed report, the
extension's own, or one computed from a downloaded record), a marked spreadsheet, or a paper tally
entered as one. This module compares them.

**Two observation reports** (`compare_reports`), for the same extract and board, ballot by ballot:
a ballot both observed with different verdicts (``match``, ``discrepancy``, ``other_board``), or
discrepancies naming different contests when both name them, is a difference. Ballots only one of
them observed, and ballots not finished, are counted, not compared.

**An observation report and CORLA's discrepancy report** (`compare_with_corla`): CORLA publishes,
per contest of each ballot where the audit board's interpretation differs from the cast vote
record, the county, contest, imprinted ID, both choices and a reason (``Audit Board Error``,
``Wrong Ballot``, ...); `read_corla_discrepancies` reads that file in any of the forms the
classifier recognizes (`corla_observer.classify`, kind ``discrepancy_report``). For each ballot the
observer saw, of the report's county:

- ``observer_only``: the observer heard a discrepancy and CORLA reports none on that ballot;
- ``corla_only``: CORLA reports a discrepancy on a ballot the observer heard match;
- ``contests_differ``: both found one, but the observer named contests CORLA does not, or the
  reverse;
- ``agree``: both found one in the same contests, or both none.

These are differences to explain, not verdicts: a board can rightly settle on another reading of a
voter's intent than the one read out, or re-read a ballot after a wrong one was pulled. A ballot the
observer marked as another board's, or did not finish, is not compared; CORLA's discrepancies on
ballots of the observer's list (``listed``, the extract's imprinted IDs for the board) that the
observer did not record are listed as ``not_observed``.

Contest names are compared ignoring case and spacing; the extract takes them from the county's cast
vote record, as CORLA does.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from corla_observer.obsreport import _norm

COMPARED = frozenset({"match", "discrepancy", "other_board"})


@dataclass(frozen=True)
class CorlaDiscrepancy:
    """One row of CORLA's discrepancy report: one contest on one ballot."""

    county: str
    imprinted_id: str
    contest: str
    cvr_choice: str
    board_choice: str
    reason: str


@dataclass
class Comparison:
    """The outcome of a comparison: each ballot's finding, and counts."""

    findings: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def add(self, kind: str, **detail: Any) -> None:
        self.findings.append({"kind": kind} | detail)
        self.counts[kind] = self.counts.get(kind, 0) + 1

    @property
    def differences(self) -> list[dict[str, Any]]:
        return [f for f in self.findings if f["kind"] not in ("agree", "only_in_a", "only_in_b", "unfinished")]

    def to_dict(self) -> dict[str, Any]:
        return {"counts": dict(sorted(self.counts.items())), "findings": self.findings, "differences": len(self.differences)}


def _contest_names(ballot: Mapping[str, Any]) -> set[str]:
    return {_norm(c["contest"]) for c in ballot.get("contests", [])}


def compare_reports(a: Mapping[str, Any], b: Mapping[str, Any]) -> Comparison:
    """Two observation reports of one extract and board, ballot by ballot (see the module documentation).

    Raises:
        ValueError: if they are of different extracts or boards.
    """
    if a["extract_sha256"] != b["extract_sha256"] or str(a["board"]) != str(b["board"]):
        raise ValueError("the reports are of different extracts or boards")
    out = Comparison()
    left = {x["position"]: x for x in a["ballots"]}
    right = {x["position"]: x for x in b["ballots"]}
    for position in sorted(set(left) | set(right)):
        x, y = left.get(position), right.get(position)
        if x is None or y is None:
            out.add("only_in_a" if y is None else "only_in_b", position=position, imprinted_id=(x or y or {}).get("imprinted_id", ""))
            continue
        if x["verdict"] not in COMPARED or y["verdict"] not in COMPARED:
            out.add("unfinished", position=position, imprinted_id=x["imprinted_id"])
        elif x["verdict"] != y["verdict"]:
            out.add("verdicts_differ", position=position, imprinted_id=x["imprinted_id"], a=x["verdict"], b=y["verdict"])
        elif x["contests"] and y["contests"] and _contest_names(x) != _contest_names(y):
            out.add("contests_differ", position=position, imprinted_id=x["imprinted_id"],
                    a=sorted(c["contest"] for c in x["contests"]), b=sorted(c["contest"] for c in y["contests"]))  # fmt: skip
        else:
            out.add("agree", position=position, imprinted_id=x["imprinted_id"])
    return out


def read_corla_discrepancies(data: bytes) -> list[CorlaDiscrepancy]:
    """CORLA's discrepancy report, from its published file (CSV, workbook or JSON).

    Raises:
        ValueError: if the file is not a discrepancy report.
    """
    from corla_observer.classify import classify_bytes
    from corla_observer.datamap import column, table_rows

    found = classify_bytes(data)
    if found.kind != "discrepancy_report":
        raise ValueError(f"not a CORLA discrepancy report (read as {found.kind})")
    header, rows = table_rows(data, found)
    matched = found.evidence.get("matched_columns") or {}
    assert isinstance(matched, dict)

    def col(role: str, *spellings: str) -> str | None:
        name = matched.get(role)
        return str(name) if isinstance(name, str) else column(header, *spellings)

    names = {
        "county": col("county", "county_name", "county"),
        "imprinted_id": col("imprinted_id", "imprinted_id", "imprinted id"),
        "contest": col("contest", "contest_name", "contest"),
        "cvr": column(header, "choice_per_voting_computer", "choice per voting computer"),
        "board": column(header, "audit_board_selection", "audit board selection"),
        "reason": col("reason", "discrepancy reason", "discrepancy type"),
    }

    def get(row: Mapping[str, str], key: str) -> str:
        name = names[key]
        return row.get(name, "").strip().strip('"') if name else ""

    out = []
    for row in rows:
        imprinted = get(row, "imprinted_id")
        if not imprinted or imprinted.lower().startswith("end of"):
            continue
        out.append(CorlaDiscrepancy(get(row, "county"), imprinted, get(row, "contest"), get(row, "cvr"), get(row, "board"), get(row, "reason")))
    return out


def read_corla_file(path: Path | str) -> list[CorlaDiscrepancy]:
    """`read_corla_discrepancies` of a file."""
    return read_corla_discrepancies(Path(path).read_bytes())


def compare_with_corla(report: Mapping[str, Any], corla: Iterable[CorlaDiscrepancy], *, listed: Iterable[str] = ()) -> Comparison:
    """An observation report against CORLA's discrepancies (see the module documentation)."""
    county = _norm(report["county"])
    by_ballot: dict[str, list[CorlaDiscrepancy]] = {}
    for d in corla:
        if _norm(d.county) == county or not d.county:
            by_ballot.setdefault(d.imprinted_id, []).append(d)
    out = Comparison()
    seen = set()
    for ballot in report["ballots"]:
        imprinted = ballot["imprinted_id"]
        seen.add(imprinted)
        theirs = by_ballot.get(imprinted, [])
        detail: dict[str, Any] = {"position": ballot["position"], "imprinted_id": imprinted}
        if theirs:
            detail["corla"] = [{"contest": d.contest, "cvr": d.cvr_choice, "board": d.board_choice, "reason": d.reason} for d in theirs]
        if ballot["verdict"] not in ("match", "discrepancy"):
            out.add("unfinished" if ballot["verdict"] == "incomplete" else "other_board", **detail)
        elif ballot["verdict"] == "match":
            out.add("corla_only" if theirs else "agree", **detail)
        elif not theirs:
            out.add("observer_only", **detail, observed=ballot["contests"] or ballot["note"])
        else:
            mine, corla_names = _contest_names(ballot), {_norm(d.contest) for d in theirs}
            if mine and mine != corla_names:
                out.add("contests_differ", **detail, observed=ballot["contests"])
            else:
                out.add("agree", **detail)
    for imprinted in sorted(set(listed) - seen):
        if imprinted in by_ballot:
            out.add("not_observed", imprinted_id=imprinted, corla=[{"contest": d.contest, "reason": d.reason} for d in by_ballot[imprinted]])
    return out


def render_comparison(comparison: Comparison, a: str = "a", b: str = "b") -> str:
    """A comparison as plain text: the counts, then each difference."""
    lines = ["  ".join(f"{k}: {v}" for k, v in sorted(comparison.counts.items())) or "nothing to compare"]
    for f in comparison.findings:
        if f["kind"] in ("agree", "only_in_a", "only_in_b", "unfinished", "other_board"):
            continue
        extra = ""
        if f["kind"] == "verdicts_differ":
            extra = f"{a} {f['a']}, {b} {f['b']}"
        elif "corla" in f:
            extra = "CORLA: " + "; ".join(f"{c['contest']} ({c['reason']})" for c in f["corla"])
        if f.get("observed"):
            seen = f["observed"]
            extra += ("; " if extra else "") + "observed: " + (seen if isinstance(seen, str) else "; ".join(f"{c['contest']}: {c['observed']}" for c in seen))
        lines.append(f"  {f['kind']:<16} {f.get('position', ''):>5}  {f['imprinted_id']:<14} {extra}")
    return "\n".join(lines) + "\n"
