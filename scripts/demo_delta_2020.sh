#!/usr/bin/env bash
# Make, or reset, the starting state of the Delta County demonstration (docs/walkthroughs/demo-delta-2020-general.md).
#
# Written 2026-10-28 for a hands-on demo in which a coordinator does every step on the web pages
# himself, with real public data: Colorado's 2020 general election, a stand-in for the 2026 general
# (its election day was 3 November too). The starting state is an empty 2020 general workspace:
#
#   - election.toml for 2020/general (`election init --election 2020/general`): Delta is the county
#     followed; the live Audit Center is never crawled; instead each "Fetch now" brings in the next
#     state of the 2020 general's audit center from the public mirror (MIRROR, a clone of
#     https://github.com/nealmcb/auditcenter), the first being the files posted before the roll;
#     no election night results are fetched;
#   - nothing else: no audit center files, no uploads, no audit board count, no seed, no extract,
#     no targeted ballots, no observer links.
#
#   scripts/demo_delta_2020.sh WORKSPACE MIRROR           # a new workspace (WORKSPACE must not exist)
#   scripts/demo_delta_2020.sh WORKSPACE MIRROR --reset   # back to the starting state
#
# --reset works only on a workspace this script made (it holds demo-delta-2020.json): it deletes
# everything in it but the coordinator accounts (accounts/coordinators.json; observer and reader links
# go too), then sets it up again. Stop the web server first, and start it again after (it keeps the
# old event log open).
#
# Add a coordinator once, after the first run:
#   uv run corla-observer user add NAME --workspace WORKSPACE
set -euo pipefail

usage() { echo "usage: $0 WORKSPACE MIRROR [--reset]" >&2; exit 2; }
[ $# -ge 2 ] && [ $# -le 3 ] || usage
WS=$1 MIRROR=$2 RESET=${3:-}
[ -z "$RESET" ] || [ "$RESET" = --reset ] || usage
MARKER=demo-delta-2020.json

REPO=$(cd "$(dirname "$0")/.." && pwd)
UV=$(command -v uv || echo "$HOME/.local/bin/uv")
co() { (cd "$REPO" && "$UV" run --quiet corla-observer "$@"); }

if [ -n "$RESET" ]; then
  [ -f "$WS/$MARKER" ] || { echo "error: $WS was not made by this script (no $MARKER): refusing to touch it" >&2; exit 2; }
  keep=$(mktemp -d)
  [ -f "$WS/accounts/coordinators.json" ] && cp -p "$WS/accounts/coordinators.json" "$keep/"
  find "$WS" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  if [ -f "$keep/coordinators.json" ]; then
    mkdir -m 700 "$WS/accounts"
    cp -p "$keep/coordinators.json" "$WS/accounts/"
  fi
  rm -rf "$keep"
elif [ -e "$WS" ]; then
  echo "error: $WS exists; to start it again, add --reset" >&2; exit 2
fi
mkdir -p "$WS"
WS=$(cd "$WS" && pwd)
MIRROR=$(cd "$MIRROR" && pwd)
git -C "$MIRROR" rev-parse --verify --quiet 382a035fe0ff90291382a6dfb33bf583032d1605^{commit} >/dev/null \
  || { echo "error: $MIRROR lacks the mirror's commit 382a035 (git -C $MIRROR fetch)" >&2; exit 2; }
printf '{"demo": "Delta County, 2020 general election"}\n' > "$WS/$MARKER"
co election init --workspace "$WS" --election 2020/general --mirror "$MIRROR"
echo "starting state ready in $WS"
