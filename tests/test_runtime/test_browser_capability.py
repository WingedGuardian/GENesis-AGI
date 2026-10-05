"""browser_automation capability: engine readiness -> capabilities.json.

A launchable Camoufox engine surfaces as `ok` (active); anything else as
`degraded` with the reason logged, never `failed`: the probe must not raise.
It must also never call camoufox's own path lookup, which can delete an engine
and download another (see genesis.browser.engine).
"""

from __future__ import annotations

import logging
from pathlib import Path

from genesis.browser import engine
from genesis.runtime import GenesisRuntime


def _bare_runtime():
    rt = GenesisRuntime.__new__(GenesisRuntime)
    rt._bootstrap_manifest = {}
    rt._browser_engine_path = None
    return rt


def test_ready_engine_is_active(monkeypatch, tmp_path):
    status = engine.EngineStatus(engine.READY, "Camoufox 156.0.1-beta.34", tmp_path)
    monkeypatch.setattr(engine, "camoufox_engine_status", lambda: status)
    rt = _bare_runtime()
    rt._run_init_step("browser_automation", rt._init_browser_automation)
    assert rt._bootstrap_manifest["browser_automation"] == "ok"
    assert rt._browser_engine_path == str(tmp_path)


def test_missing_engine_is_degraded_with_reason(monkeypatch, caplog):
    status = engine.EngineStatus(
        engine.LEGACY_LAYOUT, f"pre-0.5 engine; {engine.PROVISION_HINT}"
    )
    monkeypatch.setattr(engine, "camoufox_engine_status", lambda: status)
    rt = _bare_runtime()
    with caplog.at_level(logging.INFO):
        rt._run_init_step("browser_automation", rt._init_browser_automation)
    assert rt._bootstrap_manifest["browser_automation"] == "degraded"
    assert "pre-0.5 engine" in caplog.text and engine.PROVISION_HINT in caplog.text


def test_probe_error_degrades_never_fails(monkeypatch):
    def boom():
        raise RuntimeError("unexpected")

    monkeypatch.setattr(engine, "camoufox_engine_status", boom)
    rt = _bare_runtime()
    rt._run_init_step("browser_automation", rt._init_browser_automation)
    assert rt._bootstrap_manifest["browser_automation"] == "degraded"


def test_status_module_never_imports_camoufox():
    """engine.py must stay a pure file reader: importing camoufox would make the
    probe depend on (and risk calling) the package's destructive path lookup."""
    import ast

    tree = ast.parse(Path(engine.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "camoufox" not in imported
    # platformdirs is allowed: it is camoufox's own path resolver, with no side effects.
