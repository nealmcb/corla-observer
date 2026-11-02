#!/usr/bin/env python3
"""Build the 2020 general election test data bundle for testing-mode servers.

Written 2026-11-02 for the "Populate test data" button (`corla_observer.quicksetup`), which uploads a
county's manifest and redacted CVR from this bundle under the county's demonstration name ("Mesa
(2020 demo)", `corla_observer.classify.DEMO_COUNTIES`). The bundle is copied to each testing server
and named there with ``serve --test-data DIR``; it is never committed (the CVRs are large).

For each Colorado county, from public sources only:

- **manifest.csv**: the Audit Center's posted manifest, from a clone of the public mirror,
  ``MIRROR/2020/general/round_1/manifest-<County without spaces>.csv``;
- **cvr.csv**: the county's CVR as compiled at votedatabase.com from Colorado Open Records Act
  requests, ``VOTEDATABASE/co/2020/<County>/``: the folder's largest CSV. For Delta, this repository's
  ``tests/fixtures/delta-2020/`` copy is used instead, the file CORLA imported byte for byte.

Counties whose CVR is larger than ``--max-mb`` (default 50) are left out, as are counties with no
manifest or no CVR; each is listed in ``bundle.json``, which also records every file's source path,
size and SHA-256. Some CVRs need redaction: the server's redaction check rejects them, as it should.

Usage:
    uv run python scripts/make_test_bundle.py --mirror MIRROR --votedatabase VOTEDATABASE --out DIR [--max-mb 50]

Then copy DIR to the server (e.g. as a tar file) and start it with ``--test-data DIR``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from corla_observer.classify import COLORADO_COUNTIES  # noqa: E402

DELTA_FIXTURE = REPO / "tests" / "fixtures" / "delta-2020"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def cvr_for(county: str, votedatabase: Path) -> Path | None:
    if county == "Delta":
        return DELTA_FIXTURE / "Modified CVR_Export_20201113093409.csv"
    folder = votedatabase / "co" / "2020" / county
    csvs = sorted((p for p in folder.glob("*") if p.is_file() and p.suffix.lower() == ".csv"), key=lambda p: (-p.stat().st_size, p.name))
    return csvs[0] if csvs else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mirror", type=Path, required=True, help="a clone of the public Audit Center mirror")
    parser.add_argument("--votedatabase", type=Path, required=True, help="the votedatabase.com CVR collection")
    parser.add_argument("--out", type=Path, required=True, help="the bundle's directory (must not exist)")
    parser.add_argument("--max-mb", type=float, default=50.0, help="leave out CVRs larger than this (MB)")
    args = parser.parse_args()
    if args.out.exists():
        parser.error(f"{args.out} exists")
    args.out.mkdir(parents=True)
    included, left_out = {}, {}
    for county in COLORADO_COUNTIES:
        manifest = args.mirror / "2020" / "general" / "round_1" / f"manifest-{county.replace(' ', '')}.csv"
        cvr = cvr_for(county, args.votedatabase)
        if not manifest.is_file():
            left_out[county] = "no posted manifest"
            continue
        if cvr is None:
            left_out[county] = "no CVR"
            continue
        size = cvr.stat().st_size
        if size > args.max_mb * 1e6:
            left_out[county] = f"CVR of {size / 1e6:.0f} MB, over {args.max_mb:g} MB"
            continue
        target = args.out / county
        target.mkdir()
        shutil.copyfile(manifest, target / "manifest.csv")
        shutil.copyfile(cvr, target / "cvr.csv")
        included[county] = {
            name: {"source": str(src), "bytes": src.stat().st_size, "sha256": sha256(target / name)}
            for name, src in (("manifest.csv", manifest), ("cvr.csv", cvr))
        }
    (args.out / "bundle.json").write_text(
        json.dumps({"election": "2020 general", "counties": included, "left_out": left_out}, indent=1, sort_keys=True) + "\n"
    )
    total = sum(f["bytes"] for c in included.values() for f in c.values())
    print(f"{len(included)} counties, {total / 1e6:.0f} MB, in {args.out}; left out {len(left_out)}: "
          + ", ".join(f"{c} ({why})" for c, why in left_out.items()))  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
