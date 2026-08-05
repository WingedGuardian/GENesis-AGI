#!/usr/bin/env bash
# Genesis — promote the local models.md overlay onto the tracked reference.
#
# The weekly models_md_synthesis job refreshes a LOCAL overlay
# (~/.genesis/output/models.md) instead of committing to tracked source, so a
# clone's autonomous run never diverges docs/reference/models.md (which used to
# make every `git pull` conflict). The tracked doc is a maintainer-curated
# reference. Run this to copy a reviewed overlay back onto it, then review the
# diff and open a PR for a deliberate public refresh.
#
# Usage:
#   ./scripts/promote_models_md.sh
#
# Env:
#   GENESIS_OUTPUT_DIR   overlay location (default ~/.genesis/output)
#
# Idempotent: a plain copy. Writes nothing if the overlay is absent.
set -euo pipefail

# Resolve HOME robustly: a stripped-env interactive shell can leave HOME unset,
# which under `set -u` would abort at the $HOME default below. Fall back to the
# passwd entry, then a conventional path. (Only referenced when GENESIS_OUTPUT_DIR
# is also unset, but the default is still evaluated, so guard it.)
if [ -z "${HOME:-}" ]; then
    # Resolve from passwd (field 6), matching Python's expanduser. Fail closed
    # rather than guessing /home/<user>, so we never resolve the overlay path
    # to a location that disagrees with the rest of the system.
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
    if [ -z "$HOME" ]; then
        echo "ERROR: HOME is unset and could not be resolved from passwd." >&2
        echo "Re-run with HOME exported (or set GENESIS_OUTPUT_DIR)." >&2
        exit 1
    fi
    export HOME
fi

REPO_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
OVERLAY="${GENESIS_OUTPUT_DIR:-$HOME/.genesis/output}/models.md"
TRACKED="$REPO_ROOT/docs/reference/models.md"

if [[ ! -f "$OVERLAY" ]]; then
    echo "No overlay at $OVERLAY — the weekly synthesis job hasn't produced one yet." >&2
    echo "(It seeds itself from the tracked doc on first run, then updates in place.)" >&2
    exit 1
fi

# Compare before copying (git-independent, so the helper also works from a
# tarball checkout). cmp -s is non-zero when TRACKED is absent → we still copy.
if cmp -s "$OVERLAY" "$TRACKED"; then
    echo "Overlay already identical to docs/reference/models.md — nothing to promote."
    exit 0
fi

cp "$OVERLAY" "$TRACKED"

echo "Copied overlay -> docs/reference/models.md."
echo "Review with:  git -C \"$REPO_ROOT\" diff -- docs/reference/models.md"
echo "Then commit on a branch and open a PR for the public refresh."
