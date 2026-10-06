#!/usr/bin/env bash
# Install or upgrade Genesis's browser stack, starting with the `browser` extra
# (camoufox, playwright, patchright); the steps are listed in
# src/genesis/browser/provision.py.
#
# bootstrap.sh and install.sh run this on every install, and update.sh after a
# recorded update. It is idempotent and safe to re-run by hand. It never fails
# its caller: every problem is printed, the last line is the outcome, and the
# `browser_automation` capability (capabilities.json) reports the end state.
#
# The transaction lives in src/genesis/browser/provision.py and runs in ONE
# process. The worktree-guarded editable install stays in scripts/lib/venv_setup.sh
# (its single home); provision.py calls it from there.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GENESIS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="${GENESIS_VENV:-$GENESIS_ROOT/.venv}"
PY="$VENV_DIR/bin/python"

if [ ! -x "$PY" ]; then
    echo "  browser stack: SKIPPED (no venv at $VENV_DIR; run bootstrap first)"
    exit 0
fi

"$PY" -m genesis.browser.provision run --root "$GENESIS_ROOT" --lib "$SCRIPT_DIR/lib/venv_setup.sh" \
    || echo "  browser stack: FAILED (provision exited $?; see above)"
exit 0
