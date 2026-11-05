# shellcheck shell=bash
# Sourced by install.sh and bootstrap.sh. Defines functions only.
#
# CI tests Genesis on Python 3.12. pyproject allows newer, and an existing venv
# on one keeps working, but a new venv should get the version that is tested.
# A host whose only python3 is 3.13 or 3.14 may have no 3.12 package to fall
# back on: Ubuntu 26.04 ships 3.14 alone. uv's build needs no package source.

# True when a venv is about to be created and $2 is newer than what CI tests.
# Mirrors the "create or recreate" test both callers apply to the venv, so a
# host that already runs a venv gets nothing installed behind its back on the
# bootstrap every update runs.
genesis_new_venv_wants_python312() {
    local venv="$1" python="$2"
    if [ -d "$venv" ] && [ -x "$venv/bin/python" ] && [ -x "$venv/bin/pip" ]; then
        return 1
    fi
    "$python" -c 'import sys; sys.exit(0 if sys.version_info[:2] > (3, 12) else 1)' 2>/dev/null
}

# Installs uv's pre-built Python 3.12 (no compilation) and puts python3.12 on
# PATH. Returns non-zero, having changed no package source, when it cannot.
genesis_install_python312_uv() {
    echo "    Trying uv (pre-built Python 3.12 binary — no compilation needed)..."
    local uv_bin installer py_path
    uv_bin="$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")"
    if [ ! -x "$uv_bin" ]; then
        # Download-to-temp before executing: a transfer that drops mid-stream
        # must not run a truncated installer.
        installer="$(mktemp 2>/dev/null)" || return 1
        if curl -LsSf https://astral.sh/uv/install.sh -o "$installer"; then
            sh "$installer" >/dev/null || true
        fi
        rm -f "$installer"
        # The installer honours these before its default.
        for uv_bin in "${UV_INSTALL_DIR:-/nonexistent}/uv" "${XDG_BIN_HOME:-/nonexistent}/uv" "$HOME/.local/bin/uv"; do
            [ -x "$uv_bin" ] && break
        done
        [ -x "$uv_bin" ] || return 1
    fi
    "$uv_bin" python install 3.12 --quiet || return 1
    # --system: without it, uv answers with a .venv in the working directory.
    py_path="$("$uv_bin" python find --system 3.12)" || return 1
    [ -n "$py_path" ] && [ -x "$py_path" ] || return 1
    # So 'python3.12' is discoverable system-wide
    sudo -n ln -sf "$py_path" /usr/local/bin/python3.12 2>/dev/null \
        || { mkdir -p "$HOME/.local/bin" && ln -sf "$py_path" "$HOME/.local/bin/python3.12"; } 2>/dev/null \
        || true
    export PATH="$HOME/.local/bin:$PATH"
    command -v python3.12 >/dev/null 2>&1
}
