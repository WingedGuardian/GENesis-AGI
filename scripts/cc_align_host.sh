#!/usr/bin/env bash
#
# cc_align_host.sh — nightly HOST Claude Code / Node.js pin alignment.
#
# WHY: the host VM's CC/Node are re-aligned to the repo pins ONLY when
# scripts/update.sh runs. Between updates, a repo pin bump leaves the host's
# `claude -p` recovery brain (guardian/diagnosis.py — the highest-stakes CC
# call in the system) lagging the pin. This timer closes that window: it re-runs
# the SAME host-alignment logic update.sh uses (cc_align_host_sync in
# scripts/lib/cc_version.sh), nightly, via the guardian gateway.
#
# HOST-ONLY by design: it only issues gateway SSH calls (the host-side
# update-cc/update-node verbs handle their own privilege), so it needs no local
# sudo and is safe under a NoNewPrivileges/ProtectSystem=strict systemd unit.
# The CONTAINER's own CC is already aligned on every update.sh run
# (unconditional), so there is no container leg here.
#
# Non-fatal and idempotent: a guardian-less install is a clean no-op; an
# unreachable host is logged (guardian's own health probe surfaces a down host)
# and exits 0; only a GENUINE heal failure (host reachable but update-cc /
# update-node rejected) exits non-zero so the systemd unit enters `failed` and
# the miss is visible in `systemctl --user status genesis-cc-align.service`.
#
# Invoked by scripts/systemd/genesis-cc-align.{service,timer}.template.

set -u

GENESIS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CC_ENV="$GENESIS_ROOT/scripts/lib/cc_version.sh"
VENV_PY="$GENESIS_ROOT/.venv/bin/python"
GUARDIAN_CONFIG="$HOME/.genesis/guardian_remote.yaml"
SSH_KEY="$HOME/.ssh/genesis_guardian_ed25519"

# ── Single-flight guard: never let two alignment runs issue concurrent
# `update-cc` npm installs at the host prefix (they are not serialized host
# side). Non-blocking — if a run is already in flight, this one is redundant.
LOCKFILE="$HOME/.genesis/locks/cc_align_host.lock"
mkdir -p "$(dirname "$LOCKFILE")" 2>/dev/null || true
if ! exec {LOCK_FD}>"$LOCKFILE"; then
    echo "cc_align_host: cannot open lockfile $LOCKFILE — skipping (non-fatal)"
    exit 0
fi
if ! flock -n "$LOCK_FD"; then
    echo "cc_align_host: another cc_align_host run is in progress — skipping"
    exit 0
fi

# ── Guardian-less install → clean no-op ──
if [ ! -f "$GUARDIAN_CONFIG" ]; then
    echo "cc_align_host: no guardian_remote.yaml — host alignment not applicable (no-op)"
    exit 0
fi
if [ ! -f "$CC_ENV" ]; then
    echo "cc_align_host: $CC_ENV missing — cannot resolve pins (skipping)"
    exit 0
fi
if [ ! -f "$SSH_KEY" ]; then
    echo "cc_align_host: guardian SSH key $SSH_KEY missing — cannot reach host (skipping)"
    exit 0
fi

# ── Resolve the repo pins + the shared aligner. unset first so an inherited
# env override never beats the repo pin (the exact bug cc_version.sh warns
# about); the source defines cc_align_host_sync AND sets CC_VERSION/NODE_MAJOR.
HOST_CC_DEGRADED=""
unset CC_VERSION NODE_MAJOR
# shellcheck source=/dev/null
source "$CC_ENV"

# ── Parse host_ip / host_user (yaml.safe_load for robustness, mirroring
# update.sh). A missing/broken venv python or unparseable config → clean skip.
HOST_IP=""
HOST_USER="ubuntu"
if [ -x "$VENV_PY" ]; then
    HOST_IP=$("$VENV_PY" -c "import yaml, pathlib; print(yaml.safe_load(pathlib.Path('$GUARDIAN_CONFIG').read_text()).get('host_ip', ''))" 2>/dev/null || true)
    HOST_USER=$("$VENV_PY" -c "import yaml, pathlib; print(yaml.safe_load(pathlib.Path('$GUARDIAN_CONFIG').read_text()).get('host_user', 'ubuntu'))" 2>/dev/null || echo "ubuntu")
else
    echo "cc_align_host: venv python ($VENV_PY) unavailable — cannot parse guardian config (skipping)"
    exit 0
fi
if [ -z "$HOST_IP" ]; then
    echo "cc_align_host: host_ip unparseable in $GUARDIAN_CONFIG — skipping (non-fatal)"
    exit 0
fi

# ── Read host state once (deployed CC/Node come from this single response) ──
HOST_VER_RAW="$(ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
    "${HOST_USER}@${HOST_IP}" version 2>/dev/null || true)"

echo "cc_align_host: aligning host ${HOST_USER}@${HOST_IP} to pins (CC=${CC_VERSION:-?}, Node major=${NODE_MAJOR:-?})"
cc_align_host_sync "$HOST_USER" "$HOST_IP" "$SSH_KEY" "$HOST_VER_RAW" || true

# ── Surface the result. A genuine heal FAILURE (host reachable but update-cc /
# update-node rejected → guardian_host_cc/node) exits non-zero so the unit is
# marked failed. A merely-unreachable host is NOT a timer failure (guardian's
# own health probe already alerts on a down host), so it exits 0.
case "$HOST_CC_DEGRADED" in
    *guardian_host_cc*|*guardian_host_node*)
        echo "cc_align_host: WARNING — host alignment FAILED to heal drift ($HOST_CC_DEGRADED); the host recovery brain may lag the pin"
        exit 3
        ;;
    *guardian_host_unreachable*)
        echo "cc_align_host: host unreachable this run ($HOST_CC_DEGRADED) — guardian health covers a down host; nothing to heal"
        exit 0
        ;;
    "")
        echo "cc_align_host: host aligned (or already at pins)"
        exit 0
        ;;
    *)
        echo "cc_align_host: completed with note ($HOST_CC_DEGRADED)"
        exit 0
        ;;
esac
