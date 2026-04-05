#!/bin/bash
# Capture the current Genesis recovery baseline without mutating the repo.

set -euo pipefail

GENESIS_REPO="${GENESIS_REPO:-$HOME/genesis}"
OUT_DIR="${1:-$HOME/tmp/genesis-recovery-$(date -u +%Y%m%dT%H%M%SZ)}"

mkdir -p "$OUT_DIR"

capture_repo() {
    local name="$1"
    local repo="$2"
    local target="$OUT_DIR/$name"

    mkdir -p "$target"
    git -C "$repo" rev-parse HEAD > "$target/head.txt"
    git -C "$repo" branch --show-current > "$target/branch.txt"
    git -C "$repo" status --short > "$target/status.txt"
    git -C "$repo" diff > "$target/unstaged.diff" || true
    git -C "$repo" diff --cached > "$target/staged.diff" || true
    git -C "$repo" ls-files --others --exclude-standard > "$target/untracked.txt"
}

capture_repo genesis "$GENESIS_REPO"

python3 --version > "$OUT_DIR/python_version.txt"
node --version > "$OUT_DIR/node_version.txt"
if command -v claude >/dev/null 2>&1; then
    claude --version > "$OUT_DIR/claude_version.txt" || true
fi

if [ -d "${VENV_PATH:-$GENESIS_REPO/.venv}" ]; then
    "${VENV_PATH:-$GENESIS_REPO/.venv}/bin/python" -m pip freeze > "$OUT_DIR/pip_freeze.txt" || true
fi

if [ -f "${SECRETS_PATH:-$GENESIS_REPO/secrets.env}" ]; then
    stat "${SECRETS_PATH:-$GENESIS_REPO/secrets.env}" > "$OUT_DIR/secrets_stat.txt" || true
fi

echo "Recovery state captured at $OUT_DIR"
