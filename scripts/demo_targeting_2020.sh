#!/usr/bin/env bash
# Make a workspace replaying the 2020 general election to its final state, for trying the targeting menu.
#
# Written 2026-11-11 for the targeting menu (docs/TARGETING.md, "The targeting menu"): a real county's
# menu needs CORLA's contest lists of every round, its contests by county, its CVR-to-audit-board
# comparisons and the seed, with the county's redacted CVR and manifest. This script makes one:
#
#   1. `election init --election 2020/general` (as scripts/demo_delta_2020.sh does);
#   2. `rehearse fetch` of the mirror's 2020 general at MIRROR_COMMIT with nothing left out: every
#      file the mirror holds, rounds 1 to 3 and the final reports, logged as a simulated fetch. The
#      mirror is only read, through `git ls-tree` and `git show`;
#   3. the seed, read from round 1's State Audit Report in the mirror (`seed read`) and entered
#      (`seed set`);
#   4. each county's redacted CVR and manifest uploaded from CVR_DIR/<County>/cvr.csv and
#      manifest.csv (public-records copies; they stay out of every repository). A CVR that fails the
#      redaction check (groups of fewer than 10 identical ballots) is rejected, as on a real server;
#      for a local demonstration only, REDACTION_THRESHOLD=1 accepts it (Mesa's 2020 copy has its
#      Redlands 360 districts in groups of 5 ballots). Never use that on a server others can see.
#
#   scripts/demo_targeting_2020.sh WORKSPACE MIRROR CVR_DIR COUNTY...
#
# WORKSPACE must not exist. Add a coordinator afterwards:
#   uv run corla-observer user add NAME --workspace WORKSPACE
# and serve it:
#   uv run corla-observer serve --workspace WORKSPACE --host 127.0.0.1 --port 8080
set -euo pipefail

usage() { echo "usage: $0 WORKSPACE MIRROR CVR_DIR COUNTY..." >&2; exit 2; }
[ $# -ge 4 ] || usage
WS=$1 MIRROR=$2 CVRS=$3
shift 3
MIRROR_COMMIT=382a035fe0ff90291382a6dfb33bf583032d1605
ACTOR=coordinator:demo

REPO=$(cd "$(dirname "$0")/.." && pwd)
UV=$(command -v uv || echo "$HOME/.local/bin/uv")
co() { (cd "$REPO" && "$UV" run --quiet corla-observer "$@"); }

[ ! -e "$WS" ] || { echo "error: $WS exists" >&2; exit 2; }
MIRROR=$(cd "$MIRROR" && pwd)
CVRS=$(cd "$CVRS" && pwd)
git -C "$MIRROR" rev-parse --verify --quiet "$MIRROR_COMMIT^{commit}" >/dev/null \
  || { echo "error: $MIRROR lacks the mirror's commit ${MIRROR_COMMIT:0:7}" >&2; exit 2; }
for county in "$@"; do
  [ -f "$CVRS/$county/cvr.csv" ] && [ -f "$CVRS/$county/manifest.csv" ] \
    || { echo "error: no $CVRS/$county/cvr.csv and manifest.csv" >&2; exit 2; }
done
mkdir -p "$WS"
WS=$(cd "$WS" && pwd)

co election init --workspace "$WS" --election 2020/general --mirror "$MIRROR"
co rehearse fetch --workspace "$WS" --mirror "$MIRROR" --commit "$MIRROR_COMMIT" --election 2020/general

report=$(mktemp --suffix=.xlsx)
trap 'rm -f "$report"' EXIT
git -C "$MIRROR" show "$MIRROR_COMMIT:2020/general/round_1/stateReport.xlsx" > "$report"
seed=$(co seed read "$report" | tr -d '[:space:]')
co seed set --workspace "$WS" --actor "$ACTOR" --source "State Audit Report, round 1 (the mirror's 2020/general/round_1/stateReport.xlsx)" "$seed"

for county in "$@"; do
  co upload --workspace "$WS" --actor "$ACTOR" --county "$county" --threshold "${REDACTION_THRESHOLD:-10}" \
    "$CVRS/$county/cvr.csv" "$CVRS/$county/manifest.csv"
done
echo "ready: $WS (seed $seed; counties: $*)"
