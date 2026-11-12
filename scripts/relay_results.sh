#!/usr/bin/env bash
# Relay Clarity's election night results to a corla-observer server that Clarity refuses.
#
# Clarity's front end answers some addresses (the corla-observer sprite's cloud address among them,
# since 6 November 2026) with an empty HTTP 202, its bot challenge, while it serves a home
# connection. Run this on a machine Clarity serves, by hand or from cron: it saves the current
# state detail and the named counties' own summaries (corla-observer results download), copies
# each file not yet relayed to the server, and records it there with corla-observer results add,
# which checks the totals and updates the estimates. See docs/RESULTS_RELAY.md.
#
# Usage: relay_results.sh HOST WORKSPACE ELECTION_ID COUNTY...
#   HOST         the server, as ssh knows it (corla-observer.sprite-app)
#   WORKSPACE    the server's workspace (/home/sprite/workspaces/2026-general)
#   ELECTION_ID  the state election's Clarity ID (127407 for the 2026 general)
#   COUNTY       each county whose own results to relay as well ("La Plata" Weld Morgan)
#
# Environment:
#   RELAY_ACTOR      who is responsible, as coordinator:ID (default coordinator: then $USER, else
#                    $LOGNAME, which cron sets, else id -un)
#   RELAY_STATE      local directory for the files and the record of what was relayed
#                    (default ~/.local/state/corla-observer-relay/HOST)
#   CORLA_OBSERVER   the local corla-observer command (default: this checkout's .venv/bin/corla-observer)
#   REMOTE_CHECKOUT  the server's checkout (default ~/corla-observer); it runs uv from there
#
# Files already relayed are listed in RELAY_STATE/relayed.tsv (scope, version, sha256, time), so a
# version is fetched and sent once. Exits non-zero if Clarity refuses or a step fails; nothing is
# marked relayed unless the server recorded it.
set -euo pipefail

host=${1:?usage: relay_results.sh HOST WORKSPACE ELECTION_ID COUNTY...}
workspace=${2:?usage: relay_results.sh HOST WORKSPACE ELECTION_ID COUNTY...}
election_id=${3:?usage: relay_results.sh HOST WORKSPACE ELECTION_ID COUNTY...}
shift 3

here=$(cd "$(dirname "$0")/.." && pwd)
cli=${CORLA_OBSERVER:-$here/.venv/bin/corla-observer}
actor=${RELAY_ACTOR:-coordinator:${USER:-${LOGNAME:-$(id -un)}}}
state=${RELAY_STATE:-$HOME/.local/state/corla-observer-relay/$host}
remote_checkout=${REMOTE_CHECKOUT:-'~/corla-observer'}
mkdir -p "$state/files"
touch "$state/relayed.tsv"

# The latest version relayed per scope, as --have options.
have=()
while IFS=$'\t' read -r scope version _; do
    [ -n "$scope" ] && have+=(--have "$scope=$version")
done < <(sort -t$'\t' -k1,1 -k2,2n "$state/relayed.tsv" | awk -F'\t' '{last[$1]=$0} END {for (s in last) print last[s]}')

counties=()
for c in "$@"; do counties+=(--county "$c"); done

saved=$("$cli" results download --to "$state/files" --election-id "$election_id" "${counties[@]}" "${have[@]}")
[ -n "$saved" ] || { echo "nothing new"; exit 0; }

ssh -o BatchMode=yes "$host" mkdir -p results-relay
while IFS=$'\t' read -r path scope version url published; do
    name=$(basename "$path")
    scp -q -o BatchMode=yes "$path" "$host:results-relay/$name" < /dev/null
    note="relayed from $(hostname) by scripts/relay_results.sh: Clarity refuses the server's address"
    # printf %q quotes each argument for the remote shell; the file's ~ is left for it to expand.
    args=(--workspace "$workspace" --actor "$actor" --scope "$scope" --source-url "$url" --note "$note")
    # A county's summary does not say when it was generated: give the time Clarity's settings list for it.
    [ "${published:--}" != "-" ] && args+=(--file-time "$published")
    remote=$(printf '%q ' "${args[@]}")
    ssh -n -o BatchMode=yes "$host" "cd $remote_checkout && ~/.local/bin/uv run corla-observer results add ~/results-relay/$(printf %q "$name") $remote"
    printf '%s\t%s\t%s\t%s\n' "$scope" "$version" "$(sha256sum "$path" | cut -d' ' -f1)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$state/relayed.tsv"
done <<< "$saved"
