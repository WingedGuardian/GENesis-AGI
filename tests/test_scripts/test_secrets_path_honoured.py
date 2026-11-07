"""Every secrets.env loader honours SECRETS_PATH, as env.secrets_path() does.

The four scripts below load secrets.env at IMPORT time, before `genesis` is
importable, so each resolves the path itself. Each is run in a child whose
`dotenv.load_dotenv` is replaced by a probe that prints the path it was handed
and exits on the spot. A script whose guarded load is skipped (no file at its
default path) runs its module top level to the end instead; each keeps `main()`
behind a `__main__` guard, which `run_name="secrets_probe"` never satisfies.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = [
    "scripts/genesis_session_context.py",
    "scripts/genesis_urgent_alerts.py",
    "scripts/gmodel",
    "scripts/proactive_memory_hook.py",
]

_CHILD = """
import os, runpy, sys, types
probe = types.ModuleType("dotenv")
def load_dotenv(path=None, *a, **k):
    print("LOADED", os.fspath(path), flush=True)
    os._exit(0)
probe.load_dotenv = load_dotenv
sys.modules["dotenv"] = probe
runpy.run_path(sys.argv[1], run_name="secrets_probe")
print("NO-LOAD", flush=True)
"""


def _loaded(script: str, tmp_path: Path, secrets: Path | None) -> str:
    return _loaded_with(script, tmp_path, None if secrets is None else str(secrets))


def _loaded_with(script: str, tmp_path: Path, secrets: str | None) -> str:
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(tmp_path),
        "GENESIS_HOME": str(tmp_path / ".genesis"),
        "GENESIS_GMODEL_REEXEC": "1",  # gmodel: never re-exec under a venv here
    }
    if secrets is not None:
        env["SECRETS_PATH"] = secrets
    out = subprocess.run(
        [sys.executable, "-c", _CHILD, str(REPO / script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    lines = [ln for ln in out.stdout.splitlines() if ln.startswith(("LOADED", "NO-LOAD"))]
    assert lines, (out.returncode, out.stdout[-2000:], out.stderr[-2000:])
    return lines[0]


@pytest.mark.parametrize("script", SCRIPTS)
def test_secrets_path_names_the_file_each_script_loads(script, tmp_path):
    secrets = tmp_path / "chosen" / "secrets.env"
    secrets.parent.mkdir()
    secrets.write_text("GENESIS_SECRETS_PROBE=1\n")
    assert _loaded(script, tmp_path, secrets) == f"LOADED {secrets}"


@pytest.mark.parametrize("script", SCRIPTS)
def test_a_home_relative_secrets_path_is_expanded(script, tmp_path):
    """As env.secrets_path() does: a value from a unit file or YAML keeps its `~`."""
    (tmp_path / "s.env").write_text("GENESIS_SECRETS_PROBE=1\n")
    env_value = Path("~/s.env")
    assert _loaded_with(script, tmp_path, str(env_value)) == f"LOADED {tmp_path / 's.env'}"


@pytest.mark.parametrize("script", SCRIPTS)
def test_without_secrets_path_the_repo_file_is_still_the_one(script, tmp_path):
    """Behaviour-neutral where SECRETS_PATH is unset (every install today)."""
    line = _loaded(script, tmp_path, None)
    repo_file = REPO / "secrets.env"
    if line == "NO-LOAD":
        assert not repo_file.is_file()  # the is_file()-guarded loaders skip it
    else:
        assert line == f"LOADED {repo_file}"


def test_reflection_golden_set_reads_secrets_path(tmp_path, monkeypatch):
    from genesis.eval import reflection_golden_set as mod

    secrets = tmp_path / "secrets.env"
    secrets.write_text("GENESIS_SECRETS_PROBE_RGS=from-secrets-path\n")
    monkeypatch.setenv("SECRETS_PATH", str(secrets))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # setenv first, so teardown restores ABSENCE (a bare delenv of an absent
    # key records nothing, and the value _ensure_secrets() writes would leak).
    monkeypatch.setenv("GENESIS_SECRETS_PROBE_RGS", "x")
    monkeypatch.delenv("GENESIS_SECRETS_PROBE_RGS")
    monkeypatch.setattr(mod, "_secrets_loaded", False)
    mod._ensure_secrets()
    assert os.environ["GENESIS_SECRETS_PROBE_RGS"] == "from-secrets-path"


def test_reflection_golden_set_skips_the_home_fallback_under_secrets_path(tmp_path, monkeypatch):
    from genesis.eval import reflection_golden_set as mod

    home = tmp_path / "home"
    (home / "genesis").mkdir(parents=True)
    (home / "genesis" / "secrets.env").write_text("GENESIS_SECRETS_PROBE_RGS=from-home\n")
    monkeypatch.setenv("SECRETS_PATH", str(tmp_path / "absent.env"))
    monkeypatch.setattr(Path, "home", lambda: home)
    # setenv first, so teardown restores ABSENCE (a bare delenv of an absent
    # key records nothing, and the value _ensure_secrets() writes would leak).
    monkeypatch.setenv("GENESIS_SECRETS_PROBE_RGS", "x")
    monkeypatch.delenv("GENESIS_SECRETS_PROBE_RGS")
    monkeypatch.setattr(mod, "_secrets_loaded", False)
    mod._ensure_secrets()
    assert "GENESIS_SECRETS_PROBE_RGS" not in os.environ


def test_the_guardian_bridge_defaults_to_secrets_path(tmp_path, monkeypatch):
    from genesis.guardian import credential_bridge

    chosen = tmp_path / "secrets.env"
    monkeypatch.setenv("SECRETS_PATH", str(chosen))
    assert credential_bridge._container_secrets() == chosen
