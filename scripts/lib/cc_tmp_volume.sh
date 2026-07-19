# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
#
# cc_tmp_volume_apply — isolate the container's Claude Code scratch dir
# (~/.genesis/cc-tmp) onto a DEDICATED incus custom storage volume.
#
# WHY: cc-tmp is the TMPDIR for every CC session and for genesis-server. Today
# it shares the container root filesystem with everything else, which is a
# two-way hazard: a runaway temp write can fill the root disk (killing every CC
# session), and any other rootfs writer can starve cc-tmp below the watchgod's
# ~150 MiB "sacred ground" and kill sessions from the other direction. Moving
# cc-tmp onto its own size-capped volume closes both: a runaway fills only its
# own volume (the rootfs never moves), and nothing else can crowd it out.
#
# Spike-proven on the real host (2026-07-18, incus 6.0): a fresh custom volume
# mounts owned by container-root; a `chown` from container-root makes it
# writable by the container user with the correct unprivileged host mapping —
# so `security.shifted` (privesc advisory GHSA-56mx-8g9f-5crf) is NOT needed.
# The disk device hot-plugs onto a running container with no restart, and
# filling the volume to ENOSPC left the container rootfs `df` byte-identical.
#
# Headroom: the volume is THIN (an empty create costs ~zero physical pool
# space) and its SIZE caps the worst case (a full runaway adds at most
# CCTMPVOL_SIZE_GIB to the pool). Physical pool exhaustion is already tiered-
# alerted by the guardian's measure_storage_pool — so no create-time headroom
# guard is needed here (an over-commit reading from `incus storage info` would
# be the wrong signal on a thin pool anyway).
#
# Degrades gracefully to a one-line skip (rc=0, never aborts the caller under
# `set -e`): no incus, container not running, a live CC session (attaching over
# cc-tmp would shadow that session's open temp files — converges on a later
# apply when quiet), already attached (idempotent), or a write-verify failure
# (rolls the device back — an attached-but-UNWRITABLE cc-tmp would brick every
# session, strictly worse than not-yet-isolated). We deliberately do NOT delete
# the pre-split contents: they get harmlessly shadowed under the mount (tiny,
# one-time) rather than risk yanking an in-flight genesis-server temp file.
#
# Sourced by scripts/host-setup.sh (fresh installs + retrofit on re-run) and by
# the guardian-gateway.sh `redeploy` verb (existing installs retrofit on their
# next update, when no CC session is live).
#
# Test seams (defaults are the real system; pytest overrides these and stubs
# `incus` on PATH):
CCTMPVOL_DISABLE="${CCTMPVOL_DISABLE:-0}"
CCTMPVOL_SIZE_GIB="${CCTMPVOL_SIZE_GIB:-2}"      # floor 1; 4x the 500 MiB watchgod du-budget
CCTMPVOL_CONTAINER="${CCTMPVOL_CONTAINER:-}"     # "" → guardian.yaml container_name, then "genesis"
CCTMPVOL_USER="${CCTMPVOL_USER:-ubuntu}"         # in-container owner of cc-tmp
CCTMPVOL_DEVICE_NAME="${CCTMPVOL_DEVICE_NAME:-cc-tmp}"
CCTMPVOL_VOLUME_NAME="${CCTMPVOL_VOLUME_NAME:-}" # "" → <container>-cc-tmp (collision-safe per host)
CCTMPVOL_POOL="${CCTMPVOL_POOL:-}"               # "" → the root device's pool
CCTMPVOL_MOUNT_PATH="${CCTMPVOL_MOUNT_PATH:-}"   # "" → <container home>/.genesis/cc-tmp
CCTMPVOL_GUARDIAN_YAML="${CCTMPVOL_GUARDIAN_YAML:-$HOME/.local/state/genesis-guardian/guardian.yaml}"

# _cctmpvol_container — resolve the target container: explicit seam, else the
# container_name from guardian.yaml, else the universal "genesis" default
# (matches install_guardian.sh / guardian config).
_cctmpvol_container() {
    if [[ -n "$CCTMPVOL_CONTAINER" ]]; then
        echo "$CCTMPVOL_CONTAINER"
        return 0
    fi
    local name=""
    if [[ -f "$CCTMPVOL_GUARDIAN_YAML" ]]; then
        name="$(awk -F':' '/^container_name:/ {sub(/^[^:]*:/,""); gsub(/[" '"'"']/,""); print; exit}' \
            "$CCTMPVOL_GUARDIAN_YAML" 2>/dev/null)" || name=""
    fi
    echo "${name:-genesis}"
}

# _cctmpvol_pool <container> — the pool backing the container's root device
# (so the cc-tmp volume lives in the same pool), else the first storage pool.
_cctmpvol_pool() {
    local c="$1" pool=""
    if [[ -n "$CCTMPVOL_POOL" ]]; then
        echo "$CCTMPVOL_POOL"
        return 0
    fi
    pool="$(incus config device get "$c" root pool 2>/dev/null)" || pool=""
    if [[ -z "$pool" ]]; then
        pool="$(incus storage list -f csv 2>/dev/null | head -1 | cut -d, -f1)" || pool=""
    fi
    echo "$pool"
}

# _cctmpvol_volume_name <container> — <container>-cc-tmp unless overridden.
_cctmpvol_volume_name() {
    if [[ -n "$CCTMPVOL_VOLUME_NAME" ]]; then
        echo "$CCTMPVOL_VOLUME_NAME"
    else
        echo "${1}-cc-tmp"
    fi
}

# _cctmpvol_size_gib — integer >= 1 (a bad value floors to the 2 GiB default).
_cctmpvol_size_gib() {
    local n="$CCTMPVOL_SIZE_GIB"
    if [[ ! "$n" =~ ^[0-9]+$ || "$n" -lt 1 ]]; then
        n=2
    fi
    echo "$n"
}

# _cctmpvol_mount_path <container> — the in-container cc-tmp path. Resolves the
# container user's home via getent (config-overridable via the seam).
_cctmpvol_mount_path() {
    local c="$1" home=""
    if [[ -n "$CCTMPVOL_MOUNT_PATH" ]]; then
        echo "$CCTMPVOL_MOUNT_PATH"
        return 0
    fi
    home="$(incus exec "$c" -- getent passwd "$CCTMPVOL_USER" 2>/dev/null | cut -d: -f6)" || home=""
    echo "${home:-/home/$CCTMPVOL_USER}/.genesis/cc-tmp"
}

# cc_tmp_volume_apply — the entrypoint. Always returns 0.
cc_tmp_volume_apply() {
    echo "--- cc-tmp dedicated volume (blast-radius isolation) ---"

    if [[ "$CCTMPVOL_DISABLE" == "1" ]]; then
        echo "  Skipped: CCTMPVOL_DISABLE=1."
        return 0
    fi
    if ! command -v incus >/dev/null 2>&1; then
        echo "  Skipped: incus not available (not a guardian host vantage)."
        return 0
    fi

    local c dev pool vol size path
    c="$(_cctmpvol_container)"
    dev="$CCTMPVOL_DEVICE_NAME"

    if ! incus info "$c" >/dev/null 2>&1; then
        echo "  Skipped: container '$c' not found."
        return 0
    fi
    if [[ "$(incus info "$c" 2>/dev/null | awk -F': *' '/^Status:/ {print tolower($2); exit}')" != "running" ]]; then
        echo "  Skipped: container '$c' is not running."
        return 0
    fi

    # Idempotency: the device already exists → cc-tmp is already isolated.
    if incus config device get "$c" "$dev" source >/dev/null 2>&1; then
        echo "  cc-tmp already on a dedicated volume (device '$dev' on '$c')."
        return 0
    fi

    # Live-CC guard: attaching over cc-tmp shadows a running session's open
    # temp files. Skip; a later apply (redeploy, or a deliberate quiet-moment
    # run) converges. Do NOT broaden to genesis-server — that would make the
    # apply permanently unreachable on a live install; its cc-tmp temp files
    # are short-lived and the pre-split contents are shadowed, not deleted.
    if incus exec "$c" -- pgrep -x claude >/dev/null 2>&1; then
        echo "  Skipped: a Claude Code session is live in '$c' — attach would shadow its"
        echo "           open temp files. Converges on a later apply during a quiet window."
        return 0
    fi

    pool="$(_cctmpvol_pool "$c")"
    if [[ -z "$pool" ]]; then
        echo "  Skipped: could not resolve a storage pool for '$c'."
        return 0
    fi

    # The isolation AND the size cap only hold on a backend that both puts the
    # volume on its own device/dataset and enforces a per-volume quota. A `dir`
    # pool places the custom volume on the SAME host filesystem as the container
    # root and only quotas with fs-level project quotas — so a runaway fill
    # could still reach the rootfs and the cap would be cosmetic. Only apply on
    # block/CoW backends where the guarantee is real; skip (honestly) otherwise.
    local driver
    driver="$(incus storage show "$pool" 2>/dev/null | awk -F': *' '/^driver:/ {print $2; exit}')"
    case "$driver" in
        lvm | zfs | btrfs | ceph) : ;;
        *)
            echo "  Skipped: pool '$pool' (driver='${driver:-unknown}') does not enforce a"
            echo "           per-volume size cap on its own device — cc-tmp isolation would be"
            echo "           cosmetic. A block/CoW-backed pool (lvm/zfs/btrfs/ceph) is required."
            return 0 ;;
    esac

    vol="$(_cctmpvol_volume_name "$c")"
    size="$(_cctmpvol_size_gib)"
    path="$(_cctmpvol_mount_path "$c")"

    # Create the volume if absent (thin — an empty create costs ~zero pool).
    if ! incus storage volume show "$pool" "$vol" >/dev/null 2>&1; then
        if ! incus storage volume create "$pool" "$vol" "size=${size}GiB" >/dev/null 2>&1; then
            echo "  WARNING: could not create storage volume '$pool/$vol' — cc-tmp NOT isolated."
            return 0
        fi
        echo "  + created storage volume '$pool/$vol' (${size}GiB)."
    fi

    # Attach (hot-plug, no restart — spike-proven). Leave the volume on failure
    # so a later apply can retry without re-creating it.
    if ! incus config device add "$c" "$dev" disk \
        pool="$pool" source="$vol" path="$path" >/dev/null 2>&1; then
        echo "  WARNING: could not attach volume at '$path' — cc-tmp NOT isolated (volume kept for retry)."
        return 0
    fi

    # IO-limit parity: a temp storm on cc-tmp must not bypass the host IO caps
    # the root device carries. Mirror root's limits (each key set separately —
    # the two-pair form errors 'Invalid key=value configuration', spike 2026-07-18).
    local rlim wlim
    rlim="$(incus config device get "$c" root limits.read 2>/dev/null)" || rlim=""
    wlim="$(incus config device get "$c" root limits.write 2>/dev/null)" || wlim=""
    [[ -n "$rlim" ]] && incus config device set "$c" "$dev" limits.read "$rlim" 2>/dev/null || true
    [[ -n "$wlim" ]] && incus config device set "$c" "$dev" limits.write "$wlim" 2>/dev/null || true

    # Make it writable by the container user (spike: fresh volume mounts owned
    # by container-root; chown from container-root maps to the correct
    # unprivileged host uid). Then VERIFY a real uid write, and ROLL BACK the
    # device on failure — an attached-but-unwritable cc-tmp bricks every CC
    # session, which is worse than staying un-isolated.
    incus exec "$c" -- chown "$CCTMPVOL_USER:$CCTMPVOL_USER" "$path" 2>/dev/null || true
    incus exec "$c" -- chmod 700 "$path" 2>/dev/null || true
    local uid gid
    uid="$(incus exec "$c" -- id -u "$CCTMPVOL_USER" 2>/dev/null)" || uid=""
    gid="$(incus exec "$c" -- id -g "$CCTMPVOL_USER" 2>/dev/null)" || gid=""
    if [[ -n "$uid" && -n "$gid" ]] \
        && incus exec "$c" --user "$uid" --group "$gid" -- \
            sh -c "touch '$path/.cctmpvol-verify' && rm -f '$path/.cctmpvol-verify'" 2>/dev/null; then
        echo "  + cc-tmp now on dedicated volume '$pool/$vol' (${size}GiB) at $path on '$c'."
    else
        incus config device remove "$c" "$dev" >/dev/null 2>&1 || true
        echo "  WARNING: volume attached but not writable by $CCTMPVOL_USER — rolled back the device."
        echo "           cc-tmp stays on the rootfs (un-isolated). Check container idmap:"
        echo "           incus config device add $c $dev disk pool=$pool source=$vol path=$path (then chown)."
    fi
    return 0
}

# cc_tmp_volume_remove — operator rollback / clean opt-out. Detaches the device
# and deletes the volume (cc-tmp reverts to the rootfs). Always returns 0.
cc_tmp_volume_remove() {
    echo "--- Removing cc-tmp dedicated volume ---"
    if ! command -v incus >/dev/null 2>&1; then
        echo "  Skipped: incus not available."
        return 0
    fi
    local c dev pool vol
    c="$(_cctmpvol_container)"
    dev="$CCTMPVOL_DEVICE_NAME"
    if ! incus info "$c" >/dev/null 2>&1; then
        echo "  Skipped: container '$c' not found."
        return 0
    fi
    # Removing the device yanks the filesystem from under any open fd — refuse
    # while a CC session is live.
    if incus exec "$c" -- pgrep -x claude >/dev/null 2>&1; then
        echo "  Skipped: a Claude Code session is live in '$c' — remove during a quiet window."
        return 0
    fi
    pool="$(_cctmpvol_pool "$c")"
    vol="$(_cctmpvol_volume_name "$c")"
    incus config device remove "$c" "$dev" >/dev/null 2>&1 || true
    incus storage volume delete "$pool" "$vol" >/dev/null 2>&1 || true
    echo "  + cc-tmp reverted to the container rootfs (device + volume removed)."
    return 0
}
