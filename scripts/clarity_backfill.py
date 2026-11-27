#!/usr/bin/env python3
"""Recover every version Clarity published of an election's results, including ones nobody saved.

Written 2026-11-25 for the 2026 general (Clarity election 127407): the results relay saved only the
versions current when it happened to run, so updates published in between (and before it started)
were missed. Clarity keeps serving old versions at `/CO/<ID>/<version>/...`, but there is no list of
them; version numbers are shared with other elections, so most numbers in a range are 404.

What it does:

1. Probes each version number in a range (by default from `--start` to the current version) with
   one small request for `json/en/electionsettings.json`, at most `--rate` requests a second, and
   keeps every version that answers. Numbers already saved in `--dir` are skipped.
2. For each state version found, saves `state-VERSION-detailxml.zip` (the state's detailed results,
   by county) and `state-VERSION-electionsettings.json`.
3. From every saved state version's settings, collects each county's own election ID and version
   (`participatingcounties`), and saves each distinct county version's
   `County-VERSION-summary.json` (the county's own contests, totals only).

The file names match the results relay's (`scripts/relay_results.sh`), so
`scripts/results_changes.py --dir DIR` reads the result. Standard library only.

    python3 scripts/clarity_backfill.py --dir DIR --election-id 127407 --start 380000 [--rate 2]
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://results.enr.clarityelections.com/CO"
UA = "corla-observer/0.1.0 (independent public observation)"


class Fetcher:
    """GETs with a fixed user agent and at most `rate` requests a second."""

    def __init__(self, rate: float) -> None:
        self.gap = 1.0 / rate
        self.last = 0.0

    def get(self, url: str) -> bytes | None:
        wait = self.last + self.gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
                if r.status != 200 or not data:
                    return None
        except urllib.error.HTTPError:
            return None
        except (urllib.error.URLError, TimeoutError):
            time.sleep(10)
            return None
        return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


def county_versions(settings: bytes) -> list[tuple[str, str, str]]:
    """(county, county election ID, county version) for each participating county."""
    d = json.loads(settings)
    out = []
    for entry in d.get("settings", {}).get("electiondetails", {}).get("participatingcounties", []):
        parts = entry.split("|")
        if len(parts) >= 3:
            out.append((parts[0], parts[1], parts[2]))
    return out


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--election-id", default="127407")
    p.add_argument("--start", type=int, required=True, help="first version number to probe")
    p.add_argument("--end", type=int, help="last version number to probe (default: current)")
    p.add_argument("--rate", type=float, default=2.0, help="requests a second (default 2)")
    args = p.parse_args(argv)
    out, eid, f = args.dir, args.election_id, Fetcher(args.rate)
    out.mkdir(parents=True, exist_ok=True)

    end = args.end
    if end is None:
        current = f.get(f"{BASE}/{eid}/current_ver.txt")
        end = int(current.decode().strip())
    have = {int(x.name.split("-")[1]) for x in out.glob("state-*-detailxml.zip")}
    found = 0
    for v in range(args.start, end + 1):
        if v in have:
            if not (out / f"state-{v}-electionsettings.json").exists():
                settings = f.get(f"{BASE}/{eid}/{v}/json/en/electionsettings.json")
                if settings is not None:
                    (out / f"state-{v}-electionsettings.json").write_bytes(settings)
            continue
        settings = f.get(f"{BASE}/{eid}/{v}/json/en/electionsettings.json")
        if settings is None or not settings.lstrip().startswith(b"{"):
            continue
        detail = f.get(f"{BASE}/{eid}/{v}/reports/detailxml.zip")
        if detail is None:
            print(f"state {v}: settings but no detail file", flush=True)
            continue
        (out / f"state-{v}-electionsettings.json").write_bytes(settings)
        (out / f"state-{v}-detailxml.zip").write_bytes(detail)
        found += 1
        print(f"state {v}: saved", flush=True)

    seen: set[tuple[str, str]] = set()
    for s in sorted(out.glob("state-*-electionsettings.json")):
        for county, cid, cver in county_versions(s.read_bytes()):
            name = county.replace(" ", "_")
            target = out / f"{name}-{cver}-summary.json"
            if (name, cver) in seen or target.exists():
                continue
            seen.add((name, cver))
            data = f.get(f"{BASE}/{name}/{cid}/{cver}/json/en/summary.json")
            if data is not None:
                target.write_bytes(data)
                print(f"{county} {cver}: saved", flush=True)
    print(f"done: {found} new state versions in {args.start}..{end}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
