"""Regression guard for the code-intel runner systemd unit's PATH.

The idle-gated runner (genesis-code-intel.service) invokes two external
indexers: ``gitnexus`` (installed to ~/.npm-global/bin) and
``codebase-memory-mcp`` (installed to ~/.local/bin — the vendor install.sh
default, cf. .claude/mcp/run-codebase-memory). The unit's Environment=PATH
MUST contain BOTH dirs, or the entrypoint exits 3 ("requested tool missing")
and the runner keeps the marker forever without indexing.

This is not hypothetical: ~/.local/bin was omitted from the template, so cbm
indexing was silently dead for days (156 rc=3 "nothing indexed", zero rc=0,
the runner re-escalating "first full cbm index due" every 5 min) while
gitnexus kept working. This test locks in both dirs so the regression can't
ship again.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE = _REPO_ROOT / "scripts" / "systemd" / "genesis-code-intel.service.template"
_CBM_LAUNCHER = _REPO_ROOT / ".claude" / "mcp" / "run-codebase-memory"


def _path_line() -> str:
    for line in _TEMPLATE.read_text(encoding="utf-8").splitlines():
        if line.startswith("Environment=PATH="):
            return line
    raise AssertionError("no Environment=PATH= line in genesis-code-intel.service.template")


def test_runner_path_includes_cbm_and_gitnexus_dirs() -> None:
    path_line = _path_line()
    # gitnexus lives here; cbm installs here. Both are required — omitting either
    # makes the entrypoint exit 3 and the runner index nothing.
    assert "__HOME__/.local/bin" in path_line, (
        "code-intel runner PATH omits ~/.local/bin — codebase-memory-mcp indexing "
        "will silently fail (rc=3, nothing indexed). This is the exact regression "
        "that killed cbm indexing for days."
    )
    assert "__HOME__/.npm-global/bin" in path_line, (
        "code-intel runner PATH omits ~/.npm-global/bin — gitnexus indexing will fail."
    )


def test_managed_launcher_does_not_select_a_path_provider() -> None:
    """Frontend uses managed authority; legacy runner PATH is not its binary pin."""
    launcher = _CBM_LAUNCHER.read_text(encoding="utf-8")
    assert 'scripts/codebase_managed.py" launch' in launcher
    assert "CODEBASE_MEMORY_MCP_BIN" not in launcher
    assert "command -v codebase-memory-mcp" not in launcher
