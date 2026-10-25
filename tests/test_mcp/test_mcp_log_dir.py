"""GENESIS_MCP_LOG_DIR moves the MCP log handler that importing the browser module attaches.

The handler is attached at import, in every process that imports the module, so
the test runs the import in a child with a scratch HOME: no real log is opened.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SCRIPT = """
import logging
import genesis.mcp.health.browser as browser
files = [h.baseFilename for h in logging.getLogger("genesis").handlers
         if isinstance(h, logging.FileHandler)]
print("FILES=" + repr(files))
print("TS=" + str(browser._TS_LOG_PATH))
"""


def _run(tmp_path: Path, log_dir: str | None) -> dict[str, str]:
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True)
    env = {"PATH": os.defpath, "HOME": str(home), "PYTHONPATH": str(repo / "src")}
    if log_dir is not None:
        env["GENESIS_MCP_LOG_DIR"] = log_dir
    out = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    return dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)


def test_unset_keeps_the_home_tmp_log(tmp_path):
    got = _run(tmp_path, None)
    home_tmp = tmp_path / "home" / "tmp"
    assert got["FILES"] == repr([str(home_tmp / "mcp_health.log")])
    assert got["TS"] == str(home_tmp / "turnstile_debug.log")


def test_the_variable_moves_both_logs(tmp_path):
    elsewhere = tmp_path / "logs"
    elsewhere.mkdir()
    got = _run(tmp_path, str(elsewhere))
    assert got["FILES"] == repr([str(elsewhere / "mcp_health.log")])
    assert got["TS"] == str(elsewhere / "turnstile_debug.log")
    assert not (tmp_path / "home" / "tmp" / "mcp_health.log").exists()


def test_a_missing_directory_attaches_no_handler(tmp_path):
    got = _run(tmp_path, str(tmp_path / "absent"))
    assert got["FILES"] == "[]"
    assert not (tmp_path / "absent").exists()


def test_a_home_relative_value_is_expanded(tmp_path):
    (tmp_path / "home" / "logs").mkdir(parents=True)
    got = _run(tmp_path, "~/logs")
    logs = tmp_path / "home" / "logs"
    assert got["FILES"] == repr([str(logs / "mcp_health.log")])
    assert got["TS"] == str(logs / "turnstile_debug.log")
