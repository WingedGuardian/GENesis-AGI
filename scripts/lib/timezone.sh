#!/bin/bash
# Timezone helpers shared by install.sh and host-setup.sh.
#
# The install's one timezone setting is `timezone` in ~/.genesis/config/genesis.yaml
# (the dashboard's Timezone control writes it; genesis.env.user_timezone() reads it
# first). These helpers let the installers read that value and set a system clock
# without ever reporting a change that did not happen.

# genesis_config_timezone <genesis.yaml path>
#   Prints the file's top-level `timezone:` value, unquoted, when it is a real
#   choice. Prints nothing when the file or key is missing, the value is empty, or
#   it is UTC / Etc/UTC: the shipped example carries `timezone: "UTC"` as a
#   placeholder, so UTC there cannot be told apart from "never chosen" (migration
#   0086 treats it the same way).
genesis_config_timezone() {
    local cfg="$1" line value
    [ -f "$cfg" ] || return 0
    line="$(grep -m1 -E '^timezone:' "$cfg" 2>/dev/null)" || return 0
    value="${line#timezone:}"
    value="${value%%#*}"                    # drop a trailing comment
    value="$(printf '%s' "$value" | tr -d "\"'[:space:]")"
    case "$value" in
        ""|UTC|Etc/UTC) return 0 ;;
        /*|*..*) return 0 ;;                # never a path outside the zone database
    esac
    # Only a zone this system can load. YAML null/booleans and typos fall through to
    # the next source, as genesis.env.user_timezone() rejects them at runtime too.
    [ -f "/usr/share/zoneinfo/$value" ] || return 0
    printf '%s\n' "$value"
}

# apply_system_timezone <zone> <label> [command prefix ...]
#   Sets the clock with `timedatectl set-timezone` (run through the optional
#   prefix, e.g. `sudo` or `incus exec <name> --`). Prints "+ <label> timezone:
#   <zone>" only when the clock really reads <zone> afterwards, otherwise a
#   WARNING naming the manual command. Returns 0 on success, 1 otherwise. Safe
#   under `set -euo pipefail` whether or not the caller guards the return code.
apply_system_timezone() {
    local zone="$1" label="$2"
    shift 2
    local now=""
    if "$@" timedatectl set-timezone "$zone" 2>/dev/null; then
        now="$("$@" timedatectl show -p Timezone --value 2>/dev/null || true)"
        if [ "$now" = "$zone" ]; then
            echo "    + $label timezone: $zone"
            return 0
        fi
        echo "    WARNING: the $label clock reads '${now:-unknown}' after setting $zone." \
            "Check it with: ${*:+$* }timedatectl show -p Timezone --value"
        return 1
    fi
    echo "    WARNING: could not set the $label timezone to $zone." \
        "Set it manually: ${*:+$* }timedatectl set-timezone $zone"
    return 1
}
