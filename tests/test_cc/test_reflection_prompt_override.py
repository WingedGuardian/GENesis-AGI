"""Reflection-prompt user-overlay: ``system_prompt_for_depth`` checks a
``~/.genesis/config/reflection/`` overlay (env-overridable via
``GENESIS_REFLECTION_PROMPT_DIR``) BEFORE the repo default ``prompt_dir``.

This gives a writable, out-of-git-tree target for hand-overriding the
reflection prompt (and the future Evo promotion path). No overlay file → behavior
is identical to before.
"""

from genesis.awareness.types import Depth
from genesis.cc.reflection_bridge._prompts import system_prompt_for_depth


def _base_dir(tmp_path):
    base = tmp_path / "identity"
    base.mkdir()
    (base / "REFLECTION_DEEP.md").write_text("BASE deep prompt")
    return base


def test_override_dir_takes_precedence(tmp_path, monkeypatch):
    base = _base_dir(tmp_path)
    override = tmp_path / "override"
    override.mkdir()
    (override / "REFLECTION_DEEP.md").write_text("OVERRIDE deep prompt")
    monkeypatch.setenv("GENESIS_REFLECTION_PROMPT_DIR", str(override))

    assert system_prompt_for_depth(Depth.DEEP, base) == "OVERRIDE deep prompt"


def test_override_model_specific_wins(tmp_path, monkeypatch):
    base = _base_dir(tmp_path)
    override = tmp_path / "override"
    override.mkdir()
    (override / "REFLECTION_DEEP.md").write_text("OVERRIDE base")
    # DEEP maps to the SONNET model variant.
    (override / "REFLECTION_DEEP_SONNET.md").write_text("OVERRIDE sonnet")
    monkeypatch.setenv("GENESIS_REFLECTION_PROMPT_DIR", str(override))

    assert system_prompt_for_depth(Depth.DEEP, base) == "OVERRIDE sonnet"


def test_no_override_file_falls_back_to_prompt_dir(tmp_path, monkeypatch):
    base = _base_dir(tmp_path)
    override = tmp_path / "override"
    override.mkdir()  # exists but empty → no override file
    monkeypatch.setenv("GENESIS_REFLECTION_PROMPT_DIR", str(override))

    assert system_prompt_for_depth(Depth.DEEP, base) == "BASE deep prompt"


def test_missing_override_dir_uses_prompt_dir(tmp_path, monkeypatch):
    base = _base_dir(tmp_path)
    monkeypatch.setenv(
        "GENESIS_REFLECTION_PROMPT_DIR", str(tmp_path / "does_not_exist"),
    )

    assert system_prompt_for_depth(Depth.DEEP, base) == "BASE deep prompt"


def test_env_unset_does_not_crash(tmp_path, monkeypatch):
    base = _base_dir(tmp_path)
    monkeypatch.delenv("GENESIS_REFLECTION_PROMPT_DIR", raising=False)
    # Default overlay (~/.genesis/config/reflection) has no file on a fresh box,
    # so it falls through to the repo prompt_dir — and never raises.
    out = system_prompt_for_depth(Depth.DEEP, base)
    assert isinstance(out, str) and out
