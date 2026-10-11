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
#   detect-secrets   secret scan over src/ config/ scripts/ .github/ (tip; plus
#                    the branch history's added lines on a branch push)
#   gitleaks         whole-tree gitleaks scan (version + checksum pinned, --redact)
#   gitleaks-history gitleaks over every commit in the scan range (branch pushes)
#   class            advisory class scan (never gating)
#   email            personal-email scan (tip; plus the branch's history on a
#                    branch push)
#   binary           tracked binary/data artifact scan (tip; plus every commit
#                    in the branch's history on a branch push)
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

# Run from the repository root whatever the caller's cwd. CDPATH is cleared so
# `cd` cannot search it and land in a different tree.
cd -- "$(unset CDPATH; cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"

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

_detect_secrets_scan() {
  # $1: output JSON; remaining args: paths, relative to the current directory.
  # Relative ONLY: MEASURED with detect-secrets 1.5.0, an absolute path outside
  # the working directory is not scanned at all and reports no results.
  local out="$1"
  shift
  detect-secrets scan --all-files \
      --exclude-files '\.git/' \
      --exclude-files '__pycache__/' \
      --exclude-files '\.pyc$' \
      --exclude-files 'vendor/' \
      "$@" > "$out"
}

_detect_secrets_count() {
  python3 -c "
import json, sys
data = json.load(open(sys.argv[1]))
results = data.get('results', {})
real = sum(len([f for f in v if 'CACHEDIR' not in fp])
           for fp, v in results.items())
print(real)
" "$1"
}

step_detect_secrets() {
  local count hist_count=0 hist_dir="$WORK/ds-history"
  _detect_secrets_scan "$WORK/ds-results.json" src/ config/ scripts/ .github/
  count="$(_detect_secrets_count "$WORK/ds-results.json")"
  if [[ "${LEAK_SCAN_RANGE-}" == "branch" ]]; then
    # A branch push publishes its whole history, so a secret added and then
    # removed before the push is still public. Every line the branch's commits
    # added under the same roots is written to <commit>/<path> in a scratch
    # tree, which is scanned from inside so its paths are relative; a finding
    # names the commit and path, never the value (the JSON holds hashes).
    rm -rf -- "$hist_dir"
    mkdir -p -- "$hist_dir"
    if ! python3 scripts/ci/leak_scan_added_lines.py --materialize "$hist_dir" >/dev/null; then
      echo "::error::detect-secrets history scan: the branch range could not be read. Failing closed."
      exit 3
    fi
    (cd -- "$hist_dir" && _detect_secrets_scan "$WORK/ds-history.json" .)
    hist_count="$(_detect_secrets_count "$WORK/ds-history.json")"
  fi
  if [[ "$count" != "0" || "$hist_count" != "0" ]]; then
    echo "::error::detect-secrets found $count potential secret(s) at the tip and $hist_count in the branch history"
    [[ "$count" == "0" ]] || cat "$WORK/ds-results.json"
    [[ "$hist_count" == "0" ]] || cat "$WORK/ds-history.json"
    exit 1
  fi
  echo "Secret scan: CLEAN (0 findings)"
}

_gitleaks_bin() {
  # Version+checksum pinned download, once per job. --redact is MANDATORY on
  # every gitleaks call: these logs are public, and a found secret must never
  # be echoed into them.
  local GITLEAKS_VERSION=8.22.1
  local GITLEAKS_SHA256=2f92ab3b8e08319ac30836c32b90818e01519c3a4982771e4f45a7f5607872f7
  if [[ ! -x "$WORK/gitleaks" ]]; then
    curl -sSfL -o "$WORK/gitleaks.tgz" \
      "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"
    echo "${GITLEAKS_SHA256}  $WORK/gitleaks.tgz" | sha256sum -c - >&2
    tar -xzf "$WORK/gitleaks.tgz" -C "$WORK" gitleaks
  fi
  printf '%s\n' "$WORK/gitleaks"
}

step_gitleaks() {
  # Blocking. Covers the whole TREE (incl. docs/) with the repo's
  # .gitleaks.toml PII/infrastructure rules — the detect-secrets step only
  # covers src/config/scripts/.github.
  local gl
  gl="$(_gitleaks_bin)"
  "$gl" detect --no-git --redact -c .gitleaks.toml --source .
  echo "Leak scan (gitleaks): CLEAN"
}

step_gitleaks_history() {
  # Blocking. The tree scan above reads only the tip, so a secret added and
  # then removed inside the branch passes it while staying in published
  # history. This scans every commit in the scan range with the same rules.
  # The range comes from scripts/ci/leak_scan_added_lines.py (the private
  # step's own definition; it fails closed on an unresolvable range), so both
  # history scans cover the same commits.
  local gl range
  gl="$(_gitleaks_bin)"
  range="$(python3 scripts/ci/leak_scan_added_lines.py --range)"
  if [[ -z "$range" ]]; then
    echo "::error::gitleaks history: empty scan range. Failing closed."
    exit 3
  fi
  # --remerge-diff: a merge commit is read as what its conflict resolution
  # added, so a value introduced while resolving is scanned (MEASURED with
  # gitleaks 8.22.1: the plain range misses it, this catches it, and a clean
  # merge of main adds nothing).
  "$gl" git --redact -c .gitleaks.toml --log-opts="--remerge-diff $range" .
  echo "Leak scan (gitleaks history, $range): CLEAN"
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
    # path:line only: these logs are public and outlive the branch, so the
    # matched text itself is never printed.
    printf '%s\n' "$findings" | cut -d: -f1,2 | sed 's/^/::warning::class match at /'
    n="$(printf '%s\n' "$findings" | wc -l)"
    echo "Class scan: $n advisory finding(s) — non-gating."
  else
    echo "Class scan: CLEAN"
  fi
}

# The address pattern and its allowlist, shared by the tip and history halves of
# the email scan so the two cannot drift.
EMAIL_RE='[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'

_email_allowlist() {
  # stdin: "path:location:content" lines; stdout: the ones not allowlisted.
  grep -vE '(^|[^a-zA-Z0-9._+-])(noreply|no-reply)@' | \
    grep -vE 'backup@genesis\.local\b' | \
    grep -vE 'feedback@anthropic\.com\b' | \
    grep -vE 'support@anthropic\.com\b' | \
    grep -vE '@(example|example\.com|example\.org|localhost|test|invalid)\b' | \
    grep -vE '@(claude|github|gitlab|sentry|grafana|slack|discord)\.com\b' | \
    grep -vE 'user@[0-9]+\.service'
}

step_email() {
  local hits hist_lines hist_hits
  hits=$(
    rg -n --hidden \
      --glob '!**/.git/**' \
      --glob '!**/tests/**' \
      --glob '!**/readme-legacy.md' \
      --glob '!**/vendor/**' \
      -e "$EMAIL_RE" \
      src/ config/ scripts/ .github/ 2>/dev/null | _email_allowlist || true
  )
  if [[ "${LEAK_SCAN_RANGE-}" == "branch" ]]; then
    # A branch push publishes its whole history, so an address added and then
    # removed before the push is still public. Read every line the branch's
    # commits added, in the same paths the tip scan covers.
    if ! hist_lines="$(python3 scripts/ci/leak_scan_added_lines.py --with-paths)"; then
      echo "::error::email history scan: the branch range could not be read. Failing closed."
      exit 3
    fi
    hist_hits=$(
      printf '%s\n' "$hist_lines" | \
        grep -E '^(src|config|scripts|\.github)/' | \
        grep -vE '^([^:]*/)?(tests|vendor)/|^([^:]*/)?readme-legacy\.md:' | \
        grep -E "$EMAIL_RE" | _email_allowlist || true
    )
    hits="$(printf '%s\n%s\n' "$hits" "$hist_hits" | grep -v '^$' || true)"
  fi
  if [[ -n "$hits" ]]; then
    # path:line (tip) or path:commit (history) only: these logs are public, so
    # the address is never printed.
    echo "::error::Email scan found $(printf '%s\n' "$hits" | wc -l) personal email address(es) at:"
    printf '%s\n' "$hits" | cut -d: -f1,2
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
  # :(icase) so recording.WAV or model.ONNX is caught as well: a filename's
  # case does not change what the file holds (git pathspec magic, gitglossary).
  data_globs=(
    ':(icase)*.wav' ':(icase)*.flac' ':(icase)*.pcm' ':(icase)*.mp3'
    ':(icase)*.m4a' ':(icase)*.ogg' ':(icase)*.opus'
    ':(icase)*.db' ':(icase)*.db-wal' ':(icase)*.db-shm'
    ':(icase)*.sqlite' ':(icase)*.sqlite3'
    ':(icase)*.onnx'
    ':(icase)*speaker_registry*.json' ':(icase)ambient_enroll_*.json'
  )
  # Allowlist: ':(exclude)path' entries for confirmed-legit files.
  data_allow=()
  hits=$(git ls-files -z -- "${data_globs[@]}" "${data_allow[@]}" 2>/dev/null \
         | tr '\0' '\n' || true)
  if [[ "${LEAK_SCAN_RANGE-}" == "branch" ]]; then
    # A branch push publishes every commit, so an artifact added and deleted
    # before the push is still downloadable. List each such path any commit in
    # the branch range added; a merge counts through its conflict resolution,
    # as in the other history scans, and renames are split into delete + add
    # so a renamed-in artifact is seen too.
    local range hist
    if ! range="$(python3 scripts/ci/leak_scan_added_lines.py --range)" || [[ -z "$range" ]]; then
      echo "::error::binary history scan: the branch range could not be read. Failing closed."
      exit 3
    fi
    if ! hist="$(git log --remerge-diff --no-renames --diff-filter=A --name-only --format= \
                 "$range" -- "${data_globs[@]}" "${data_allow[@]}")"; then
      echo "::error::binary history scan: git log failed over $range. Failing closed."
      exit 3
    fi
    hits="$(printf '%s\n%s\n' "$hits" "$hist" | grep -v '^$' | sort -u || true)"
  fi
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
    # Fail closed: an unreadable body is unscanned text, not an empty one.
    if ! body="$(gh pr view "$PR_NUMBER" --json body -q .body)"; then
      echo "::error::private-pattern scan: could not read the PR body. Failing closed."
      exit 3
    fi
  fi

  printf '%s\n%s\n' "$added" "$body" | python3 scripts/ci/private_pattern_scan.py --patterns "$pf"
}

case "${1-}" in
  install)        step_install ;;
  detect-secrets) step_detect_secrets ;;
  gitleaks)       step_gitleaks ;;
  gitleaks-history) step_gitleaks_history ;;
  class)          step_class ;;
  email)          step_email ;;
  binary)         step_binary ;;
  private)        step_private ;;
  *)
    echo "usage: $0 {install|detect-secrets|gitleaks|gitleaks-history|class|email|binary|private}" >&2
    exit 2
    ;;
esac
