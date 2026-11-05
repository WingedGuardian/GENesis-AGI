# shellcheck shell=bash
# Sourced by install.sh and bootstrap.sh. Defines functions only.
#
# CI tests Genesis on Python 3.12. pyproject allows newer, and an existing venv
# on one keeps working, but a new venv should get the version that is tested.
# A host whose only python3 is 3.13 or 3.14 may have no 3.12 package to fall
# back on: Ubuntu 26.04 ships 3.14 alone. uv's build needs no package source.

# The uv a host is brought up to, as with Claude Code and litellm: a reviewed version,
# moved by a PR. uv only knows the Python patches released before its own build, so an
# aged pin holds patches back. Bump these together; an update's summary says when
# it has aged past the limit.
GENESIS_UV_VERSION="0.13.7"
GENESIS_UV_PINNED_ON="2026-11-05"
GENESIS_UV_PIN_MAX_AGE_DAYS=120

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
# PATH where it can. Returns non-zero, having changed no package source, when
# there is no such Python to use.
genesis_install_python312_uv() {
    echo "    Trying uv (pre-built Python 3.12 binary — no compilation needed)..."
    local uv_bin installer py_path
    uv_bin="$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")"
    if [ ! -x "$uv_bin" ]; then
        # Download-to-temp before executing: a transfer that drops mid-stream
        # must not run a truncated installer.
        installer="$(mktemp 2>/dev/null)" || return 1
        if curl -LsSf "https://astral.sh/uv/$GENESIS_UV_VERSION/install.sh" -o "$installer"; then
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
    GENESIS_PYTHON312="$py_path"
    # So 'python3.12' is discoverable system-wide
    sudo -n ln -sf "$py_path" /usr/local/bin/python3.12 2>/dev/null \
        || { mkdir -p "$HOME/.local/bin" && ln -sf "$py_path" "$HOME/.local/bin/python3.12"; } 2>/dev/null \
        || true
    export PATH="$HOME/.local/bin:$PATH"
}

# The interpreter to build a venv from. Prefer uv's own path over a link to it:
# a venv built through a link in another directory fails to start on some builds
# (measured on 3.12.10, not 3.12.15).
genesis_python_bin() {
    if [ -n "${GENESIS_PYTHON312:-}" ]; then
        echo "$GENESIS_PYTHON312"
    else
        command -v python3.12 || command -v python3
    fi
}

# Brings uv up to the pin; never down, so a newer uv installed for something else
# stays. A uv that cannot update itself (a package manager's) is reported, not fatal.
_genesis_uv_to_pin() {
    local uv_bin="$1" have
    have="$("$uv_bin" --version 2>/dev/null)"
    have="${have#uv }"
    have="${have%% *}"
    [ -n "$have" ] && [ "$have" != "$GENESIS_UV_VERSION" ] || return 0
    [ "$(printf '%s\n%s\n' "$have" "$GENESIS_UV_VERSION" | sort -V | head -n 1)" = "$have" ] || return 0
    if UV_NO_MODIFY_PATH=1 "$uv_bin" self update "$GENESIS_UV_VERSION" --quiet >/dev/null 2>&1; then
        GENESIS_UV_MOVED="$have to $GENESIS_UV_VERSION"
    else
        GENESIS_UV_BEHIND="$have"
    fi
}

# A package manager patches its own Python; nothing else patches uv's, so the
# bootstrap an update runs calls this. Does nothing unless the venv at $1 runs on a
# uv Python. A patch installs beside the old one, so a process already running keeps
# its interpreter, though its later imports read the new patch's stdlib (measured
# clean, 3.12.10 to 3.12.16). For the caller to report, this sets:
#   GENESIS_UV_PYTHON_MOVED  "<old> to <new>", when the venv's patch changed
#   GENESIS_UV_MOVED         "<old> to <new>", when uv itself changed
#   GENESIS_UV_BEHIND        uv's version, when it is below the pin and stays there
#   GENESIS_UV_PIN_STALE     1, when the pin has aged past GENESIS_UV_PIN_MAX_AGE_DAYS
#   GENESIS_UV_PYTHON_ERROR  uv's last line, when this returns non-zero
genesis_upgrade_uv_python() {
    local venv="$1" uv_bin managed real was minor out target target_real now
    GENESIS_UV_PYTHON_MOVED="" GENESIS_UV_MOVED="" GENESIS_UV_BEHIND="" GENESIS_UV_PIN_STALE="" GENESIS_UV_PYTHON_ERROR=""
    [ -x "$venv/bin/python" ] || return 0
    uv_bin="$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")"
    managed="$("$uv_bin" python dir 2>/dev/null)" || return 0
    # Checked after canonicalising: readlink answers nothing for a missing parent, and
    # an empty directory here would make every path below look managed.
    managed="$(readlink -f "$managed")"
    [ -n "$managed" ] || return 0
    real="$(readlink -f "$venv/bin/python")"
    case "$real" in
        "$managed"/*) ;;
        *) return 0 ;;
    esac
    minor="$("$venv/bin/python" -c "import sys; print('.'.join(map(str, sys.version_info[:2])))")" || return 1
    _genesis_uv_to_pin "$uv_bin"
    if [ "$(( ($(date +%s) - $(date -d "$GENESIS_UV_PINNED_ON" +%s)) / 86400 ))" -gt "$GENESIS_UV_PIN_MAX_AGE_DAYS" ]; then
        GENESIS_UV_PIN_STALE=1
    fi
    if ! out="$("$uv_bin" python upgrade "$minor" 2>&1)"; then
        GENESIS_UV_PYTHON_ERROR="$(printf '%s\n' "$out" | tail -n 1)"
        return 1
    fi
    # --managed-python: a uv.toml can otherwise make this answer with the system's.
    if ! target="$("$uv_bin" python find --managed-python --system "$minor" 2>&1)"; then
        GENESIS_UV_PYTHON_ERROR="$(printf '%s\n' "$target" | tail -n 1)"
        return 1
    fi
    target_real="$(readlink -f "$target")"
    case "$target_real" in
        "$managed"/*) ;;
        *)
            GENESIS_UV_PYTHON_ERROR="uv named a Python it does not manage: $target"
            return 1
            ;;
    esac
    # pyvenv.cfg names the exact patch, and the graph census compares it with what a
    # process runs. Compared on every run, so a repair that failed last time is retried.
    was="$(sed -n 's/^version = //p' "$venv/pyvenv.cfg" 2>/dev/null)"
    if [ "$(readlink -f "$venv/bin/python")" != "$target_real" ] \
        || [ "$(readlink -f "$(sed -n 's/^executable = //p' "$venv/pyvenv.cfg" 2>/dev/null)")" != "$target_real" ]; then
        # A venv built on a versioned path does not follow uv's upgrade, and --upgrade
        # rewrites the record without re-pointing it (measured). So re-point it first.
        if [ "$(readlink -f "$venv/bin/python")" != "$target_real" ]; then
            if [ ! -L "$venv/bin/python$minor" ]; then
                GENESIS_UV_PYTHON_ERROR="$venv/bin/python$minor is not a symlink"
                return 1
            fi
            ln -sfn "$target" "$venv/bin/python$minor" || return 1
        fi
        # --without-pip: otherwise this also moves pip, which a rollback then has to
        # fetch, and an outage is a likely reason for the rollback.
        if ! out="$("$target" -m venv --upgrade --without-pip "$venv" 2>&1)"; then
            GENESIS_UV_PYTHON_ERROR="$(printf '%s\n' "$out" | tail -n 1)"
            return 1
        fi
    fi
    now="$("$venv/bin/python" --version 2>&1)"
    if [ -n "$was" ] && [ "Python $was" != "$now" ]; then
        GENESIS_UV_PYTHON_MOVED="Python $was to $now"
    fi
}
