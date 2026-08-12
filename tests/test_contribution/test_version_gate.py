"""Unit tests for genesis.contribution.version_gate."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from genesis.contribution import version_gate


def test_parse_clean_json():
    raw = json.dumps({
        "already_fixed": True,
        "confidence": 90,
        "matched_commit_sha": "abc1234",
        "reasoning": "dup",
    })
    ok, obj = version_gate.parse_llm_response(raw)
    assert ok is True
    assert obj["already_fixed"] is True
    assert obj["confidence"] == 90


def test_parse_json_with_fences():
    raw = "```json\n" + json.dumps({"already_fixed": False, "confidence": 10}) + "\n```"
    ok, obj = version_gate.parse_llm_response(raw)
    assert ok is True
    assert obj["already_fixed"] is False


def test_parse_json_with_prose():
    raw = "Here is my verdict: " + json.dumps(
        {"already_fixed": False, "confidence": 25}
    ) + " end."
    ok, obj = version_gate.parse_llm_response(raw)
    assert ok is True


def test_parse_missing_required_field():
    raw = json.dumps({"already_fixed": True})  # no confidence
    ok, obj = version_gate.parse_llm_response(raw)
    assert ok is False


def test_parse_garbage():
    ok, obj = version_gate.parse_llm_response("not even close to json")
    assert ok is False


def test_build_prompt_includes_all_inputs():
    prompt = version_gate.build_prompt(
        user_subject="fix: foo",
        user_body="body text",
        user_diff="--- a\n+++ b\n",
        upstream_commits=[
            {"sha": "abc", "subject": "up1", "body": "up body"},
        ],
    )
    assert "fix: foo" in prompt
    assert "body text" in prompt
    assert "--- a" in prompt
    assert "abc" in prompt
    assert "up1" in prompt


def test_build_prompt_empty_upstream():
    prompt = version_gate.build_prompt("s", "b", "d", [])
    assert "empty" in prompt.lower()


@pytest.mark.asyncio
async def test_sha_match_short_circuits():
    r = await version_gate.check_version_gate(
        user_subject="s", user_body="b", user_diff="d",
        user_sha="same", upstream_sha="same",
    )
    assert r.version_match is True
    assert r.already_fixed is False
    assert r.confidence == 0


@pytest.mark.asyncio
async def test_no_upstream_commits_proceeds():
    with patch("genesis.contribution.version_gate.fetch_upstream_log", return_value=[]):
        r = await version_gate.check_version_gate(
            user_subject="s", user_body="b", user_diff="d",
            user_sha="a", upstream_sha="b",
        )
    assert r.already_fixed is False
    assert r.upstream_commit_count == 0


@pytest.mark.asyncio
async def test_llm_says_fixed_high_confidence(monkeypatch):
    fake_commits = [{"sha": "x", "subject": "upstream fix", "body": ""}]
    monkeypatch.setattr(version_gate, "fetch_upstream_log", lambda *a, **k: fake_commits)
    monkeypatch.setenv("GROQ_API_KEY", "fake-for-selection")
    mock_resp = AsyncMock(return_value=json.dumps({
        "already_fixed": True, "confidence": 90,
        "matched_commit_sha": "x", "reasoning": "exact dup",
    }))
    with patch("genesis.contribution.version_gate._call_llm", mock_resp):
        r = await version_gate.check_version_gate(
            user_subject="s", user_body="b", user_diff="d",
            user_sha="a", upstream_sha="b",
        )
    assert r.already_fixed is True
    assert r.confidence == 90
    assert r.matched_sha == "x"
    assert r.parse_ok is True


@pytest.mark.asyncio
async def test_llm_says_fixed_but_below_threshold(monkeypatch):
    fake_commits = [{"sha": "x", "subject": "upstream maybe", "body": ""}]
    monkeypatch.setattr(version_gate, "fetch_upstream_log", lambda *a, **k: fake_commits)
    monkeypatch.setenv("GROQ_API_KEY", "fake-for-selection")
    mock_resp = AsyncMock(return_value=json.dumps({
        "already_fixed": True, "confidence": 50,
        "matched_commit_sha": "x", "reasoning": "maybe",
    }))
    with patch("genesis.contribution.version_gate._call_llm", mock_resp):
        r = await version_gate.check_version_gate(
            user_subject="s", user_body="b", user_diff="d",
            user_sha="a", upstream_sha="b",
        )
    # Below threshold (75) → allow contribution
    assert r.already_fixed is False
    assert r.confidence == 50


@pytest.mark.asyncio
async def test_llm_says_not_fixed(monkeypatch):
    fake_commits = [{"sha": "x", "subject": "unrelated", "body": ""}]
    monkeypatch.setattr(version_gate, "fetch_upstream_log", lambda *a, **k: fake_commits)
    monkeypatch.setenv("GROQ_API_KEY", "fake-for-selection")
    mock_resp = AsyncMock(return_value=json.dumps({
        "already_fixed": False, "confidence": 90,
        "matched_commit_sha": None, "reasoning": "no match",
    }))
    with patch("genesis.contribution.version_gate._call_llm", mock_resp):
        r = await version_gate.check_version_gate(
            user_subject="s", user_body="b", user_diff="d",
            user_sha="a", upstream_sha="b",
        )
    assert r.already_fixed is False
    assert r.confidence == 90


@pytest.mark.asyncio
async def test_llm_error_fails_open(monkeypatch):
    fake_commits = [{"sha": "x", "subject": "unrelated", "body": ""}]
    monkeypatch.setattr(version_gate, "fetch_upstream_log", lambda *a, **k: fake_commits)
    monkeypatch.setenv("GROQ_API_KEY", "fake-for-selection")

    async def boom(prompt, model):
        raise RuntimeError("network down")

    with patch("genesis.contribution.version_gate._call_llm", side_effect=boom):
        r = await version_gate.check_version_gate(
            user_subject="s", user_body="b", user_diff="d",
            user_sha="a", upstream_sha="b",
        )
    assert r.already_fixed is False
    assert r.parse_ok is False
    assert r.llm_error == "network down"


@pytest.mark.asyncio
async def test_unparseable_response_fails_open(monkeypatch):
    fake_commits = [{"sha": "x", "subject": "unrelated", "body": ""}]
    monkeypatch.setattr(version_gate, "fetch_upstream_log", lambda *a, **k: fake_commits)
    monkeypatch.setenv("GROQ_API_KEY", "fake-for-selection")
    mock_resp = AsyncMock(return_value="not json at all")
    with patch("genesis.contribution.version_gate._call_llm", mock_resp):
        r = await version_gate.check_version_gate(
            user_subject="s", user_body="b", user_diff="d",
            user_sha="a", upstream_sha="b",
        )
    assert r.already_fixed is False
    assert r.parse_ok is False
    assert r.llm_error == "parse_error"


# Every env spelling the key resolver accepts, for all three fallback services —
# tests that need "no key present" must clear ALL of them (a dev shell with
# secrets.env sourced would otherwise leak API_KEY_GROQ into the test).
_ALL_KEY_SPELLINGS = tuple(
    pattern
    for svc in ("OPENROUTER", "GROQ", "GOOGLE")
    for pattern in (f"API_KEY_{svc}", f"{svc}_API_KEY", f"{svc}_API_TOKEN")
)


def _clear_all_keys(monkeypatch):
    for var in _ALL_KEY_SPELLINGS:
        monkeypatch.delenv(var, raising=False)


@pytest.mark.asyncio
async def test_no_api_key_fails_open(monkeypatch):
    fake_commits = [{"sha": "x", "subject": "unrelated", "body": ""}]
    monkeypatch.setattr(version_gate, "fetch_upstream_log", lambda *a, **k: fake_commits)
    _clear_all_keys(monkeypatch)
    r = await version_gate.check_version_gate(
        user_subject="s", user_body="b", user_diff="d",
        user_sha="a", upstream_sha="b",
    )
    assert r.already_fixed is False
    assert r.parse_ok is False
    assert r.llm_error == "no_api_key"


def test_genesis_env_spelling_selects_groq(monkeypatch):
    """Regression: the Genesis convention is API_KEY_GROQ (stt, providers init,
    onboarding floor) — the old single-name GROQ_API_KEY check made every groq
    entry silently unselectable on convention-following installs."""
    _clear_all_keys(monkeypatch)
    monkeypatch.setenv("API_KEY_GROQ", "gsk-fake-genesis-spelling")
    # Force the fallback list (config availability varies by test env).
    monkeypatch.setattr(version_gate, "_load_models_from_config", lambda: [])
    assert version_gate._select_model(None) == "groq/openai/gpt-oss-120b"


def test_litellm_spelling_still_selects_groq(monkeypatch):
    """The litellm-native spelling must keep working (acceptance, not a swap)."""
    _clear_all_keys(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake-litellm-spelling")
    monkeypatch.setattr(version_gate, "_load_models_from_config", lambda: [])
    assert version_gate._select_model(None) == "groq/openai/gpt-oss-120b"


@pytest.mark.asyncio
async def test_call_llm_passes_explicit_api_key(monkeypatch):
    """litellm's own env lookup knows only GROQ_API_KEY, so _call_llm must pass
    the resolved key explicitly — otherwise a selected-via-API_KEY_GROQ model
    401s at call time (the exact failure a live smoke hit on 2026-08-12)."""
    _clear_all_keys(monkeypatch)
    monkeypatch.setenv("API_KEY_GROQ", "gsk-fake-explicit")
    captured = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)

        class _Msg:
            content = "ok"

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()

    import litellm

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    out = await version_gate._call_llm("p", "groq/openai/gpt-oss-120b")
    assert out == "ok"
    assert captured.get("api_key") == "gsk-fake-explicit"


def test_format_version_string(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "9.9.9"\n'
    )
    (tmp_path / ".genesis-source-commit").write_text("abcdef1234567890\n")
    s = version_gate.format_version_string(tmp_path)
    assert s == "9.9.9@abcdef1"


def test_fetch_upstream_log_preserves_multiline_body_codex_p2(tmp_path):
    """Codex review P2 regression: multi-line commit bodies previously
    got truncated to their first line because fetch_upstream_log split
    on \\n instead of the record separator. This test runs against a
    real throwaway git repo and asserts the full body reaches the
    parsed output."""
    import subprocess as sp
    repo = tmp_path / "r"
    repo.mkdir()

    def run(*args, **kw):
        return sp.run(["git", *args], cwd=str(repo), capture_output=True,
                      text=True, check=True, **kw)

    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (repo / "a").write_text("base\n")
    run("add", "a")
    run("commit", "-q", "-m", "base")
    base_sha = run("rev-parse", "HEAD").stdout.strip()

    # Create a commit with a multi-line body
    (repo / "b").write_text("next\n")
    run("add", "b")
    multiline = "next fix\n\nline one of body\nline two of body\nline three"
    run("commit", "-q", "-m", multiline)
    head_sha = run("rev-parse", "HEAD").stdout.strip()

    commits = version_gate.fetch_upstream_log(base_sha, head_sha, repo_path=repo)
    assert len(commits) == 1
    body = commits[0]["body"]
    assert "line one of body" in body
    assert "line two of body" in body
    assert "line three" in body


def test_read_install_sha_from_marker(tmp_path):
    (tmp_path / ".genesis-source-commit").write_text("abcdef1\n")
    assert version_gate.read_install_sha(tmp_path) == "abcdef1"


def test_read_install_sha_missing(tmp_path):
    # No marker and no git repo → None
    assert version_gate.read_install_sha(tmp_path) is None
