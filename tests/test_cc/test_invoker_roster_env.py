"""Roster routing in CCInvoker env/args construction (the two P1 safety fixes)."""
from __future__ import annotations

from genesis.cc.invoker import CCInvoker
from genesis.cc.types import CCInvocation


def _inv(**kw) -> CCInvocation:
    return CCInvocation(prompt="hi", **kw)


def test_build_env_roster_routing_sets_auth_and_model_and_pops_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic")
    inv = _inv(
        anthropic_base_url="https://open.bigmodel.cn/api/anthropic",
        anthropic_auth_token="sk-zhipu",
        model_id_override="glm-5.2",
    )
    env = CCInvoker()._build_env(inv)
    assert env["ANTHROPIC_BASE_URL"] == "https://open.bigmodel.cn/api/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-zhipu"
    # P1-A: the inherited Anthropic key must NOT travel to a third-party endpoint.
    assert "ANTHROPIC_API_KEY" not in env
    assert env["ANTHROPIC_MODEL"] == "glm-5.2"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "glm-5.2"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "glm-5.2"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "glm-5.2"


def test_build_env_claude_native_preserves_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic")
    env = CCInvoker()._build_env(_inv())  # no roster overrides
    # Claude-native path: subscription/key must be left intact, no routing vars.
    assert env.get("ANTHROPIC_API_KEY") == "sk-anthropic"
    assert "ANTHROPIC_BASE_URL" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_MODEL" not in env


def test_build_args_omits_model_flag_when_override_set():
    # P1-B: ANTHROPIC_MODEL (env) owns selection; a --model flag would override it.
    args = CCInvoker()._build_args(_inv(model_id_override="glm-5.2"))
    assert "--model" not in args


def test_build_env_tmpdir_tracks_claude_code_tmpdir(tmp_path):
    """TMPDIR must stay CONSISTENT with CLAUDE_CODE_TMPDIR (the invariant
    util/tmp.py protects) — and follow a per-invocation sandbox override, so a
    headless session's subprocess temp (e.g. the gauntlet agent running the
    fixture's pytest, whose tmp_path defaults under $TMPDIR) leaves cc-tmp instead
    of tripping genesis-tmp-watchgod."""
    invoker = CCInvoker()
    # default: both resolve to the shared cc-tmp sandbox (consistent, unchanged).
    env = invoker._build_env(_inv())
    assert env["TMPDIR"] == env["CLAUDE_CODE_TMPDIR"]
    # per-invocation override (gauntlet): TMPDIR follows the sandbox off cc-tmp.
    sandbox = str(tmp_path / "cc-sandbox")
    env2 = invoker._build_env(_inv(claude_code_tmpdir=sandbox))
    assert env2["CLAUDE_CODE_TMPDIR"] == sandbox
    assert env2["TMPDIR"] == sandbox


def test_build_args_includes_model_flag_for_claude():
    args = CCInvoker()._build_args(_inv())
    assert "--model" in args
