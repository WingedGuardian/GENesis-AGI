#!/bin/bash
# Genesis collaborative browser — VNC stack setup.
# Creates Xvfb + x11vnc + noVNC systemd units so the user can watch
# and interact with browser_collaborate via noVNC in their browser.
#
# Usage: ./scripts/setup-vnc.sh
# Idempotent — safe to re-run.

set -euo pipefail

# Resolve HOME when unset: stripped-env/systemd/sandbox invocations can leave
# HOME unset, which under `set -u` aborts at the first ${HOME} use. Fall back
# to the passwd entry for the current uid (same source Path.home() uses); fail
# closed if unresolvable. See CC memory sandbox_shell_no_home.
if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
    [ -n "$HOME" ] || { echo "ERROR: HOME is unset and could not be resolved from passwd." >&2; exit 1; }
    export HOME
fi

SCRIPT_DIR="$(unset CDPATH; cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GENESIS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SYSTEMD_DIR="$HOME/.config/systemd/user"
VNC_PASSWD="$HOME/.genesis/vnc_passwd"
BRAIN_IMG="$GENESIS_ROOT/docs/images/genesis-brain.png"

echo "=== Genesis VNC Stack Setup ==="

# ── 1. System packages ────────────────────────────────────
PKGS=(xvfb x11vnc novnc websockify feh xdotool openbox xclip)
MISSING=()
for pkg in "${PKGS[@]}"; do
    if ! dpkg -s "$pkg" &>/dev/null; then
        MISSING+=("$pkg")
    fi
done

if [ ${#MISSING[@]} -gt 0 ]; then
    echo "Installing: ${MISSING[*]}"
    sudo apt-get update -qq
    sudo apt-get install -y -qq "${MISSING[@]}"
else
    echo "All packages already installed."
fi

# ── 2. VNC password ───────────────────────────────────────
mkdir -p "$HOME/.genesis"
if [ ! -f "$VNC_PASSWD" ]; then
    echo "Creating VNC password (default: genesis)..."
    x11vnc -storepasswd genesis "$VNC_PASSWD"
    echo "Change later with: x11vnc -storepasswd <password> $VNC_PASSWD"
else
    echo "VNC password already exists at $VNC_PASSWD"
fi

# ── 3. Systemd units ─────────────────────────────────────
mkdir -p "$SYSTEMD_DIR"

# The three units are templates under scripts/systemd/vnc/: Xvfb (virtual display
# :99 with openbox), x11vnc (VNC server on :99), noVNC (browser client). Each is
# written only when the installed unit is Genesis's own render: one edited by hand
# is kept and named (lib/managed_units.sh); local changes belong in a
# <unit>.d/*.conf drop-in. The templates hold no comments on purpose: their text
# equals what this script used to write, so existing installs read as unedited.
#
# x11vnc flags (genesis-vnc.service.template):
#   -xdamage   — use X DAMAGE extension to only send changed regions (was -noxdamage)
#   -threads   — multi-threaded encoding for better FPS
#   NOTE: -ncache was removed — it creates a hidden pixel cache below the
#   visible screen that makes the framebuffer ~11x taller, causing noVNC
#   local scaling to produce a stretched/distorted view.
#   xclip required for clipboard sync (installed above)
# shellcheck source=lib/managed_units.sh
. "$SCRIPT_DIR/lib/managed_units.sh"
# shellcheck disable=SC2034  # read by genesis_install_managed_unit
GENESIS_MU_PREV="$(genesis_mu_prev_from_state)"
mkdir -p "$HOME/tmp"
_mu_dir="$(mktemp -d -p "$HOME/tmp" setup-vnc-units.XXXXXX)"
genesis_mu_precheck "$SYSTEMD_DIR" "$_mu_dir"
_brain_esc=$(printf '%s' "$BRAIN_IMG" | sed -e 's/[\\&|]/\\&/g')
for _vnc_unit in genesis-xvfb.service genesis-vnc.service genesis-novnc.service; do
    sed -e "s|__BRAIN_IMG__|$_brain_esc|g" "$GENESIS_ROOT/scripts/systemd/vnc/$_vnc_unit.template" > "$_mu_dir/render"
    genesis_install_managed_unit "scripts/systemd/vnc/$_vnc_unit.template" "$_mu_dir/render" "$SYSTEMD_DIR/$_vnc_unit"
done
rm -rf "$_mu_dir"

echo "Systemd units written to $SYSTEMD_DIR"

# ── 3b. Deploy scaled noVNC viewer ────────────────────────
# vnc_scaled.html forces scaleViewport=true so the display fits the browser
# window without clipping. Stock vnc.html defaults to no scaling (off-center
# logo, bottom-right cut off on displays wider than the browser window).
sudo cp "$SCRIPT_DIR/vnc_scaled.html" /usr/share/novnc/vnc_scaled.html
echo "✓ Deployed scripts/vnc_scaled.html → /usr/share/novnc/vnc_scaled.html"

# ── 4. Enable and start ──────────────────────────────────
# Ensure user session bus is available
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"

systemctl --user daemon-reload
systemctl --user enable --now genesis-xvfb genesis-vnc genesis-novnc

echo ""

# ── 5. Verify ─────────────────────────────────────────────
sleep 2
OK=true

if DISPLAY=:99 xdpyinfo &>/dev/null; then
    echo "✓ Xvfb display :99 is alive"
else
    echo "✗ Xvfb display :99 not responding"
    OK=false
fi

if ss -tlnp | grep -q ':5999'; then
    echo "✓ VNC server listening on port 5999"
else
    echo "✗ VNC server not listening on port 5999"
    OK=false
fi

if ss -tlnp | grep -q ':6080'; then
    echo "✓ noVNC listening on port 6080"
else
    echo "✗ noVNC not listening on port 6080"
    OK=false
fi

echo ""
if $OK; then
    # Try to get Tailscale IP for the access URL
    TS_IP=$(tailscale ip -4 2>/dev/null || echo "")
    if [ -n "$TS_IP" ]; then
        echo "Access noVNC at: http://$TS_IP:6080/vnc_scaled.html"
    else
        echo "Access noVNC at: http://localhost:6080/vnc_scaled.html"
    fi
    echo "VNC password: whatever you set (default: genesis)"
    echo ""
    echo "=== VNC stack ready ==="
else
    echo "=== Setup completed with errors — check journalctl --user -u genesis-xvfb ==="
    exit 1
fi
