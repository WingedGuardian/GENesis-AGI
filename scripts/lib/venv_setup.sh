# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
# Genesis — shared venv/package-install helpers.
# Sourced by install.sh and bootstrap.sh; not executable on its own.

# editable_install_guarded <repo_dir> <venv_path> [extras]
#
# [extras] is an optional comma-separated extras list (e.g. "browser"); the
# install target becomes "<repo_dir>[extras]". Same guard, same contract. The
# import check below proves only the core package: callers verify an extra's own
# packages themselves (pip's status is masked, and a failed resolution leaves the
# previous packages importable).
#
# Installs the Genesis package into <venv_path> in editable mode, refusing
# to run when <repo_dir> is a linked git worktree: an editable install is
# system-wide state, so pointing it at a worktree redirects EVERY Genesis
# process (server, bridge, watchdog) to load the worktree's code. This
# caused an I/O death spiral and repeated crashes on 2026-03-16.
#
# Return codes (callers decide severity):
#   0 — installed and import-verified
#   1 — blocked: <repo_dir> is a git worktree, OR git could not tell us whether
#       it is one (message already printed)
#   2 — pip ran but the package is not importable
editable_install_guarded() {
    local repo_dir="$1"
    local venv_path="$2"
    local extras="${3:-}"
    local target="$repo_dir"
    if [ -n "$extras" ]; then
        target="${repo_dir}[${extras}]"
    fi

    # Canonical worktree detection: --git-common-dir differs from --git-dir
    # in a linked worktree. Checked against repo_dir explicitly (git -C), not
    # the caller's cwd — the installer may be invoked from anywhere.
    local git_common git_dir
    # Guarded: `git rev-parse` exits 128 when repo_dir is not a repo OR under
    # git's safe.directory "dubious ownership" refusal — realistic when the
    # installer runs as a different user than the repo owner. A bare assignment
    # would abort under the callers' `set -e`, returning 128 rather than this
    # function's documented 0/1/2 contract; both callers mis-map that to
    # "pip install completed but Genesis is not importable".
    git_common="$(git -C "$repo_dir" rev-parse --git-common-dir 2>/dev/null)" || git_common=""
    git_dir="$(git -C "$repo_dir" rev-parse --git-dir 2>/dev/null)" || git_dir=""

    # UNDETERMINABLE MUST BLOCK. Letting empty vars fall through to the
    # not-a-worktree path made the guard perform the exact install it exists to
    # prevent: an editable install is system-wide state, so pointing it at a
    # worktree redirects EVERY Genesis process to that worktree's code (the
    # 2026-03-16 I/O death spiral). "I could not determine whether this is a
    # worktree" is not evidence that it is not one — and the realistic cause is
    # git's safe.directory refusal, which fires precisely when the installer
    # runs as a different user than the repo owner.
    if [ -z "$git_common" ] || [ -z "$git_dir" ]; then
        echo "    BLOCKED: cannot determine whether $repo_dir is a git worktree."
        echo "    (git rev-parse failed — not a repository, or refused for dubious"
        echo "    ownership when the installer's user differs from the repo owner.)"
        echo "    Refusing a system-wide editable install on an unverified checkout."
        return 1
    fi

    if [ "$git_common" != "$git_dir" ]; then
        echo "    BLOCKED: pip install -e from a worktree redirects ALL system imports."
        echo "    Use PYTHONPATH=$repo_dir/src instead, or run from the main checkout."
        return 1
    fi

    "$venv_path/bin/pip" install -e "$target" --quiet 2>&1 | tail -1 || true
    reinstall_damaged_distributions "$repo_dir" "$venv_path"
    # Validate pip actually installed Genesis (|| true above masks pip failures).
    # genesis.mcp.health too: the server's dashboard and health routes import it,
    # and it pulls in the MCP stack the runtime alone never imports, so a broken
    # FastMCP once passed this check while the server could not serve.
    if "$venv_path/bin/python" -c "from genesis.runtime import GenesisRuntime; import genesis.mcp.health" 2>/dev/null; then
        return 0
    fi
    return 2
}

# reinstall_damaged_distributions <repo_dir> <venv_path>
#
# Reinstalls, at the version already installed and with --no-deps, every
# distribution whose recorded files an upgrade deleted (lib/venv_missing_files.py
# names them). pip does this to a package whose modules moved into a second
# distribution: upgrading FastMCP 2 to 4 in place installs fastmcp-slim's files,
# then removes fastmcp 2's, which list the same paths, and the server can no
# longer import FastMCP. Best effort and silent when nothing is damaged; the
# import check after it is still the verdict.
reinstall_damaged_distributions() {
    local repo_dir="$1"
    local venv_path="$2"
    local spec
    "$venv_path/bin/python" -P "$repo_dir/scripts/lib/venv_missing_files.py" 2>/dev/null \
        | while IFS= read -r spec || [ -n "$spec" ]; do
            [ -n "$spec" ] || continue
            echo "    Reinstalling $spec: an upgrade deleted files it owns"
            "$venv_path/bin/pip" install --quiet --no-deps --force-reinstall "$spec" 2>&1 | tail -1 || true
        done || true
}

# install_browser_extra <repo_dir> <venv_path>
#
# The `browser` extra (camoufox, playwright, patchright), installed AFTER the core
# packages and as its own call, never in place of them: a browser package that
# fails to resolve fails the whole pip run, and folded into the core call it would
# leave the core packages stale behind the masked pip status. Callers treat a
# failure as non-fatal. The engine and Chromium these packages pin are downloaded
# later, by scripts/install_browser_stack.sh.
#
# Returns 0 when the browser packages are installed as the extra intends, 1
# otherwise (each problem printed); also 1 when the guard refused the install.
install_browser_extra() {
    local repo_dir="$1"
    local venv_path="$2"
    # Before pip: once camoufox 0.5 is installed, a manual `camoufox fetch` deletes a
    # cache holding a pre-0.5 engine unless the cache carries .0.5_FLAG (the
    # browser-stack unit marks it itself). Best effort: a fresh install has no cache.
    "$venv_path/bin/python" -m genesis.browser.provision mark-legacy 2>/dev/null || true
    # Under the browser-stack unit's own lock: pip replacing camoufox, playwright or
    # patchright while a provisioning run uses them can mix pins mid-install. Waits
    # up to GENESIS_BROWSER_STACK_WAIT_S (default 7200, the project's step floor).
    local lock_dir="${GENESIS_HOME:-$HOME/.genesis}/locks"
    local wait_s="${GENESIS_BROWSER_STACK_WAIT_S:-7200}"
    if mkdir -p "$lock_dir" 2>/dev/null && command -v flock >/dev/null 2>&1; then
        if ! (
            flock -w "$wait_s" 9 || {
                echo "browser-stack: a provisioning run still holds its lock after ${wait_s}s; browser packages not changed" >&2
                exit 75
            }
            editable_install_guarded "$repo_dir" "$venv_path" browser
        ) 9>"$lock_dir/browser-stack.lock"; then
            return 1
        fi
    else
        editable_install_guarded "$repo_dir" "$venv_path" browser || return 1
    fi
    # The checkout's ranges, not the installed metadata a failed pip left behind.
    "$venv_path/bin/python" -m genesis.browser.provision check-packages --pyproject "$repo_dir/pyproject.toml"
}

# install_runtime_extras <repo_dir> <venv_path>
#
# The optional groups every install uses (INSTALL_EXTRAS in
# lib/venv_matches_pyproject.py, the one list: `test` so an install can run its
# own suite, `payments` for the card vault), installed AFTER the core packages
# and as their own call, for the reason install_browser_extra gives. Callers
# treat a failure as non-fatal. A failure is also written to
# GENESIS_DEPLOY_SUMMARY when it is set: update.sh shows only bootstrap's last
# lines, but prints that summary on every path.
#
# Returns 0 when every package of those groups is installed at a version the
# checkout accepts, 1 otherwise (each problem printed); also 1 when the guard
# refused the install or the list cannot be read.
install_runtime_extras() {
    local repo_dir="$1"
    local venv_path="$2"
    local gate="$repo_dir/scripts/lib/venv_matches_pyproject.py"
    local extras rc=0 out
    extras="$("$venv_path/bin/python" -P "$gate" --install-extras 2>/dev/null)" || extras=""
    if [ -z "$extras" ]; then
        echo "runtime extras: cannot read the list from $gate" >&2
        rc=1
    else
        editable_install_guarded "$repo_dir" "$venv_path" "$extras" || rc=1
        # The checkout's ranges, not the installed metadata a failed pip left behind.
        out="$("$venv_path/bin/python" -P "$gate" --extras "$extras" < "$repo_dir/pyproject.toml" 2>&1)" || rc=1
        if [ -n "$out" ]; then echo "$out"; fi
    fi
    if [ "$rc" -ne 0 ] && [ -n "${GENESIS_DEPLOY_SUMMARY:-}" ]; then
        printf '%s\n' "warning: runtime extras [${extras:-unknown}] incomplete (non-fatal); install them with scripts/update.sh --post-merge" \
            >> "$GENESIS_DEPLOY_SUMMARY" 2>/dev/null || true
    fi
    return "$rc"
}
