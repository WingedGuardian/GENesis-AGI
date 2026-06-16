# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
#
# Single source of truth for the Claude Code version Genesis installs/pins.
#
# Sourced by scripts/install.sh (container), scripts/host-setup.sh (host VM),
# and scripts/update.sh (which dispatches this pin to the host via the
# guardian-gateway `update-cc` op). Bump CC_VERSION here in ONE place — the next
# `update.sh` run syncs the host so container and host never drift.
#
# Governance model (see docs/reference/cc-compatibility.md): the npm pin below
# + the unified `update-cc` updater + DISABLE_UPDATES in ~/.claude/settings.json.
# Deliberately NO managed-settings `requiredMinimumVersion` floor — a hard floor
# removes the incident-recovery downgrade path and can brick CC.
#
# Honors an inherited CC_VERSION (e.g. `CC_VERSION=2.1.180 ./install.sh`).
CC_VERSION="${CC_VERSION:-2.1.173}"
