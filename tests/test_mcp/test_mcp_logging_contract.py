"""Cold imports use only scratch homes; no browser or service is launched."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_CHILD = r'''
import json, logging, os, sys
from pathlib import Path
options = json.loads(sys.argv[1])
if options.get("named"):
    import pwd
    from types import SimpleNamespace
    original = pwd.getpwnam
    pwd.getpwnam = lambda name: SimpleNamespace(pw_dir=options["named"]) if name == "mcp-fixture" else original(name)
if options.get("open_error"):
    original = logging.FileHandler.__init__
    def initialize(self, filename, *args, **kwargs):
        if Path(filename).name == "mcp_health.log":
            raise PermissionError("synthetic optional-output failure")
        original(self, filename, *args, **kwargs)
    logging.FileHandler.__init__ = initialize
import genesis.mcp.health.browser as browser
os.chdir(options["after"])
logging.getLogger("genesis.contract-fixture").warning("synthetic-health")
browser._ts_log_write("synthetic-debug")
handlers = [h for h in logging.getLogger("genesis").handlers if isinstance(h, logging.FileHandler)]
print("CONTRACT=" + json.dumps({
    "source": browser.__file__,
    "files": [h.baseFilename for h in handlers],
    "ts": str(browser._TS_LOG_PATH) if browser._TS_LOG_PATH is not None else None,
    "filtered": [browser.CARD_FILTER in h.filters for h in handlers],
}))
'''


def _cold(tmp_path, value, *, named=None, open_error=False):
    root = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    sentinel = home / "tmp" / "mcp_health.log"
    sentinel.write_text("default-sentinel\n")
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    env = {"PATH": os.defpath, "HOME": str(home), "PYTHONPATH": str(root / "src")}
    if value is not None:
        env["GENESIS_MCP_LOG_DIR"] = value
    options = {"after": str(after), "named": str(named) if named else None, "open_error": open_error}
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, json.dumps(options)], cwd=before,
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.startswith("CONTRACT=")]
    assert len(lines) == 1, result.stdout
    got = json.loads(lines[0].removeprefix("CONTRACT="))
    assert Path(got["source"]).resolve() == root / "src/genesis/mcp/health/browser.py"
    return got, sentinel


@pytest.mark.parametrize("kind", ["relative", "tilde", "named", "absolute", "spaces", "unicode", "newline"])
def test_both_writers_keep_one_fixed_directory_after_chdir(tmp_path, kind):
    home = tmp_path / "home"
    names = {"spaces": "log space", "unicode": "logs-λ", "newline": "logs\n\n"}
    destination = home / names.get(kind, "logs")
    destination.mkdir(parents=True)
    value = str(destination)
    if kind == "relative":
        value = "logs"
    elif kind == "tilde":
        value = "~/logs"
    elif kind == "named":
        value = "~mcp-fixture/logs"
    got, sentinel = _cold(tmp_path, value, named=home if kind == "named" else None)
    assert got["files"] == [str(destination / "mcp_health.log")]
    assert got["ts"] == str(destination / "turnstile_debug.log")
    assert got["filtered"] == [True], "main's existing card-redaction constraint must remain"
    assert "synthetic-health" in (destination / "mcp_health.log").read_text()
    assert "synthetic-debug" in (destination / "turnstile_debug.log").read_text()
    assert sentinel.read_text() == "default-sentinel\n"


@pytest.mark.parametrize("kind", ["unknown-user", "missing", "not-directory", "log-is-directory", "open-error"])
def test_unusable_override_disables_both_outputs_without_default_fallback(tmp_path, kind):
    destination = tmp_path / "logs"
    value = str(destination)
    if kind == "unknown-user":
        value = "~mcp-fixture-user-that-does-not-exist/logs"
    elif kind == "not-directory":
        destination.write_text("keep\n")
    elif kind in {"log-is-directory", "open-error"}:
        destination.mkdir()
        if kind == "log-is-directory":
            (destination / "mcp_health.log").mkdir()
    got, sentinel = _cold(tmp_path, value, open_error=kind == "open-error")
    assert got["files"] == []
    assert got["ts"] is None
    assert sentinel.read_text() == "default-sentinel\n"
    assert not (sentinel.parent / "turnstile_debug.log").exists()
    assert not (destination / "turnstile_debug.log").exists()
