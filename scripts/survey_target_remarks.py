"""List the ballot cards per ballot that each election's target contest list gives in its remarks, for docs/AUDITCENTER_FORMATS.md.

What this is for: the Secretary of State's target list notes each
multi-card county's cards per ballot in its Remarks column ("2 ballot cards
per ballot*"), and computes that county's diluted margin over its ballots
times it; `corla_observer.estimate` uses these numbers before the manifests
arrive (`corla_observer.datamap.target_cards_per_ballot`). This script
checks which elections' target lists carry such remarks, and what they say.

It reads a git checkout of the mirror, https://github.com/nealmcb/auditcenter
(``--mirror``, default ``$CORLA_OBSERVER_MIRROR``), and for each tracked
target list (a file whose name contains "target" or "SelectedContests"): a spreadsheet or CSV is
read as the data map reads it, and every county with a cards-per-ballot
remark is printed; a PDF is converted with ``pdftotext`` (if installed) and
its lines that mention cards per ballot are printed as they are.

Usage::

    uv run python scripts/survey_target_remarks.py --mirror PATH
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from corla_observer.classify import classify_bytes
from corla_observer.datamap import table_rows, target_cards_per_ballot

PDF_LINE = re.compile(r"ballot\s*cards?\s*per\s*ballot", re.IGNORECASE)


def survey(mirror: Path) -> list[str]:
    """One line per target list, then one per county (or PDF line) with a cards-per-ballot remark."""
    names = subprocess.run(["git", "-C", str(mirror), "ls-files"], capture_output=True, check=True, text=True).stdout.split("\n")
    out = []
    for name in sorted(n for n in names if ("target" in n.lower() or "selectedcontests" in n.lower()) and n.lower().endswith((".xlsx", ".csv", ".pdf"))):
        data = (mirror / name).read_bytes()
        if name.lower().endswith(".pdf"):
            if shutil.which("pdftotext") is None:
                out.append(f"{name}: PDF, pdftotext not installed")
                continue
            text = subprocess.run(["pdftotext", "-layout", "-", "-"], input=data, capture_output=True, check=True).stdout.decode("utf-8", "replace")
            lines = [" ".join(line.split()) for line in text.splitlines() if PDF_LINE.search(line)]
            out.append(f"{name}: PDF, {len(lines)} line(s) mention cards per ballot")
            out.extend(f"  {line[:160]}" for line in lines)
            continue
        classification = classify_bytes(data)
        header, rows = table_rows(data, classification)
        found = target_cards_per_ballot(header, rows, classification)
        out.append(f"{name}: {classification.kind}, {len(found)} count{'y' if len(found) == 1 else 'ies'} with cards per ballot")
        out.extend(f"  {county}: {cards} ({remark})" for county, (cards, remark) in sorted(found.items()))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--mirror", type=Path, default=os.environ.get("CORLA_OBSERVER_MIRROR"))
    args = parser.parse_args(argv)
    if args.mirror is None:
        parser.error("give --mirror or set CORLA_OBSERVER_MIRROR")
    print("\n".join(survey(args.mirror)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
