#!/bin/bash
# Load the 2020 demonstration counties (corla_observer.classify.DEMO_COUNTIES) into a workspace and set up their
# targeted pull lists, as a coordinator would on /coord/targets: written 2026-10-28 for the targeted ballots demo.
#
#   scripts/demo_targets_2020.sh WORKSPACE FILES BASE_URL ACTOR_ID [OBSERVER...]
#
# FILES holds Adams/, Broomfield/ and Delta/, each with the county's published 2020 general election CVR (*.csv)
# and its round 1 manifest from the audit center (manifest-COUNTY.csv). For each county it uploads both through the
# normal path (the redaction check runs) as coordinator:ACTOR_ID, draws 20 split-ticket ballots (Hickenlooper for
# Senate with the Republican in the county's tightest US House race) into the working list, sends it to the county as
# Targeted list 1 (docs/TARGETING.md) and prints an observer link and a reader link (board 1) for each OBSERVER
# (default: neal lori).
set -euo pipefail
ws=$1 files=$2 base=$3 actor=coordinator:$4
shift 4
[ $# -gt 0 ] && observers=("$@") || observers=(neal lori)
here=$(cd "$(dirname "$0")" && pwd)
co() { uv run --project "$here/.." corla-observer "$@"; }
declare -A house=(
  [Adams]="Representative to the 117th United States Congress-District 6=Steve House"
  [Broomfield]="Representative to the 117th United States Congress-District 2=Charlie Winn"
  [Delta]="Representative to the 117th United States Congress-District 3=Lauren Boebert"
)
for county in Delta Broomfield Adams; do
  demo="$county (2020 demo)"
  cvr=$(ls "$files/$county"/*.csv | grep -v "/manifest-")
  co upload --workspace "$ws" --actor "$actor" --county "$demo" --kind redacted_cvr "$cvr"
  co upload --workspace "$ws" --actor "$actor" --county "$demo" --kind manifest "$files/$county/manifest-$county.csv"
  co targets draw --workspace "$ws" --actor "$actor" --county "$demo" \
    --require "United States Senator=John W. Hickenlooper" --require "${house[$county]}" --seed 20201106
  co targets send --workspace "$ws" --actor "$actor" --county "$demo"
  for name in "${observers[@]}"; do
    co practice link --workspace "$ws" --actor "$actor" --county "$demo" --observer "$name" --base-url "$base"
    co practice link --workspace "$ws" --actor "$actor" --county "$demo" --reader "$name" --base-url "$base"
  done
done
