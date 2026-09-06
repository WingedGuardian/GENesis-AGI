#!/usr/bin/env bash
# Capture a Windows desktop Genesis can reach over SSH, and bring the image back.
#
# GROUNDWORK. Deliberately a shell script and NOT an MCP tool: an MCP tool's
# only real advantage is that Genesis can call it autonomously, which is the
# one property this capability should not have yet. A foreground session with
# a human present runs this via Bash; nothing else can fire it.
#
# The remote half is scripts/genesis-capture.ps1, which must be installed on
# the target once (`genesis-capture.ps1 -Install`). Why a scheduled task rather
# than a direct SSH command, and why a failed capture still writes a valid
# black PNG: docs/reference/windows-remote-execution.md
#
# Device details (host, user, key) are INSTALL-SPECIFIC and never live here.
# They come from ~/.genesis/config/genesis.yaml or the env overrides below.
#
# Usage:
#   scripts/capture_desktop.sh                    # whole desktop
#   scripts/capture_desktop.sh --window "Notepad" # one window
#   scripts/capture_desktop.sh --out ~/tmp/shots  # where to land it locally
set -euo pipefail

MODE="screen"; MATCH=""; OUTDIR="${HOME}/tmp/genesis-captures"; TIMEOUT=60
while [[ $# -gt 0 ]]; do
  case "$1" in
    --window) MODE="window"; MATCH="${2:?--window needs a title substring}"; shift 2 ;;
    --out)    OUTDIR="${2:?--out needs a path}"; shift 2 ;;
    --timeout) TIMEOUT="${2:?--timeout needs seconds}"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

CFG="${HOME}/.genesis/config/genesis.yaml"
HOST="${GENESIS_DESKTOP_HOST:-}"
USER_="${GENESIS_DESKTOP_USER:-}"

if [[ -z "$HOST" || -z "$USER_" ]] && [[ -f "$CFG" ]]; then
  # Read desktop.host / desktop.user without a YAML dependency.
  HOST="${HOST:-$(sed -n '/^desktop:/,/^[^ ]/p' "$CFG" | sed -n 's/^[[:space:]]*host:[[:space:]]*//p' | tr -d '"'"'"' ' | head -1)}"
  USER_="${USER_:-$(sed -n '/^desktop:/,/^[^ ]/p' "$CFG" | sed -n 's/^[[:space:]]*user:[[:space:]]*//p' | tr -d '"'"'"' ' | head -1)}"
fi

if [[ -z "$HOST" || -z "$USER_" ]]; then
  cat >&2 <<'MSG'
NOT CONFIGURED: no desktop target.

Set GENESIS_DESKTOP_HOST and GENESIS_DESKTOP_USER, or add to
~/.genesis/config/genesis.yaml:

  desktop:
    host: <hostname-or-ip-of-your-windows-machine>
    user: <windows-username>

The machine also needs scripts/genesis-capture.ps1 installed once:
  powershell.exe -ExecutionPolicy Bypass -File genesis-capture.ps1 -Install
MSG
  exit 3
fi

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 "${USER_}@${HOST}")
mkdir -p "$OUTDIR"

remote_latest() {
  "${SSH[@]}" "powershell.exe -NoProfile -Command \"\$d=Join-Path \$env:USERPROFILE 'Pictures\\GenesisCaptures'; \$f=Get-ChildItem \$d -Filter cap-*.png -ErrorAction Ignore | Sort-Object LastWriteTime -Descending | Select-Object -First 1; if(\$f){\$f.Name}\"" 2>/dev/null | tr -d '\r\n' || true
}

# Record what already exists BEFORE triggering. Without this the poll below
# returns the first capture it finds — which on any machine that has ever run
# a capture is a STALE one, reported as a fresh success. Measured: a run after
# the DPI fix returned a 30-minute-old file and its old resolution, which read
# as "the fix changed nothing".
PRIOR=$(remote_latest)

# Trigger. The task returns immediately and its body runs asynchronously in the
# interactive session, so we wait on the OUTPUT, not on this call.
if ! "${SSH[@]}" "powershell.exe -NoProfile -Command \"Start-ScheduledTask -TaskName GenesisCapture\"" >/dev/null 2>&1; then
  echo "FAILED to trigger GenesisCapture on ${HOST} (is it installed? -Install)" >&2
  exit 4
fi

# Poll for a capture that is genuinely NEW — different from what was there
# before the trigger. Condition-based, not a fixed sleep.
LATEST=""
deadline=$(( $(date -u +%s) + TIMEOUT ))
while [[ $(date -u +%s) -lt $deadline ]]; do
  CANDIDATE=$(remote_latest)
  if [[ -n "$CANDIDATE" && "$CANDIDATE" != "$PRIOR" ]]; then LATEST="$CANDIDATE"; break; fi
  sleep 1
done

if [[ -z "$LATEST" ]]; then
  # Distinguish "nothing ran" from "something ran but produced nothing new",
  # because they need different fixes and look identical from here.
  if [[ -n "$PRIOR" ]]; then
    echo "TIMEOUT: no NEW capture within ${TIMEOUT}s (newest is still ${PRIOR}) — the task did not produce one" >&2
  else
    echo "TIMEOUT: no capture appeared within ${TIMEOUT}s (none existed before either)" >&2
  fi
  exit 5
fi

STEM="${LATEST%.png}"
"${SSH[@]}" "powershell.exe -NoProfile -Command \"Get-Content (Join-Path \$env:USERPROFILE 'Pictures\\GenesisCaptures\\${STEM}.txt')\"" 2>/dev/null | tr -d '\r' > "${OUTDIR}/${STEM}.txt" || true
# Ask the machine where its profile actually is. A Windows profile directory
# is NOT reliably C:\Users\<username> — it can be renamed, redirected, or on
# another drive, and assuming otherwise is exactly the install-specific
# guess this script exists to avoid.
REMOTE_PROFILE=$("${SSH[@]}" "powershell.exe -NoProfile -Command \"\$env:USERPROFILE\"" 2>/dev/null | tr -d '\r\n')
if [[ -z "$REMOTE_PROFILE" ]]; then
  echo "FAILED to resolve the remote user profile directory" >&2; exit 6
fi
REMOTE_PATH="${REMOTE_PROFILE//\\//}/Pictures/GenesisCaptures/${LATEST}"
scp -q -o BatchMode=yes "${USER_}@${HOST}:${REMOTE_PATH}" "${OUTDIR}/" 2>/dev/null || {
  echo "FAILED to retrieve ${REMOTE_PATH}" >&2; exit 6; }

# The remote script self-reports; a status other than OK means the image on
# disk is not a real capture even though the file exists and opens fine.
STATUS=$(sed -n 's/^status=//p' "${OUTDIR}/${STEM}.txt" 2>/dev/null | head -1)
if [[ "$STATUS" != "OK" ]]; then
  echo "CAPTURE NOT USABLE (status=${STATUS:-unknown}) — see ${OUTDIR}/${STEM}.txt" >&2
  exit 7
fi

echo "OK ${OUTDIR}/${LATEST}"
sed -n 's/^\(session_id\|rect\|distinct_colors\|window_title\)=/  \1: /p' "${OUTDIR}/${STEM}.txt"
