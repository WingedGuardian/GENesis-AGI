"""Tests for the roster SELECTION chokepoint + resume-continuity helpers
(apply_active / endpoint_payload / overrides_from_persisted)."""
from __future__ import annotations

import logging
import textwrap

import pytest

from genesis.cc import roster as R
from genesis.cc.types import CCInvocation


@pytest.fixture(autouse=True)
def _hermetic_user_overlay(tmp_path, monkeypatch):
    """Isolate merge_local_overlay's user-dir lookup (no real ~/.genesis read)."""
    monkeypatch.setattr(
        "genesis._config_overlay._user_config_dir",
        lambda: tmp_path / "user-config",
    )


@pytest.fixture(autouse=True)
def _reset_unresolvable_warn_cache():
    """Isolate the module-global warn-dedupe set for unresolvable defaults."""
    R._WARNED_UNRESOLVABLE_DEFAULTS.clear()
    yield
    R._WARNED_UNRESOLVABLE_DEFAULTS.clear()


@pytest.fixture
def roster_dir(tmp_path):
    (tmp_path / "cc_roster.yaml").write_text(textwrap.dedent(
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
        """,
    ))
    return tmp_path


def _load(roster_dir):
    return R.load_roster(roster_dir)


# --------------------------- apply_active ----------------------------------

def test_not_eligible_is_native_passthrough(roster_dir, monkeypatch):
    monkeypatch.setenv("ZHIPU_TEST_KEY", "sk")
    # default is claude here, but flip to glm to prove the gate (not the default)
    # is what keeps it native.
    (roster_dir / "cc_roster.local.yaml").write_text("default: glm-5.2\n")
    inv = CCInvocation(prompt="x", roster_eligible=False)
    out, name = R.apply_active(inv, _load(roster_dir))
    assert out is inv and name == R.CLAUDE


def test_eligible_default_claude_is_native(roster_dir):
    inv = CCInvocation(prompt="x", roster_eligible=True)
    out, name = R.apply_active(inv, _load(roster_dir))
    assert out is inv and name == "claude"


def test_eligible_glm_default_stamps_overrides(roster_dir, monkeypatch):
    monkeypatch.setenv("ZHIPU_TEST_KEY", "sk-secret")
    (roster_dir / "cc_roster.local.yaml").write_text("default: glm-5.2\n")
    inv = CCInvocation(prompt="x", model_id_override=None, roster_eligible=True)
    out, name = R.apply_active(inv, _load(roster_dir))
    assert name == "glm-5.2"
    assert out is not inv  # replaced
    assert out.anthropic_base_url == "https://open.bigmodel.cn/api/anthropic"
    assert out.anthropic_auth_token == "sk-secret"
    assert out.model_id_override == "glm-5.2"
    # untouched fields preserved
    assert out.prompt == "x" and out.roster_eligible is True


def test_eligible_glm_missing_key_falls_back_native(roster_dir, monkeypatch, caplog):
    monkeypatch.delenv("ZHIPU_TEST_KEY", raising=False)
    (roster_dir / "cc_roster.local.yaml").write_text("default: glm-5.2\n")
    inv = CCInvocation(prompt="x", roster_eligible=True)
    with caplog.at_level(logging.ERROR):
        out, name = R.apply_active(inv, _load(roster_dir))
    assert out is inv and name == R.CLAUDE  # never raises, never goes dark
    assert any("apply_active failed" in r.message for r in caplog.records)


def test_bare_resume_is_not_rerouted(roster_dir, monkeypatch):
    monkeypatch.setenv("ZHIPU_TEST_KEY", "sk")
    (roster_dir / "cc_roster.local.yaml").write_text("default: glm-5.2\n")
    inv = CCInvocation(prompt="x", roster_eligible=True, resume_session_id="cc-abc")
    out, name = R.apply_active(inv, _load(roster_dir))
    assert out is inv and name == R.CLAUDE  # resume safety: never reroute


def test_pre_stamped_override_is_respected(roster_dir):
    inv = CCInvocation(
        prompt="x", roster_eligible=True,
        anthropic_base_url="u", anthropic_auth_token="t", model_id_override="glm-5.2",
    )
    out, name = R.apply_active(inv, _load(roster_dir))
    assert out is inv and name == "glm-5.2"  # reconstruction/failover wins


# --------------------- persistence round-trip ------------------------------

def test_endpoint_payload_routed_has_no_token(roster_dir):
    p = R.endpoint_payload("glm-5.2", _load(roster_dir))
    assert p == {
        "base_url": "https://open.bigmodel.cn/api/anthropic",
        "auth_env": "ZHIPU_TEST_KEY",
        "model_id": "glm-5.2",
        "roster_model": "glm-5.2",
    }
    assert "sk" not in str(p) and "token" not in p  # NAME only


def test_endpoint_payload_native_is_none(roster_dir):
    assert R.endpoint_payload("claude", _load(roster_dir)) is None


def test_overrides_from_persisted_round_trip(roster_dir, monkeypatch):
    monkeypatch.setenv("ZHIPU_TEST_KEY", "sk-live")
    payload = R.endpoint_payload("glm-5.2", _load(roster_dir))
    ov = R.overrides_from_persisted(payload)
    assert ov == {
        "anthropic_base_url": "https://open.bigmodel.cn/api/anthropic",
        "anthropic_auth_token": "sk-live",  # re-read from env, not stored
        "model_id_override": "glm-5.2",
    }


def test_overrides_from_persisted_missing_token_raises(roster_dir, monkeypatch):
    monkeypatch.delenv("ZHIPU_TEST_KEY", raising=False)
    payload = R.endpoint_payload("glm-5.2", _load(roster_dir))
    with pytest.raises(R.RosterError):
        R.overrides_from_persisted(payload)


def test_overrides_from_persisted_incomplete_raises():
    with pytest.raises(R.RosterError):
        R.overrides_from_persisted({"base_url": "u"})  # missing auth_env/model_id


# ── a configured default that no longer resolves ─────────────────────────────
# Codex P1 on the roster-portability PR: `settings_update` persists ONLY
# `default: <name>` into the local overlay, because the model DEFINITION came
# from the shipped base. Remove that base entry and the selection is left
# without an endpoint — apply_active then falls back to native Claude, so an
# install silently runs a different provider than the one it chose. The fallback
# itself is correct (breaking every call would be worse); being ANONYMOUS about
# it is not.


def _dangling_default_roster(tmp_path):
    (tmp_path / "cc_roster.yaml").write_text(textwrap.dedent(
        """
        default: claude
        models:
          claude:
            native_subscription: true
            failover_order: 0
        """
    ))
    user_dir = tmp_path / "user-config"
    user_dir.mkdir(exist_ok=True)
    # What a settings-selected default looks like after its base entry is gone.
    (user_dir / "cc_roster.local.yaml").write_text("default: removed-peer\n")
    return tmp_path


def test_unresolvable_default_falls_back_to_claude_loudly(tmp_path, monkeypatch, caplog):
    cfg = _dangling_default_roster(tmp_path)
    monkeypatch.setattr(R, "_CONFIG_DIR", cfg)

    inv = CCInvocation(prompt="hi", roster_eligible=True)
    with caplog.at_level(logging.ERROR, logger="genesis.cc.roster"):
        out, name = R.apply_active(inv)

    assert name == R.CLAUDE  # fell back rather than breaking the call
    assert out is inv  # no routing stamped
    assert caplog.records, "a silent provider change must not be silent"
    msg = caplog.records[0].getMessage()
    assert "removed-peer" in msg  # names the unresolvable selection
    assert "cc_roster" in msg  # points at where to fix it
    # and it must not read as a generic crash
    assert "apply_active failed" not in msg


def test_unresolvable_default_warns_once_not_per_invocation(tmp_path, monkeypatch, caplog):
    """apply_active runs on the UNIVERSAL CC path — an undeduped error here logs
    a traceback on every routed call and buries the one line that matters."""
    cfg = _dangling_default_roster(tmp_path)
    monkeypatch.setattr(R, "_CONFIG_DIR", cfg)

    inv = CCInvocation(prompt="hi", roster_eligible=True)
    with caplog.at_level(logging.ERROR, logger="genesis.cc.roster"):
        for _ in range(6):
            assert R.apply_active(inv)[1] == R.CLAUDE
    assert len(caplog.records) == 1


def test_native_default_is_never_blamed_when_models_is_nulled(tmp_path, monkeypatch, caplog):
    """A bare `models:` in an overlay merges as None, so `resolve` returns None
    for EVERY name — including the native default. Announcing that `claude`
    "cannot be resolved" and that "its base entry has been removed" would be
    flatly false, and would fire on the most common configuration there is."""
    (tmp_path / "cc_roster.yaml").write_text(textwrap.dedent(
        """
        default: claude
        models:
          claude:
            native_subscription: true
        """
    ))
    user_dir = tmp_path / "user-config"
    user_dir.mkdir(exist_ok=True)
    (user_dir / "cc_roster.local.yaml").write_text("models:\n")  # nulls the mapping
    monkeypatch.setattr(R, "_CONFIG_DIR", tmp_path)

    inv = CCInvocation(prompt="hi", roster_eligible=True)
    with caplog.at_level(logging.ERROR, logger="genesis.cc.roster"):
        out, name = R.apply_active(inv)

    assert name == R.CLAUDE and out is inv  # native, as configured
    assert not [r for r in caplog.records if "cannot be resolved" in r.getMessage()], (
        "claimed the native default was orphaned"
    )


def test_repaired_then_rebroken_default_warns_again(tmp_path, monkeypatch, caplog):
    """Three-state probe. A name-keyed cache that never clears makes the SECOND
    breakage silent — which is the exact silent provider substitution this
    branch exists to remove."""
    cfg = _dangling_default_roster(tmp_path)
    monkeypatch.setattr(R, "_CONFIG_DIR", cfg)
    inv = CCInvocation(prompt="hi", roster_eligible=True)
    local = cfg / "user-config" / "cc_roster.local.yaml"

    # 1. broken -> warns
    with caplog.at_level(logging.ERROR, logger="genesis.cc.roster"):
        assert R.apply_active(inv)[1] == R.CLAUDE
    assert len(caplog.records) == 1

    # 2. repaired -> resolves, no complaint
    caplog.clear()
    local.write_text("default: claude\n")
    with caplog.at_level(logging.ERROR, logger="genesis.cc.roster"):
        assert R.apply_active(inv)[1] == R.CLAUDE
    assert not caplog.records

    # 3. broken AGAIN -> must warn again, not be swallowed by the dedupe
    caplog.clear()
    local.write_text("default: removed-peer\n")
    with caplog.at_level(logging.ERROR, logger="genesis.cc.roster"):
        assert R.apply_active(inv)[1] == R.CLAUDE
    assert len(caplog.records) == 1, "a re-break was silent"


def test_defined_but_tokenless_default_uses_the_generic_handler(roster_dir, monkeypatch, caplog):
    """Locks the load-bearing narrowing: a model that EXISTS but has no auth
    token must not be reported as undefined — different fault, different repair.
    Verified by hand during review; nothing asserted it."""
    monkeypatch.delenv("ZHIPU_TEST_KEY", raising=False)
    (roster_dir / "cc_roster.local.yaml").write_text("default: glm-5.2\n")
    inv = CCInvocation(prompt="x", roster_eligible=True)
    with caplog.at_level(logging.ERROR, logger="genesis.cc.roster"):
        out, name = R.apply_active(inv, _load(roster_dir))
    assert out is inv and name == R.CLAUDE
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "apply_active failed" in msgs  # generic path
    assert "cannot be resolved" not in msgs  # NOT the undefined-model message
