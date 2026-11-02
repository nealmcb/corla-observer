#!/usr/bin/env bash
# Update a sprite's corla-observer to a commit of the public repository, keeping its data.
#
# Run on the sprite (for example: sprite exec -s NAME -- bash -s REF WS... < scripts/update_sprite.sh).
# First, from your own machine: archive the sprite's logs (if you keep such an archive) and take a
# sprite checkpoint:
#   sprite checkpoint create -s NAME --comment "before updating corla-observer to REF"
#
# Usage: update_sprite.sh REF WORKSPACE...
#   REF        what to run: a commit, or origin/main for the latest
#   WORKSPACE  each workspace the services use, archived before anything changes
#
# What it does, stopping at the first failure:
#   1. refuses if the checkout has uncommitted changes to tracked files;
#   2. stops every service whose name starts with corla-observer, so no workspace is written meanwhile;
#   3. archives each workspace to ~/backups/NAME-TIME-OLDCOMMIT.tgz;
#   4. points origin at the public repository, fetches it and checks out REF as the branch main;
#   5. installs the locked dependencies (uv sync --frozen) and prints the new version;
#   6. starts the services again, and appends a line to ~/notes/updates.log.
# If a step fails after the services are stopped, they stay stopped: restore with the checkpoint, or
# fix and start them with /.sprite/bin/sprite-env services start NAME.
set -euo pipefail

REPO=https://github.com/nealmcb/corla-observer.git
SPRITE_ENV=/.sprite/bin/sprite-env
CHECKOUT=$HOME/corla-observer

ref=${1:?usage: update_sprite.sh REF WORKSPACE...}
shift
[ "$#" -ge 1 ] || { echo "name at least one workspace to archive" >&2; exit 2; }
for ws in "$@"; do [ -d "$ws" ] || { echo "no workspace $ws" >&2; exit 2; }; done

cd "$CHECKOUT"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "the checkout has uncommitted changes; nothing done:" >&2
    git status --short --untracked-files=no >&2
    exit 1
fi
old=$(git rev-parse --short=12 HEAD)
stamp=$(date -u +%Y%m%dT%H%M%SZ)

# The service list is JSON; each service's own entry starts with its "name". (No python here: the
# sprite's python3 is a pyenv shim that fails inside `bash -s`.)
services=$("$SPRITE_ENV" services list | grep -o '{"name":"corla-observer[^"]*"' | cut -d'"' -f4 | sort -u | tr '\n' ' ')
echo "services: ${services:-none}"
for s in $services; do "$SPRITE_ENV" services stop "$s" >/dev/null && echo "stopped $s"; done

mkdir -p "$HOME/backups" "$HOME/notes"
for ws in "$@"; do
    archive="$HOME/backups/$(basename "$ws")-$stamp-$old.tgz"
    tar -C "$(dirname "$ws")" -czf "$archive" "$(basename "$ws")"
    echo "archived $ws to $archive ($(du -h "$archive" | cut -f1))"
done

git remote set-url origin "$REPO"
git fetch --quiet origin
git checkout --quiet -B main "$ref"
new=$(git rev-parse --short=12 HEAD)
"$HOME/.local/bin/uv" sync --frozen --quiet
version=$("$HOME/.local/bin/uv" run corla-observer --version)
echo "now: $version"

for s in $services; do "$SPRITE_ENV" services start "$s" >/dev/null && echo "started $s"; done
echo "$stamp updated $old -> $new ($version); archived: $*" >> "$HOME/notes/updates.log"
