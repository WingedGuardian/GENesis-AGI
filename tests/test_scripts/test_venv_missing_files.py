"""An upgrade that deletes a distribution's own files must be repaired, not shipped.

pip upgrading FastMCP 2 to 4 in place installs fastmcp-slim's ``fastmcp/`` files,
then uninstalls fastmcp 2.14.6, whose RECORD lists the same paths, and deletes
them: metadata says installed, ``pip check`` passes, and ``from fastmcp import
FastMCP`` fails. MEASURED on a live install's update: the new server never
answered its health check and the update rolled back. Reproduced in a fresh
venv; force-reinstalling fastmcp-slim at the installed version repairs it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIB = _REPO_ROOT / "scripts" / "lib" / "venv_setup.sh"
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import venv_missing_files  # noqa: E402


def _dist(site: Path, name: str, version: str, files: dict[str, bool]) -> metadata.Distribution:
    """A dist-info whose RECORD lists ``files``; only those mapped True exist."""
    info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    rows = [f"{path},sha256=x,1" for path in files] + [f"{info.name}/RECORD,,"]
    (info / "RECORD").write_text("\n".join(rows) + "\n")
    for path, present in files.items():
        if present and not path.startswith(".."):
            target = site / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("")
    return metadata.PathDistribution(info)


def test_a_distribution_missing_a_recorded_module_is_named(tmp_path: Path) -> None:
    site = tmp_path / "site"
    damaged = _dist(
        site, "fastmcp-slim", "4.1.4", {"fastmcp/__init__.py": False, "fastmcp/server.py": True}
    )
    intact = _dist(site, "httpx", "0.28.1", {"httpx/__init__.py": True})
    assert venv_missing_files.damaged([damaged, intact]) == ["fastmcp-slim==4.1.4"]


def test_it_reads_record_itself_because_files_hides_missing_entries(tmp_path: Path) -> None:
    """From Python 3.12, Distribution.files drops entries whose file is missing,
    so a check built on it reports nothing for exactly the damaged case."""
    dist = _dist(tmp_path / "site", "fastmcp-slim", "4.1.4", {"fastmcp/__init__.py": False})
    assert "fastmcp/__init__.py" not in [str(f) for f in dist.files or []]
    assert venv_missing_files.damaged([dist]) == ["fastmcp-slim==4.1.4"]


def test_bytecode_and_out_of_tree_entries_are_not_damage(tmp_path: Path) -> None:
    dist = _dist(
        tmp_path / "site",
        "tool",
        "1.0",
        {
            "tool/__init__.py": True,
            "tool/__pycache__/__init__.cpython-312.pyc": False,
            "tool/extra.pyc": False,
            "../../../bin/tool": False,
        },
    )
    assert venv_missing_files.damaged([dist]) == []


def test_a_distribution_without_a_record_is_skipped(tmp_path: Path) -> None:
    info = tmp_path / "site" / "legacy-1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: legacy\nVersion: 1.0\n")
    assert venv_missing_files.damaged([metadata.PathDistribution(info)]) == []


def _venv(tmp_path: Path, detector_output: str, detector_rc: int = 0) -> tuple[Path, Path]:
    """A venv whose python answers the detector with ``detector_output`` and
    passes every other call; pip and python append their arguments to one log."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    log = tmp_path / "calls"
    (venv / "bin" / "pip").write_text(f'#!/bin/sh\necho "pip $*" >> "{log}"\n')
    (venv / "bin" / "python").write_text(
        "#!/bin/sh\n"
        f'echo "python $*" >> "{log}"\n'
        f'case "$*" in *venv_missing_files.py*) printf "{detector_output}"; exit {detector_rc};; esac\n'
        "exit 0\n"
    )
    for name in ("pip", "python"):
        (venv / "bin" / name).chmod(0o755)
    return venv, log


def _heal(repo: Path, venv: Path) -> subprocess.CompletedProcess:
    script = f'set -euo pipefail; source "{_LIB}"; reinstall_damaged_distributions "{repo}" "{venv}"; echo rc=$?'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)


def test_each_damaged_distribution_is_reinstalled_at_its_version_without_deps(
    tmp_path: Path,
) -> None:
    venv, log = _venv(tmp_path, "fastmcp-slim==4.1.4\\nother==2.0\\n")
    res = _heal(tmp_path, venv)
    assert res.returncode == 0 and "rc=0" in res.stdout, res.stdout + res.stderr
    pips = [line for line in log.read_text().splitlines() if line.startswith("pip ")]
    assert pips == [
        "pip install --quiet --no-deps --force-reinstall fastmcp-slim==4.1.4",
        "pip install --quiet --no-deps --force-reinstall other==2.0",
    ]
    assert "Reinstalling fastmcp-slim==4.1.4" in res.stdout


def test_nothing_damaged_means_no_pip_call_and_no_output(tmp_path: Path) -> None:
    venv, log = _venv(tmp_path, "")
    res = _heal(tmp_path, venv)
    assert res.stdout.strip() == "rc=0"
    assert not [line for line in log.read_text().splitlines() if line.startswith("pip ")]


def test_a_failing_detector_never_aborts_the_install(tmp_path: Path) -> None:
    """Callers run under set -euo pipefail; the import check stays the verdict."""
    venv, _ = _venv(tmp_path, "", detector_rc=1)
    res = _heal(tmp_path, venv)
    assert res.returncode == 0 and "rc=0" in res.stdout, res.stdout + res.stderr


def test_the_guarded_install_heals_between_pip_and_the_import_check(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    venv, log = _venv(tmp_path, "fastmcp-slim==4.1.4\\n")
    script = f'source "{_LIB}"; editable_install_guarded "{repo}" "{venv}"'
    res = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=120, env={**os.environ}
    )
    assert res.returncode == 0, res.stdout + res.stderr
    calls = log.read_text().splitlines()
    install = next(i for i, c in enumerate(calls) if c.startswith(f"pip install -e {repo}"))
    detect = next(i for i, c in enumerate(calls) if "venv_missing_files.py" in c)
    heal = calls.index("pip install --quiet --no-deps --force-reinstall fastmcp-slim==4.1.4")
    check = next(
        i for i, c in enumerate(calls) if "import genesis.mcp.health" in c
    )
    assert install < detect < heal < check
