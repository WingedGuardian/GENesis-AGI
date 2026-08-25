"""Guards for the cc-slot.sh OAuth-durability wiring (WS-1).

Two layers:
- static asserts that the gate is wired the safe way (venv python, reattach-
  gated, single-var extraction, NO token via `tmux -e`);
- a dynamic test that pulls the ACTUAL `_OAUTH_SRC` prefix line out of the
  script and runs it, proving the token is not materialized when the string is
  built (no argv/ps leak) but IS exported when the pane shell runs it, and that
  the second var in the token file does not leak into the env.

The full launch path (exec tmux) is covered by the live E2E slot test, not here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_CC_SLOT = _REPO / "scripts" / "cc-slot.sh"


@pytest.fixture(scope="module")
def script_text() -> str:
    return _CC_SLOT.read_text()


def test_gate_wired_via_venv_python(script_text):
    # Must call the gate through the venv interpreter (cc-slot.sh's PATH has no
    # venv, so bare `python` would be genesis-less and fail).
    assert '/.venv/bin/python" -m genesis.cc.login_gate' in script_text
    assert "genesis.cc.login_gate" in script_text


def test_gate_skipped_on_reattach(script_text):
    # Reattach (session already exists) must not run the gate.
    assert "_SESSION_EXISTS" in script_text
    assert '[ "$_SESSION_EXISTS" = "0" ]' in script_text


def test_bare_slots_skip_injection(script_text):
    # --bare ignores the token, so injection must be skipped there.
    assert "_HAS_BARE" in script_text
    assert '"--bare"' in script_text


def test_single_var_extraction_not_full_source(script_text):
    # Extract ONLY the token var (S2) — never `. source` the whole file.
    assert "sed -n 's/^CLAUDE_CODE_OAUTH_TOKEN=//p'" in script_text
    assert ". ~/.genesis/cc_oauth_token.env" not in script_text
    assert ". $HOME/.genesis/cc_oauth_token.env" not in script_text


def test_no_token_leak_via_tmux_e(script_text):
    # The token must NEVER be passed as a `tmux -e` value (would land in argv/ps).
    assert '-e "CLAUDE_CODE_OAUTH_TOKEN' not in script_text
    assert "-e CLAUDE_CODE_OAUTH_TOKEN" not in script_text


def test_pane_command_interpolates_oauth_prefix(script_text):
    # The pane command string must include the prefix before `claude`.
    assert "${_OAUTH_SRC}claude " in script_text


def test_gate_receives_lever_via_env(script_text):
    # A plain `GENESIS_CC_SLOT_OAUTH=always` in cc-slot.env is a non-exported
    # shell var; the gate subprocess must be handed the resolved mode explicitly
    # via `env`, or `always` silently degrades to `conditional`.
    assert 'env GENESIS_CC_SLOT_OAUTH="$_slot_oauth_mode"' in script_text


def _extract_oauth_src_line(script_text: str) -> str:
    for line in script_text.splitlines():
        if line.lstrip().startswith('_OAUTH_SRC="if'):
            return line.strip()
    raise AssertionError("could not find the _OAUTH_SRC assignment in cc-slot.sh")


def test_prefix_defers_build_and_exports_in_pane(tmp_path, script_text):
    """Run the ACTUAL prefix line: no build-time leak, correct pane export."""
    home = tmp_path
    (home / ".genesis").mkdir()
    (home / ".genesis" / "cc_oauth_token.env").write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-DYNTOKEN\nGENESIS_CC_TOKEN_CREATED_AT=1700000000\n",
    )
    oauth_src_line = _extract_oauth_src_line(script_text)

    harness = f"""
set -u
_oauth_notice="Genesis: test notice, no connectors"
_notice_q=$(printf '%q' "$_oauth_notice")
{oauth_src_line}
# 1) build-time: the token value must NOT appear in the built string
case "$_OAUTH_SRC" in
  *DYNTOKEN*) echo "BUILD_LEAK"; exit 3;;
esac
# 2) pane execution: run the prefix, then report the resulting env
eval "$_OAUTH_SRC"
printf 'TOKEN=[%s]\\n' "${{CLAUDE_CODE_OAUTH_TOKEN:-}}"
printf 'CREATED=[%s]\\n' "${{GENESIS_CC_TOKEN_CREATED_AT:-}}"
"""
    proc = subprocess.run(
        ["bash", "-c", harness],
        cwd=str(home),
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"harness failed: {proc.stdout}\n{proc.stderr}"
    assert "BUILD_LEAK" not in proc.stdout, "token materialized when building the string"
    assert "TOKEN=[sk-ant-oat-DYNTOKEN]" in proc.stdout, proc.stdout
    # The second var in the file must NOT be exported (single-var extraction).
    assert "CREATED=[]" in proc.stdout, proc.stdout
    # The notice goes to stderr, and must never contain the token value.
    assert "sk-ant-oat-DYNTOKEN" not in proc.stderr


def test_notice_text_cannot_inject_shell(tmp_path, script_text):
    """A notice string with quotes/`/$ must never break out of the command.

    Regression guard for the %q hardening: even if a future edit gives the
    notice a `"`/backtick/`$`-laden value, building + running the real
    _OAUTH_SRC line must NOT execute an injected command. Seeds a payload that
    would `touch` a canary and asserts the canary never appears.
    """
    home = tmp_path
    (home / ".genesis").mkdir()
    (home / ".genesis" / "cc_oauth_token.env").write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-DYNTOKEN\nGENESIS_CC_TOKEN_CREATED_AT=1\n",
    )
    canary = home / "PWNED"
    oauth_src_line = _extract_oauth_src_line(script_text)
    # A malicious notice: closes a naive double-quote, runs a command, re-opens.
    payload = f'evil"; touch {canary}; echo "$(touch {canary})`touch {canary}`'

    harness = f"""
set -u
_oauth_notice={payload!r}
_notice_q=$(printf '%q' "$_oauth_notice")
{oauth_src_line}
eval "$_OAUTH_SRC"
printf 'TOKEN=[%s]\\n' "${{CLAUDE_CODE_OAUTH_TOKEN:-}}"
"""
    proc = subprocess.run(
        ["bash", "-c", harness],
        cwd=str(home),
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert not canary.exists(), "notice text injected a shell command (canary created)"
    # The token export must still work despite the hostile notice.
    assert "TOKEN=[sk-ant-oat-DYNTOKEN]" in proc.stdout, proc.stdout
