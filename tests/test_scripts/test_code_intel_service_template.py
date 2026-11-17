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

import pytest

from tests.test_scripts.test_codebase_managed_lifecycle import loaded as loaded
from tests.test_scripts.test_codebase_managed_lifecycle import operator as operator
from tests.test_scripts.test_codebase_managed_templates import render as render

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE = _REPO_ROOT / "scripts" / "systemd" / "genesis-code-intel.service.template"
_CBM_LAUNCHER = _REPO_ROOT / ".claude" / "mcp" / "run-codebase-memory"


def _path_line() -> str:
    for line in _TEMPLATE.read_text(encoding="utf-8").splitlines():
        if line.startswith(("Environment=PATH=", 'Environment="PATH=')):
            return line
    raise AssertionError("no Environment=PATH= line in genesis-code-intel.service.template")


def test_runner_path_includes_cbm_and_gitnexus_dirs() -> None:
    path_line = _path_line()
    # gitnexus lives here; cbm installs here. Both are required — omitting either
    # makes the entrypoint exit 3 and the runner index nothing.
    assert "__HOME_EXEC__/.local/bin" in path_line, (
        "code-intel runner PATH omits ~/.local/bin — codebase-memory-mcp indexing "
        "will silently fail (rc=3, nothing indexed). This is the exact regression "
        "that killed cbm indexing for days."
    )
    assert "__HOME_EXEC__/.npm-global/bin" in path_line, (
        "code-intel runner PATH omits ~/.npm-global/bin — gitnexus indexing will fail."
    )


def test_managed_launcher_does_not_select_a_path_provider() -> None:
    """Frontend uses managed authority; legacy runner PATH is not its binary pin."""
    launcher = _CBM_LAUNCHER.read_text(encoding="utf-8")
    assert 'scripts/codebase_managed.py" launch' in launcher
    assert "CODEBASE_MEMORY_MCP_BIN" not in launcher
    assert "command -v codebase-memory-mcp" not in launcher


@pytest.mark.parametrize("renderer", ["install.sh", "bootstrap.sh"])
def test_runner_path_is_one_literal_environment_assignment(render, tmp_path, renderer):
    import shlex
    import shutil

    units, run = render
    shutil.copy2(_TEMPLATE, units.parent / "templates")
    home = tmp_path / 'home $%λ" \\ '
    cc_bin = tmp_path / 'node $%λ" \\ '
    result = run(renderer, home, cc_bin_dir=str(cc_bin))
    assert result.returncode == 0, result.stdout + result.stderr
    lines = (units / "genesis-code-intel.service").read_text().splitlines()
    raw, = [line.removeprefix("Environment=") for line in lines
            if line.startswith(("Environment=PATH=", 'Environment="PATH='))]
    assignment, = shlex.split(raw)
    venv = home / ".venv" if renderer == "bootstrap.sh" else tmp_path / 'venv=$%&|λ" '
    expected = f"PATH={venv}/bin:{cc_bin}:{home}/.npm-global/bin:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin"
    assert assignment.replace("%%", "%") == expected
    # systemd255's path parser doesn't unquote WorkingDirectory or preserve
    # trailing whitespace. Captured startup finds the checkout from __file__;
    # both native index paths already select their repository cwd explicitly.
    assert "WorkingDirectory=/" in lines


@pytest.mark.parametrize("renderer", ["install.sh", "bootstrap.sh"])
@pytest.mark.parametrize("suffix", ["plain", 'literal $value%λ" \\ '])
def test_runner_service_enters_captured_source_with_literal_arguments(render, tmp_path, renderer, suffix):
    import json
    import shlex
    import shutil
    import subprocess

    units, run = render
    shutil.copy2(_TEMPLATE, units.parent / "templates")
    root = tmp_path / suffix
    result = run(renderer, root)
    assert result.returncode == 0, result.stdout + result.stderr
    venv = root / ".venv" if renderer == "bootstrap.sh" else tmp_path / 'venv=$%&|λ" '
    def escaped(path):
        return str(path).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")

    # The CLI takes action, capture profile, then the shell role.
    expected = ("ExecStart=:/bin/sh -c 'exec \"$@\"' -- "
                f'"{escaped(venv)}/bin/python" -I '
                f'"{escaped(root)}/scripts/lib/code_intel_source.py" shell runner runner')
    lines = (units / "genesis-code-intel.service").read_text().splitlines()
    assert expected in lines
    # Apply only systemd's literal percent decoding, then exercise the fixed
    # positional shell trampoline. The toy interpreter observes its arguments;
    # it cannot enter the real runner or a provider.
    command = shlex.split(expected.removeprefix("ExecStart=:").replace("%%", "%"))
    program = venv / "bin/python"
    program.parent.mkdir(parents=True)
    program.write_text('#!/usr/bin/python3\nimport json,sys\nprint(json.dumps(sys.argv))\n')
    program.chmod(0o700)
    observed = subprocess.run(command, capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(observed.stdout) == [str(program), "-I", str(root / "scripts/lib/code_intel_source.py"),
                                          "shell", "runner", "runner"]


@pytest.mark.parametrize("renderer", ["install.sh", "bootstrap.sh"])
def test_runner_carries_rendered_venv_into_backend_validation(
    render, operator, loaded, tmp_path, monkeypatch, renderer,
):
    import shlex
    import shutil

    units, run = render
    shutil.copy2(_TEMPLATE, units.parent / "templates")
    home = tmp_path / 'home $%λ" \\ '
    result = run(renderer, home)
    assert result.returncode == 0, result.stdout + result.stderr
    venv = home / ".venv" if renderer == "bootstrap.sh" else tmp_path / 'venv=$%&|λ" '
    escaped = str(venv).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    lines = (units / "genesis-code-intel.service").read_text().splitlines()
    expected = f'Environment="VENV_PATH={escaped}"'
    assert expected in lines
    assignment, = shlex.split(expected.removeprefix("Environment="))
    key, selected = assignment.split("=", 1)
    selected = selected.replace("%%", "%")
    assert key == "VENV_PATH" and selected == str(venv)
    config, properties, _ = loaded
    for name in ("ExecStartEx", "ExecStartPostEx"):
        properties[name]["data"][0][1][4] = str(venv) + "/bin/python"
    monkeypatch.delenv("VENV_PATH", raising=False)
    monkeypatch.setenv("PATH", str(venv / "bin") + ":/usr/bin")
    with pytest.raises(ValueError, match="ExecStartEx"):
        operator.validate_loaded_backend(config)
    monkeypatch.setenv(key, selected)
    operator.validate_loaded_backend(config)
    monkeypatch.setenv(key, str(tmp_path / "different-venv"))
    with pytest.raises(ValueError, match="ExecStartEx"):
        operator.validate_loaded_backend(config)
