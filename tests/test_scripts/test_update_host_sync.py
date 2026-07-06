"""Host-sync config parsing in scripts/update.sh (`_sync_deploy_targets`).

PR #898 extracted the deploy-target sync into a function and re-indented the
inline ``python -c`` snippets that parse ``guardian_remote.yaml``. Indented
top-level Python is an IndentationError; the ``2>/dev/null || true`` swallowed
it, ``HOST_IP`` resolved empty, and the ENTIRE host block — guardian redeploy
plus host Node/CC pin healing — silently skipped on every update run, with
``HOST_CC_DEGRADED`` never set (update_history reported the host healthy).

These tests extract the snippets and the function from the REAL update.sh and
execute them, so the logic under test is the shipped script, not a copy. A
class-level guardrail scans every shell script for the indented-multiline
``python -c`` shape so the regression cannot be reintroduced elsewhere.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
UPDATE_SH = SCRIPTS_DIR / "update.sh"

SAMPLE_YAML = 'host_ip: "192.0.2.7"\nhost_user: "opuser"\n'


def _extract_snippet(var: str) -> str:
    """Pull the python -c payload off the HOST_IP=/HOST_USER= line."""
    for line in UPDATE_SH.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith(f'{var}=$(') and '-c "' in stripped:
            m = re.search(r'-c "(.+)" 2>/dev/null', stripped)
            assert m, f"could not extract -c payload from {var} line: {stripped}"
            return m.group(1)
    pytest.fail(f"no single-line {var}= python -c assignment found in update.sh")


def _run_snippet(var: str, config_path: Path) -> subprocess.CompletedProcess:
    code = _extract_snippet(var).replace("$GUARDIAN_CONFIG", str(config_path))
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30
    )


def test_host_ip_snippet_parses_and_reads_config(tmp_path):
    """The exact shipped snippet must be valid Python and yield host_ip.

    With the pre-fix indented body this fails with IndentationError.
    """
    cfg = tmp_path / "guardian_remote.yaml"
    cfg.write_text(SAMPLE_YAML)
    result = _run_snippet("HOST_IP", cfg)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "192.0.2.7"


def test_host_user_snippet_parses_and_defaults(tmp_path):
    cfg = tmp_path / "guardian_remote.yaml"
    cfg.write_text(SAMPLE_YAML)
    result = _run_snippet("HOST_USER", cfg)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "opuser"

    cfg.write_text('host_ip: "192.0.2.7"\n')
    result = _run_snippet("HOST_USER", cfg)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ubuntu"


def test_no_indented_multiline_python_bodies_in_shell_scripts():
    """Class guardrail: a multi-line ``python -c "`` whose body's first line is
    indented is a top-level IndentationError waiting behind 2>/dev/null.

    Unindented multi-line bodies (first line at column 0) are fine and exist
    legitimately (e.g. the network-identity block in update.sh).
    """
    violations = []
    for script in sorted(SCRIPTS_DIR.rglob("*.sh")):
        text = script.read_text()
        for m in re.finditer(r'-c "\n[ \t]+', text):
            line_no = text[: m.start()].count("\n") + 1
            violations.append(f"{script.relative_to(REPO_ROOT)}:{line_no}")
    assert not violations, (
        "indented multi-line python -c body (silent IndentationError under "
        f"2>/dev/null): {violations} — inline the snippet on one line or "
        "start its body at column 0"
    )


def _extract_sync_function() -> str:
    text = UPDATE_SH.read_text()
    start = text.index("_sync_deploy_targets() {")
    end = text.index("\n}", start) + 2
    return text[start:end]


def test_unusable_guardian_config_warns_and_marks_degraded(tmp_path):
    """Config present but unusable (here: SSH key absent) must be LOUD:
    warning on stdout + guardian_config_unreadable in HOST_CC_DEGRADED,
    never a silent skip."""
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    (home / ".genesis" / "guardian_remote.yaml").write_text(SAMPLE_YAML)

    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(sys.executable)

    script_dir = tmp_path / "scripts"  # no lib/cc_version.sh → CC sync skips
    script_dir.mkdir()

    harness = (
        f"HOME={home}\nVENV_DIR={tmp_path / 'venv'}\nSCRIPT_DIR={script_dir}\n"
        f"GENESIS_ROOT={tmp_path}\n"
        + _extract_sync_function()
        + '\n_sync_deploy_targets\necho "DEGRADED=$HOST_CC_DEGRADED"\n'
    )
    result = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "host sync SKIPPED" in result.stdout
    assert "DEGRADED=guardian_config_unreadable" in result.stdout
