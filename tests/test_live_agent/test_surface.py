"""Speech is declared where identity is injected, not filtered from responses."""

import importlib.util
from pathlib import Path

import pytest

from tests.test_live_agent.test_agent import (
    Harness,
    owner_environment,  # noqa: F401
)


@pytest.fixture
def context_module(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_PATH", str(tmp_path / "absent.env"))
    script = Path(__file__).resolve().parents[2] / "scripts" / "genesis_session_context.py"
    spec = importlib.util.spec_from_file_location("speech_context", script.resolve())
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("surface", ["terminal", "speech"])
def test_surface_reaches_sdk_child(tmp_path, surface):
    h = Harness(tmp_path, output_surface=surface)
    assert h.agent.options(None).env["GENESIS_OUTPUT_SURFACE"] == surface


def test_invalid_surface_refused(tmp_path):
    with pytest.raises(ValueError, match="surface"):
        Harness(tmp_path, output_surface="invalid")


@pytest.mark.parametrize("model,roster", [("claude-opus-4-8", ""), ("new-model", ""), ("", ""), ("claude-opus-4-8", "Peer Model")])
def test_speech_config_omits_header_but_keeps_model_facts(context_module, model, roster):
    text = context_module._session_config_block("high", model, roster, output_surface="speech")
    assert "Output surface: speech" in text
    assert "Do not emit or speak a model/effort status header" in text
    assert "Begin your first reply" not in text
    assert "Thinking effort: high" in text
    if roster:
        assert roster in text
    elif model:
        assert model in text or "Opus 4.8" in text


def test_default_remains_terminal(context_module):
    default = context_module._session_config_block("high", "new-model", "")
    explicit = context_module._session_config_block("high", "new-model", "", output_surface="terminal")
    assert default == explicit
    assert "Begin your first reply" in default


def test_actual_hook_entry_reads_surface_environment(context_module, monkeypatch, tmp_path, capsys):
    import io

    ctx = context_module
    flag = tmp_path / "flag"
    flag.touch()
    monkeypatch.setattr(ctx, "_FLAG", flag)
    monkeypatch.setattr(ctx, "_SESSION_START_FILE", tmp_path / "session_start")
    monkeypatch.setattr(ctx, "_SESSION_CONFIG", tmp_path / "absent_config")
    monkeypatch.setattr(ctx, "_mirror_path", lambda sid, part: tmp_path / f"{part}.md")
    monkeypatch.setattr(ctx, "_sync_genesis_hooks", lambda: None)
    monkeypatch.setattr(ctx, "_spawn_boundary_workers", lambda *args: None)
    monkeypatch.setattr(ctx.sys, "argv", ["context", "--part", "charter"])
    monkeypatch.setattr(ctx.sys, "stdin", io.StringIO("{}"))
    monkeypatch.setenv("GENESIS_OUTPUT_SURFACE", "speech")
    monkeypatch.delenv("GENESIS_CC_SESSION", raising=False)
    ctx._OUT = None
    ctx.main()
    text = capsys.readouterr().out
    assert "Output surface: speech" in text
    assert "Begin your first reply" not in text
