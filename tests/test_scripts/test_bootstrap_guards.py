"""Bootstrap robustness guards (deploy-audit P8a — B1/B3/B5/B6/B7/B8/B9/S3).

These harden scripts/bootstrap.sh and scripts/setup-local-config.sh against
premature completion signalling, skipped reloads, cwd-dependent MCP scope,
unvalidated timezones, newline-less secret appends, hung dependency installs,
pipe-to-shell truncation, and dead config. bootstrap.sh runs the full install
(services, VNC, memory restore) and cannot be exercised in CI, so most locks are
extraction assertions on the shipped text; B7's subtle newline logic gets a
functional bash test.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap.sh"
SETUP_LOCAL = REPO_ROOT / "scripts" / "setup-local-config.sh"


def _code(path: Path) -> str:
    """Script text minus comment-only lines (so assertions match real code)."""
    return "\n".join(ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#"))


# ── B1: setup-complete marker written only at the END ────────────────


def test_b1_setup_complete_marker_written_at_end():
    text = BOOTSTRAP.read_text()
    marker = 'touch "$HOME/.genesis/setup-complete"'
    assert text.count(marker) == 1, "the marker must be written exactly once"
    touch_at = text.index(marker)
    init_at = text.index("Initializing runtime state")
    complete_at = text.index("=== Bootstrap complete ===")
    # No longer written in the early runtime-state block …
    assert touch_at > init_at, "marker must not be written in the early runtime-state block"
    # … it sits right before the completion banner (so a crashed bootstrap leaves
    # the box looking un-onboarded — the marker gates the onboarding prompt).
    assert 0 < complete_at - touch_at < 300, "marker must be written immediately before completion"


# ── B3: daemon-reload runs unconditionally before the enable loop ────


def test_b3_daemon_reload_not_gated_on_services_updated():
    text = BOOTSTRAP.read_text()
    echo = 'echo "  systemd daemon reloaded (units changed)"'
    echo_at = text.index(echo)
    window = text[max(0, echo_at - 300) : echo_at]
    # The reload itself is unconditional now …
    assert "systemctl --user daemon-reload 2>/dev/null || true" in window
    # … and precedes the SERVICES_UPDATED gate (which now guards only the echo).
    assert 'if [[ "$SERVICES_UPDATED" = "1" ]]; then' in window
    assert window.index("daemon-reload") < window.index('if [[ "$SERVICES_UPDATED"')


# ── B5: serena project-scope register runs from the repo root ────────


def test_b5_serena_register_runs_from_repo_root():
    code = _code(BOOTSTRAP)
    # `-s project` is cwd-keyed; the register must be wrapped in a cd-subshell so
    # it always writes .mcp.json to the repo, never the caller's cwd.
    assert '( cd "$GENESIS_ROOT" && _register_mcp "serena" "project"' in code


# ── B6: timezone validated, but only when enumerable ────────────────


def test_b6_timezone_validated_when_enumerable():
    code = _code(BOOTSTRAP)
    assert "_tz_list=$(timedatectl list-timezones 2>/dev/null || true)" in code
    assert 'grep -qxF "$GENESIS_TIMEZONE"' in code
    # Validation is GUARDED on a non-empty list, so an image that can't enumerate
    # timezones is never force-reset to UTC.
    assert 'if [[ -n "$_tz_list" ]] &&' in code
    assert 'GENESIS_TIMEZONE="UTC"' in code  # the fallback on a genuinely invalid tz


# ── B7: trailing-newline guard before the secrets append ────────────


def test_b7_newline_guard_present():
    code = _code(BOOTSTRAP)
    assert '-n "$(tail -c1 "$SECRETS_FILE")"' in code


def test_b7_newline_guard_functional(tmp_path):
    """A newline is inserted ONLY when the file lacks a trailing one — so the new
    key always lands on its own line and never concatenates onto the last one.
    Runs under `set -euo pipefail` to prove the `[[ … ]] &&` guard is set-e-safe."""
    guard = (
        "set -euo pipefail; "
        '[[ -s "$SECRETS_FILE" && -n "$(tail -c1 "$SECRETS_FILE")" ]] && printf \'\\n\' >> "$SECRETS_FILE"; '
        "printf 'GENESIS_TIMEZONE=UTC\\n' >> \"$SECRETS_FILE\""
    )
    # No trailing newline → guard inserts one.
    f1 = tmp_path / "no_nl.env"
    f1.write_text("EXISTING=1")
    subprocess.run(["bash", "-c", guard], env={**os.environ, "SECRETS_FILE": str(f1)}, check=True)
    assert f1.read_text() == "EXISTING=1\nGENESIS_TIMEZONE=UTC\n"
    # Already has a trailing newline → no extra blank line.
    f2 = tmp_path / "with_nl.env"
    f2.write_text("EXISTING=1\n")
    subprocess.run(["bash", "-c", guard], env={**os.environ, "SECRETS_FILE": str(f2)}, check=True)
    assert f2.read_text() == "EXISTING=1\nGENESIS_TIMEZONE=UTC\n"


# ── B8: dependency installs are timeout-bounded + retried ───────────


def test_b8_skillspector_install_is_bounded():
    code = _code(BOOTSTRAP)
    assert "timeout 300 git clone --depth 1 https://github.com/NVIDIA/SkillSpector.git" in code
    assert "_ss_clone || { sleep 2; _ss_clone; }" in code  # one retry rides out a blip
    assert 'timeout 300 "$SKILLSPECTOR_DIR/.venv/bin/pip" install' in code
    # A partial clone (mid-transfer failure leaves a stub .git) must be cleared
    # before each attempt and must not wedge future runs: rm -rf before cloning,
    # and gate on a completion marker (pyproject.toml/setup.py), not bare .git.
    assert 'rm -rf "$SKILLSPECTOR_DIR"; timeout 300 git clone' in code
    assert '! -f "$SKILLSPECTOR_DIR/pyproject.toml"' in code
    assert '"$SKILLSPECTOR_DIR/.git"' not in code  # the wedge-prone bare-.git gate is gone


# ── B9: remote installers download-to-temp, never pipe to a shell ───


def test_b9_no_pipe_to_shell():
    code = _code(BOOTSTRAP)
    # The truncation-prone pipe-to-shell forms are gone …
    assert "install.sh | bash" not in code
    assert "uv/install.sh | sh" not in code
    # … replaced by download-to-file then execute-from-file (full download first).
    assert (
        'curl -fsSL https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/install.sh -o "$_cbm_installer"'
        in code
    )
    assert 'bash "$_cbm_installer" --ui --skip-config' in code
    assert 'curl -LsSf https://astral.sh/uv/install.sh -o "$_uv_installer"' in code
    assert 'sh "$_uv_installer"' in code


# ── S3: dead REPO_EXAMPLE var + phantom migration comment removed ───


def test_s3_dead_repo_example_removed():
    cfg = SETUP_LOCAL.read_text()
    assert "REPO_EXAMPLE" not in cfg
    assert "genesis.yaml.example" not in cfg
    assert "Migrate from repo YAML" not in cfg  # the misleading comment is corrected
