"""Shared fixtures for browser-level dashboard E2E (Playwright).

See ``test_dashboard_e2e.py`` for the harness rationale. Playwright + Chromium
are the ``.[browser]`` extra (absent in CI), so the whole ``tests/test_ui/``
tree skips there via ``importorskip`` and is additionally ``--ignore``d by CI.
"""

from __future__ import annotations

import threading

import pytest
from werkzeug.serving import make_server

# Skip the entire directory cleanly when Playwright isn't installed (CI, or an
# install without the browser extra).
pytest.importorskip("playwright", reason="playwright not installed (browser extra)")
from playwright.sync_api import sync_playwright  # noqa: E402


def _build_dashboard_app(monkeypatch, tmp_home, *, password: str | None):
    """Build the real dashboard Flask app without full-runtime bootstrap.

    Reuses ``StandaloneAdapter._create_flask_app()`` (which touches no instance
    state) for zero drift from production, then registers only the dashboard
    blueprint. ``_register_blueprints`` also wires the outreach blueprint through
    a live ``GenesisRuntime.instance()`` — unnecessary for UI E2E, so skipped.
    Routes that need the runtime degrade (HTTP 503); the UI shell still renders.

    Home isolation (so tests never touch the real ~/.genesis): ``_create_flask_app``
    writes two files at build time — the Flask secret key (via
    ``get_or_create_secret_key``, which uses ``Path.home()`` directly) and the
    internal API token (via ``apply_api_mutation_gate`` →
    ``get_or_create_internal_api_token`` → ``genesis_home()``). We redirect
    ``GENESIS_HOME`` to a tmp dir (covers the token writer and any future
    genesis_home()-based writer) AND patch the secret-key getter (it bypasses
    genesis_home()); we also reset the module-global token cache so it re-mints
    under the fake home instead of returning/leaking the real one.
    """
    monkeypatch.setenv("GENESIS_HOME", str(tmp_home))
    monkeypatch.setattr("genesis.dashboard.auth._internal_token_cache", None, raising=False)
    monkeypatch.setattr(
        "genesis.dashboard.auth.get_or_create_secret_key",
        lambda: "test-secret-key-e2e",
    )
    if password:
        monkeypatch.setenv("DASHBOARD_PASSWORD", password)
    else:
        monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)

    from genesis.hosting.standalone import StandaloneAdapter

    adapter = StandaloneAdapter(host="127.0.0.1", port=0)
    app = adapter._create_flask_app()

    from genesis.dashboard.api import blueprint as dash_bp

    if "genesis_dashboard" not in app.blueprints:
        app.register_blueprint(dash_bp)
    return app


@pytest.fixture
def serve_dashboard(monkeypatch, tmp_path):
    """Factory fixture: ``start(password=None) -> base_url``.

    Serves the dashboard on an OS-assigned localhost port in a daemon thread.
    Every server started is shut down on teardown. ``GENESIS_HOME`` is redirected
    into ``tmp_path`` so no test writes to the real ~/.genesis (see
    ``_build_dashboard_app``).
    """
    tmp_home = tmp_path / "genesis-home"
    tmp_home.mkdir(parents=True, exist_ok=True)
    servers: list[tuple] = []

    def start(*, password: str | None = None) -> str:
        app = _build_dashboard_app(monkeypatch, tmp_home, password=password)
        srv = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        servers.append((srv, thread))
        return f"http://127.0.0.1:{srv.server_port}"

    yield start

    for srv, thread in servers:
        srv.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def browser_page():
    """Headless Chromium page. Skips if the browser binary can't launch."""
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - env without a browser binary
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page()
        yield page
        browser.close()


@pytest.fixture
def page_errors(browser_page):
    """Collect UNCAUGHT JS exceptions (pageerror) for a zero-JS-error assertion.

    Deliberately does NOT collect ``console.error``: a backend route degrading to
    HTTP 503 (no runtime booted) surfaces as a console error, not a JS fault —
    that is the backend's concern, tested elsewhere, not a dashboard UI bug.
    """
    errors: list[str] = []
    browser_page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    return errors
