#!/usr/bin/env bash
# Leak scan — the ONE implementation behind every leak-scan step in CI.
#
# Called step by step from two workflows, so the scan logic cannot drift
# between them:
#   * .github/workflows/ci.yml, job `leak-detector` — pull requests to main and
#     pushes to main. `leak-detector` is a REQUIRED status check
#     (.github/rulesets/checks.json); its job id and step layout stay in ci.yml.
#   * .github/workflows/branch-leak-scan.yml, job `branch-leak-scan` — every
#     push to every non-main branch, so a branch is never public unchecked,
#     whether or not a PR exists for it and whichever client pushed it.
#
# Usage: scripts/ci/leak_scan.sh <step>
#   install          pinned scanner install (detect-secrets, ripgrep)
#   detect-secrets   secret scan over src/ config/ scripts/ .github/
#   gitleaks         whole-tree gitleaks scan (version + checksum pinned, --redact)
#   class            advisory class scan (never gating)
#   email            personal-email scan
#   binary           tracked binary/data artifact scan
#   private          private-pattern exact scan of ADDED lines (hard gate)
#
# Each step exits nonzero on a finding (except `class`, which is advisory).
# Steps are separate so each one stays its own named step in the Actions UI.
#
# Every version pin and checksum below is load-bearing: the jobs that run this
# are required checks with no bypass actor, so an unpinned scanner release that
# fires on pre-existing content would block every merge. See
# .github/rulesets/README.md. A change here changes BOTH workflows at once —
# that is the point.

set -euo pipefail

# Run from the repository root whatever the caller's cwd.
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

# Scratch directory for scanner output and the gitleaks download. CI uses /tmp;
# a local run can point it elsewhere.
WORK="${LEAK_SCAN_WORKDIR:-/tmp}"

step_install() {
  # `detect-secrets` PINNED for the same reason `ruff` is: `leak-detector`
  # is a required check with no bypass actor, it scans the WHOLE tree, and
  # a new detector in an unpinned release would fire on pre-existing
  # content and block every merge. See .github/rulesets/README.md.
  pip install 'detect-secrets==1.5.0'
  sudo apt-get install -y ripgrep
}

step_detect_secrets() {
  detect-secrets scan --all-files \
      --exclude-files '\.git/' \
      --exclude-files '__pycache__/' \
      --exclude-files '\.pyc$' \
      --exclude-files 'vendor/' \
      src/ config/ scripts/ .github/ > "$WORK/ds-results.json"
  local count
  count=$(python3 -c "
import json, sys
data = json.load(open(sys.argv[1]))
results = data.get('results', {})
real = sum(len([f for f in v if 'CACHEDIR' not in fp])
           for fp, v in results.items())
print(real)
" "$WORK/ds-results.json")
  if [[ "$count" != "0" ]]; then
    echo "::error::detect-secrets found $count potential secrets"
    cat "$WORK/ds-results.json"
    exit 1
  fi
  echo "Secret scan: CLEAN (0 findings)"
}

step_gitleaks() {
  # Blocking. Covers the whole tree (incl. docs/) with the repo's
  # .gitleaks.toml PII/infrastructure rules — the detect-secrets step only
  # covers src/config/scripts/.github. Version+checksum pinned;
  # --redact is MANDATORY: these logs are public, a found secret
  # must never be echoed into them.
  local GITLEAKS_VERSION=8.22.1
  local GITLEAKS_SHA256=2f92ab3b8e08319ac30836c32b90818e01519c3a4982771e4f45a7f5607872f7
  curl -sSfL -o "$WORK/gitleaks.tgz" \
    "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"
  echo "${GITLEAKS_SHA256}  $WORK/gitleaks.tgz" | sha256sum -c -
  tar -xzf "$WORK/gitleaks.tgz" -C "$WORK" gitleaks
  "$WORK/gitleaks" detect --no-git --redact -c .gitleaks.toml --source .
  echo "Leak scan (gitleaks): CLEAN"
}

step_class() {
  # Advisory — NEVER gating. Broad RFC1918 / CGNAT / IPv6-ULA and
  # /home/<user> CLASSES, surfaced as GitHub warnings. Rationale: CI must not
  # hard-block an external contributor for a legitimate private-range example
  # (e.g. 10.0.0.1 in a docker-compose). This install's EXACT values are
  # hard-gated by the private-pattern exact scan. Doc examples should use the
  # RFC 5737 ranges (192.0.2.x / 198.51.100.x / 203.0.113.x), which are
  # deliberately not matched. Network-address class vocabulary comes from
  # scripts/check_portability.sh --warn (single source of truth).
  local net paths findings n
  net="$(bash scripts/check_portability.sh --warn 2>/dev/null || true)"
  # Home-path + CC-slug classes (check_portability owns network only).
  # Exclude scanner-definition files that embed these patterns as data.
  paths="$(rg -n --hidden \
    --glob '!**/.git/**' \
    --glob '!**/src/genesis/contribution/sanitize.py' \
    --glob '!**/scripts/hooks/commit-msg' \
    --glob '!**/scripts/check_portability.sh' \
    --glob '!**/scripts/ci/leak_scan.sh' \
    --glob '!**/.github/workflows/ci.yml' \
    -e '/home/[a-z_][a-z0-9_-]*/genesis' \
    -e '/home/[a-z_][a-z0-9_-]*/\.[A-Za-z]' \
    -e '-home-[a-z0-9-]+-genesis' \
    . 2>/dev/null || true)"
  findings="$(printf '%s\n%s\n' "$net" "$paths" | grep -vE '^[[:space:]]*$' || true)"
  if [[ -n "$findings" ]]; then
    printf '%s\n' "$findings" | sed 's/^/::warning::/'
    n="$(printf '%s\n' "$findings" | wc -l)"
    echo "Class scan: $n advisory finding(s) — non-gating."
  else
    echo "Class scan: CLEAN"
  fi
}

step_email() {
  local hits
  hits=$(
    rg -n --hidden \
      --glob '!**/.git/**' \
      --glob '!**/tests/**' \
      --glob '!**/readme-legacy.md' \
      --glob '!**/vendor/**' \
      -e '[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}' \
      src/ config/ scripts/ .github/ 2>/dev/null | \
    grep -vE '(^|[^a-zA-Z0-9._+-])(noreply|no-reply)@' | \
    grep -vE 'backup@genesis\.local\b' | \
    grep -vE 'feedback@anthropic\.com\b' | \
    grep -vE 'support@anthropic\.com\b' | \
    grep -vE '@(example|example\.com|example\.org|localhost|test|invalid)\b' | \
    grep -vE '@(claude|github|gitlab|sentry|grafana|slack|discord)\.com\b' | \
    grep -vE 'user@[0-9]+\.service' \
      || true
  )
  if [[ -n "$hits" ]]; then
    echo "::error::Email scan found personal email addresses:"
    printf '%s\n' "$hits"
    exit 1
  fi
  echo "Email scan: CLEAN"
}

step_binary() {
  # Voiceprints, audio, databases, and models match NONE of the text
  # scans above, so a `git add -f` past .gitignore would commit one
  # unnoticed. Block any TRACKED artifact of these types (uses the git
  # index, so untracked local data is ignored).
  local data_globs data_allow hits
  data_globs=(
    '*.wav' '*.flac' '*.pcm' '*.mp3' '*.m4a' '*.ogg' '*.opus'
    '*.db' '*.db-wal' '*.db-shm' '*.sqlite' '*.sqlite3'
    '*.onnx'
    '*speaker_registry*.json' 'ambient_enroll_*.json'
  )
  # Allowlist: ':(exclude)path' entries for confirmed-legit files.
  data_allow=()
  hits=$(git ls-files -z -- "${data_globs[@]}" "${data_allow[@]}" 2>/dev/null \
         | tr '\0' '\n' || true)
  if [[ -n "$hits" ]]; then
    echo "::error::Binary/data artifacts must never be committed (voiceprints, audio, databases, models):"
    printf '%s\n' "$hits"
    exit 1
  fi
  echo "Binary/data artifact scan: CLEAN"
}

step_private() {
  # The REAL install identifiers come from the GENESIS_PRIVATE_PATTERNS repo
  # secret, mapped by the calling workflow into $PRIV — ZERO such values are
  # tracked in this repo. Whether this step runs at all (canonical repo,
  # non-fork, non-Dependabot) is decided by the workflow's `if:`.
  #
  # Inputs (environment, set by the workflow):
  #   PRIV             raw secret value
  #   EVENT_NAME       github.event_name
  #   PR_NUMBER        pull_request only — its body is scanned too
  #   GH_TOKEN         pull_request only — reads the PR body
  #   PUSH_BEFORE      push to main — previous tip
  #   HEAD_SHA         github.sha
  #   LEAK_SCAN_RANGE  `branch` on a non-main branch push: scan every commit
  #                    the branch carries that main does not (see
  #                    scripts/ci/leak_scan_added_lines.py)
  #
  # scripts/ci/private_pattern_scan.py does ALL filtering + validation of the
  # pattern set (blank/comment lines, empty set, malformed ERE) in tested
  # Python. It exits 0=clean, 1=leak (counts only, never content),
  # 3=fail-loud (unprovisioned/corrupt secret); set -e propagates a nonzero
  # exit as the gate's failure.
  local pf added body
  pf="$(mktemp)"
  printf '%s' "${PRIV-}" > "$pf"

  # The ADDED lines across EVERY commit's patch in range — NOT the net
  # tree-to-tree diff (a value added then removed within the same PR/push
  # leaves the net diff clean yet stays in published history). RANGE
  # SELECTION is security-critical and lives in tested Python; it fails
  # CLOSED (nonzero exit, which set -e turns into this step's failure) on an
  # unresolvable range rather than emitting empty. See
  # tests/test_scripts/test_leak_scan_added_lines.py.
  added="$(python3 scripts/ci/leak_scan_added_lines.py)"
  body=""
  if [[ "${EVENT_NAME-}" == "pull_request" ]]; then
    body="$(gh pr view "$PR_NUMBER" --json body -q .body 2>/dev/null || echo '')"
  fi

  printf '%s\n%s\n' "$added" "$body" | python3 scripts/ci/private_pattern_scan.py --patterns "$pf"
}

case "${1-}" in
  install)        step_install ;;
  detect-secrets) step_detect_secrets ;;
  gitleaks)       step_gitleaks ;;
  class)          step_class ;;
  email)          step_email ;;
  binary)         step_binary ;;
  private)        step_private ;;
  *)
    echo "usage: $0 {install|detect-secrets|gitleaks|class|email|binary|private}" >&2
    exit 2
    ;;
esac
