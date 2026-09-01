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
    # The token-prep prefix runs BEFORE cd, leaving the `cd && claude` guard intact.
    assert "${_OAUTH_SRC}cd ${GENESIS_ROOT} && command claude " in script_text


def test_claude_stays_under_cd_guard(script_text):
    # `_OAUTH_SRC` ends in `;`. Placing it AFTER the `&&` (`cd $ROOT && <prefix>;
    # claude`) would bind the `&&` to the prefix only and run claude even if cd
    # fails. Keeping the prefix BEFORE cd preserves the original `cd && claude` guard.
    assert "${_OAUTH_SRC}cd ${GENESIS_ROOT} && command claude " in script_text
    assert "&& { ${_OAUTH_SRC}claude" not in script_text  # not the brace-group shape


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


def _extract_launch_line(script_text: str) -> str:
    """The pane-command string, as the `_PANE_CMD=…` assignment's right-hand side.

    It is built ONCE into a variable and used twice — handed to tmux on the
    create path, and typed into the pane on the heal path — so that a healed
    slot cannot drift into running a different claude. Returns the quoted value
    so callers can splice it straight into a harness.
    """
    for line in script_text.splitlines():
        stripped = line.strip()
        if stripped.startswith('_PANE_CMD="${_OAUTH_SRC}cd ${GENESIS_ROOT} &&'):
            return stripped[len("_PANE_CMD=") :]
    raise AssertionError("could not find the _PANE_CMD pane command in cc-slot.sh")


def test_pane_command_is_built_once_and_reused(script_text):
    """The create and heal paths must share ONE builder.

    If the heal path ever grows its own copy of the launch string, a healed slot
    silently loses the OAuth prefix / permission flag / exit-capture trailer and
    re-poisons itself the next time claude exits.
    """
    assert script_text.count('_PANE_CMD="${_OAUTH_SRC}cd ${GENESIS_ROOT} &&') == 1
    # Used by the exec (create) and by send-keys (heal) — and nowhere else.
    assert script_text.count('"$_PANE_CMD"') == 2


def test_pane_command_bypasses_the_bashrc_wrapper_function(tmp_path, script_text):
    """A shared STRING is not shared EXECUTION.

    tmux runs the create path under a non-interactive `sh -c` (no rc files); the
    heal path is typed into an INTERACTIVE shell that has sourced ~/.bashrc,
    where `claude` is a wrapper FUNCTION. If the payload said bare `claude` the
    function would interpose on the heal path only — re-deriving the environment
    and invoking cc_exit_capture twice — so the two paths would diverge despite
    passing the string-identity check above.
    """
    launch = _extract_launch_line(script_text)
    marker = tmp_path / "wrapper_ran.txt"
    binv = tmp_path / "bin"
    binv.mkdir()
    real = binv / "claude"
    real.write_text("#!/usr/bin/env bash\nexit 0\n")
    real.chmod(0o755)

    harness = f"""
set -u
export PATH={str(binv)!r}:/usr/bin:/bin
GENESIS_ROOT={str(tmp_path)!r}
_OAUTH_SRC=""
CC_PERM_FLAG=""
CLAUDE_ARGS_Q=""
SLOT="test"
mkdir -p {str(tmp_path)!r}/scripts
printf '#!/usr/bin/env bash\\nexit 0\\n' > {str(tmp_path)!r}/scripts/cc_exit_capture.sh
chmod +x {str(tmp_path)!r}/scripts/cc_exit_capture.sh
# Stand in for the bashrc wrapper an interactive shell would have defined.
claude() {{ echo "WRAPPER" > {str(marker)!r}; }}
pane_cmd={launch}
eval "$pane_cmd"
"""
    subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=30)
    assert not marker.exists(), (
        "the pane command resolved to the bashrc wrapper function; "
        "the heal path would diverge from the create path"
    )


def _run_cd_guard(script_text: str, root: str, canary: Path, fakebin: Path):
    """Run the REAL pane launch string with a fake `claude` (touches the canary)
    and a `;`-terminated _OAUTH_SRC (mimics the real prefix). Returns (rc, canary_exists).
    """
    launch = _extract_launch_line(script_text)
    harness = f"""
set -u
export PATH="{fakebin}:/usr/bin:/bin"
export CDGUARD_CANARY={str(canary)!r}
GENESIS_ROOT={root!r}
_OAUTH_SRC="export CDGUARD_PREFIX_RAN=1; "
CC_PERM_FLAG=""
CLAUDE_ARGS_Q=""
SLOT="test"
pane_cmd={launch}
bash -c "$pane_cmd"
echo "PANE_EXIT=$?"
"""
    proc = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
    )
    return proc, canary.exists()


def test_cd_guard_skips_claude_on_bad_cd(tmp_path, script_text):
    """With a `;`-terminated _OAUTH_SRC, a FAILED cd must not launch claude — and a
    SUCCESSFUL cd must. Proves the `{ …; }` grouping (verify-RED vs the un-grouped
    `cd && <prefix>; claude`, which runs claude regardless of cd)."""
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    fake_claude = fakebin / "claude"
    fake_claude.write_text('#!/usr/bin/env bash\ntouch "$CDGUARD_CANARY"\nexit 0\n')
    fake_claude.chmod(0o755)

    # bad cd → claude NOT run, pane exits with cd's failure code
    canary_bad = tmp_path / "PANE_RAN_BAD"
    proc_bad, ran_bad = _run_cd_guard(
        script_text,
        str(tmp_path / "does-not-exist"),
        canary_bad,
        fakebin,
    )
    assert not ran_bad, f"claude ran despite a failed cd\n{proc_bad.stdout}\n{proc_bad.stderr}"
    assert "PANE_EXIT=0" not in proc_bad.stdout, proc_bad.stdout

    # good cd → claude DOES run, pane exits 0
    good_root = tmp_path / "good"
    good_root.mkdir()
    canary_good = tmp_path / "PANE_RAN_GOOD"
    proc_good, ran_good = _run_cd_guard(script_text, str(good_root), canary_good, fakebin)
    assert ran_good, f"claude did not run on a good cd\n{proc_good.stdout}\n{proc_good.stderr}"
    assert "PANE_EXIT=0" in proc_good.stdout, proc_good.stdout


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
