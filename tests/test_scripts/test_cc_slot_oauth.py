"""Guards for the cc-slot.sh OAuth-durability wiring (WS-1).

Static asserts that the gate is wired the safe way (venv python, lever handed
via env, reattach/--bare skip, gate-authored notice captured from stdout, token
read with the SAME parser as the decision, non-empty export guard, NO token via
`tmux -e`), plus a dynamic test that runs the ACTUAL `_OAUTH_SRC` line and proves
the token is not materialized when the string is built (no argv/ps leak) but IS
exported when the pane shell runs it, with the notice on stderr only.

The full launch path (exec tmux) is covered by the live E2E slot test, not here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_CC_SLOT = _REPO / "scripts" / "cc-slot.sh"
_SRC = _REPO / "src"


@pytest.fixture(scope="module")
def script_text() -> str:
    return _CC_SLOT.read_text()


def test_gate_wired_via_venv_python_with_env_lever(script_text):
    assert '/.venv/bin/python" -m genesis.cc.login_gate' in script_text
    # The resolved lever is handed to the subprocess (a non-exported cc-slot.env
    # var would otherwise never reach it).
    assert 'env GENESIS_CC_SLOT_OAUTH="$_slot_oauth_mode"' in script_text


def test_notice_captured_from_gate_stdout(script_text):
    # The gate authors the notice (single authority) — cc-slot captures it.
    assert "_oauth_notice=$(timeout 30 env GENESIS_CC_SLOT_OAUTH=" in script_text
    # No hard-coded notice branch in bash anymore.
    assert 'if [ "$_slot_oauth_mode" = "always" ]; then' not in script_text


def test_gate_skipped_on_reattach_and_bare(script_text):
    assert '[ "$_SESSION_EXISTS" = "0" ]' in script_text
    assert '[ "$_HAS_BARE" = "0" ]' in script_text
    assert '"--bare"' in script_text


def test_pane_uses_same_parser_not_sed(script_text):
    # Single parser: pane reads via login_health.read_fallback_token, NOT a
    # divergent sed/whole-file source.
    assert "read_fallback_token" in script_text
    assert "sed -n 's/^CLAUDE_CODE_OAUTH_TOKEN=" not in script_text
    assert ". ~/.genesis/cc_oauth_token.env" not in script_text


def test_non_empty_export_guard(script_text):
    # Never export a blank credential if the read fails/empties. (The guard lives
    # inside the double-quoted _OAUTH_SRC assignment, so it is backslash-escaped
    # in the file.)
    assert r"if [ -n \"\$_gt\" ]; then export CLAUDE_CODE_OAUTH_TOKEN=" in script_text


def test_no_token_leak_via_tmux_e(script_text):
    assert '-e "CLAUDE_CODE_OAUTH_TOKEN' not in script_text
    assert "-e CLAUDE_CODE_OAUTH_TOKEN" not in script_text


def test_pane_command_interpolates_oauth_prefix(script_text):
    assert "${_OAUTH_SRC}claude " in script_text


def _extract_oauth_src_line(script_text: str) -> str:
    for line in script_text.splitlines():
        if line.lstrip().startswith('_OAUTH_SRC="_gt='):
            return line.strip()
    raise AssertionError("could not find the _OAUTH_SRC assignment in cc-slot.sh")


def _pane_line(script_text: str) -> str:
    """The real _OAUTH_SRC line with the venv python swapped for this test's
    interpreter (so the pane subprocess can import genesis via PYTHONPATH)."""
    line = _extract_oauth_src_line(script_text)
    return line.replace("${GENESIS_ROOT}/.venv/bin/python", sys.executable)


def _run_pane(harness_body: str, home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", harness_body],
        cwd=str(home),
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "PYTHONPATH": str(_SRC)},
        capture_output=True,
        text=True,
    )


def test_prefix_defers_build_and_exports_in_pane(tmp_path, script_text):
    home = tmp_path
    (home / ".genesis").mkdir()
    (home / ".genesis" / "cc_oauth_token.env").write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-DYNTOKEN\nGENESIS_CC_TOKEN_CREATED_AT=1\n",
    )
    pane_line = _pane_line(script_text)
    harness = f"""
set -u
_oauth_notice="Genesis: test notice, no connectors"
_notice_q=$(printf '%q' "$_oauth_notice")
{pane_line}
case "$_OAUTH_SRC" in *DYNTOKEN*) echo BUILD_LEAK; exit 3;; esac
eval "$_OAUTH_SRC"
printf 'TOKEN=[%s]\\n' "${{CLAUDE_CODE_OAUTH_TOKEN:-}}"
"""
    proc = _run_pane(harness, home)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "BUILD_LEAK" not in proc.stdout, "token materialized when building the string"
    assert "TOKEN=[sk-ant-oat-DYNTOKEN]" in proc.stdout, proc.stdout
    assert "sk-ant-oat-DYNTOKEN" not in proc.stderr, "token leaked to stderr"


def test_missing_token_does_not_export_blank(tmp_path, script_text):
    home = tmp_path
    (home / ".genesis").mkdir()  # no token file
    pane_line = _pane_line(script_text)
    harness = f"""
set -u
_oauth_notice="x"
_notice_q=$(printf '%q' "$_oauth_notice")
{pane_line}
eval "$_OAUTH_SRC"
printf 'TOKEN=[%s]\\n' "${{CLAUDE_CODE_OAUTH_TOKEN:-}}"
"""
    proc = _run_pane(harness, home)
    assert "TOKEN=[]" in proc.stdout, proc.stdout  # non-empty guard held


def test_notice_text_cannot_inject_shell(tmp_path, script_text):
    """Regression for the %q hardening: a hostile notice must not execute."""
    home = tmp_path
    (home / ".genesis").mkdir()
    (home / ".genesis" / "cc_oauth_token.env").write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-DYNTOKEN\n",
    )
    canary = home / "PWNED"
    pane_line = _pane_line(script_text)
    payload = f'evil"; touch {canary}; echo "$(touch {canary})`touch {canary}`'
    harness = f"""
set -u
_oauth_notice={payload!r}
_notice_q=$(printf '%q' "$_oauth_notice")
{pane_line}
eval "$_OAUTH_SRC"
printf 'TOKEN=[%s]\\n' "${{CLAUDE_CODE_OAUTH_TOKEN:-}}"
"""
    proc = _run_pane(harness, home)
    assert not canary.exists(), "notice text injected a shell command (canary created)"
    assert "TOKEN=[sk-ant-oat-DYNTOKEN]" in proc.stdout, proc.stdout
