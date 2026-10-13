"""browser_automation capability: is a Camoufox engine launchable on this install?

The runtime asks ``python -m genesis.browser.engine --json`` in a subprocess, so
genesis-server never imports camoufox (which imports playwright). A launchable
engine is ``active``; anything else, including a probe that fails, hangs or prints
garbage, is ``degraded``, never ``failed``. These tests run real subprocesses.
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

from genesis.runtime import GenesisRuntime, _init_delegates
from genesis.runtime._capabilities import _CAPABILITY_DESCRIPTIONS


def _bare_runtime():
    rt = GenesisRuntime.__new__(GenesisRuntime)
    rt._bootstrap_manifest = {}
    rt._browser_engine_path = None
    return rt


def _probe(monkeypatch, code: str, timeout: float | None = None):
    monkeypatch.setattr(_init_delegates, "_BROWSER_PROBE_CMD", (sys.executable, "-c", code))
    if timeout is not None:
        monkeypatch.setattr(_init_delegates, "_BROWSER_PROBE_TIMEOUT_S", timeout)


async def _run(rt):
    await rt._run_init_step_async("browser_automation", rt._init_browser_automation)


def test_capability_is_mapped_and_described():
    # Without the _INIT_CHECKS entry the step can never record "degraded".
    assert GenesisRuntime._INIT_CHECKS.get("browser_automation") == "_browser_engine_path"
    assert "browser_automation" in _CAPABILITY_DESCRIPTIONS


def test_probe_runs_the_engine_module():
    assert _init_delegates._BROWSER_PROBE_CMD[1:] == ("-m", "genesis.browser.engine", "--json")
    assert _init_delegates._BROWSER_PROBE_TIMEOUT_S == 60


@pytest.mark.asyncio
async def test_ready_engine_is_active(monkeypatch):
    status = {"kind": "ready", "detail": "Camoufox", "path": "/engines/camoufox-bin"}
    _probe(monkeypatch, f"print('noise'); print({json.dumps(json.dumps(status))})")
    rt = _bare_runtime()
    await _run(rt)
    assert rt._browser_engine_path == "/engines/camoufox-bin"
    assert rt._bootstrap_manifest["browser_automation"] == "ok"


@pytest.mark.asyncio
async def test_missing_engine_is_degraded_with_its_reason(monkeypatch, caplog):
    status = {
        "kind": "not_installed",
        "detail": "Camoufox official 156.0.1-beta.34 is not installed",
        "path": None,
    }
    _probe(monkeypatch, f"print({json.dumps(json.dumps(status))})")
    rt = _bare_runtime()
    with caplog.at_level("INFO"):
        await _run(rt)
    assert rt._browser_engine_path is None
    assert rt._bootstrap_manifest["browser_automation"] == "degraded"
    assert "156.0.1-beta.34" in caplog.text


@pytest.mark.asyncio
async def test_a_broken_engine_with_a_path_is_degraded(monkeypatch):
    """INVALID names the broken binary; a path alone must not read as launchable."""
    status = {
        "kind": "invalid",
        "detail": "camoufox-bin is not executable",
        "path": "/engines/camoufox-bin",
    }
    _probe(monkeypatch, f"print({json.dumps(json.dumps(status))})")
    rt = _bare_runtime()
    await _run(rt)
    assert rt._browser_engine_path is None
    assert rt._bootstrap_manifest["browser_automation"] == "degraded"


@pytest.mark.asyncio
async def test_a_probe_that_fails_after_printing_ready_is_degraded(monkeypatch):
    status = {"kind": "ready", "detail": "Camoufox", "path": "/engines/camoufox-bin"}
    _probe(monkeypatch, f"import sys; print({json.dumps(json.dumps(status))}); sys.exit(1)")
    rt = _bare_runtime()
    await _run(rt)
    assert rt._bootstrap_manifest["browser_automation"] == "degraded"


@pytest.mark.parametrize(
    "code",
    [
        "print('not json')",
        "import sys; sys.exit(3)",
        'print(\'{"kind": "ready"}\')',  # no detail: malformed
        "",
    ],
)
@pytest.mark.asyncio
async def test_a_broken_probe_degrades_never_fails(monkeypatch, code):
    _probe(monkeypatch, code)
    rt = _bare_runtime()
    await _run(rt)
    assert rt._browser_engine_path is None
    assert rt._bootstrap_manifest["browser_automation"] == "degraded"


@pytest.mark.asyncio
async def test_a_hung_probe_is_killed_and_reaped(monkeypatch, tmp_path, caplog):
    pid_file = tmp_path / "pid"
    _probe(
        monkeypatch,
        f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)",
        timeout=1.0,
    )
    rt = _bare_runtime()
    started = time.monotonic()
    with caplog.at_level("WARNING"):
        await _run(rt)
    assert time.monotonic() - started < 10
    assert rt._bootstrap_manifest["browser_automation"] == "degraded"
    assert "timed out" in caplog.text
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):  # a zombie would still answer
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_a_probe_that_cannot_start_degrades(monkeypatch):
    monkeypatch.setattr(_init_delegates, "_BROWSER_PROBE_CMD", ("/nonexistent/python", "-c", ""))
    rt = _bare_runtime()
    await _run(rt)
    assert rt._bootstrap_manifest["browser_automation"] == "degraded"
