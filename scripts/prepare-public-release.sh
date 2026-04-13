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

# ── 3. Voice-master sanity check ─────────────────────────
# User voice data lives in the out-of-repo overlay at
# ~/.claude/skills/voice-master/. The in-repo voice-master directory MUST
# contain only generic template machinery — no exemplars, no voice-dimensions.
# This check asserts the architecture guarantee and fails the release if
# user data has accidentally landed back in the repo.
echo "  [3/9] Voice-master sanity check..."

VOICE_DIR="$OUTPUT_DIR/src/genesis/skills/voice-master"
VOICE_REFS="$VOICE_DIR/references"
EXEMPLAR_DIR="$VOICE_REFS/exemplars"

# 3a. The in-repo exemplars dir must contain only README.md (no real samples).
if [ -d "$EXEMPLAR_DIR" ]; then
    stray=$(find "$EXEMPLAR_DIR" -maxdepth 1 -type f ! -name 'README.md' 2>/dev/null || true)
    if [ -n "$stray" ]; then
        echo "    FATAL: in-repo exemplars directory contains non-template files:"
        echo "$stray" | sed 's|^|      |'
        echo "    User voice data belongs in \$GENESIS_VOICE_OVERLAY (default"
        echo "    ~/.claude/skills/voice-master/exemplars/), NOT in the repo."
        echo "    Move the user data out of the repo and retry."
        exit 1
    fi
    echo "    + exemplars dir clean (README.md only)"
else
    echo "    FATAL: $EXEMPLAR_DIR is missing — voice-master structure is broken"
    exit 1
fi

# 3b. voice-dimensions.md (user-specific) must NOT exist in the repo.
if [ -f "$VOICE_REFS/voice-dimensions.md" ]; then
    echo "    FATAL: $VOICE_REFS/voice-dimensions.md exists in the repo."
    echo "    User voice data belongs in the overlay at"
    echo "    ~/.claude/skills/voice-master/voice-dimensions.md."
    echo "    The in-repo version must be voice-dimensions-TEMPLATE.md only."
    exit 1
fi

# 3c. voice-dimensions-TEMPLATE.md must exist (the public fallback).
if [ ! -f "$VOICE_REFS/voice-dimensions-TEMPLATE.md" ]; then
    echo "    FATAL: $VOICE_REFS/voice-dimensions-TEMPLATE.md is missing."
    echo "    Voice-master requires a template fallback for users with no overlay."
    exit 1
fi

echo "    + voice-master in-repo structure clean"

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
    sed -i 's|/home/ubuntu/inbox|${GENESIS_INBOX_PATH:-~/inbox}|g' \
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

# Files that legitimately contain these literal patterns as DATA, not as
# user-config leakage. Templating them mangles the sanitizer's own regex
# patterns, the tz.py default timezone constant, and test fixtures that
# assert on these exact strings. Mirrors the SCAN_EXCLUDES list below but
# uses find-compatible `-path` syntax.
#
# Category:
#   (a) Contribution sanitizer — its source defines the patterns used to
#       detect machine-specific content in contributor diffs. Templating
#       would convert regexes like `/home/ubuntu/genesis` into broken
#       `${HOME}/genesis` (where $ is a regex anchor).
#   (b) Test fixtures + conftest — assert on literal paths/IPs/timezones
#       as part of what they're testing.
#   (c) Timezone util — `_DEFAULT_TZ = "America/New_York"` is the
#       module's default, not a user config leak.
#   (d) Install / setup scripts — already handled by the existing
#       `-not -name` clauses on the /home/ubuntu pass. Included here for
#       symmetry so every pass shares the same exclusion set.
TEMPLATE_EXCLUDES=(
    # (a) contribution sanitizer
    -not -path "*/src/genesis/contribution/sanitize.py"
    -not -path "*/tests/test_contribution/test_sanitize.py"
    # (b) test fixtures + conftest
    -not -path "*/tests/test_autonomy/test_protection.py"
    -not -path "*/tests/test_hooks/test_inline_hooks.py"
    -not -path "*/tests/conftest.py"
    # (c) tz util + its tests
    -not -path "*/src/genesis/util/tz.py"
    -not -path "*/tests/test_util/test_tz.py"
    # (d) install/setup scripts (mirror the /home/ubuntu pass)
    -not -name "host-setup.sh"
    -not -name "install_guardian.sh"
    -not -name "install.sh"
    -not -name "uninstall.sh"
    # Release machinery — defined in SCAN_EXCLUDES too
    -not -name "prepare-public-release.sh"
    -not -name "push-public-release.sh"
    -not -name "release-script-guarantees.md"
)

# Replace Ollama IP in all files
find "$OUTPUT_DIR" -type f "${TEMPLATE_EXCLUDES[@]}" \( -name "*.py" -o -name "*.yaml" -o -name "*.yml" -o -name "*.md" -o -name "*.sh" \) \
    -exec grep -l "10\.176\.34\.199" {} \; 2>/dev/null | while IFS= read -r f; do
    sed -i 's|10\.176\.34\.199:11434|${OLLAMA_URL:-localhost:11434}|g' "$f"
    sed -i 's|10\.176\.34\.199|${OLLAMA_HOST:-localhost}|g' "$f"
    echo "    + $(basename "$f"): Ollama IP templated"
done

# Replace LM Studio IP
find "$OUTPUT_DIR" -type f "${TEMPLATE_EXCLUDES[@]}" \( -name "*.py" -o -name "*.yaml" -o -name "*.yml" -o -name "*.md" \) \
    -exec grep -l "192\.168\.50\.100" {} \; 2>/dev/null | while IFS= read -r f; do
    sed -i 's|192\.168\.50\.100:1234|${LM_STUDIO_HOST:-localhost:1234}|g' "$f"
    sed -i 's|192\.168\.50\.100|${LM_STUDIO_HOST:-localhost}|g' "$f"
    echo "    + $(basename "$f"): LM Studio IP templated"
done

# Replace VM IP
find "$OUTPUT_DIR" -type f "${TEMPLATE_EXCLUDES[@]}" \( -name "*.py" -o -name "*.yaml" -o -name "*.yml" -o -name "*.md" -o -name "*.sh" \) \
    -exec grep -l "192\.168\.50\.77" {} \; 2>/dev/null | while IFS= read -r f; do
    sed -i 's|192\.168\.50\.77|${VM_HOST:-localhost}|g' "$f"
    echo "    + $(basename "$f"): VM IP templated"
done

# Replace WingedGuardian in scripts and docs (not already handled by CLAUDE.md step)
find "$OUTPUT_DIR" -type f "${TEMPLATE_EXCLUDES[@]}" \( -name "*.py" -o -name "*.sh" -o -name "*.md" -o -name "*.service" \) \
    -exec grep -l "WingedGuardian" {} \; 2>/dev/null | while IFS= read -r f; do
    # Replace private repo references but preserve the public repo name.
    # WingedGuardian/GENesis-AGI is the public repo itself — keep that as-is.
    sed -i '/GENesis-AGI/!s|WingedGuardian|YOUR_GITHUB_USER|g' "$f"
    echo "    + $(basename "$f"): GitHub username templated"
done

# Replace container IP
find "$OUTPUT_DIR" -type f "${TEMPLATE_EXCLUDES[@]}" \( -name "*.py" -o -name "*.yaml" -o -name "*.yml" -o -name "*.md" \) \
    -exec grep -l "10\.176\.34\.206" {} \; 2>/dev/null | while IFS= read -r f; do
    sed -i 's|10\.176\.34\.206|${CONTAINER_IP:-localhost}|g' "$f"
    echo "    + $(basename "$f"): Container IP templated"
done

# Scrub known IPv6 ULA addresses (container + host)
find "$OUTPUT_DIR" -type f "${TEMPLATE_EXCLUDES[@]}" \( -name "*.py" -o -name "*.yaml" -o -name "*.yml" -o -name "*.md" -o -name "*.sh" \) \
    -exec grep -l 'fd42:e3ba:1142:18bb:216:3eff:fe93:5e04' {} \; 2>/dev/null | while IFS= read -r f; do
    sed -i 's|fd42:e3ba:1142:18bb:216:3eff:fe93:5e04|${CONTAINER_IPV6:-not configured}|g' "$f"
    echo "    + $(basename "$f"): Container IPv6 templated"
done
find "$OUTPUT_DIR" -type f "${TEMPLATE_EXCLUDES[@]}" \( -name "*.py" -o -name "*.yaml" -o -name "*.yml" -o -name "*.md" -o -name "*.sh" \) \
    -exec grep -l 'fd4d:77b8:b157:7fdf:be24:11ff:feab:89f5' {} \; 2>/dev/null | while IFS= read -r f; do
    sed -i 's|fd4d:77b8:b157:7fdf:be24:11ff:feab:89f5|${HOST_IPV6:-not configured}|g' "$f"
    echo "    + $(basename "$f"): Host IPv6 templated"
done

# Catch-all: any remaining 192.168.50.x private subnet references
find "$OUTPUT_DIR" -type f "${TEMPLATE_EXCLUDES[@]}" \( -name "*.py" -o -name "*.yaml" -o -name "*.yml" -o -name "*.md" -o -name "*.sh" \) \
    -exec grep -l '192\.168\.50\.' {} \; 2>/dev/null | while IFS= read -r f; do
    sed -i 's|192\.168\.50\.[0-9]\+:[0-9]\+|${LOCAL_HOST:-localhost:8080}|g' "$f"
    sed -i 's|192\.168\.50\.[0-9]\+|${LOCAL_HOST:-localhost}|g' "$f"
    echo "    + $(basename "$f"): remaining private subnet IPs templated"
done

# Replace hardcoded /home/ubuntu/ install path in docs and source
# TEMPLATE_EXCLUDES already covers host-setup.sh, install_guardian.sh,
# install.sh, uninstall.sh — these reference the container's internal
# /home/ubuntu path, not the host user's home.
find "$OUTPUT_DIR" -type f "${TEMPLATE_EXCLUDES[@]}" \( -name "*.md" -o -name "*.py" -o -name "*.sh" \) \
    -exec grep -l "/home/ubuntu/" {} \; 2>/dev/null | while IFS= read -r f; do
    sed -i 's|/home/ubuntu/|${HOME}/|g' "$f"
    echo "    + $(basename "$f"): install path templated"
done

# Replace user timezone with UTC default in config and source.
# IMPORTANT: -maxdepth here would previously cut recursion into src/genesis/
# subdirectories (find applies -maxdepth to all starting paths). Drop it so
# deep matches (e.g. src/genesis/ego/types.py, src/genesis/inbox/config.py)
# actually get replaced. TEMPLATE_EXCLUDES now protects util/tz.py from
# being templated — its `_DEFAULT_TZ = "America/New_York"` constant is a
# module default, not a user config leak.
find "$OUTPUT_DIR/config" "$OUTPUT_DIR/src" "$OUTPUT_DIR/scripts" -type f "${TEMPLATE_EXCLUDES[@]}" \
    \( -name "*.yaml" -o -name "*.yml" -o -name "*.py" -o -name "*.example" -o -name "*.sh" \) \
    -not -path "*/tests/*" -not -path "*/test_*" \
    -exec grep -l "America/New_York" {} \; 2>/dev/null | while IFS= read -r f; do
    sed -i 's|America/New_York|UTC|g' "$f"
    echo "    + ${f#$OUTPUT_DIR/}: timezone templated"
done
# Also pick up root-level files at depth 1
find "$OUTPUT_DIR" -maxdepth 1 -type f \( -name "*.yaml" -o -name "*.yml" -o -name "*.py" -o -name "*.example" \) \
    -exec grep -l "America/New_York" {} \; 2>/dev/null | while IFS= read -r f; do
    sed -i 's|America/New_York|UTC|g' "$f"
    echo "    + $(basename "$f"): timezone templated (root)"
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
# Verify ripgrep is installed — the scan silently passes if rg is missing,
# which would be a safety regression. Fail hard instead.
if ! command -v rg >/dev/null 2>&1; then
    echo "  [8/9] FATAL: ripgrep (rg) not installed. Cannot run portability scan."
    exit 1
fi

echo "  [8/9] Running portability scan..."
# Exclusion list: files that legitimately contain the patterns being
# scanned. Three categories:
#  (a) Scanner / release machinery — contain the patterns as string
#      literals in scanner code, instructions, or audit docs.
#  (b) Container install scripts — intentionally reference /home/ubuntu
#      as container-internal paths (excluded from the /home/ubuntu/
#      replacement pass at line 443-452 for the same reason).
#  (c) User-facing UI text / generic docstrings — IANA timezone lists in
#      dashboard dropdowns, EST/EDT as docstring examples of tz
#      abbreviations, etc.
#
# ripgrep glob patterns must be basename-style (**/<name>) because
# --glob paths are matched relative to the matching directory, not the
# starting search path.
SCAN_EXCLUDES=(
    --glob '!**/.git/**'
    # (a) scanner + release machinery
    --glob '!**/prepare-public-release.sh'
    --glob '!**/push-public-release.sh'
    --glob '!**/public-release.yaml'
    --glob '!**/release-script-guarantees.md'
    # Contribution-pipeline sanitizer and CI leak-detector: their source
    # defines the regex patterns used to detect machine-specific content.
    # The literals appearing here are scanner definitions, not leaks.
    # sanitize.py tests mirror the same literals as fixtures.
    --glob '!**/src/genesis/contribution/sanitize.py'
    --glob '!**/tests/test_contribution/test_sanitize.py'
    --glob '!**/.github/workflows/ci.yml'
    # (b) container install scripts — container-internal /home/ubuntu refs
    --glob '!**/host-setup.sh'
    --glob '!**/install.sh'
    --glob '!**/install_guardian.sh'
    --glob '!**/uninstall.sh'
    # (c) generic docstrings / UI content
    --glob '!**/src/genesis/util/tz.py'
    --glob '!**/src/genesis/observability/service_status.py'
    --glob '!**/src/genesis/dashboard/templates/genesis_dashboard.html'
    # (d) vendored third-party code — not user-specific, may contain
    # false-positive matches (e.g. EDT as an EDIFACT segment type in
    # Ace editor's mode-edifact.js, not the timezone abbreviation).
    --glob '!**/vendor/**'
    --glob '!**/node_modules/**'
)
portability_hits=$(
    rg -n --hidden "${SCAN_EXCLUDES[@]}" \
        -e '/home/ubuntu/genesis' \
        -e '/home/ubuntu/agent-zero' \
        -e '/home/ubuntu/\.' \
        -e '-home-ubuntu-genesis' \
        -e '10\.176\.34\.199' \
        -e '10\.176\.34\.206' \
        -e '192\.168\.50\.' \
        -e '\bWingedGuardian/(Genesis|genesis-backups)\b' \
        -e 'America/New_York' \
        -e '\b(EST|EDT)\b' \
        -e '5070ti' \
        -e 'fd42:e3ba' \
        -e 'fd4d:77b8' \
        "$OUTPUT_DIR/src" "$OUTPUT_DIR/config" "$OUTPUT_DIR/scripts" "$OUTPUT_DIR/.github" \
        $([ -f "$OUTPUT_DIR/env.example" ] && echo "$OUTPUT_DIR/env.example") \
        2>/dev/null || true
)
if [ -n "$portability_hits" ]; then
    echo "    ! portability scan found machine-specific references:"
    echo "$portability_hits" | sed -n '1,40p'
    echo "    BLOCKING: remove or parameterize these before publishing."
    exit 1
else
    echo "    + portability scan: CLEAN"
fi

# ── 8b. Fingerprint scan (belt-and-suspenders) ───────────
# Loads user-defined fingerprint patterns from ~/.genesis/release-fingerprints.txt
# (one pattern per line, blank lines and lines starting with # are ignored).
# This catches persona names, personal handles, and any other user-defined
# strings that should never appear in the public release.
#
# The fingerprint file lives OUTSIDE the repo on purpose: the whole point is
# to scan FOR these strings, so they cannot themselves live in the tree being
# scanned. Keep the file at ~/.genesis/release-fingerprints.txt (not backed
# up by genesis-backups).
#
# A minimal generic scan also runs: personal-email-domain regex with an
# allowlist for known-safe addresses (noreply, backup@genesis.local, etc.).
echo "  [8b/9] Running fingerprint scan..."

FINGERPRINT_FILE="${GENESIS_RELEASE_FINGERPRINTS:-$HOME/.genesis/release-fingerprints.txt}"
fingerprint_hits=""

# Fingerprint scan exclusions: extends the portability SCAN_EXCLUDES with
# additional paths that legitimately contain email-shaped strings that are
# not user identifiers:
#  - tests/ use fake email fixtures (a@b.com, c@d.com) for mail handling
#  - docs/reference/readme-legacy.md has placeholder examples like
#    my-nanobot@gmail.com (should eventually be rewritten to use
#    @example.com per RFC 2606, but not in this pass).
FINGERPRINT_EXCLUDES=(
    "${SCAN_EXCLUDES[@]}"
    --glob '!**/tests/**'
    --glob '!**/readme-legacy.md'
)

# User-defined fingerprints (exclusions mirror the portability scan)
if [ -f "$FINGERPRINT_FILE" ]; then
    fp_patterns=()
    while IFS= read -r line; do
        # Skip blank lines and comments
        [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
        fp_patterns+=(-e "$line")
    done < "$FINGERPRINT_FILE"

    if [ ${#fp_patterns[@]} -gt 0 ]; then
        fingerprint_hits=$(
            rg -n --hidden "${FINGERPRINT_EXCLUDES[@]}" \
                "${fp_patterns[@]}" \
                "$OUTPUT_DIR" 2>/dev/null || true
        )
        # Each pattern produces 2 array entries (-e + value), so divide by 2 for the user-visible count.
        pattern_count=$(( ${#fp_patterns[@]} / 2 ))
        echo "    + loaded ${pattern_count} user-defined fingerprint pattern(s) from $FINGERPRINT_FILE"
    fi
else
    echo "    . no user fingerprint file at $FINGERPRINT_FILE (optional)"
fi

# Generic email scan (allowlist model, not denylist).
# Scan for any email address, then filter out known-safe patterns.
# The allowlist approach catches personal emails across all providers, not
# just the popular few (gmail/yahoo/etc.) — missing a provider in a denylist
# creates a false-negative leak path; adding an entry to an allowlist only
# creates a false-positive we can see and fix.
#
# Allowlist patterns must be ANCHORED so substring matches don't leak past:
# - "noreply@" alone would allow "evil-noreply@gmail.com". Use "^[^@]*noreply@"
#   (start of local-part or word boundary) or specific full addresses.
email_regex='[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
generic_email_hits=$(
    rg -n --hidden "${FINGERPRINT_EXCLUDES[@]}" \
        -e "$email_regex" \
        "$OUTPUT_DIR" 2>/dev/null | \
    grep -vE '(^|[^a-zA-Z0-9._+-])(noreply|no-reply)@' | \
    grep -vE '(^|[^a-zA-Z0-9._+-])backup@genesis\.local\b' | \
    grep -vE '(^|[^a-zA-Z0-9._+-])feedback@anthropic\.com\b' | \
    grep -vE '(^|[^a-zA-Z0-9._+-])pr-bot@' | \
    grep -vE '(^|[^a-zA-Z0-9._+-])support@anthropic\.com\b' | \
    grep -vE '@(example|example\.com|example\.org|localhost|test|invalid)\b' | \
    grep -vE '@claude\.com\b' | \
    grep -vE '@(github|gitlab|sentry|grafana|slack|discord)\.com\b' | \
    grep -vE 'user@[0-9]+\.service' | \
    grep -vE '@[0-9]+\.service\b' \
        || true
)

if [ -n "$fingerprint_hits" ] || [ -n "$generic_email_hits" ]; then
    echo "    ! fingerprint scan found matches:"
    [ -n "$fingerprint_hits" ] && echo "$fingerprint_hits" | sed -n '1,20p'
    [ -n "$generic_email_hits" ] && {
        echo "    (personal email domains)"
        echo "$generic_email_hits" | sed -n '1,10p'
    }
    echo "    BLOCKING: these strings must not appear in the public release."
    echo "    Either remove them from the source or add an exception to the"
    echo "    allowlist in scripts/prepare-public-release.sh."
    exit 1
else
    echo "    + fingerprint scan: CLEAN"
fi

# ── 8c. CHANGELOG check ──────────────────────────────────
echo "  [8c/9] Checking CHANGELOG..."
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
echo "    1. grep -r '10.176.34\|192.168.50\|WingedGuardian\|5070ti\|nanobot' $OUTPUT_DIR"
echo "    2. Check voice-master/references/exemplars/ contains only README.md"
echo "    3. Check voice-master/references/voice-dimensions-TEMPLATE.md is the only voice-dimensions file"
echo "    4. Check no product track plans remain"
echo "    5. Verify docs/history/, docs/superpowers/, docs/gtm/ are absent"
echo "    6. Fingerprint file: \$GENESIS_RELEASE_FINGERPRINTS (default ~/.genesis/release-fingerprints.txt)"
echo ""
