"""reflection_models settings validator + domain registration."""

from __future__ import annotations

from genesis.dashboard.routes.settings import _FORM_DOMAINS
from genesis.mcp.health.settings import _DOMAIN_REGISTRY, _DOMAIN_VALIDATORS

_validate = _DOMAIN_VALIDATORS["reflection_models"]


def test_domain_registered_with_config_file():
    dom = _DOMAIN_REGISTRY["reflection_models"]
    assert dom.config_filename == "reflection_models.yaml"
    assert dom.readonly is False
    assert dom.needs_restart is False  # read live per reflection


def test_has_form_on_dashboard():
    """The domain is in the dashboard form allowlist so it renders editable."""
    assert "reflection_models" in _FORM_DOMAINS


def test_valid_changes_pass():
    assert _validate({"deep": {"effort": "xhigh"}}) == []
    assert _validate({"deep": {"model": "opus"}}) == []
    assert _validate({"strategic": {"model": "opus", "effort": "max"}}) == []
    assert _validate({"light": {"model": "haiku"}}) == []


def test_unknown_depth_rejected():
    (err,) = _validate({"bogus": {"model": "opus"}})
    assert "Unknown depth" in err


def test_unknown_subkey_rejected():
    errs = _validate({"deep": {"temperature": 0.5}})
    assert any("Unknown key" in e for e in errs)


def test_non_mapping_spec_rejected():
    errs = _validate({"deep": "sonnet"})
    assert any("must be a mapping" in e for e in errs)


def test_bad_model_rejected():
    errs = _validate({"deep": {"model": "gpt-9"}})
    assert any("model must be one of" in e for e in errs)


def test_bad_effort_rejected():
    errs = _validate({"deep": {"effort": "turbo"}})
    assert any("effort must be one of" in e for e in errs)
