# Genesis — shared venv/package-install helpers.
# Sourced by install.sh and bootstrap.sh; not executable on its own.

# editable_install_guarded <repo_dir> <venv_path>
#
# Installs the Genesis package into <venv_path> in editable mode, refusing
# to run when <repo_dir> is a linked git worktree: an editable install is
# system-wide state, so pointing it at a worktree redirects EVERY Genesis
# process (server, bridge, watchdog) to load the worktree's code. This
# caused an I/O death spiral and repeated crashes on 2026-03-16.
#
# Return codes (callers decide severity):
#   0 — installed and import-verified
#   1 — blocked: <repo_dir> is a git worktree (message already printed)
#   2 — pip ran but the package is not importable
editable_install_guarded() {
    local repo_dir="$1"
    local venv_path="$2"

    # Canonical worktree detection: --git-common-dir differs from --git-dir
    # in a linked worktree. Checked against repo_dir explicitly (git -C), not
    # the caller's cwd — the installer may be invoked from anywhere.
    local git_common git_dir
    git_common="$(git -C "$repo_dir" rev-parse --git-common-dir 2>/dev/null)"
    git_dir="$(git -C "$repo_dir" rev-parse --git-dir 2>/dev/null)"
    if [ -n "$git_common" ] && [ -n "$git_dir" ] && [ "$git_common" != "$git_dir" ]; then
        echo "    BLOCKED: pip install -e from a worktree redirects ALL system imports."
        echo "    Use PYTHONPATH=$repo_dir/src instead, or run from the main checkout."
        return 1
    fi

    "$venv_path/bin/pip" install -e "$repo_dir" --quiet 2>&1 | tail -1 || true
    # Validate pip actually installed Genesis (|| true above masks pip failures)
    if "$venv_path/bin/python" -c "from genesis.runtime import GenesisRuntime" 2>/dev/null; then
        return 0
    fi
    return 2
}
