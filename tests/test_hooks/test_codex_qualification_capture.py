"""Collection/source binding without native execution or Docker."""

import json
from types import SimpleNamespace

import pytest

from tests.test_hooks import codex_qualification as capture


@pytest.mark.parametrize("drift", ["none", "source", "binary", "census", "test-tools"])
def test_collection_is_frozen_before_execution(tmp_path, monkeypatch, drift):
    source = tmp_path / "src/witness.py"
    source.parent.mkdir()
    source.write_text("fixture = True\n")
    test_tools = tmp_path / ".github/test-tools/requirements.txt"
    test_tools.parent.mkdir(parents=True)
    test_tools.write_text("pytest==9.2.0\n")
    binary = tmp_path / "codex"
    binary.write_bytes(b"fixture executable; never executed")
    binary.with_name("codex-code-mode-host").write_bytes(b"fixture companion")
    manifest = tmp_path / "expected.json"
    for key, value in {"GENESIS_CODEX_APP_SERVER_BIN": binary, "QUALIFICATION_HEAD": "head",
                       "QUALIFICATION_BASE": "base", "QUALIFICATION_MANIFEST": manifest}.items():
        monkeypatch.setenv(key, str(value))
    session = SimpleNamespace(config=SimpleNamespace(rootpath=tmp_path, option=SimpleNamespace(collectonly=True)),
                              items=[SimpleNamespace(nodeid="fixture-case")])
    capture.pytest_collection_finish(session)
    frozen = json.loads(manifest.read_text())
    assert frozen["cases"] == ["fixture-case"] and frozen["sources"]["src/witness.py"]
    session.config.option.collectonly = False
    if drift == "source":
        source.write_text("fixture = False\n")
    elif drift == "binary":
        binary.write_bytes(b"different fixture")
    elif drift == "census":
        session.items = []
    elif drift == "test-tools":
        test_tools.write_text("pytest==0.0.0\n")
    if drift == "none":
        capture.pytest_collection_finish(session)
    else:
        with pytest.raises(AssertionError, match="identity drift"):
            capture.pytest_collection_finish(session)
