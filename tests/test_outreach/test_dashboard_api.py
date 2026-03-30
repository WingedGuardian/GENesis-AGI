"""Tests for outreach dashboard API endpoints."""


def test_blueprint_importable():
    from genesis.outreach.api import outreach_api
    assert outreach_api.name == "outreach_api"


def test_blueprint_has_url_prefix():
    from genesis.outreach.api import outreach_api
    assert outreach_api.url_prefix == "/api/genesis/outreach"
