"""Compare the print layouts on a real extract: text size, room per column, lines, sheets and how many names had to be shortened.

What this is for: choosing the default print layout
(`corla_observer.printlayout.LAYOUTS`, docs/EXTRACT.md "The four
layouts"). For one county's latest extract in a workspace and one audit
board, it prints, per layout: the text's size and x-height (against the
July 2026 printout's Courier 7.46 pt), the column's width and about how
many characters of the extract's own names fit in it, lines per column
and per sheet, the board's sheets (its pages and the provenance sheet),
and how many distinct contest names and choices were printed shorter than
in full, and how many of the board's contest lines have a shortened name or
choice. With ``--qr-json`` (the output of ``scripts/qr_size_rates.py``) it
adds the share of simulated whole-sheet phone photos whose QR code decoded,
at the layout's QR code size.

Usage::

    uv run python scripts/layout_comparison.py --workspace WS --county Weld [--board 1] [--qr-json qr.json] [--markdown]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from corla_observer import printlayout
from corla_observer.events import EventLog
from corla_observer.extract import board_pages, latest_extract, print_names, print_rows
from corla_observer.printnames import base_contest, split_value
from corla_observer.workspace import Workspace


def compare(extract: dict[str, Any], sha: str, board: int) -> list[dict[str, Any]]:
    ballots = [b for b in extract["ballots"] if b["board"] == board]
    contests = sorted({str(c) for b in ballots for c, _ in b["votes"]})
    choices = sorted({n for b in ballots for _, v in b["votes"] for n in split_value(str(v))[1]})
    corpus = " ".join(base_contest(c) for c in contests) + " " + " ".join(choices)
    rows = []
    for name, lay in printlayout.LAYOUTS.items():
        names = print_names(extract, name)
        per_char = printlayout.text_width(corpus, printlayout.SANS, lay.size) / len(corpus)
        pages = board_pages(extract, sha, board, name)
        lines = [(c, v, pc, pv) for b in ballots if b["status"] == "votes" for (c, v), (pc, pv) in zip(b["votes"], print_rows(b, names), strict=True)]
        short_lines = sum(1 for c, v, pc, pv in lines if pc != base_contest(str(c)) or pv != str(v).replace(" + ", ", ").replace("no vote", "NO VOTE"))
        rows.append(
            {
                "layout": name,
                "sheet": "landscape" if lay.landscape else "portrait",
                "columns": lay.columns,
                "font": f"DejaVu Sans Condensed {lay.size:g} pt",
                "x_height_pt": round(lay.x_height, 2),
                "x_height_vs_july": round(lay.x_height / printlayout.MIN_X_HEIGHT, 2),
                "column_mm": round(lay.column_width / printlayout.MM, 1),
                "chars_per_column": int(lay.column_width / per_char),
                "lines_per_column": lay.column_lines,
                "lines_per_sheet": lay.column_lines * lay.columns,
                "sheets": len(pages) + 1,
                "pages": len(pages),
                "contests_shortened": sum(1 for c in contests if names.contest(c) != base_contest(c)),
                "contests": len(contests),
                "choices_shortened": sum(1 for n in choices if names.vote(n) != n),
                "choices": len(choices),
                "lines_shortened": short_lines,
                "lines": len(lines),
                "qr_mm": round(lay.qr_side / printlayout.MM, 1),
            }
        )
    return rows


def qr_rates(path: Path) -> dict[tuple[str, float, str], tuple[int, int, int]]:
    """(layout, size in mm, condition) to (plain decodes, the photo check's decodes, images)."""
    out: dict[tuple[str, float, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    for r in json.loads(path.read_text(encoding="utf-8")):
        cell = out[(r["layout"], float(r["size_mm"]), r["condition"])]
        cell[0] += bool(r["plain"])
        cell[1] += bool(r["check"])
        cell[2] += 1
    return {k: (v[0], v[1], v[2]) for k, v in out.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--county", required=True)
    parser.add_argument("--board", type=int, default=1)
    parser.add_argument("--qr-json", type=Path)
    parser.add_argument("--condition", default="phone photo 1600 px wide")
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()
    ws = Workspace.open(args.workspace)
    with EventLog.open(ws.root, read_only=True) as log:
        found = latest_extract(ws, log, args.county)
    if found is None:
        raise SystemExit(f"no extract for {args.county}")
    extract, sha = found
    rows = compare(extract, sha, args.board)
    rates = qr_rates(args.qr_json) if args.qr_json else {}
    for row in rows:
        got = rates.get((row["layout"], row["qr_mm"], args.condition))
        row["qr_decoded"] = f"{got[1]}/{got[2]} (plain zbarimg {got[0]}/{got[2]})" if got else ""
    if args.markdown:
        print("| Layout | Font | x-height | Column | Characters per column | Lines per column | Lines per sheet | Sheets, board " + str(args.board) + " | Contests shortened | Choices shortened | Contest lines with a shortened name | QR code | QR decoded, " + args.condition + " |")
        print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for r in rows:
            print(
                f"| `{r['layout']}` | {r['font']} | {r['x_height_pt']} pt ({r['x_height_vs_july']}x July's) | {r['column_mm']} mm | about {r['chars_per_column']} "
                f"| {r['lines_per_column']} | {r['lines_per_sheet']} | {r['sheets']} | {r['contests_shortened']} of {r['contests']} | {r['choices_shortened']} of {r['choices']} "
                f"| {r['lines_shortened']} of {r['lines']} | {r['qr_mm']} mm | {r['qr_decoded']} |"
            )
    else:
        print(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
