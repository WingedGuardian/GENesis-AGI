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
