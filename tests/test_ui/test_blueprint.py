"""Tests for the Genesis UI overlay blueprint."""

from __future__ import annotations

import pytest
from flask import Flask

from genesis.ui.blueprint import blueprint, register_injection


@pytest.fixture()
def app():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


# ── Static asset serving ─────────────────────────────────────────────


def test_overlay_css_serves(client):
    resp = client.get("/genesis-ui/genesis-overlay.css")
    assert resp.status_code == 200
    assert "text/css" in resp.content_type


def test_overlay_js_serves(client):
    resp = client.get("/genesis-ui/genesis-overlay.js")
    assert resp.status_code == 200


def test_logo_svg_serves(client):
    resp = client.get("/genesis-ui/genesis-logo.svg")
    assert resp.status_code == 200


def test_watermark_svg_serves(client):
    resp = client.get("/genesis-ui/genesis-watermark.svg")
    assert resp.status_code == 200


def test_nonexistent_asset_404(client):
    resp = client.get("/genesis-ui/nonexistent.xyz")
    assert resp.status_code == 404


# ── HTML injection ───────────────────────────────────────────────────


def test_injection_inserts_tags():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    register_injection(app)
    app.config["TESTING"] = True

    @app.route("/")
    def index_page():
        return "<html><head></head><body>Hello</body></html>"

    with app.test_client() as c:
        resp = c.get("/")
        html = resp.get_data(as_text=True)
        assert "/genesis-ui/genesis-overlay.css" in html
        assert "/genesis-ui/genesis-overlay.js" in html


def test_injection_skips_non_root_html():
    """Injection only fires on / path, not on component HTML fragments."""
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    register_injection(app)
    app.config["TESTING"] = True

    @app.route("/components/test.html")
    def component_page():
        return "<html><head></head><body>Component</body></html>"

    with app.test_client() as c:
        resp = c.get("/components/test.html")
        html = resp.get_data(as_text=True)
        assert "/genesis-ui/" not in html


def test_injection_skips_non_html():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    register_injection(app)
    app.config["TESTING"] = True

    @app.route("/test-json")
    def test_json():
        from flask import jsonify
        return jsonify({"hello": "world"})

    with app.test_client() as c:
        resp = c.get("/test-json")
        data = resp.get_data(as_text=True)
        assert "/genesis-ui/" not in data


def test_injection_no_double_inject():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    register_injection(app)
    app.config["TESTING"] = True

    @app.route("/")
    def root_already():
        return '<html><head><script src="/genesis-ui/genesis-overlay.js"></script></head><body></body></html>'

    with app.test_client() as c:
        resp = c.get("/")
        html = resp.get_data(as_text=True)
        # Should appear exactly once (the original), not duplicated
        assert html.count("/genesis-ui/genesis-overlay.js") == 1


# NOTE: /api/genesis/ui/* data endpoints moved to genesis_dashboard blueprint.
# Tests for those endpoints: tests/test_dashboard/test_ui_data.py
