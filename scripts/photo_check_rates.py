"""Measure the photo check's verdicts across photo conditions and tampering, on pages printed by the real pipeline.

What this is for: docs/PHOTO_CHECK.md reports how often the photo check
(`corla_observer.photocheck`) gives each verdict for genuine and for
tampered printouts under various photo conditions. This script produces
those figures, and can be re-run whenever the check, the print layout or
the OCR changes, to see what changed.

It builds the synthetic but realistic extract of ``tests/photofixtures.py``
in a scratch workspace, with an evidence commitment covering it; prints
board 1's sheets through the real printout, its PDF rasterized by
``pdftoppm`` (``tests/photoimages.py``), in every layout, as they are and
with pen ticks in a third of the boxes, plus tampered printouts (a changed
choice, an imprinted ID swapped for another ballot's, another page's QR
code pasted on); degrades each sheet by every condition in
``photoimages.DEGRADATIONS``; checks every image; and prints, per layout
and condition, how many genuine pages got each verdict, and how many
tampered pages were caught (MISMATCH) or merely not confirmed, and how
often the identification line's short hashes were read.

A genuine page should be MATCH, or MATCH WITH UNREAD LINES when the photo
is poor (UNKNOWN PAGE when not even the QR code or the short hashes could
be read); a tampered one MISMATCH, or at worst not MATCH. A genuine page
called MISMATCH, or a tampered one called MATCH, is a failure, listed in
full.

By default it measures a reduced set, which takes a few minutes: the
default layout (`DEFAULT_LAYOUT`) under six conditions (`REDUCED`: a clean
scan, on a desk, turned 3 degrees, a 4 percent perspective, JPEG quality
30 and the combined "phone photo"), 7 sheets each, 42 checks. ``--all``
measures every layout under every condition (420 checks, an hour or more on
a laptop); ``--layout`` and ``--condition`` choose others. tesseract runs
single-threaded (`corla_observer.photoimage.tesseract_env`), so ``--jobs``
checks run side by side (at most 6 by default). Each check's result is
printed as it finishes ("n of N") and appended to the ``--json`` file (one
JSON object per line), so a long run can be followed and its partial
results kept.

Usage::

    uv run python scripts/photo_check_rates.py --out DIR [--all | --layout L ... --condition NAME ...]
        [--pages N] [--jobs N] [--json results.jsonl]

DIR receives the rendered and degraded images (keep it out of the
repository). Needs pdftoppm, tesseract and zbarimg.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

_TESTS = Path(__file__).resolve().parent.parent / "tests"
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import photoimages  # noqa: E402
from photofixtures import commit_evidence_record, realistic_extract, store_extract  # noqa: E402

from corla_observer.events import EventLog  # noqa: E402
from corla_observer.extract import DEFAULT_LAYOUT, LAYOUTS, board_pages  # noqa: E402
from corla_observer.photocheck import VERDICTS, Verdict, check_photo  # noqa: E402
from corla_observer.workspace import Workspace  # noqa: E402


#: The conditions measured by default.
REDUCED = ("clean scan", "on a desk", "rotated 3 degrees", "perspective 4%", "JPEG quality 30", "phone photo (all moderate)")


def _check(args: tuple[str, str]) -> dict[str, Any]:
    workspace, image = args
    ws = Workspace.open(Path(workspace))
    started = time.monotonic()
    with EventLog.open(ws.root, read_only=True) as log:
        result = check_photo(ws, log, Path(image).read_bytes())
    return {"verdict": result.verdict, "summary": result.summary, "counts": result.counts(), "seconds": round(time.monotonic() - started, 1),
            "differences": [f"{d.key}: {d.expected!r} read {d.read!r}" for d in result.differences()],
            "ident": {line.key[6:]: line.status for line in result.lines if line.key.startswith("ident:")}}  # fmt: skip


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--layout", action="append", choices=list(LAYOUTS))
    parser.add_argument("--condition", action="append", choices=list(photoimages.DEGRADATIONS))
    parser.add_argument("--pages", type=int, default=2, help="genuine pages per layout (default 2)")
    parser.add_argument("--all", action="store_true", help="every layout under every condition")
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--json", type=Path, help="append each check's result here, one JSON object per line")
    args = parser.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    layouts = args.layout or (list(LAYOUTS) if args.all else [DEFAULT_LAYOUT])
    conditions = args.condition or (list(photoimages.DEGRADATIONS) if args.all else list(REDUCED))

    ws_root = out / "workspace"
    extract = realistic_extract()
    if not ws_root.exists():
        ws = Workspace.open(ws_root)
        with EventLog.open(ws.root) as log:
            stored, index = store_extract(ws, log, extract)
            commit_evidence_record(ws, log, {"derived/extract-Weld.json": stored, "derived/extract-Weld-pages.json": index}, bitcoin=920000)
    ws = Workspace.open(ws_root)
    with EventLog.open(ws.root, read_only=True) as log:
        found = next(e.file_sha256 for e in log.iter_events() if e.kind == "extract.built")
    assert found is not None
    sha: str = found

    # (layout, case, genuine?, sheet path)
    sheets: list[tuple[str, str, bool, Path]] = []
    for layout in layouts:
        pages = board_pages(extract, sha, 1, layout)
        first = pages[0].ballots
        other_id = str(pages[1].ballots[3]["imprinted_id"])
        printed = photoimages.render_sheets(extract, sha, 1, layout, out / "sheets")
        sheets += [(layout, "genuine", True, p) for p in printed[1 : 1 + args.pages]]
        sheets.append((layout, "provenance", True, printed[0]))
        ticked = photoimages.render_sheets(extract, sha, 1, layout, out / "ticked", alter=photoimages.add_ticks(3))
        sheets.append((layout, "genuine, pen ticks", True, ticked[1]))
        votes = [v for b in first for v in b["votes"]]
        governor = next(i for i, (c, v) in enumerate(votes) if c.startswith("Governor") and v == "Michael Bennet")
        cases: list[tuple[str, photoimages.Alter]] = [
            ("tampered: a choice", photoimages.tamper_choice(1, governor, "Phil Weiser")),
            ("tampered: an imprinted ID", photoimages.tamper_id(1, 2, other_id)),
            ("tampered: another page's QR", photoimages.swap_qr(extract, sha, 1, layout, 1, 2)),
        ]
        for name, alter in cases:
            target = out / re.sub(r"[^a-z]+", "-", name)
            sheets.append((layout, name, False, photoimages.render_sheets(extract, sha, 1, layout, target, alter=alter)[1]))

    jobs: list[tuple[str, str, bool, str, Path]] = []
    for layout, case, genuine, sheet in sheets:
        for condition in conditions:
            folder = out / "degraded" / layout / re.sub(r"[^a-z]+", "-", case)
            folder.mkdir(parents=True, exist_ok=True)
            jobs.append((layout, case, genuine, condition, photoimages.degrade(sheet, condition, folder)))
    results: list[dict[str, Any]] = [{} for _ in jobs]
    if args.json:
        args.json.write_text("", encoding="utf-8")
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(_check, (str(ws_root), str(j[4]))): n for n, j in enumerate(jobs)}
        for done, future in enumerate(as_completed(futures), start=1):
            n = futures[future]
            layout, case, genuine, condition, image = jobs[n]
            results[n] = future.result()
            print(f"{done} of {len(jobs)}: {layout} / {case} / {condition}: {results[n]['summary']} ({results[n]['seconds']} s)", flush=True)
            if args.json:
                record = {"layout": layout, "case": case, "genuine": genuine, "condition": condition, "image": image.name, **results[n]}
                with args.json.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")

    table: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    failures = []
    records = []
    for (layout, case, genuine, condition, image), result in zip(jobs, results, strict=True):
        kind = "genuine" if genuine else "tampered"
        table[(layout, kind, condition)][result["verdict"]] += 1
        bad = (genuine and result["verdict"] == Verdict.MISMATCH) or (not genuine and result["verdict"] == Verdict.MATCH)
        if bad:
            failures.append((layout, case, condition, result))
        records.append({"layout": layout, "case": case, "genuine": genuine, "condition": condition, "image": image.name, **result})

    short = {Verdict.MATCH: "MATCH", Verdict.UNREAD: "UNREAD", Verdict.MISMATCH: "MISMATCH", Verdict.UNKNOWN: "UNKNOWN"}
    print(f"{'layout':12} {'pages':9} {'condition':28} " + " ".join(f"{short[v]:>8}" for v in VERDICTS))
    for (layout, kind, condition), counts in sorted(table.items(), key=lambda kv: (layouts.index(kv[0][0]), kv[0][1], conditions.index(kv[0][2]))):
        print(f"{layout:12} {kind:9} {condition:28} " + " ".join(f"{counts[v]:>8}" for v in VERDICTS))
    total = Counter((r["genuine"], r["verdict"]) for r in records)
    genuine_n = sum(n for (g, _), n in total.items() if g)
    tampered_n = sum(n for (g, _), n in total.items() if not g)
    print()
    print(f"genuine: {genuine_n} images; " + ", ".join(f"{short[v]} {total[(True, v)]}" for v in VERDICTS))
    print(f"tampered: {tampered_n} images; " + ", ".join(f"{short[v]} {total[(False, v)]}" for v in VERDICTS))
    # How often each short hash of the identification line was read (matched), on the genuine pages (a pasted QR code makes it differ).
    for part in ("extract", "page_hash"):
        seen = [r["ident"][part] for r in records if r["genuine"] and part in r.get("ident", {})]
        print(f"identification line {part} (genuine pages): matched {sum(s == 'matched' for s in seen)} of {len(seen)}")
    print(f"mean seconds per image: {sum(r['seconds'] for r in records) / max(1, len(records)):.1f}")
    for layout, case, condition, result in failures:
        print(f"FAILURE {layout} / {case} / {condition}: {result['summary']}")
        for line in result["differences"][:6]:
            print(f"    {line}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
