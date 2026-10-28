"""Host-safe fixture tests; never invoke an upstream executable locally."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "scripts/ci/headlong_fixture.py"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/ci/{name}.py")
    module = importlib.util.module_from_spec(spec)
    if name == "headlong_controls":
        with patch.dict(sys.modules, {"headlong_fixture": load("headlong_fixture")}):
            spec.loader.exec_module(module)
    else:
        spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("kind", ["effect", "noop", "failure"])
def test_fixture_adapter_process(tmp_path, kind):
    messages = [{"role": "user", "content": "earlier"},
                {"role": "assistant", "content": "earlier answer"},
                {"role": "user", "content": "current résumé # ' $()"}]
    system = tmp_path / "system"
    system.write_text("separate system")
    result = subprocess.run(
        [sys.executable, str(FIXTURE), "--model", "synthetic", "--max-tokens", "99",
         "--system-prompt-file", str(system), "--thinking", "high"],
        input=json.dumps(messages), text=True, capture_output=True,
        env={"PATH": "/usr/bin:/bin", "CONTROL_KIND": kind,
             "CONTROL_CAPTURE": str(tmp_path / "capture.jsonl"), "TEXT_ROUTE": "fixed"},
        timeout=10,
    )
    assert result.returncode == (7 if kind == "failure" else 0), result.stderr
    capture = json.loads((tmp_path / "capture.jsonl").read_text())
    assert capture["messages"] == messages
    assert capture["system"] == system.read_text()
    assert capture["thinking"] == "high"
    assert ("effect.txt" in result.stdout) == (kind == "effect")
    assert not (tmp_path / "effect.txt").exists()


@pytest.mark.parametrize("defect", ["empty", "error", "missing-result", "tool", "mismatch"])
def test_cli_output_rejects_incomplete_or_nontext(defect):
    fixture = load("headlong_fixture")
    rows = fixture.cli_records("synthetic text")
    if defect == "empty":
        rows = []
    elif defect == "error":
        rows[-1]["is_error"] = True
    elif defect == "missing-result":
        rows.pop()
    elif defect == "tool":
        rows[0]["message"]["content"].append({"type": "tool_use"})
    else:
        rows[-1]["result"] = "different"
    with pytest.raises(ValueError):
        fixture.cli_text("\n".join(json.dumps(row) for row in rows))


def test_cli_text_requires_matching_success():
    fixture = load("headlong_fixture")
    output = "\n".join(json.dumps(row) for row in fixture.cli_records("hello résumé\nworld"))
    assert fixture.cli_text(output) == "hello résumé\nworld"


def test_cli_adapter_preserves_complete_history_and_system(tmp_path, monkeypatch):
    fixture = load("headlong_fixture")
    fake = tmp_path / "fake-claude"
    fake.write_bytes(FIXTURE.read_bytes())
    fake.chmod(0o755)
    messages = [{"role": "user", "content": "first"},
                {"role": "assistant", "content": "past response"},
                {"role": "user", "content": "now"}]
    command = [str(fake), "--output-format", "stream-json", "--input-format", "stream-json",
               "--verbose", "--tools", "", "--mcp-config", '{"mcpServers":{}}',
               "--strict-mcp-config", "--setting-sources=", "--system-prompt", "system"]
    monkeypatch.setattr(fixture, "sdk_command", lambda model, system: command)
    monkeypatch.setenv("CONTROL_KIND", "noop")
    monkeypatch.setenv("CONTROL_CAPTURE", str(tmp_path / "capture.jsonl"))
    assert fixture.cli_completion(messages, "system", "synthetic") == fixture.response("noop")
    capture = json.loads((tmp_path / "capture.jsonl").read_text())
    assert capture["messages"] == messages and capture["system"] == "system"


@pytest.mark.parametrize("kind", ["effect", "noop", "failure"])
def test_route_predicate_checks_actual_evidence(kind):
    controls = load("headlong_controls")
    rows = [{"type": "shellm-run", "step_id": "run"},
            {"type": "final", "run_id": "run", "content": "done" if kind == "effect" else "no-op"}]
    evidence = {"returncode": 7 if kind == "failure" else 0,
                "effect": b"synthetic-effect\n" if kind == "effect" else None,
                "run_id": "run", "trajectory": rows if kind != "failure" else rows[:1],
                "capture_verified": True}
    assert controls.qualify(kind, evidence)
    assert not controls.qualify(kind, {**evidence, "capture_verified": False})
    assert not controls.qualify(kind, {**evidence, "effect": b"corrupted"})
    if kind != "failure":
        assert not controls.qualify(kind, {**evidence, "returncode": 7})
        assert not controls.qualify(kind, {**evidence, "trajectory": rows[:1]})
        assert not controls.qualify(kind, {**evidence, "run_id": "other"})


@pytest.mark.parametrize("output", ["", "cases: pass=0 fail=0 skip=0", "cases: pass=1 fail=0 skip=1",
                                    "1 passed, 2 failed", "ok one\nFAIL two"])
def test_upstream_empty_skipped_failed_is_not_success(output):
    assert not load("headlong_controls").upstream_passed(output, minimum=1)


@pytest.mark.parametrize("output", ["cases: pass=3 fail=0 skip=0", "3 passed, 0 failed",
                                    "PASS test_one\n7 passed, 0 failed"])
def test_upstream_requires_actual_assertions(output):
    assert load("headlong_controls").upstream_passed(output, minimum=3)
    assert not load("headlong_controls").upstream_passed(output, minimum=8)


def test_dependency_inventory_is_retained_whole(tmp_path):
    controls = load("headlong_controls")
    (tmp_path / "packages.txt").write_text("bash\t5.2\njq\t1.6\n")
    (tmp_path / "python-packages.txt").write_text("claude-agent-sdk==0.2.172\n" + "dependency==1\n" * 200)
    result = controls.dependency_inventory(tmp_path)
    assert result == {"os": (tmp_path / "packages.txt").read_text(),
                      "python": (tmp_path / "python-packages.txt").read_text()}


def test_final_report_retains_inventory(tmp_path, monkeypatch, capsys):
    import tempfile

    controls = load("headlong_controls")
    inventory = {"os": "bash\t5.2\n", "python": "claude-agent-sdk==0.2.172\n"}
    monkeypatch.setattr(controls, "preflight", lambda: None)
    monkeypatch.setattr(controls, "tree_hash", lambda root: "fixed-source-hash")
    monkeypatch.setattr(controls, "dependency_inventory", lambda: inventory, raising=False)
    monkeypatch.setattr(controls.importlib.metadata, "version", lambda name: "0.2.172")
    monkeypatch.setattr(controls, "TemporaryDirectory", lambda **kwargs: tempfile.TemporaryDirectory(dir=tmp_path))
    monkeypatch.setattr(controls, "run_upstream", lambda root: [{"passed": True}] * 8)
    monkeypatch.setattr(controls, "run_route", lambda root, route, kind: {"route": route, "control": kind, "passed": True})
    assert controls.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dependencies"] == inventory
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("route", ["fixed", "http", "cli"])
def test_request_capture_requires_current_complete_transport(route):
    controls = load("headlong_controls")
    source = "http" if route == "http" else "adapter"
    original = {"source": source, "system": "system", "messages": [{"role": "user", "content": "current-marker"}]}
    captures = [original] if route != "cli" else [original, {**original, "source": "fake-cli"}]
    assert controls.request_verified(route, captures, "current-marker")
    assert not controls.request_verified(route, [], "current-marker")
    assert not controls.request_verified(route, captures, "missing-marker")
    assert not controls.request_verified(route, captures * 2, "current-marker")
    assert not controls.request_verified(route, [{**row, "system": ""} for row in captures], "current-marker")
    summaries = [{**row, "system": "You produce concise summaries as JSON."} for row in captures]
    assert not controls.request_verified(route, summaries, "current-marker")
    if route == "cli":
        assert not controls.request_verified(route, [original], "current-marker")
        changed = {**captures[-1], "messages": [{"role": "user", "content": "current-marker altered"}]}
        assert not controls.request_verified(route, [original, changed], "current-marker")
        changed = {**captures[-1], "system": "altered system"}
        assert not controls.request_verified(route, [original, changed], "current-marker")


def test_a_route_that_fails_before_writing_its_run_id_is_reported_failed(tmp_path, monkeypatch):
    controls = load("headlong_controls")
    monkeypatch.setattr(
        controls.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, stdout="", stderr="startup failed"),
    )
    report = controls.run_route(tmp_path / "adapter-failure", "adapter", "failure")
    assert report["run_id"] == "" and report["passed"] is False
    assert report["stderr"] == "startup failed"
