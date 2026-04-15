#!/bin/bash
# Fail fast on machine-specific runtime assumptions that should not survive public migration work.

set -euo pipefail

REPO_DIR="${1:-$(cd "$(dirname "$0")/.." && pwd)}"

patterns=(
  '/home/ubuntu'
  '10\.176\.34\.199'
  '192\.168\.50\.100'
)

targets=(
  "$REPO_DIR/src"
  "$REPO_DIR/config"
  "$REPO_DIR/scripts"
  "$REPO_DIR/env.example"
)

status=0
for pattern in "${patterns[@]}"; do
    if rg -n --hidden --glob '!**/.git/**' --glob '!**/.claude/**' --glob '!**/logs/**' --glob '!**/docs/**' --glob '!**/CLAUDE.md' --glob '!**/genesis-container-setup.md' --glob '!**/scripts/check_portability.sh' --glob '!**/scripts/prepare-public-release.sh' --glob '!**/scripts/cc_cli_output/**' --glob '!**/scripts/spike_*' "$pattern" "${targets[@]}" >"$HOME/tmp/genesis-portability-scan.txt"; then
        echo "Portability check failed for pattern: $pattern"
        cat "$HOME/tmp/genesis-portability-scan.txt"
        status=1
    fi
done

rm -f "$HOME/tmp/genesis-portability-scan.txt"
exit "$status"
