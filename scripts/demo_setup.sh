#!/usr/bin/env bash
# Build a local demo workspace for the walkthroughs in docs/walkthroughs/.
#
# Written 2026-10-26 for the coordinator and observer walkthrough scripts
# (docs/walkthroughs/coordinator-single-county.md and observer.md), which are
# meant to be recorded as training videos with the fictional Testland county
# (corla_observer.testcounty). It makes re-running them from nothing one
# command, on a laptop, never on a server holding a real election.
#
#   scripts/demo_setup.sh DIR            # ready for the coordinator walkthrough
#   scripts/demo_setup.sh DIR --through links   # also everything up to the links,
#                                               # for recording the observer walkthrough alone
#
# DIR must not exist yet. It gets:
#   DIR/ws        the workspace (election.toml, the event log, the store)
#   DIR/testland  Testland's files: manifest, redacted CVR, pull list
#   DIR/secrets   the server secret and receipt key (kept outside the workspace)
#   DIR/serve.sh  starts the web server for this workspace
#
# The workspace holds Testland only: no real election's files are loaded, and
# none are needed. (Its election.toml is the 2026 general's setup, as on the
# real server: the dashboard's calendar lines are the real election's, and
# Testland, a test county, stays off the public pages.) A test county is self-contained (docs/EXTRACT.md): its
# extract is built from its own uploads, its pull list and redacted CVR, or
# without the pull list from its own predicted selections (the seed, its own
# manifest, its targeted contest and a fixed sample count of 50).
#
# Environment: DEMO_PASSWORD (the demo coordinators' password, default
# "testland-demo-only"), DEMO_PORT (8765), DEMO_SEED (a fictional 20-digit
# seed).
set -euo pipefail

usage() { echo "usage: $0 DIR [--through setup|links]" >&2; exit 2; }
[ $# -ge 1 ] || usage
DIR=$1; shift
THROUGH=setup
if [ $# -gt 0 ]; then
  [ "$1" = "--through" ] && [ $# -eq 2 ] || usage
  THROUGH=$2
fi
case $THROUGH in setup|links) ;; *) usage ;; esac
PASSWORD=${DEMO_PASSWORD:-testland-demo-only}
PORT=${DEMO_PORT:-8765}
SEED=${DEMO_SEED:-41368579216342078921}

if [ -e "$DIR" ]; then echo "error: $DIR exists; choose a new directory" >&2; exit 2; fi
REPO=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$DIR"
DIR=$(cd "$DIR" && pwd)
WS=$DIR/ws
co() { (cd "$REPO" && uv run --quiet corla-observer "$@"); }

co election init --workspace "$WS" >/dev/null
for name in alice bob; do
  printf "%s\n" "$PASSWORD" | co user add "$name" --workspace "$WS" --password-stdin
done
co practice test-county --out "$DIR/testland" >/dev/null
mkdir -p "$DIR/secrets"
cat > "$DIR/serve.sh" <<EOF
#!/usr/bin/env bash
cd "$REPO" && exec uv run corla-observer serve --workspace "$WS" --host 127.0.0.1 --port $PORT --secret-file "$DIR/secrets/server-secret"
EOF
chmod +x "$DIR/serve.sh"

if [ "$THROUGH" = links ]; then
  A=coordinator:alice
  T=$DIR/testland
  co upload --workspace "$WS" --actor $A --county Testland "$T/Testland_BallotManifest_TEST.csv" "$T/Testland_CVR_Export_TEST.csv"
  co audit-boards --workspace "$WS" --county Testland --count 1 --actor $A
  co seed set --workspace "$WS" --actor $A --source "Testland demo roll" "$SEED"
  co extract build --workspace "$WS" --county Testland --actor $A >/dev/null   # the predicted order, before the pull list
  co upload --workspace "$WS" --actor $A --county Testland "$T/Testland_ballots_to_audit_TEST.csv"
  co extract build --workspace "$WS" --county Testland --actor $A
  echo
  echo "Links (each is shown only once; these are for http://127.0.0.1:$PORT):"
  for who in "observer ann (phone)" "observer ben (laptop)" "reader rita"; do
    read -r role name _ <<<"$who"
    url=$(co practice link --workspace "$WS" --county Testland --board 1 "--$role" "$name" --base-url "http://127.0.0.1:$PORT" 2>/dev/null)
    echo "  $who:"
    echo "    $url"
  done
fi

echo
echo "Workspace: $WS"
echo "Testland's files: $DIR/testland"
echo "Coordinators: alice and bob, password: $PASSWORD"
echo "Start the server:"
echo "  $DIR/serve.sh"
echo "then open http://127.0.0.1:$PORT/coord"
