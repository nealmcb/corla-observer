"""Check the sample-count estimates against CORLA's published counts, on a past election from the public mirror.

What this is for: before the November 2026 general election, the round-one
sample counts must be estimated (`corla_observer.estimate`) from election
night results and, until the manifests arrive, ballots cast times cards per
ballot. This script measures how close those estimates come to CORLA's own
counts, for the 2026 primary by default, under several states of knowledge,
and checks that every low estimate is at most CORLA's count (so that every
ballot an interim extract marks certain was in fact drawn). It also compares
each contest's margin in the counties' redacted CVRs (counting the
anonymizer's aggregate rows; `corla_observer.tally`) with CORLA's and the
results' margins.

It builds a scratch workspace per scenario (deleted afterwards): the audit
center files as the mirror held them at ``--commit`` (repeatable; the
target list's commit by default, and the manifests' commit for the
"with manifests" scenario), plus the results files given with ``--results
SCOPE=FILE`` (``state`` or a county; detail.xml or its zip, detail.xlsx, or
a county's summary.json, as `corla_observer.enr.read_results` reads them).
Results files are not in this repository; the 2026 primary's final ones
came from https://results.enr.clarityelections.com/CO/126592/ (state:
``377440/reports/detailxml.zip``; counties: ``CO/<County>/<id>/<version>/json/en/summary.json``),
and the mirror's ``2026/primary/observerfiles/SOS_ENR_detail_20260710.xlsx``
is the state's detail as of 10 July.

Usage::

    uv run python scripts/validate_estimates.py --mirror MIRROR \\
        --results state=detailxml.zip --results "La Plata=La_Plata.json" ... \\
        [--prelim-results state=SOS_ENR_detail_20260710.xlsx] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from corla_observer.election import ElectionSetup, parse_setup
from corla_observer.enr import load_results, record_results_file
from corla_observer.estimate import CountCheck, check_counts, corla_contest_counts, estimate_contests
from corla_observer.events import EventLog
from corla_observer.datamap import build_map
from corla_observer.rehearse import PRIMARY_2026_COUNTY_FILES, stage_fetch
from corla_observer.tally import cvr_margin, tally_cvr
from corla_observer.workspace import Workspace

ELECTION = "2026/primary"
TARGETS_COMMIT = "5108046"  # 2026-07-07: the target contest list
MANIFESTS_COMMIT = "43b6c78"  # 2026-07-13 16:15: every manifest, corrected, and the upload status
FINAL_COMMIT = "ba18ba6"  # 2026-07-16: CORLA's final reports
CONTEST_LIST = "2026/primary/finalReports/ContestsListRound1.csv"


def primary_setup() -> ElectionSetup:
    """A setup for the primary: one card per ballot everywhere (its ballots had one card)."""
    return parse_setup(f'format = 1\nelection = "{ELECTION}"\n[results]\nfinal_after = "2026-07-20 00:00"\n')


def scenario(
    mirror: Path,
    commits: Sequence[str],
    results: Sequence[tuple[str, Path]],
    results_time: str,
    corla: dict[str, tuple[int, int | None, int | None]],
) -> list[CountCheck]:
    """The estimates made from the mirror at ``commits`` and the given results files, checked against CORLA's counts."""
    root = Path(tempfile.mkdtemp(prefix="validate-estimates-"))
    try:
        ws = Workspace.open(root)
        with EventLog.open(root) as log:
            for commit in commits:
                stage_fetch(ws, log, mirror, commit, election=ELECTION)
            for scope, path in results:
                record_results_file(
                    ws, log, path.read_bytes(), url=f"file:{path.name}", scope=scope, what="detail",
                    election_id=None, version=None, simulated={"source": "file", "time": results_time},
                )  # fmt: skip
            datamap = build_map(log, ws)
            estimates = estimate_contests(datamap, setup=primary_setup(), results=load_results(ws, log), log=log)
        return check_counts(estimates, corla)
    finally:
        shutil.rmtree(root)


def summarize(name: str, checks: Sequence[CountCheck]) -> dict[str, object]:
    """Counts of exact, close and far estimates, the mean and worst errors, and any low estimate above CORLA's count."""
    compared = [c for c in checks if c.error is not None]
    errors = [c.error for c in compared if c.error is not None]
    relative = [abs(c.error) / c.corla_count for c in compared if c.error is not None and c.corla_count]
    worst = sorted(compared, key=lambda c: -abs(c.error or 0))[:5]
    return {
        "scenario": name,
        "contests": len(checks),
        "compared": len(compared),
        "exact": sum(1 for e in errors if e == 0),
        "within_2": sum(1 for e in errors if abs(e) <= 2),
        "within_10pct": sum(1 for r in relative if r <= 0.10),
        "mean_abs_error": round(statistics.mean(abs(e) for e in errors), 2) if errors else None,
        "median_rel_error_pct": round(100 * statistics.median(relative), 1) if relative else None,
        "low_above_corla": [c.name for c in compared if c.low_ok is False],
        "worst": [
            {"name": c.name, "estimate": c.central, "low": c.low, "corla": c.corla_count, "margin": c.margin,
             "corla_margin": c.corla_margin, "denominator": c.denominator, "corla_denominator": c.corla_denominator,
             "margin_source": c.margin_source[:80], "denominator_source": c.denominator_source[:80]}
            for c in worst
        ],  # fmt: skip
        "not_compared": [c.name for c in checks if c.error is None][:20],
    }


def cvr_margins(mirror: Path, corla: dict[str, tuple[int, int | None, int | None]], results: Sequence[tuple[str, Path]]) -> list[dict[str, object]]:
    """Each observer county's targeted contests: the margin in its redacted CVR against CORLA's and the results'."""
    from corla_observer.enr import contest_votes, find_contest, margin_of, read_results

    state = next((read_results(p.read_bytes()) for s, p in results if s == "state"), None)
    rows: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory() as tmp:
        for county, files in PRIMARY_2026_COUNTY_FILES.items():
            path = Path(tmp) / files["redacted_cvr"]
            path.write_bytes(subprocess.run(
                ["git", "-C", str(mirror), "show", f"{FINAL_COMMIT}:{ELECTION}/observerfiles/{files['redacted_cvr']}"],
                check=True, capture_output=True).stdout)  # fmt: skip
            tally = tally_cvr(path)
            for name, (_, margin, _) in corla.items():
                if not name.endswith(f"({county})"):
                    continue
                contest = tally.find(name)
                enr = None
                if state is not None:
                    found = find_contest(state, name)
                    if found is not None:
                        enr = margin_of(contest_votes(found, [county], True), found.vote_for)
                rows.append({
                    "county": county, "contest": name, "corla_margin": margin, "enr_margin": enr,
                    "cvr_margin": cvr_margin(contest) if contest else None,
                    "cvr_redacted_rows": contest.redacted_rows if contest else None,
                    "cvr_aggregate_rows": contest.aggregate_rows if contest else None,
                })  # fmt: skip
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mirror", type=Path, default=os.environ.get("CORLA_OBSERVER_MIRROR"))
    parser.add_argument("--results", action="append", default=[], help="SCOPE=FILE: final results (state, or a county with _ for spaces)")
    parser.add_argument("--prelim-results", action="append", default=[], help="SCOPE=FILE: earlier results")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.mirror:
        parser.error("--mirror or $CORLA_OBSERVER_MIRROR is needed")
    mirror = Path(args.mirror)

    def pairs(items: Sequence[str]) -> list[tuple[str, Path]]:
        return [(item.split("=", 1)[0].replace("_", " "), Path(item.split("=", 1)[1])) for item in items]

    final, prelim = pairs(args.results), pairs(args.prelim_results)
    corla = corla_contest_counts(subprocess.run(["git", "-C", str(mirror), "show", f"{FINAL_COMMIT}:{CONTEST_LIST}"], check=True, capture_output=True).stdout)
    scenarios: list[tuple[str, list[str], list[tuple[str, Path]], str]] = [("target list only (7 July)", [TARGETS_COMMIT], [], "2026-07-07T18:00:00.000Z")]
    if prelim:
        scenarios.append(("results of 10 July, before manifests", [TARGETS_COMMIT], prelim, "2026-07-10T22:00:00.000Z"))
    if final:
        scenarios.append(("final results, before manifests", [TARGETS_COMMIT], final, "2026-07-23T22:00:00.000Z"))
        scenarios.append(("final results and manifests", [TARGETS_COMMIT, MANIFESTS_COMMIT], final, "2026-07-23T22:00:00.000Z"))
    report = {
        "scenarios": [summarize(name, scenario(mirror, commits, files, moment, corla)) for name, commits, files, moment in scenarios],
        "cvr_margins": cvr_margins(mirror, corla, final or prelim),
    }
    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
        return 0
    for s in report["scenarios"]:
        assert isinstance(s, dict)
        print(f"== {s['scenario']}: {s['compared']} of {s['contests']} compared; exact {s['exact']}, within 2 draws {s['within_2']}, "
              f"within 10% {s['within_10pct']}; mean |error| {s['mean_abs_error']}; median relative error {s['median_rel_error_pct']}%")  # fmt: skip
        print(f"   low estimate above CORLA's count: {s['low_above_corla'] or 'none'}")
        for w in s["worst"]:  # type: ignore[attr-defined]
            print(f"   worst: {w['name']}: estimate {w['estimate']} (low {w['low']}) vs CORLA {w['corla']}; "
                  f"margin {w['margin']} vs {w['corla_margin']}; cards {w['denominator']} vs {w['corla_denominator']}")  # fmt: skip
        if s["not_compared"]:
            print(f"   not compared: {s['not_compared']}")
    print("== CVR margins (redacted CVRs, aggregate rows counted)")
    for row in report["cvr_margins"]:
        print(f"   {row}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
