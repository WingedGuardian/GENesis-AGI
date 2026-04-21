#!/bin/bash
# Genesis collaborative browser — VNC stack setup.
# Creates Xvfb + x11vnc + noVNC systemd units so the user can watch
# and interact with browser_collaborate via noVNC in their browser.
#
# Usage: ./scripts/setup-vnc.sh
# Idempotent — safe to re-run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GENESIS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SYSTEMD_DIR="$HOME/.config/systemd/user"
VNC_PASSWD="$HOME/.genesis/vnc_passwd"
BRAIN_IMG="$GENESIS_ROOT/docs/images/genesis-brain.png"

echo "=== Genesis VNC Stack Setup ==="

# ── 1. System packages ────────────────────────────────────
PKGS=(xvfb x11vnc novnc websockify feh xdotool)
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

# Xvfb — virtual display :99
cat > "$SYSTEMD_DIR/genesis-xvfb.service" << EOF
[Unit]
Description=Genesis Virtual Display (Xvfb)
After=default.target

[Service]
Type=simple
ExecStart=/usr/bin/Xvfb :99 -screen 0 1920x1080x24 -ac
ExecStartPost=/bin/bash -c 'sleep 1 && HOME=%h DISPLAY=:99 /usr/bin/feh --bg-center $BRAIN_IMG'
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

# x11vnc — VNC server on display :99
cat > "$SYSTEMD_DIR/genesis-vnc.service" << EOF
[Unit]
Description=Genesis VNC Server (x11vnc)
After=genesis-xvfb.service
Requires=genesis-xvfb.service

[Service]
Type=simple
Environment=DISPLAY=:99
ExecStart=/usr/bin/x11vnc -display :99 -forever -shared -rfbauth %h/.genesis/vnc_passwd -rfbport 5900 -noxdamage
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

# noVNC — browser-based VNC client
cat > "$SYSTEMD_DIR/genesis-novnc.service" << EOF
[Unit]
Description=Genesis noVNC (browser-based VNC client)
After=genesis-vnc.service
Requires=genesis-vnc.service

[Service]
Type=simple
ExecStart=/usr/bin/websockify --web=/usr/share/novnc/ 6080 localhost:5900
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

echo "Systemd units written to $SYSTEMD_DIR"

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

if ss -tlnp | grep -q ':5900'; then
    echo "✓ VNC server listening on port 5900"
else
    echo "✗ VNC server not listening on port 5900"
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
        echo "Access noVNC at: http://$TS_IP:6080/vnc.html"
    else
        echo "Access noVNC at: http://localhost:6080/vnc.html"
    fi
    echo "VNC password: whatever you set (default: genesis)"
    echo ""
    echo "=== VNC stack ready ==="
else
    echo "=== Setup completed with errors — check journalctl --user -u genesis-xvfb ==="
    exit 1
fi
