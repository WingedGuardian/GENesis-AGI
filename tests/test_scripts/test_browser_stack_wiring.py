"""Every install gets the browser stack, and no install waits on its download.

bootstrap.sh (which update.sh runs) installs the browser packages after the core
packages, never fatally, and starts genesis-browser-stack.service without waiting.
install.sh runs the step inline, where the user and sudo are at hand. update.sh
itself runs nothing: its bootstrap starts the unit, and the unit's daily timer
retries everything else.
"""

from __future__ import annotations

import re
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_START = "systemctl --user start --no-block genesis-browser-stack.service"


def _code(name: str) -> str:
    """The script with comment lines dropped, so prose never satisfies a check."""
    lines = (_SCRIPTS / name).read_text().splitlines()
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))


def test_bootstrap_installs_the_browser_packages_after_the_core_never_fatally():
    code = _code("bootstrap.sh")
    core = code.index('editable_install_guarded "$GENESIS_ROOT" "$VENV_DIR" ||')
    browser = code.index('install_browser_extra "$GENESIS_ROOT" "$VENV_DIR"')
    assert core < browser
    statement = code[browser : code.index("\n", code.index("\\\n", browser) + 2)]
    assert "||" in statement and "exit" not in statement


def test_bootstrap_starts_the_unit_without_waiting_after_the_timers():
    code = _code("bootstrap.sh")
    timers = code.index('for template in "$SYSTEMD_TEMPLATE_DIR"/*.timer.template')
    assert code.index(_START) > timers
    assert code.index(_START) > code.index("systemctl --user daemon-reload")


def test_install_runs_the_step_inline_and_warns_on_failure():
    code = _code("install.sh")
    core = code.index('editable_install_guarded "$REPO_DIR" "$VENV_PATH" ||')
    assert core < code.index('install_browser_extra "$REPO_DIR" "$VENV_PATH"')
    inline = code.index('bash "$SCRIPT_DIR/install_browser_stack.sh"')
    timers = code.index('for template in "$SYSTEMD_TEMPLATE_DIR"/*.timer.template')
    assert inline > timers  # its unit and timer exist by then
    assert re.search(r"else\n\s+setup_warn \"browser stack not ready", code[inline:])
    # The timer loop above can start a catch-up run first; the inline step waits
    # for it and reports its own outcome instead of exiting 0 on the lock.
    assert "GENESIS_BROWSER_STACK_WAIT_S=7200" in code[timers:inline]


def test_install_inside_a_claude_code_session_starts_the_unit_instead():
    """The provisioner refuses to run inside a session (exit 2)."""
    code = _code("install.sh")
    branch = code.index('[ -n "${CLAUDECODE:-}" ]; then')
    assert branch < code.index(_START, branch) < code.index("install_browser_stack.sh", branch)


def test_update_runs_no_browser_step_of_its_own():
    """A 1.5 GB download inside update.sh lengthened its stop window and needed
    deferral flags (#2956); its bootstrap starts the unit instead."""
    code = _code("update.sh")
    assert "install_browser_stack" not in code
    assert "genesis-browser-stack" not in code


def test_install_runs_the_step_only_after_genesis_installed():
    """A blocked or failed package step already warned; the step would only add a
    second, misleading failure."""
    code = _code("install.sh")
    assert code.index("_genesis_installed=1") < code.index('install_browser_extra "$REPO_DIR"')
    gate = code.index('if [ "$_genesis_installed" != "1" ]; then')
    assert gate < code.index('[ -n "${CLAUDECODE:-}" ]; then') < code.index(
        'bash "$SCRIPT_DIR/install_browser_stack.sh"'
    )
