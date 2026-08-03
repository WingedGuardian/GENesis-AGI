"""Unit tests for the live functional floor (``genesis.onboarding.floor``).

The floor is the honest "is this Genesis functional" signal: CC OAuth login AND
≥1 routing-consumed LLM key AND ≥1 embedding key. It is computed live, never a
marker file. ANTHROPIC_API_KEY must NOT count toward the LLM leg (routing has no
``type: anthropic`` provider).
"""

from __future__ import annotations

import json

from genesis.onboarding import floor as floor_mod
from genesis.onboarding.floor import cc_oauth_present, compute_floor

# --------------------------------------------------------------------------
# cc_oauth_present() truth table
# --------------------------------------------------------------------------


def test_cc_oauth_env_token_overrides_everything(monkeypatch, tmp_path):
    # Explicit token env wins even if no credentials file exists anywhere.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-abc")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cc_oauth_present() is True


def test_cc_oauth_token_file(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path))
    tokfile = tmp_path / ".genesis" / "cc_oauth_token.env"
    tokfile.parent.mkdir(parents=True)
    tokfile.write_text("CLAUDE_CODE_OAUTH_TOKEN=tok-from-file\n")
    assert cc_oauth_present() is True


def test_cc_oauth_token_file_empty_value_does_not_count(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path))
    tokfile = tmp_path / ".genesis" / "cc_oauth_token.env"
    tokfile.parent.mkdir(parents=True)
    tokfile.write_text("CLAUDE_CODE_OAUTH_TOKEN=\n")
    assert cc_oauth_present() is False


def test_cc_oauth_credentials_json_with_structural_key(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    cfg = tmp_path / "claude-cfg"
    cfg.mkdir()
    (cfg / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "x"}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cc_oauth_present() is True


def test_cc_oauth_credentials_json_without_structural_key(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    cfg = tmp_path / "claude-cfg"
    cfg.mkdir()
    # An API-key-mode or otherwise-shaped file lacking claudeAiOauth → not logged in.
    (cfg / ".credentials.json").write_text(json.dumps({"somethingElse": True}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cc_oauth_present() is False


def test_cc_oauth_absent_everywhere(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cc_oauth_present() is False


def test_cc_oauth_home_credentials_when_no_config_dir(monkeypatch, tmp_path):
    # With CLAUDE_CONFIG_DIR unset, falls back to ~/.claude/.credentials.json.
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(json.dumps({"claudeAiOauth": {}}))
    assert cc_oauth_present() is True


# --------------------------------------------------------------------------
# compute_floor() conjunction + key-name matching
# --------------------------------------------------------------------------


def _oauth(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(floor_mod, "cc_oauth_present", lambda: value)


def test_floor_met_when_all_three_legs_present(monkeypatch):
    _oauth(monkeypatch, True)
    f = compute_floor({"API_KEY_OPENROUTER": "x", "API_KEY_VOYAGE": "y"})
    assert (f.cc_oauth, f.llm_key_present, f.embedding_key_present, f.floor_met) == (
        True,
        True,
        True,
        True,
    )


def test_floor_unmet_missing_embedding(monkeypatch):
    _oauth(monkeypatch, True)
    assert compute_floor({"API_KEY_OPENROUTER": "x"}).floor_met is False


def test_floor_unmet_missing_llm(monkeypatch):
    _oauth(monkeypatch, True)
    assert compute_floor({"API_KEY_VOYAGE": "y"}).floor_met is False


def test_floor_unmet_missing_cc_oauth(monkeypatch):
    _oauth(monkeypatch, False)
    assert compute_floor({"API_KEY_OPENROUTER": "x", "API_KEY_VOYAGE": "y"}).floor_met is False


def test_anthropic_key_does_not_satisfy_llm_leg(monkeypatch):
    _oauth(monkeypatch, True)
    f = compute_floor({"ANTHROPIC_API_KEY": "sk-ant-xxx", "API_KEY_VOYAGE": "y"})
    assert f.llm_key_present is False  # not routing-consumed → must not count
    assert f.floor_met is False


def test_google_key_counts_for_both_legs(monkeypatch):
    # GOOGLE_API_KEY is in both the LLM and embedding sets.
    _oauth(monkeypatch, True)
    f = compute_floor({"GOOGLE_API_KEY": "g"})
    assert f.llm_key_present is True
    assert f.embedding_key_present is True
    assert f.floor_met is True


def test_sentinel_values_do_not_count(monkeypatch):
    _oauth(monkeypatch, True)
    for bad in ("", "None", "NA", "   "):
        f = compute_floor({"API_KEY_GROQ": bad, "API_KEY_VOYAGE": "y"})
        assert f.llm_key_present is False, f"{bad!r} should not count"


def test_as_dict_shape(monkeypatch):
    _oauth(monkeypatch, True)
    d = compute_floor({"API_KEY_GROQ": "x", "OPENAI_API_KEY": "o"}).as_dict()
    assert d == {
        "cc_oauth": True,
        "llm_key_present": True,
        "embedding_key_present": True,
        "floor_met": True,
    }
