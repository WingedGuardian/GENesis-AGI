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


# --- apply_routing_env: the shared invoker/gmodel routing-env contract ----------

_MODEL_SLOTS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)


def test_apply_routing_env_peer_sets_all_and_drops_api_key():
    env = {"ANTHROPIC_API_KEY": "sk-anthropic", "PATH": "/x"}
    out = R.apply_routing_env(
        env,
        base_url="https://open.bigmodel.cn/api/anthropic",
        auth_token="zk-secret",
        model_id="glm-5.2",
    )
    assert out is env  # mutates in place
    assert env["ANTHROPIC_BASE_URL"] == "https://open.bigmodel.cn/api/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "zk-secret"
    assert all(env[s] == "glm-5.2" for s in _MODEL_SLOTS)
    assert "ANTHROPIC_API_KEY" not in env  # credential isolation
    assert env["PATH"] == "/x"  # unrelated vars untouched


def test_apply_routing_env_native_pops_routing_keeps_api_key():
    # Native Claude: no override fields → all routing popped, but the Anthropic key
    # is preserved (the caller decides Max-vs-key for the native path).
    env = {
        "ANTHROPIC_API_KEY": "sk-anthropic",
        "ANTHROPIC_BASE_URL": "stale",
        "ANTHROPIC_AUTH_TOKEN": "stale",
        "ANTHROPIC_MODEL": "stale",
    }
    R.apply_routing_env(env, base_url=None, auth_token=None, model_id=None)
    assert "ANTHROPIC_BASE_URL" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert all(s not in env for s in _MODEL_SLOTS)
    assert env["ANTHROPIC_API_KEY"] == "sk-anthropic"  # kept for native


def test_apply_routing_env_auth_token_only_still_drops_api_key():
    # Defense-in-depth: an inconsistent combo (token but no base_url) must still
    # drop the Anthropic key so it can't leak to a third-party endpoint.
    env = {"ANTHROPIC_API_KEY": "sk-anthropic"}
    R.apply_routing_env(env, base_url=None, auth_token="zk-secret", model_id=None)
    assert "ANTHROPIC_API_KEY" not in env
    assert env["ANTHROPIC_AUTH_TOKEN"] == "zk-secret"


def test_shipped_config_ships_no_peers():
    """DELIVERABLE LOCK: config/cc_roster.yaml ships INFRASTRUCTURE, not peers.

    A peer pins a `model_id`, and model ids go EOL — this file shipped
    `glm-5.2` past its replacement by `glm-5.3`. A stale pinned peer fails at
    the one moment it is needed (when the subscription caps) and fails while
    LOOKING configured, which is worse than an empty roster failing honestly.
    So peers are declared per-install in ~/.genesis/config/cc_roster.local.yaml.

    Asserts on the SHIPPED FILE directly rather than the merged roster, so
    neither a developer's real overlay nor the repo-relative fallback can mask
    a regression. Without this, re-adding a peer to the shipped config would
    break nothing — every other roster test writes its own synthetic yaml.
    """
    import yaml

    from genesis.cc.roster import _CONFIG_DIR, _ROSTER_FILE

    base = yaml.safe_load((_CONFIG_DIR / _ROSTER_FILE).read_text())
    assert base["default"] == "claude"
    assert set(base["models"]) == {"claude"}, (
        "config/cc_roster.yaml must ship no peers — declare them in "
        "~/.genesis/config/cc_roster.local.yaml. See the file header for why "
        "(version churn: a stale pinned model_id fails exactly when needed)."
    )
    assert base["models"]["claude"].get("native_subscription") is True


def test_every_documented_auth_env_has_a_secrets_slot():
    """CROSS-FILE GUARD: a peer example may not name a key the template lacks.

    Round-2 defect on PR #1606: the shipped China example told users to set
    `auth_env: ZHIPU_API_KEY` against `/api/anthropic`, while
    `secrets.env.example` — edited in the SAME change — declared that variable
    to be the general/prepaid key for a different endpoint. Two files, each
    internally consistent, contradicting each other. Nothing checked the pair,
    so a human reviewer had to notice.

    Scans COMMENTED examples too, on purpose: the shipped roster declares no
    active peers (see test_shipped_config_ships_no_peers), so every auth_env in
    it lives in a comment — which is exactly where the defect was.
    """
    import re

    from genesis.cc.roster import _CONFIG_DIR, _ROSTER_FILE

    roster_text = (_CONFIG_DIR / _ROSTER_FILE).read_text()
    # Prose mentions a key to warn AGAINST it ("NOT ZHIPU_API_KEY"), so match
    # only the field form `auth_env: NAME`, commented or not.
    named = set(re.findall(r"auth_env:\s*([A-Z][A-Z0-9_]+)", roster_text))
    assert named, "no auth_env examples found — has the example block moved?"

    example = _CONFIG_DIR.parent / "secrets.env.example"
    declared = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", example.read_text(), re.MULTILINE))

    missing = named - declared
    assert not missing, (
        f"config/cc_roster.yaml names auth_env {sorted(missing)}, which "
        "secrets.env.example does not declare. A user following the example "
        "would set a variable nothing reads. Add the slot (with its own "
        "`# Used by:` and `# Signup:` lines) or fix the example."
    )

    # "Is it declared?" is NOT enough, and verifying-RED proved it: the round-2
    # defect named ZHIPU_API_KEY, which IS a declared slot — it is simply the
    # WRONG KIND of key. The real invariant is about the endpoint: every roster
    # peer reaches its provider over the Anthropic protocol (that is what
    # `anthropic_base_url` means), and those coding endpoints require a
    # CODING-PLAN key, never the general/prepaid one. The naming convention
    # `*_CODING_API_KEY` is what makes that mechanically checkable, and
    # secrets.env.example documents it.
    wrong_class = {n for n in named if not n.endswith("_CODING_API_KEY")}
    assert not wrong_class, (
        f"config/cc_roster.yaml names auth_env {sorted(wrong_class)} for a peer. "
        "A roster peer talks to an Anthropic-protocol coding endpoint, which "
        "needs a CODING-PLAN key (`*_CODING_API_KEY`) — a general/prepaid key "
        "there returns `1113 Insufficient balance` on a funded account. If a "
        "provider genuinely uses one key for both, rename the slot so the "
        "convention still reads true."
    )


def test_a_peer_too_long_to_observe_is_announced_at_load(monkeypatch, caplog):
    """Selection accepts any configured name; the availability record rejects one
    over its bound rather than truncating it, because a truncated key would merge
    two peers and let one peer's success clear another's failure.

    So such a peer serves traffic while being permanently invisible in health.
    That is a silent hole in an observability surface, which is the one thing
    that surface exists to prevent — so it is announced at load time.
    """
    from genesis.cc import roster as R

    long_name = "p" * (R._MAX_OBSERVABLE_NAME + 1)
    monkeypatch.setenv("FAKE_PEER_TOKEN", "x")
    fake = {"models": {
        "active-one": {"failover_order": 0, "auth_env": "FAKE_PEER_TOKEN"},
        long_name: {"failover_order": 1, "auth_env": "FAKE_PEER_TOKEN"},
    }}

    with caplog.at_level("WARNING", logger="genesis.cc.roster"):
        chain = R.failover_chain("active-one", roster=fake)

    assert long_name in chain, "selection must still route to it — this is not a gate"
    hits = [r for r in caplog.records if "never appear in peer-availability" in r.getMessage()]
    assert len(hits) == 1, f"an unobservable peer must be announced, got {len(hits)}"
    assert long_name not in caplog.text, "the name itself must not be logged whole"


def test_a_non_string_roster_key_cannot_disable_the_backup_chain(monkeypatch, caplog):
    """A hand-edited YAML roster with an unquoted numeric key parses to an int.

    The observability warning added at load time calls len() on the name, and a
    TypeError there is raised while BUILDING failover_chain — before the
    per-peer skip logic — so one malformed entry silently disabled the entire
    backup chain at exactly the moment it was needed. The defect was introduced
    by the warning itself, which is why this lock exists.
    """
    from genesis.cc import roster as R

    monkeypatch.setenv("FAKE_PEER_TOKEN", "x")
    fake = {"models": {
        "active-one": {"failover_order": 0, "auth_env": "FAKE_PEER_TOKEN"},
        123: {"failover_order": 1, "auth_env": "FAKE_PEER_TOKEN"},
        "good-peer": {"failover_order": 2, "auth_env": "FAKE_PEER_TOKEN"},
    }}

    with caplog.at_level("WARNING", logger="genesis.cc.roster"):
        chain = R.failover_chain("active-one", roster=fake)

    assert "good-peer" in chain, "one malformed key must not cost the whole chain"
    assert 123 not in chain
    hits = [r for r in caplog.records if "not a string" in r.getMessage()]
    assert len(hits) == 1, "the skipped entry must be announced"
    assert "123" not in caplog.text, "the key's VALUE must not be logged"
