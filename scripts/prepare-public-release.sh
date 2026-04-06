#!/bin/bash
# Genesis v3 — Prepare public release
# Copies the repo to a staging directory, strips user-specific content,
# templates config files, and excludes private data.
#
# Usage:
#   ./scripts/prepare-public-release.sh [output_dir]
#   Default output: ~/tmp/genesis-public-release/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT_DIR="${1:-$HOME/tmp/genesis-public-release}"

echo ""
echo "  Genesis v3 — Prepare Public Release"
echo "  ─────────────────────────────────────────"
echo ""
echo "  Source: $REPO_DIR"
echo "  Output: $OUTPUT_DIR"
echo ""

# ── 1. Clean copy (no .git, no untracked) ────────────────
echo "  [1/9] Creating clean copy from git archive..."

rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"

# Use git archive to get only tracked files (no .git dir, no untracked files)
git -C "$REPO_DIR" archive HEAD | tar -x -C "$OUTPUT_DIR"

echo "    + Clean copy created"

# ── 2. Exclude user-specific directories/files ───────────
echo "  [2/9] Removing user-specific content..."

# Product track plans (user's business ideas)
rm -f "$OUTPUT_DIR"/docs/plans/2026-03-05-track*.md
rm -f "$OUTPUT_DIR"/docs/plans/2026-03-05-multi-track-*.md

# Research profiles (user's research interests)
rm -rf "$OUTPUT_DIR"/config/research-profiles/

# Module configs — remove user-specific configs (private IPs), keep native + template
rm -f "$OUTPUT_DIR"/config/modules/career-agent.yaml
rm -rf "$OUTPUT_DIR"/config/external-modules/

# Spike/experiment scripts
rm -f "$OUTPUT_DIR"/scripts/spike_*.py
rm -rf "$OUTPUT_DIR"/scripts/cc_cli_output

# Career agent plans (user-specific)
rm -f "$OUTPUT_DIR"/docs/plans/2026-03-30-career-agent-improvements.md

# Codebase audit reports (contain user-specific findings)
rm -f "$OUTPUT_DIR"/docs/reference/2026-03-19-genesis-codebase-audit.md
rm -f "$OUTPUT_DIR"/docs/reference/2026-03-20-article-eval-action-items.md
rm -f "$OUTPUT_DIR"/docs/reference/CODEBASE_AUDIT_REPORT.md
rm -f "$OUTPUT_DIR"/docs/reference/codebase-audit-report.md
rm -f "$OUTPUT_DIR"/docs/reference/2026-03-24-split-large-files-audit.md

# Infrastructure-specific docs (contain user's network topology)
rm -f "$OUTPUT_DIR"/docs/reference/networking-summary.txt
rm -f "$OUTPUT_DIR"/docs/reference/review-summary.md
rm -f "$OUTPUT_DIR"/docs/reference/project-outline.txt

# GTM strategy (internal marketing playbook — never public)
rm -rf "$OUTPUT_DIR"/docs/gtm/

# V1/nanobot project history — contains internal IPs, hardware refs, old hostnames
rm -rf "$OUTPUT_DIR"/docs/history/

# Internal superpowers design specs (private product planning)
rm -rf "$OUTPUT_DIR"/docs/superpowers/

# Dated internal audit and sprint docs
# (2026-03-22-genesis-web-ui-audit.md is untracked; rm -f is a no-op but documents intent)
rm -f "$OUTPUT_DIR"/docs/plans/2026-03-22-genesis-web-ui-audit.md
rm -f "$OUTPUT_DIR"/docs/reference/2026-03-20-genesis-codebase-audit.md

# Database files
rm -f "$OUTPUT_DIR"/genesis.db
rm -f "$OUTPUT_DIR"/data/genesis.db

echo "    + User-specific files removed"

# ── 3. Template voice exemplars ──────────────────────────
echo "  [3/9] Templating voice exemplars..."

EXEMPLAR_DIR="$OUTPUT_DIR/src/genesis/skills/voice-master/references/exemplars"

for file in social.md professional.md longform.md; do
    if [ -f "$EXEMPLAR_DIR/$file" ]; then
        category="${file%.md}"
        cat > "$EXEMPLAR_DIR/$file" << TEMPLATE
# ${category^} Exemplars

Each exemplar below was extracted from the user's real writing and curated
during a calibration session. Use these as stylistic reference — match
sentence structure, vocabulary level, and directness. Do NOT copy content.

## Exemplar Format

\`\`\`
### Exemplar [N]: [brief label]
- **Source:** transcript session [date] / inbox / manual / calibration
- **Tone:** direct / reflective / persuasive / analytical / casual
- **Formality:** 1-5 (1=casual, 5=formal)
- **Topic domain:** [your domain]
- **Why it's distinctive:** [1 sentence on what makes this "you"]

> [The actual passage, 50-200 words]
\`\`\`

---

*No exemplars yet. Run \`/voice calibrate\` to populate.*
TEMPLATE
        echo "    + $file templated"
    fi
done

# Template index
cat > "$EXEMPLAR_DIR/index.md" << 'INDEX'
# Voice Exemplar Index

This index lists all curated voice exemplars with metadata for selection.
When generating content, scan this table to find exemplars whose tone,
formality, and domain best match the current request, then read the
appropriate file to get the full exemplar text.

## Selection Instructions

1. Read the request: what medium, tone, formality level, and topic domain?
2. Scan the table below for 3-5 best matches
3. Read the file listed in the "File" column to get the full exemplar text
4. Use matched exemplars as stylistic reference during generation

## Exemplar Registry

| # | Label | File | Tone | Formality (1-5) | Domain | Why Distinctive |
|---|-------|------|------|------------------|--------|-----------------|

*No exemplars registered yet. Run `/voice calibrate` or `/voice curate` to populate.*
INDEX
echo "    + index.md templated"

# Template voice-dimensions.md to be generic
cat > "$OUTPUT_DIR/src/genesis/skills/voice-master/references/voice-dimensions.md" << 'VOICEDIM'
# Voice Dimensions

Supplementary voice rules for edge cases the exemplars don't cover.

**When exemplars conflict with these rules, the exemplars win.** Exemplars are
the primary source of truth — they show what the user actually sounds like.
These dimensions are fallback guidance.

---

## Tone

<!-- Describe your natural tone. Examples: direct, conversational,
     technically grounded, formal, casual, etc. -->

## Sentence Structure

<!-- How do you naturally structure sentences? Short and punchy? Long and
     flowing? Mix of both? Do you use fragments for emphasis? -->

## Vocabulary

<!-- What's your vocabulary register? Industry jargon or plain language?
     Formal or casual? Any words/phrases you tend to use? -->

## Perspective

<!-- How do you frame ideas? First-person experience? Third-person analysis?
     Do you take strong positions or acknowledge nuance? -->

## Humor

<!-- What's your humor style? Dry? Self-deprecating? None? -->
VOICEDIM
echo "    + voice-dimensions.md templated"

# ── 4. Template LinkedIn skill content ───────────────────
echo "  [4/9] Templating skill-specific content..."

# Replace user expertise/audience in linkedin-post-writer
if [ -f "$OUTPUT_DIR/src/genesis/skills/linkedin-post-writer/SKILL.md" ]; then
    sed -i '/^## Topic Areas/,/^## /{
        /^## Topic Areas/!{/^## /!d}
    }' "$OUTPUT_DIR/src/genesis/skills/linkedin-post-writer/SKILL.md"

    sed -i '/^## Topic Areas/a\
\
<!-- Configure your expertise areas here. Examples:\
- Cloud engineering and infrastructure\
- DevOps / platform engineering\
- AI/ML infrastructure\
- Your specific domain expertise\
-->' "$OUTPUT_DIR/src/genesis/skills/linkedin-post-writer/SKILL.md"

    sed -i '/^## Audience/,/^## /{
        /^## Audience/!{/^## /!d}
    }' "$OUTPUT_DIR/src/genesis/skills/linkedin-post-writer/SKILL.md"

    sed -i '/^## Audience/a\
\
<!-- Configure your target audience here. Examples:\
- Technical professionals in your field\
- Engineering managers and directors\
- Recruiters and hiring managers\
-->' "$OUTPUT_DIR/src/genesis/skills/linkedin-post-writer/SKILL.md"

    echo "    + linkedin-post-writer templated"
fi

# ── 5. Template config files ─────────────────────────────
echo "  [5/9] Templating config files..."

# model_routing.yaml — replace hardcoded IPs
if [ -f "$OUTPUT_DIR/config/model_routing.yaml" ]; then
    sed -i 's|http://10\.176\.34\.199:11434|${OLLAMA_URL:-http://localhost:11434}|g' \
        "$OUTPUT_DIR/config/model_routing.yaml"
    sed -i 's|http://192\.168\.50\.100:1234/v1|${LM_STUDIO_URL:-http://localhost:1234/v1}|g' \
        "$OUTPUT_DIR/config/model_routing.yaml"
    echo "    + model_routing.yaml templated"
fi

# inbox_monitor.yaml — replace hardcoded path
if [ -f "$OUTPUT_DIR/config/inbox_monitor.yaml" ]; then
    sed -i 's|${HOME}/inbox|${GENESIS_INBOX_PATH:-~/inbox}|g' \
        "$OUTPUT_DIR/config/inbox_monitor.yaml"
    echo "    + inbox_monitor.yaml templated"
fi

# CLAUDE.md — strip user-specific content for public release
if [ -f "$OUTPUT_DIR/CLAUDE.md" ]; then
    # Single-line deletions
    sed -i '/Ground-up rebuild, NOT a continuation of nanobot/d' "$OUTPUT_DIR/CLAUDE.md"
    sed -i '/\*\*Container\*\*: Ubuntu/d' "$OUTPUT_DIR/CLAUDE.md"
    sed -i '/\*\*Ollama\*\*:/d' "$OUTPUT_DIR/CLAUDE.md"

    # Multi-line block removals and simplifications
    python3 - "$OUTPUT_DIR/CLAUDE.md" << 'PYEOF'
import re, sys

path = sys.argv[1]
with open(path) as f:
    content = f.read()

warnings = []

def checked_sub(pattern, repl, text, label, **kwargs):
    result, count = re.subn(pattern, repl, text, **kwargs)
    if count == 0:
        warnings.append(label)
    return result

# Remove Build Order section — stops at next ## heading or end of file.
content = checked_sub(r'\n## Build Order\n[\s\S]*?(?=\n## |\Z)', '\n', content, "Build Order removal")

if warnings:
    print(f"WARNING: {len(warnings)} regex(es) matched 0 times — CLAUDE.md structure may have changed:", file=sys.stderr)
    for w in warnings:
        print(f"  - {w}", file=sys.stderr)

with open(path, 'w') as f:
    f.write(content)
PYEOF
    if [ $? -ne 0 ]; then
        echo "    FATAL: CLAUDE.md Python transform failed — aborting release"; exit 1
    fi
    echo "    + CLAUDE.md templated"
fi

# .claude/docs/ — strip user-specific content from extracted CC docs
if [ -f "$OUTPUT_DIR/.claude/docs/dual-repo.md" ]; then
    python3 - "$OUTPUT_DIR/.claude/docs/dual-repo.md" << 'PYEOF'
import re, sys

path = sys.argv[1]
with open(path) as f:
    content = f.read()

# "(private, will go public)" → "(public)"
content = content.replace('(private, will go public)', '(public)')

# Strip "Generated from..." clause from the repo bullet
content = re.sub(
    r' Generated from\s+the working repo via `scripts/prepare-public-release\.sh`\.',
    '',
    content
)

# Remove "Update Workflow" section (heading through code block and trailing text)
content = re.sub(r'\n## Update Workflow\n[\s\S]*?(?=\n## |\Z)', '\n', content)

# Remove "What the Release Script Strips" section
content = re.sub(r'\n## What the Release Script Strips\n[\s\S]*?(?=\n## |\Z)', '\n', content)

# Shorten Secret Scanning to first sentence
content = re.sub(
    r'(`detect-secrets` \(Yelp\) is installed in the venv\.) Used[\s\S]*?the public repo\.',
    r'\1',
    content
)

with open(path, 'w') as f:
    f.write(content)
PYEOF
    echo "    + .claude/docs/dual-repo.md templated"
fi

# USER.md — replace with onboarding template for public release
cat > "$OUTPUT_DIR/src/genesis/identity/USER.md" << 'USERMD'
<!-- Genesis: For richer user context (interests, projects, expertise, patterns),
     read USER_KNOWLEDGE.md or query memory_recall. Do not modify this file
     autonomously — it is user-edited only. -->

# User Profile

<!--
This is YOUR file — Genesis will never modify it autonomously.
Edit it to tell Genesis who you are. The more context you provide,
the better Genesis can serve you.

Genesis also builds a deeper understanding of you over time in
USER_KNOWLEDGE.md (auto-synthesized from interactions). That file
is system-managed; this one is yours.
-->

- **Name**: Your name
- **Timezone**: Your timezone
- **Background**: What you do, your expertise areas
- **Communication**: How you prefer Genesis to communicate
- **Autonomy**: What Genesis can do without asking vs. what needs approval
- **Priorities**: What matters most to you in this project
USERMD
echo "    + USER.md templated"

# USER_KNOWLEDGE.md — empty template for public release
cat > "$OUTPUT_DIR/src/genesis/identity/USER_KNOWLEDGE.md" << 'UKMD'
# User Knowledge Base

> Auto-synthesized from Genesis memory system. Last updated: not yet run.
> Source of truth: memory system (Qdrant + SQLite). This file is a materialized cache.
> Do not hand-edit — changes will be overwritten by next synthesis cycle.

## Interests & Active Curiosity

_(max 15 items — oldest/lowest-signal pruned when full)_

## Active Projects

_(max 10 items)_

## Expertise Map

_(max 20 items)_

## Goals & Priorities

_(max 10 items)_

## Interaction Patterns

_(max 10 items, high-confidence only)_

## Recent Themes

_(max 10 items — from cross-interaction synthesis)_
UKMD
echo "    + USER_KNOWLEDGE.md templated"

# ── 5b. Broad IP/username replacement across all files ────
echo "  [5b/9] Replacing hardcoded IPs and usernames globally..."

# Replace Ollama IP in all files
find "$OUTPUT_DIR" -type f \( -name "*.py" -o -name "*.yaml" -o -name "*.yml" -o -name "*.md" -o -name "*.sh" \) \
    -exec grep -l "10\.176\.34\.199" {} \; 2>/dev/null | while IFS= read -r f; do
    sed -i 's|10\.176\.34\.199:11434|${OLLAMA_URL:-localhost:11434}|g' "$f"
    sed -i 's|10\.176\.34\.199|${OLLAMA_HOST:-localhost}|g' "$f"
    echo "    + $(basename "$f"): Ollama IP templated"
done

# Replace LM Studio IP
find "$OUTPUT_DIR" -type f \( -name "*.py" -o -name "*.yaml" -o -name "*.yml" -o -name "*.md" \) \
    -exec grep -l "192\.168\.50\.100" {} \; 2>/dev/null | while IFS= read -r f; do
    sed -i 's|192\.168\.50\.100:1234|${LM_STUDIO_HOST:-localhost:1234}|g' "$f"
    sed -i 's|192\.168\.50\.100|${LM_STUDIO_HOST:-localhost}|g' "$f"
    echo "    + $(basename "$f"): LM Studio IP templated"
done

# Replace VM IP
find "$OUTPUT_DIR" -type f \( -name "*.py" -o -name "*.yaml" -o -name "*.yml" -o -name "*.md" -o -name "*.sh" \) \
    -exec grep -l "192\.168\.50\.77" {} \; 2>/dev/null | while IFS= read -r f; do
    sed -i 's|192\.168\.50\.77|${VM_HOST:-localhost}|g' "$f"
    echo "    + $(basename "$f"): VM IP templated"
done

# Replace YOUR_GITHUB_USER in scripts and docs (not already handled by CLAUDE.md step)
find "$OUTPUT_DIR" -type f \( -name "*.py" -o -name "*.sh" -o -name "*.md" -o -name "*.service" \) \
    -exec grep -l "YOUR_GITHUB_USER" {} \; 2>/dev/null | while IFS= read -r f; do
    # Replace private repo references but preserve the public repo name.
    # WingedGuardian/GENesis-AGI is the public repo itself — keep that as-is.
    sed -i '/GENesis-AGI/!s|WingedGuardian|YOUR_GITHUB_USER|g' "$f"
    echo "    + $(basename "$f"): GitHub username templated"
done

# Replace container IP
find "$OUTPUT_DIR" -type f \( -name "*.py" -o -name "*.yaml" -o -name "*.yml" -o -name "*.md" \) \
    -exec grep -l "10\.176\.34\.206" {} \; 2>/dev/null | while IFS= read -r f; do
    sed -i 's|10\.176\.34\.206|${CONTAINER_IP:-localhost}|g' "$f"
    echo "    + $(basename "$f"): Container IP templated"
done

# Scrub known IPv6 ULA addresses (container + host)
find "$OUTPUT_DIR" -type f \( -name "*.py" -o -name "*.yaml" -o -name "*.yml" -o -name "*.md" -o -name "*.sh" \) \
    -exec grep -l '${CONTAINER_IPV6:-not configured}' {} \; 2>/dev/null | while IFS= read -r f; do
    sed -i 's|${CONTAINER_IPV6:-not configured}|${CONTAINER_IPV6:-not configured}|g' "$f"
    echo "    + $(basename "$f"): Container IPv6 templated"
done
find "$OUTPUT_DIR" -type f \( -name "*.py" -o -name "*.yaml" -o -name "*.yml" -o -name "*.md" -o -name "*.sh" \) \
    -exec grep -l '${HOST_IPV6:-not configured}' {} \; 2>/dev/null | while IFS= read -r f; do
    sed -i 's|${HOST_IPV6:-not configured}|${HOST_IPV6:-not configured}|g' "$f"
    echo "    + $(basename "$f"): Host IPv6 templated"
done

# Catch-all: any remaining 192.168.50.x private subnet references
find "$OUTPUT_DIR" -type f \( -name "*.py" -o -name "*.yaml" -o -name "*.yml" -o -name "*.md" -o -name "*.sh" \) \
    -exec grep -l '192\.168\.50\.' {} \; 2>/dev/null | while IFS= read -r f; do
    sed -i 's|192\.168\.50\.[0-9]\+:[0-9]\+|${LOCAL_HOST:-localhost:8080}|g' "$f"
    sed -i 's|192\.168\.50\.[0-9]\+|${LOCAL_HOST:-localhost}|g' "$f"
    echo "    + $(basename "$f"): remaining private subnet IPs templated"
done

# Replace hardcoded ${HOME}/ install path in docs and source
# EXCLUDE host-setup.sh, install_guardian.sh, install.sh — these reference
# the container's internal /home/ubuntu path, not the host user's home.
find "$OUTPUT_DIR" -type f \( -name "*.md" -o -name "*.py" -o -name "*.sh" \) \
    -not -name "host-setup.sh" \
    -not -name "install_guardian.sh" \
    -not -name "install.sh" \
    -exec grep -l "${HOME}/" {} \; 2>/dev/null | while IFS= read -r f; do
    sed -i 's|${HOME}/|${HOME}/|g' "$f"
    echo "    + $(basename "$f"): install path templated"
done

# Replace user timezone with UTC default in config and source (not tests/docs)
find "$OUTPUT_DIR/config" "$OUTPUT_DIR/src" "$OUTPUT_DIR" -maxdepth 1 -type f \( -name "*.yaml" -o -name "*.yml" -o -name "*.py" -o -name "*.example" \) \
    -exec grep -l "America/New_York" {} \; 2>/dev/null | while IFS= read -r f; do
    sed -i 's|America/New_York|UTC|g' "$f"
    echo "    + $(basename "$f"): timezone templated"
done

# ── 5c. Clean hardware-specific references from reference docs ──
echo "  [5c/9] Cleaning hardware references from reference docs..."

for f in "$OUTPUT_DIR/docs/reference/testing.md" "$OUTPUT_DIR/docs/reference/troubleshooting.md"; do
    if [ -f "$f" ]; then
        sed -i 's|5070ti (LM Studio)|LM Studio on local GPU host|g' "$f"
        sed -i 's|5070ti host|local inference host|g' "$f"
        sed -i 's|(no GPU) + 5070ti|(no GPU) + local GPU host|g' "$f"
        sed -i 's|5070ti|local GPU host|g' "$f"
        echo "    + $(basename "$f"): hardware refs cleaned"
    fi
done

# ── 6. Ensure .gitignore covers private data ─────────────
echo "  [6/9] Verifying .gitignore..."

GITIGNORE="$OUTPUT_DIR/.gitignore"
for pattern in "data/genesis.db" "genesis.db" "secrets.env" "logs/" ".firecrawl/" ".playwright-mcp/" "*.jsonl"; do
    if ! grep -qF "$pattern" "$GITIGNORE" 2>/dev/null; then
        echo "$pattern" >> "$GITIGNORE"
        echo "    + Added $pattern to .gitignore"
    fi
done

# ── 7. Secret scan ───────────────────────────────────────
echo "  [7/9] Running secret scanner..."

if command -v detect-secrets &>/dev/null; then
    scan_output=$(detect-secrets scan --all-files \
        --exclude-files '\.db$|package-lock\.json|\.pyc$' \
        "$OUTPUT_DIR" 2>/dev/null)
    finding_count=$(echo "$scan_output" | python3 -c "
import json, sys
data = json.load(sys.stdin)
results = data.get('results', {})
# Filter out CACHEDIR.TAG false positives
real = sum(len([f for f in findings if 'CACHEDIR' not in fp])
           for fp, findings in results.items())
print(real)
" 2>/dev/null || echo "error")

    if [ "$finding_count" = "0" ]; then
        echo "    + detect-secrets: CLEAN (0 findings)"
    elif [ "$finding_count" = "error" ]; then
        echo "    ! detect-secrets: scan error (review manually)"
    else
        echo "    ! detect-secrets: $finding_count potential secrets found!"
        echo "    Run: detect-secrets scan --all-files $OUTPUT_DIR"
        echo "    BLOCKING: Do not push until findings are reviewed."
    fi
else
    echo "    ! detect-secrets not installed (pip install detect-secrets)"
    echo "    Skipping automated secret scan."
fi

# ── 8. Portability scan ──────────────────────────────────
echo "  [8/9] Running portability scan..."
portability_hits=$(
    rg -n --hidden --glob '!**/.git/**' \
        -e '${HOME}/genesis' \
        -e '${HOME}/agent-zero' \
        -e '${HOME}/\.' \
        -e '-home-ubuntu-genesis' \
        -e '10\.176\.34\.199' \
        -e '10\.176\.34\.206' \
        -e '192\.168\.50\.' \
        -e 'YOUR_GITHUB_USER' \
        -e 'America/New_York' \
        "$OUTPUT_DIR/src" "$OUTPUT_DIR/config" "$OUTPUT_DIR/scripts" \
        $([ -f "$OUTPUT_DIR/env.example" ] && echo "$OUTPUT_DIR/env.example") \
        2>/dev/null || true
)
if [ -n "$portability_hits" ]; then
    echo "    ! portability scan found machine-specific references:"
    echo "$portability_hits" | sed -n '1,40p'
    echo "    BLOCKING: remove or parameterize these before publishing."
else
    echo "    + portability scan: CLEAN"
fi

# ── 8b. CHANGELOG check ──────────────────────────────────
echo "  [8b/9] Checking CHANGELOG..."
if [[ ! -f "$OUTPUT_DIR/CHANGELOG.md" ]]; then
    echo "    ! CHANGELOG.md missing from staging."
    echo "      Create it and commit before tagging a release."
elif grep -q "^## \[Unreleased\]" "$OUTPUT_DIR/CHANGELOG.md"; then
    # Has [Unreleased] — check it has actual content (not just the header)
    # Use found-flag pattern: skip header line, stop at next ## [, print content
    unreleased_content=$(awk '/^## \[Unreleased\]/{found=1; next} found && /^## \[/{exit} found{print}' \
        "$OUTPUT_DIR/CHANGELOG.md" | grep -v '^$' | head -3)
    if [[ -z "$unreleased_content" ]]; then
        echo "    ! CHANGELOG.md has an empty [Unreleased] section."
        echo "      Populate it before tagging a release."
    else
        echo "    + CHANGELOG.md has [Unreleased] content (ready to tag)"
    fi
else
    echo "    + CHANGELOG.md present (no [Unreleased] section — already released)"
fi

# ── 9. Report ────────────────────────────────────────────
echo "  [9/9] Release preparation complete."
echo ""

# Count what's in the output
file_count=$(find "$OUTPUT_DIR" -type f | wc -l)
dir_count=$(find "$OUTPUT_DIR" -type d | wc -l)
size=$(du -sh "$OUTPUT_DIR" | cut -f1)

# Record source commit for tracking
source_commit=$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown")
echo "$source_commit" > "$OUTPUT_DIR/.genesis-source-commit"

echo "  ─────────────────────────────────────────"
echo "  Output: $OUTPUT_DIR"
echo "  Source: $source_commit"
echo "  Files: $file_count  Directories: $dir_count  Size: $size"
echo ""
echo "  Manual verification:"
echo "    1. grep -r '10.176.34\|192.168.50\|YOUR_GITHUB_USER\|5070ti\|nanobot' $OUTPUT_DIR"
echo "    2. Check exemplar files are empty templates"
echo "    3. Check no product track plans remain"
echo "    4. Verify docs/history/ and docs/superpowers/ are absent"
echo ""
