#!/usr/bin/env bash
#
# cc_tmp_align_host.sh — opportunistic cc-tmp blast-radius isolation via the host.
#
# WHY: ~/.genesis/cc-tmp is the TMPDIR for every CC session + genesis-server. The
# remediation that moves it onto a dedicated, size-capped incus volume
# (scripts/lib/cc_tmp_volume.sh::cc_tmp_volume_apply) MUST run host-side (needs
# incus) and deliberately SKIPS while any CC session is live (attaching over
# cc-tmp would shadow open temp files). Today it only fires from the guardian
# `redeploy` verb — which lands whenever an update happens, almost never at a
# CC-quiet moment — so on a busy install the apply skips forever and cc-tmp stays
# on the rootfs. This entrypoint fires the host-side apply OPPORTUNISTICALLY from
# the container: at container cold-start (before genesis-server + interactive
# sessions attach — the one deterministically quiet window) and periodically via
# a timer (applies only if the host observes CC quiet). It NEVER forces: the
# host-side guard still skips if a session is live, and this simply records the
# outcome so the awareness posture can distinguish "blocked on a live CC session"
# (converging) from a terminal failure.
#
# HOST-ONLY reach by design: it only issues a single gateway SSH verb
# (`cc-tmp-apply`), which runs the apply with the host's own incus + privilege —
# so it needs no local sudo and is safe under NoNewPrivileges/ProtectSystem=strict.
#
# Non-fatal + idempotent: a guardian-less install is a clean no-op; an
# unreachable host or an old gateway (no `cc-tmp-apply` verb) records an honest
# marker and exits 0. The apply itself is idempotent (already-isolated → no-op).
# Always exits 0 — the durable signal is the marker + the awareness posture nag,
# not this unit's status; a persistent gap surfaces as an infra_profile fact.
#
# Invoked by scripts/systemd/genesis-cc-tmp-align.{service,timer}.template.

set -u

GENESIS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$GENESIS_ROOT/.venv/bin/python"
GUARDIAN_CONFIG="$HOME/.genesis/guardian_remote.yaml"
SSH_KEY="$HOME/.ssh/genesis_guardian_ed25519"
MARKER="$HOME/.genesis/state/cc_tmp_apply.json"

# ── Single-flight guard: never let two runs issue concurrent host applies. ──
LOCKFILE="$HOME/.genesis/locks/cc_tmp_align_host.lock"
mkdir -p "$(dirname "$LOCKFILE")" 2>/dev/null || true
if ! exec {LOCK_FD}>"$LOCKFILE"; then
    echo "cc_tmp_align_host: cannot open lockfile $LOCKFILE — skipping (non-fatal)"
    exit 0
fi
if ! flock -n "$LOCK_FD"; then
    echo "cc_tmp_align_host: another run is in progress — skipping"
    exit 0
fi

# ── Guardian-less install → clean no-op (no host plane to reach). ──
if [ ! -f "$GUARDIAN_CONFIG" ]; then
    echo "cc_tmp_align_host: no guardian_remote.yaml — host apply not applicable (no-op)"
    exit 0
fi
if [ ! -f "$SSH_KEY" ]; then
    echo "cc_tmp_align_host: guardian SSH key $SSH_KEY missing — cannot reach host (skipping)"
    exit 0
fi
if [ ! -x "$VENV_PY" ]; then
    echo "cc_tmp_align_host: venv python ($VENV_PY) unavailable — cannot parse guardian config (skipping)"
    exit 0
fi

# ── Parse host_ip / host_user (yaml.safe_load, mirroring cc_align_host.sh). ──
HOST_IP="$("$VENV_PY" -c "import yaml, pathlib; print(yaml.safe_load(pathlib.Path('$GUARDIAN_CONFIG').read_text()).get('host_ip', ''))" 2>/dev/null || true)"
HOST_USER="$("$VENV_PY" -c "import yaml, pathlib; print(yaml.safe_load(pathlib.Path('$GUARDIAN_CONFIG').read_text()).get('host_user', 'ubuntu'))" 2>/dev/null || echo "ubuntu")"
if [ -z "$HOST_IP" ]; then
    echo "cc_tmp_align_host: host_ip unparseable in $GUARDIAN_CONFIG — skipping (non-fatal)"
    exit 0
fi

# ── Issue the host-side apply verb. `timeout` bounds a hung incus op on the
# host (fast in practice; 90s is generous). Unreachable/old-gateway → empty or
# a non-JSON `denied`, both handled by the marker writer below. ──
RESP="$(timeout 90 ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
    "${HOST_USER}@${HOST_IP}" cc-tmp-apply 2>/dev/null || true)"

# ── Record the outcome marker (robust to non-JSON / old gateway / unreachable).
# Read by infra_profile's storage collector so the awareness posture nag can
# distinguish blocked-on-live-CC (converging) from a terminal skip. ──
mkdir -p "$(dirname "$MARKER")" 2>/dev/null || true
"$VENV_PY" - "$MARKER" "$RESP" <<'PY'
import datetime
import json
import os
import sys
import tempfile

marker = sys.argv[1]
resp = sys.argv[2] if len(sys.argv) > 2 else ""
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
try:
    d = json.loads(resp)
    if not isinstance(d, dict):
        raise ValueError("not an object")
    if d.get("ok") is False:
        reason = "gateway-error"
        applied = False
        procs = None
    else:
        reason = d.get("result") or "unknown"
        applied = bool(d.get("applied"))
        procs = d.get("claude_procs")
except Exception:
    # empty stdout (host unreachable) or non-JSON "denied" (gateway predates the
    # cc-tmp-apply verb) — an honest "could not reach the apply this run".
    reason = "unreachable-or-old-gateway"
    applied = False
    procs = None

out = {
    "last_attempt_at": now,
    "last_reason": reason,
    "claude_procs": procs,
    "applied": applied,
}
mdir = os.path.dirname(marker)
fd, tmp = tempfile.mkstemp(dir=mdir, prefix=".cc_tmp_apply.", suffix=".json")
try:
    with os.fdopen(fd, "w") as f:
        json.dump(out, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, marker)
except Exception:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
print(f"cc_tmp_align_host: result={reason} applied={applied} procs={procs}")
PY

exit 0
