"""Tests for the model-roster policy layer (genesis.cc.roster)."""
from __future__ import annotations

import textwrap

import pytest

from genesis.cc import roster as R


@pytest.fixture(autouse=True)
def _hermetic_user_overlay(tmp_path, monkeypatch):
    """Isolate merge_local_overlay's user-dir lookup so tests don't read the real
    ~/.genesis/config and the repo-relative fallback is deterministic."""
    monkeypatch.setattr(
        "genesis._config_overlay._user_config_dir",
        lambda: tmp_path / "user-config",
    )


def _write_roster(tmp_path, body: str):
    (tmp_path / "cc_roster.yaml").write_text(textwrap.dedent(body))
    return tmp_path


@pytest.fixture
def roster_dir(tmp_path):
    return _write_roster(
        tmp_path,
        """
        default: claude
        models:
          claude:
            native_subscription: true
            failover_order: 0
          glm-5.2:
            anthropic_base_url: "https://open.bigmodel.cn/api/anthropic"
            auth_env: ZHIPU_TEST_KEY
            model_id: glm-5.2
            failover_order: 1
          deepseek:
            anthropic_base_url: "https://api.deepseek.com/anthropic"
            auth_env: DEEPSEEK_TEST_KEY
            model_id: deepseek-v4-pro
            failover_order: 2
        """,
    )


def test_active_model_default(roster_dir):
    assert R.active_model(R.load_roster(roster_dir)) == "claude"


def test_overrides_for_claude_is_empty(roster_dir):
    # Native subscription → no routing overrides (preserves Max subscription).
    assert R.overrides_for("claude", R.load_roster(roster_dir)) == {}


def test_overrides_for_glm_with_env(roster_dir, monkeypatch):
    monkeypatch.setenv("ZHIPU_TEST_KEY", "sk-secret")
    ov = R.overrides_for("glm-5.2", R.load_roster(roster_dir))
    assert ov == {
        "anthropic_base_url": "https://open.bigmodel.cn/api/anthropic",
        "anthropic_auth_token": "sk-secret",
        "model_id_override": "glm-5.2",
    }


def test_overrides_for_missing_auth_raises(roster_dir, monkeypatch):
    monkeypatch.delenv("ZHIPU_TEST_KEY", raising=False)
    with pytest.raises(R.RosterError):
        R.overrides_for("glm-5.2", R.load_roster(roster_dir))


def test_overrides_for_unknown_raises(roster_dir):
    with pytest.raises(R.RosterError):
        R.overrides_for("nope", R.load_roster(roster_dir))


def test_failover_chain_orders_and_skips_unconfigured(roster_dir, monkeypatch):
    # Only GLM has a key → deepseek (no key) is skipped; claude always available.
    monkeypatch.setenv("ZHIPU_TEST_KEY", "sk-secret")
    monkeypatch.delenv("DEEPSEEK_TEST_KEY", raising=False)
    chain = R.failover_chain("claude", R.load_roster(roster_dir))
    assert chain == ["glm-5.2"]  # deepseek skipped (unconfigured)

    # With both keys, ordering follows failover_order ascending.
    monkeypatch.setenv("DEEPSEEK_TEST_KEY", "sk-ds")
    chain = R.failover_chain("claude", R.load_roster(roster_dir))
    assert chain == ["glm-5.2", "deepseek"]


def test_failover_chain_excludes_active(roster_dir, monkeypatch):
    monkeypatch.setenv("ZHIPU_TEST_KEY", "sk-secret")
    chain = R.failover_chain("glm-5.2", R.load_roster(roster_dir))
    assert "glm-5.2" not in chain
    assert "claude" in chain  # native peer always available


def test_failover_invocations_stamps_fresh_peer(roster_dir, monkeypatch):
    from genesis.cc.types import CCInvocation

    monkeypatch.setenv("ZHIPU_TEST_KEY", "sk-secret")
    monkeypatch.delenv("DEEPSEEK_TEST_KEY", raising=False)
    base = CCInvocation(
        prompt="hi", resume_session_id="cc-home", roster_eligible=True,
        session_key="k1",
    )
    invs = R.failover_invocations("claude", base, R.load_roster(roster_dir))
    assert [name for name, _ in invs] == ["glm-5.2"]  # deepseek unconfigured
    _, peer = invs[0]
    # FRESH session; routed peer stays roster_eligible=True so the chokepoint's
    # override-present guard honors the endpoint AND reports the correct name.
    assert peer.resume_session_id is None
    assert peer.roster_eligible is True
    assert peer.model_id_override == "glm-5.2"
    assert peer.anthropic_base_url == "https://open.bigmodel.cn/api/anthropic"
    assert peer.anthropic_auth_token == "sk-secret"
    assert peer.session_key == "k1"  # /stop targeting preserved
    assert peer.prompt == "hi"  # rest of the invocation preserved


def test_failover_invocations_native_peer_has_no_overrides_but_disables_reselect(
    roster_dir, monkeypatch,
):
    # When GLM is active and fails, Claude (native) is the peer: empty overrides,
    # but roster_eligible MUST be False so the chokepoint can't re-route it back
    # to the global default (the loop-back bug guard).
    monkeypatch.setenv("ZHIPU_TEST_KEY", "sk-secret")
    monkeypatch.delenv("DEEPSEEK_TEST_KEY", raising=False)
    from genesis.cc.types import CCInvocation

    # base carries GLM routing (as a routed-resume would when default=glm) — the
    # native Claude peer MUST clear it, not leak it through.
    base = CCInvocation(
        prompt="hi", roster_eligible=True,
        anthropic_base_url="https://open.bigmodel.cn/api/anthropic",
        anthropic_auth_token="sk-secret",
        model_id_override="glm-5.2",
    )
    invs = R.failover_invocations("glm-5.2", base, R.load_roster(roster_dir))
    names = [name for name, _ in invs]
    assert names[0] == "claude"
    _, claude_peer = invs[0]
    assert claude_peer.roster_eligible is False  # load-bearing
    assert claude_peer.model_id_override is None  # routing CLEARED for native peer
    assert claude_peer.anthropic_base_url is None
    assert claude_peer.anthropic_auth_token is None


def test_failover_invocations_skips_unusable_peer(roster_dir, monkeypatch):
    # deepseek present in chain only if keyed; with no key it never appears.
    monkeypatch.setenv("ZHIPU_TEST_KEY", "sk-secret")
    monkeypatch.delenv("DEEPSEEK_TEST_KEY", raising=False)
    from genesis.cc.types import CCInvocation

    invs = R.failover_invocations(
        "claude", CCInvocation(prompt="x"), R.load_roster(roster_dir),
    )
    assert [name for name, _ in invs] == ["glm-5.2"]


def test_local_overlay_merges(tmp_path, monkeypatch):
    _write_roster(
        tmp_path,
        """
        default: claude
        models:
          claude:
            native_subscription: true
        """,
    )
    (tmp_path / "cc_roster.local.yaml").write_text("default: glm-5.2\n")
    merged = R.load_roster(tmp_path)
    assert R.active_model(merged) == "glm-5.2"


def test_user_dir_overlay_controls_default(roster_dir, tmp_path):
    # Mirrors settings_update writing ~/.genesis/config/cc_roster.local.yaml:
    # the loader MUST honor the user-dir overlay (cfg-001 — the bug the Phase-1
    # review caught), not only the repo-relative sibling.
    user_dir = tmp_path / "user-config"
    user_dir.mkdir()
    (user_dir / "cc_roster.local.yaml").write_text("default: glm-5.2\n")
    assert R.active_model(R.load_roster(roster_dir)) == "glm-5.2"


def test_non_dict_config_is_ignored(tmp_path):
    (tmp_path / "cc_roster.yaml").write_text("- just\n- a\n- list\n")
    assert R.load_roster(tmp_path) == {}  # no crash on malformed config
