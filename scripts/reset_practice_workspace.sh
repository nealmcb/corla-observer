#!/usr/bin/env bash
# Put a practice workspace back in its starting state, for a testing-mode deployment's Reset button.
#
# Written 2026-11-03 to replace a hand-written script on the corla-observer sprite, so that every reset
# is recorded: `corla-observer workspace reset` archives the workspace to BACKUPS/NAME-TIME.tgz, empties
# it but for the coordinator accounts (observer and reader links go), and starts the new event log with
# workspace.reset, naming the old log's head and the archive's hash (corla_observer.reset).
#
# Usage (on the sprite, from the service's reset command):
#   scripts/reset_practice_workspace.sh SERVICE WORKSPACE [BACKUPS]
#     SERVICE    the web service to stop and start again (corla-observer); never the scheduler
#     WORKSPACE  the practice workspace (~/practice-ws)
#     BACKUPS    where the archive goes (default ~/backups)
set -euo pipefail
[ $# -ge 2 ] && [ $# -le 3 ] || { echo "usage: $0 SERVICE WORKSPACE [BACKUPS]" >&2; exit 2; }
SERVICE=$1 WS=$2 BACKUPS=${3:-$HOME/backups}
S=/.sprite/bin/sprite-env
REPO=$(cd "$(dirname "$0")/.." && pwd)
UV=$(command -v uv || echo "$HOME/.local/bin/uv")
"$S" services stop "$SERVICE" >/dev/null 2>&1 || true
(cd "$REPO" && "$UV" run --quiet corla-observer workspace reset --workspace "$WS" --backups "$BACKUPS" --confirm)
"$S" services start "$SERVICE" >/dev/null
echo "$(basename "$WS") reset: empty, coordinator accounts kept, the reset recorded in its new log; $SERVICE restarted"
