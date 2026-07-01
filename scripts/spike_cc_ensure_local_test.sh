#!/usr/bin/env bash
# Unit test for cc_ensure_local (scripts/lib/cc_version.sh).
#
# Stubs `npm` and `claude` in a curated-PATH sandbox (only these stubs + the
# coreutils cc_ensure_local needs) so the real system CC is NEVER touched.
# Verifies the branching + the "2.1.173 (Claude Code)" version parse.
#
# Scenarios:
#   A. npm absent              -> skip, return 0, no install
#   B. claude already at pin    -> no-op, return 0, no `npm install`
#   C. claude present, drifted  -> `npm install --prefix <binprefix> @pin`, no sudo, return 0
#   D. claude absent (fresh)    -> `npm install --prefix <cfgprefix> @pin`, return 0
#   E. version parse            -> "2.1.170 (Claude Code)" recognized as drift, not pin
#
# All state isolated to ~/tmp throwaway dirs.
set -u
set -o pipefail

PASS=0
FAIL=0
FAIL_DETAILS=()

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CC_ENV="$SCRIPT_DIR/lib/cc_version.sh"
mkdir -p "$HOME/tmp"   # NEVER the default $TMPDIR (= CC's watchgod-policed cc-tmp)
SANDBOX="$(mktemp -d -p "$HOME/tmp" cc_ensure_test.XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT

PIN="$(bash -c "source '$CC_ENV'; echo \"\$CC_VERSION\"")"   # the pinned version

pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); FAIL_DETAILS+=("$1"); echo "  FAIL: $1"; }

# Build a fresh sandbox prefix with a curated bin dir (stubs + needed coreutils).
new_prefix() {
    local pfx="$SANDBOX/$1"
    local bin="$pfx/bin"
    mkdir -p "$bin"
    local c
    for c in awk dirname id timeout bash env; do   # NOT sudo — write_sudo_stub creates it fresh
        local real; real="$(command -v "$c" 2>/dev/null || true)"
        [ -n "$real" ] && ln -sf "$real" "$bin/$c"
    done
    echo "$pfx"
}

# Write the npm stub. Logs `install` invocations to $CALLS; answers
# `config get prefix` with $NPM_CFG_PREFIX; marks $INSTALLED_FLAG on install.
write_npm_stub() {
    local bin="$1/bin"
    cat > "$bin/npm" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = "config" ] && [ "${2:-}" = "get" ] && [ "${3:-}" = "prefix" ]; then
    echo "${NPM_CFG_PREFIX:-/usr/local}"; exit 0
fi
case " $* " in *" install "*) echo "npm $*" >> "$CALLS"; : > "$INSTALLED_FLAG";; esac
exit 0
EOF
    chmod +x "$bin/npm"
}

# Write a sudo stub that records the call then execs the rest (so the wrapped npm
# stub still runs) — lets us assert the sudo-prepend on the system-prefix branch.
write_sudo_stub() {
    local bin="$1/bin"
    cat > "$bin/sudo" <<'EOF'
#!/usr/bin/env bash
echo "sudo $*" >> "$CALLS"
exec "$@"
EOF
    chmod +x "$bin/sudo"
}

# Write the claude stub: reports $START_VER until $INSTALLED_FLAG appears, then $PIN.
write_claude_stub() {
    local bin="$1/bin"
    cat > "$bin/claude" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then
    if [ -f "$INSTALLED_FLAG" ]; then echo "$PIN (Claude Code)"; else echo "$START_VER (Claude Code)"; fi
fi
EOF
    chmod +x "$bin/claude"
}

# Run cc_ensure_local under the sandbox PATH; capture rc.
run_ensure() {
    local pfx="$1"
    PATH="$pfx/bin" bash -c "source '$CC_ENV'; cc_ensure_local"
}

# ── A. npm absent → skip, rc 0, no install ────────────────────────────────
{
    pfx="$(new_prefix a)"; rm -f "$pfx/bin/npm"     # no npm stub → npm absent
    export CALLS="$pfx/calls"; : > "$CALLS"; export INSTALLED_FLAG="$pfx/flag"
    export PIN START_VER="0.0.0" NPM_CFG_PREFIX="$pfx"
    write_claude_stub "$pfx"
    if run_ensure "$pfx" >/dev/null 2>&1 && [ ! -s "$CALLS" ]; then
        pass "A: npm absent → skip, no install"
    else
        fail "A: npm absent should skip cleanly (rc=$? calls=$(cat "$CALLS"))"
    fi
}

# ── B. claude at pin → no-op, no `npm install` ────────────────────────────
{
    pfx="$(new_prefix b)"
    export CALLS="$pfx/calls"; : > "$CALLS"; export INSTALLED_FLAG="$pfx/flag"; rm -f "$INSTALLED_FLAG"
    export PIN START_VER="$PIN" NPM_CFG_PREFIX="$pfx"
    write_npm_stub "$pfx"; write_claude_stub "$pfx"
    if run_ensure "$pfx" >/dev/null 2>&1 && [ ! -s "$CALLS" ]; then
        pass "B: at-pin → no-op, no install"
    else
        fail "B: at-pin should be a no-op (calls=$(cat "$CALLS"))"
    fi
}

# ── C. drifted, user prefix → install --prefix <binprefix> @pin, no sudo ──
{
    pfx="$(new_prefix c)"
    export CALLS="$pfx/calls"; : > "$CALLS"; export INSTALLED_FLAG="$pfx/flag"; rm -f "$INSTALLED_FLAG"
    export PIN START_VER="2.1.170" NPM_CFG_PREFIX="$pfx"
    write_npm_stub "$pfx"; write_claude_stub "$pfx"
    if run_ensure "$pfx" >/dev/null 2>&1; then
        line="$(cat "$CALLS")"
        if grep -q -- "--prefix $pfx " <<<"$line" && grep -q -- "@anthropic-ai/claude-code@${PIN}" <<<"$line" && ! grep -q "sudo" <<<"$line"; then
            pass "C: drift(user) → install --prefix $pfx @$PIN, no sudo"
        else
            fail "C: wrong npm invocation: $line"
        fi
    else
        fail "C: drift(user) returned non-zero"
    fi
}

# ── D. absent → install to configured prefix @pin ─────────────────────────
# Asserts the fresh-install invocation. The return-0 post-install verify path is
# already covered by C (drift → install → claude reports pin → rc 0); emulating
# "npm materializes the binary" here would only re-test the harness, so rc is
# ignored and we assert the npm command instead.
{
    pfx="$(new_prefix d)"; rm -f "$pfx/bin/claude"     # claude absent at decision time
    export CALLS="$pfx/calls"; : > "$CALLS"; export INSTALLED_FLAG="$pfx/flag"; rm -f "$INSTALLED_FLAG"
    export PIN START_VER="0.0.0" NPM_CFG_PREFIX="$pfx"
    write_npm_stub "$pfx"
    run_ensure "$pfx" >/dev/null 2>&1 || true          # rc ignored (no real binary appears)
    line="$(cat "$CALLS")"
    if grep -q -- "--prefix $pfx " <<<"$line" && grep -q -- "@anthropic-ai/claude-code@${PIN}" <<<"$line"; then
        pass "D: absent → install --prefix $pfx @$PIN (from npm config prefix)"
    else
        fail "D: wrong/no npm invocation: $line"
    fi
}

# ── E. version parse: "X (Claude Code)" drift is detected ─────────────────
{
    pfx="$(new_prefix e)"
    export CALLS="$pfx/calls"; : > "$CALLS"; export INSTALLED_FLAG="$pfx/flag"; rm -f "$INSTALLED_FLAG"
    export PIN START_VER="2.1.170" NPM_CFG_PREFIX="$pfx"
    write_npm_stub "$pfx"; write_claude_stub "$pfx"
    run_ensure "$pfx" >/dev/null 2>&1
    if [ -s "$CALLS" ]; then
        pass "E: '2.1.170 (Claude Code)' parsed as drift → install fired"
    else
        fail "E: version-suffix parse failed — drift not detected"
    fi
}

# ── F. fresh, SYSTEM prefix → sudo env "PATH=..." prepended ───────────────
{
    pfx="$(new_prefix f)"; rm -f "$pfx/bin/claude"     # claude absent → fresh path
    export CALLS="$pfx/calls"; : > "$CALLS"; export INSTALLED_FLAG="$pfx/flag"; rm -f "$INSTALLED_FLAG"
    export PIN START_VER="0.0.0" NPM_CFG_PREFIX="/opt/cctest"   # matches /opt/* → sudo branch
    write_npm_stub "$pfx"; write_sudo_stub "$pfx"
    run_ensure "$pfx" >/dev/null 2>&1 || true
    line="$(cat "$CALLS")"
    if grep -q "^sudo " <<<"$line" && grep -q -- "--prefix /opt/cctest" <<<"$line" && grep -q -- "@anthropic-ai/claude-code@${PIN}" <<<"$line"; then
        pass "F: system prefix → sudo env prepended, --prefix /opt/cctest @$PIN"
    else
        fail "F: sudo/system-prefix branch wrong: $line"
    fi
}

# ── G. post-install verify fails (install didn't reach pin) → return 1 ─────
{
    pfx="$(new_prefix g)"
    export CALLS="$pfx/calls"; : > "$CALLS"; export INSTALLED_FLAG="$pfx/flag"; rm -f "$INSTALLED_FLAG"
    export PIN START_VER="2.1.170" NPM_CFG_PREFIX="$pfx"
    # npm stub that logs install but does NOT flip the version (no INSTALLED_FLAG),
    # so the post-install re-check still sees START_VER != pin.
    cat > "$pfx/bin/npm" <<'EOF'
#!/usr/bin/env bash
[ "${1:-}" = "config" ] && { echo "${NPM_CFG_PREFIX:-/usr/local}"; exit 0; }
case " $* " in *" install "*) echo "npm $*" >> "$CALLS";; esac
exit 0
EOF
    chmod +x "$pfx/bin/npm"
    write_claude_stub "$pfx"     # always reports START_VER (flag never set)
    if run_ensure "$pfx" >/dev/null 2>&1; then
        fail "G: expected non-zero return when install didn't reach pin"
    else
        pass "G: post-install mismatch → return 1 (non-fatal for callers)"
    fi
}

echo ""
echo "cc_ensure_local: $PASS passed, $FAIL failed"
if [ "$FAIL" -ne 0 ]; then
    printf '  - %s\n' "${FAIL_DETAILS[@]}"
    exit 1
fi
