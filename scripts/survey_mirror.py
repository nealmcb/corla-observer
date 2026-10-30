"""Classify every file of every election in the public audit center mirror, for the format survey.

What this is for: docs/AUDITCENTER_FORMATS.md records, per election, which
kinds of file the Secretary of State's Audit Center published, under which
names, with which header variants, and what `corla_observer.classify` makes
of each. This script produces the raw material for that document, and can be
re-run whenever the classifier changes, to see what changed.

It reads a git checkout of the mirror, https://github.com/nealmcb/auditcenter
(``--mirror``, default ``$CORLA_OBSERVER_MIRROR``), lists the tracked files
under each election directory (``git ls-files``, so untracked files are
ignored), classifies each by content with `corla_observer.classify`, and
writes one JSON line per file to ``--out``::

    {"election": "2022/general", "path": "2022/general/round_1/Baca.csv",
     "size": 1234, "sha256": "...", "kind": "county_manifest", "county": "Baca",
     "name_county": "Baca", "evidence": {...}}

``name_county`` is the county the file's *name* suggests (see
`corla_observer.classify.county_from_filename`), so that files published
under another county's name stand out. Audio and video files are listed but
not classified.

Then it prints a per-election summary (``--summary text``, the default, or
``json``): the kinds found and how many of each, the header variants per
kind, every ``unknown`` file with its format, files whose content names a
different county from their file name, values in county columns that are not
county names, and groups of byte-identical files.

Usage::

    uv run python scripts/survey_mirror.py --out survey.jsonl [ELECTION_DIR ...]

With no election directories, every election directory known to the mirror
(`DEFAULT_ELECTIONS`) and the archived Audit Center pages (``auditCenters``)
are surveyed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from corla_observer.classify import COLORADO_COUNTIES, classify_bytes, county_from_filename

#: The mirror's election directories, oldest first. Years with several
#: elections have one directory per election under the year.
DEFAULT_ELECTIONS = (
    "2017",
    "2018",
    "2018G",
    "2019",
    "2020/presidential",
    "2020/statePrimary",
    "2020/general",
    "2021/coordinated",
    "2022/primary",
    "2022/general",
    "2023/coordinated",
    "2024/presidential",
    "2024/primary",
    "2024/general",
    "2025",
    "2025-irv",
    "2026/primary",
    "auditCenters",
)

#: Extensions listed but not classified.
SKIPPED_SUFFIXES = frozenset({".mp4", ".m4a", ".mov", ".mp3"})

def tracked_files(mirror: Path, directory: str) -> list[str]:
    """The files git tracks under ``directory`` in the mirror checkout."""
    result = subprocess.run(
        ["git", "-C", str(mirror), "ls-files", "-z", "--", directory],
        check=True,
        capture_output=True,
    )
    return sorted(p for p in result.stdout.decode("utf-8").split("\0") if p)


def election_of(path: str, elections: Sequence[str]) -> str:
    """The longest election directory containing ``path``."""
    best = ""
    for election in elections:
        if (path == election or path.startswith(election + "/")) and len(election) > len(best):
            best = election
    return best or path.split("/", 1)[0]


def classify_one(args: tuple[str, str, str]) -> dict[str, Any]:
    mirror, path, election = args
    data = (Path(mirror) / path).read_bytes()
    record: dict[str, Any] = {
        "election": election,
        "path": path,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "name_county": county_from_filename(path),
    }
    if Path(path).suffix.lower() in SKIPPED_SUFFIXES:
        record.update(kind="skipped", county=None, evidence={"format": Path(path).suffix.lower()[1:]})
        return record
    result = classify_bytes(data)
    record.update(kind=result.kind, county=result.county, evidence=result.evidence)
    return record


def survey(mirror: Path, elections: Sequence[str], jobs: int) -> list[dict[str, Any]]:
    tasks = []
    for election in elections:
        for path in tracked_files(mirror, election):
            if "/observerfiles/" in f"/{path}" or path.endswith(".md"):
                # Observers' own notes and files are not audit center data;
                # county-provided files are surveyed separately below.
                if not re.search(r"(?i)(manifest|cvr|auditreport)", path):
                    continue
            tasks.append((str(mirror), path, election_of(path, elections)))
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        return list(pool.map(classify_one, tasks, chunksize=4))


def header_signature(record: dict[str, Any]) -> str | None:
    columns = record.get("evidence", {}).get("matched_columns")
    if not isinstance(columns, dict):
        return None
    return " | ".join(f"{role}={text}" for role, text in sorted(columns.items()))


def summarize(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    by_election: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_election[record["election"]].append(record)
    summary: dict[str, Any] = {}
    for election, items in by_election.items():
        kinds = Counter(r["kind"] for r in items)
        variants: dict[str, Counter[str]] = defaultdict(Counter)
        for r in items:
            sig = header_signature(r)
            if sig:
                variants[r["kind"]][sig] += 1
        unknown = [
            {"path": r["path"], "format": r["evidence"].get("format"), "first_row": r["evidence"].get("first_row"),
             "sheets": r["evidence"].get("sheets")}
            for r in items
            if r["kind"] == "unknown"
        ]
        unknown_formats = Counter(u["format"] for u in unknown)
        mismatched = [
            {"path": r["path"], "name_county": r["name_county"], "content_county": r["county"]}
            for r in items
            if r["name_county"] and r["kind"] == "county_manifest" and r["county"] != r["name_county"]
        ]
        odd_values = {
            r["path"]: r["evidence"]["unrecognized_county_values"]
            for r in items
            if r.get("evidence", {}).get("unrecognized_county_values")
        }
        by_hash: dict[str, list[str]] = defaultdict(list)
        for r in items:
            if r["size"] > 0:
                by_hash[r["sha256"]].append(r["path"])
        duplicates = [paths for paths in by_hash.values() if len(paths) > 1]
        manifest_counties = sorted({r["county"] for r in items if r["kind"] == "county_manifest" and r["county"]})
        seeds = sorted(
            {
                str(r["evidence"].get("audit_random_seed") or r["evidence"].get("seed"))
                for r in items
                if r["kind"] in ("state_audit_report", "seed")
            }
        )
        paths_by_kind: dict[str, list[str]] = defaultdict(list)
        for r in items:
            paths_by_kind[r["kind"]].append(r["path"])
        summary[election] = {
            "files": len(items),
            "paths_by_kind": {k: v for k, v in sorted(paths_by_kind.items())},
            "kinds": dict(sorted(kinds.items())),
            "header_variants": {k: dict(v.most_common()) for k, v in sorted(variants.items())},
            "unknown_formats": dict(unknown_formats.most_common()),
            "unknown_tables": [u for u in unknown if u["format"] in ("csv", "xlsx")],
            "name_content_county_mismatch": mismatched,
            "unrecognized_county_values": odd_values,
            "duplicate_groups": duplicates,
            "manifest_counties": len(manifest_counties),
            "missing_manifest_counties": sorted(set(COLORADO_COUNTIES) - set(manifest_counties)),
            "seeds": seeds,
        }
    return summary


def print_text(summary: dict[str, Any]) -> None:
    for election, s in summary.items():
        print(f"=== {election}: {s['files']} files")
        print("  kinds: " + ", ".join(f"{k} {n}" for k, n in s["kinds"].items()))
        print(f"  manifests for {s['manifest_counties']} counties; seeds: {s['seeds']}")
        for kind, paths in s["paths_by_kind"].items():
            if kind not in ("unknown", "skipped"):
                more = f" (+{len(paths) - 3} more)" if len(paths) > 3 else ""
                print(f"  {kind}: {', '.join(paths[:3])}{more}")
        if 0 < s["manifest_counties"] < 64:
            print(f"  counties without a manifest: {s['missing_manifest_counties']}")
        for kind, variants in s["header_variants"].items():
            print(f"  {kind} headers:")
            for sig, n in variants.items():
                print(f"    {n:4} {sig}")
        if s["unknown_formats"]:
            print(f"  unknown by format: {s['unknown_formats']}")
        for u in s["unknown_tables"]:
            print(f"    unknown table {u['path']}: {u['first_row'] or u['sheets']}")
        for m in s["name_content_county_mismatch"]:
            print(f"  NAME/CONTENT county mismatch: {m}")
        for path, values in s["unrecognized_county_values"].items():
            print(f"  unrecognized county values in {path}: {values}")
        for group in s["duplicate_groups"]:
            print(f"  identical files: {group}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("elections", nargs="*", default=list(DEFAULT_ELECTIONS))
    parser.add_argument("--mirror", default=os.environ.get("CORLA_OBSERVER_MIRROR"))
    parser.add_argument("--out", required=True, help="JSONL file of per-file results to write")
    parser.add_argument("--summary", choices=("text", "json"), default="text")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument(
        "--from-jsonl", action="store_true", help="summarize an existing --out file instead of classifying"
    )
    args = parser.parse_args(argv)
    out = Path(args.out)
    if args.from_jsonl:
        records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line]
    else:
        if not args.mirror:
            parser.error("give --mirror or set CORLA_OBSERVER_MIRROR to a checkout of the mirror")
        records = survey(Path(args.mirror), args.elections, args.jobs)
        with open(out, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary = summarize(records)
    if args.summary == "json":
        json.dump(summary, sys.stdout, ensure_ascii=False, indent=1)
        print()
    else:
        print_text(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
