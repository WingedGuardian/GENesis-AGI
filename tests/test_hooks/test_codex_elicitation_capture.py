"""Collection/capture controls use fake executable bytes, never execute them."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_hooks import codex_elicitation_capture as capture


def setup_capture(tmp_path, monkeypatch):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("codex", "codex-code-mode-host"):
        (binaries / name).write_bytes(b"fake non-executable test bytes")
    for name in ("GENESIS_CODEX_APP_SERVER_BIN", "GENESIS_CODEX_CLI_BIN"):
        monkeypatch.setenv(name, str(binaries / "codex"))
    for name, value in {"QUALIFICATION_HEAD": "head", "QUALIFICATION_BASE": "base",
                        "ELICITATION_RUN_ID": "123", "ELICITATION_ATTEMPT": "1",
                        "ELICITATION_MANIFEST": str(tmp_path / "expected.json"),
                        "ELICITATION_REPORT": str(tmp_path / "report.json")}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(capture, "_cases", {})
    monkeypatch.setattr(capture, "_manifest_digest", None)
    session = SimpleNamespace(config=SimpleNamespace(rootpath=tmp_path, option=SimpleNamespace(
        collectonly=True, basetemp=str(tmp_path / "native"))), items=[
            SimpleNamespace(nodeid=f"{capture.PREFIX}[{case}]") for case in capture.CASES])
    return session


@pytest.mark.parametrize("damage", [None, "missing", "extra", "duplicate", "binary", "source", "run", "attempt"])
def test_capture_freezes_exact_collection_source_binary_and_run(tmp_path, monkeypatch, damage):
    session = setup_capture(tmp_path, monkeypatch)
    capture.pytest_collection_finish(session)
    original = (tmp_path / "expected.json").read_bytes()
    session.config.option.collectonly = False
    if damage == "missing":
        session.items.pop()
    elif damage == "extra":
        session.items.append(SimpleNamespace(nodeid="extra"))
    elif damage == "duplicate":
        session.items[-1] = session.items[0]
    elif damage == "binary":
        (tmp_path / "bin/codex").write_bytes(b"changed binary")
    elif damage == "source":
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts/changed.py").write_text("changed")
    elif damage in {"run", "attempt"}:
        monkeypatch.setenv("ELICITATION_RUN_ID" if damage == "run" else "ELICITATION_ATTEMPT", "stale")
    if damage:
        with pytest.raises(AssertionError):
            capture.pytest_collection_finish(session)
    else:
        capture.pytest_collection_finish(session)
    assert (tmp_path / "expected.json").read_bytes() == original


def test_capture_retains_failed_phase_and_raw_receipt(tmp_path, monkeypatch):
    session = setup_capture(tmp_path, monkeypatch)
    capture.pytest_collection_finish(session)
    session.config.option.collectonly = False
    root = Path(session.config.option.basetemp) / "case"
    root.mkdir(parents=True)
    (root / "elicitation-evidence.json").write_text('{"incomplete": true}')
    item = SimpleNamespace(nodeid=session.items[0].nodeid, config=session.config, funcargs={"tmp_path": root})
    for phase in ("setup", "call", "teardown"):
        hook = capture.pytest_runtest_makereport(item, None)
        next(hook)
        report = SimpleNamespace(when=phase, outcome="failed" if phase == "call" else "passed")
        with pytest.raises(StopIteration):
            hook.send(SimpleNamespace(get_result=lambda report=report: report))
    capture.pytest_sessionfinish(session, 1)
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["exitstatus"] == 1
    assert report["cases"][item.nodeid] == {"phases": [["setup", "passed"], ["call", "failed"], ["teardown", "passed"]],
                                              "receipt": "case/elicitation-evidence.json"}
    assert "case/elicitation-evidence.json" in report["artifacts"]
    assert report["manifest_digest"] == capture._manifest_digest
