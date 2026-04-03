#!/usr/bin/env bash
# vendor_assets.sh — Copy static assets for Genesis standalone dashboard.
#
# Copies from Agent Zero's webui/ if available, otherwise prints instructions.
# Idempotent: skips if assets already present.
#
# Usage: bash scripts/vendor_assets.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
WEBUI_DIR="$REPO_DIR/src/genesis/dashboard/webui"
AZ_ROOT="${AZ_ROOT:-$HOME/agent-zero}"
AZ_WEBUI="$AZ_ROOT/webui"

# Check if assets already present (Alpine.js as canary)
if [ -f "$WEBUI_DIR/vendor/alpine/alpine.min.js" ]; then
    echo "Static assets already vendored — skipping."
    exit 0
fi

if [ ! -d "$AZ_WEBUI" ]; then
    echo "ERROR: Agent Zero webui not found at $AZ_WEBUI"
    echo "Set AZ_ROOT to your Agent Zero install, or manually copy assets into:"
    echo "  $WEBUI_DIR/"
    exit 1
fi

echo "Vendoring static assets from $AZ_WEBUI ..."

mkdir -p "$WEBUI_DIR"/{vendor/alpine,vendor/ace-min,vendor/google,css,js,public}

# Vendor libraries
cp -r "$AZ_WEBUI/vendor/alpine/"*     "$WEBUI_DIR/vendor/alpine/"
cp -r "$AZ_WEBUI/vendor/ace-min/"*    "$WEBUI_DIR/vendor/ace-min/"
cp -r "$AZ_WEBUI/vendor/google/"*     "$WEBUI_DIR/vendor/google/"

# Base styles
cp "$AZ_WEBUI/index.css"              "$WEBUI_DIR/index.css"
cp "$AZ_WEBUI/css/modals.css"         "$WEBUI_DIR/css/modals.css"
cp "$AZ_WEBUI/css/buttons.css"        "$WEBUI_DIR/css/buttons.css"

# Favicon
cp "$AZ_WEBUI/public/favicon.svg"     "$WEBUI_DIR/public/favicon.svg"

echo "Done. Vendored $(du -sh "$WEBUI_DIR" | cut -f1) of static assets."
echo "NOTE: js/initFw.js and js/api.js are Genesis-specific forks (not copied from AZ)."
