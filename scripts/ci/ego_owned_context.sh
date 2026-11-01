#!/usr/bin/env bash
# Owned context calls only pinned data helpers; model text never becomes shell.
set -euo pipefail
# shellcheck disable=SC1091
source /headlong-b/thinkers/_lib/common.sh
if [[ "${1:-}" == --redact ]]; then
    prompt_redact "$(cat "$OWNED_RAW_PROMPT")"
    exit
fi
printf '<context>\nC1 identity and goals:\n'
cat "$IDENTITY_DIR/core_identity_prompt.md"
get_goals "$MEM_DIR"
printf '\nFixture skill:\n'
cat "$SKILLS_DIR/fixture/SKILL.md"
printf '\nCurrent attributed trigger:\n'
cat "$OWNED_TRIGGER_FILE"
printf '\nRecent source history (details pointers resolve original records):\n'
_recent_stream 20
query=$(jq -r '.content // ""' "$OWNED_TRIGGER_FILE" | cut -c1-600)
related=$(_related_memories "$query" "${OWNED_RELATED_PREV:-}" 3)
printf '\nRelated memory pointers:\n%s\n</context>\n' "$related"
printf '%s\n' "$related" | sed -n 's/^- \([^ ]*\) \[.*/\1/p' > "$OWNED_RELATED_FILE"
