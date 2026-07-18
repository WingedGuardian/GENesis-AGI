"""Regression test for ``scripts/install_guardian.sh`` prerequisite probing.

The Guardian installer runs under ``set -euo pipefail`` and host-setup.sh runs it
BEFORE Node/Claude Code are installed. An unguarded ``CLAUDE_PATH=$(command -v
claude)`` therefore aborts the whole installer on every fresh host (``command -v``
exits non-zero when claude is absent → ``set -e`` kills the script) even though
the CLI is documented as optional. This test extracts the actual probe line from
the script and proves it survives the strict mode with claude absent.
"""

import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install_guardian.sh"


def _claude_probe_line() -> str:
    for line in _SCRIPT.read_text().splitlines():
        if "CLAUDE_PATH=$(command -v claude" in line:
            return line.strip()
    raise AssertionError("CLAUDE_PATH probe line not found in install_guardian.sh")


def test_claude_probe_is_set_e_safe_when_claude_absent():
    """The real probe line, run under the script's own strict mode with claude
    absent (empty PATH), must not abort — otherwise fresh-host installs die."""
    line = _claude_probe_line()
    # Empty PATH is set INSIDE the script (so `command -v claude` finds nothing);
    # `command`/`true`/`echo` are builtins. subprocess still finds bash via the
    # real inherited env.
    script = f"export PATH=/nonexistent\nset -euo pipefail\n{line}\necho SURVIVED\n"
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"CLAUDE_PATH probe aborts under set -e when claude is absent "
        f"(fresh-host install would fail here):\n{line}\nstderr: {proc.stderr}"
    )
    assert "SURVIVED" in proc.stdout, proc.stdout
