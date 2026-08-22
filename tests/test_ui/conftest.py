"""Shared fixtures for browser-level dashboard E2E (Playwright).

See ``test_dashboard_e2e.py`` for the harness rationale. Playwright + Chromium
are the ``.[browser]`` extra (absent in CI), so when it's missing this conftest
sets ``collect_ignore_glob`` to skip the whole ``tests/test_ui/`` tree cleanly
during collection (CI also ``--ignore``s it, belt-and-suspenders).
"""

from __future__ import annotations

import threading

import pytest
from werkzeug.serving import make_server

# Playwright is the optional ``.[browser]`` extra. A conftest MUST import cleanly
# even when it's absent, because pytest reads ``collect_ignore_glob`` from the
# imported module — a module-level ``importorskip`` or ``import playwright`` would
# instead raise *while loading the conftest*, which pytest surfaces as a COLLECTION
# ERROR (exit 1), not a skip. So detect the extra here and, when absent, exclude
# the whole directory; the ``sync_playwright`` import is deferred into the one
# fixture that needs it.
try:
    import playwright  # noqa: F401
except ImportError:
    collect_ignore_glob = ["*"]


def _isolate_dashboard_home(monkeypatch, tmp_home):
    """Redirect every dashboard route module's import-time ``Path.home()``-bound
    path onto ``tmp_home``, so no E2E test can read or mutate the developer's real
    ``~/genesis`` / ``~/.genesis`` / ``~/.claude`` trees.

    WHY per-module patching: these modules bind their path globals from
    ``Path.home()`` AT IMPORT (not ``GENESIS_HOME`` / not a late ``HOME`` redirect),
    so a value already evaluated at import time is immune to an env change. The
    shell polls several of these on load (``/updates/status`` WRITES, ``/files``
    READS the real home, ``/setup-status`` READS), and ``files.py`` exposes
    write/delete/upload routes rooted at the real home. Request-time
    ``Path.home()`` calls (state/health/cc_sessions/vitals) are already covered by
    the ``HOME`` redirect in ``_build_dashboard_app``; only the import-bound globals
    below need explicit patching.

    ROOT CAUSE (tracked for a production refactor, not fixable in the harness): the
    dashboard routes package binds host paths at import instead of resolving them
    lazily / honoring an env override. Until that lands, this map must track any new
    import-bound home global added to ``src/genesis/dashboard/routes/``.
    """
    gen = tmp_home / "genesis"
    dot = tmp_home / ".genesis"
    # Create the isolated roots so routes that list/stat them (e.g. /files defaults
    # to <home>/genesis) see an existing EMPTY dir and return 200, not a 400/500
    # that would surface as a JS error in the browser tests.
    for d in (gen, gen / "data", gen / "logs", dot, tmp_home / ".claude"):
        d.mkdir(parents=True, exist_ok=True)
    # (dotted module global) -> isolated tmp path
    _patches = {
        # updates.py — /updates/status GET auto-resolves an observation (WRITE)
        "genesis.dashboard.routes.updates._HOME": tmp_home,
        "genesis.dashboard.routes.updates._GENESIS_ROOT": gen,
        "genesis.dashboard.routes.updates._UPDATE_SCRIPT": gen / "scripts" / "update.sh",
        "genesis.dashboard.routes.updates._DB_PATH": gen / "data" / "genesis.db",
        "genesis.dashboard.routes.updates._FAILURE_FILE": dot / "last_update_failure.json",
        "genesis.dashboard.routes.updates._GENESIS_DIR": dot,
        "genesis.dashboard.routes.updates._SUMMARY_FILE": dot / "last_update_summary.txt",
        "genesis.dashboard.routes.updates._ESCALATION_FILE": dot / "update_escalation.txt",
        "genesis.dashboard.routes.updates._CONFLICT_FILE": dot / "update_conflicts.json",
        "genesis.dashboard.routes.updates._STATE_FILE": dot / "update_state.json",
        "genesis.dashboard.routes.updates._PID_FILE": dot / "update_in_progress.pid",
        # files.py — /files READS real home; write/delete/upload routes too
        "genesis.dashboard.routes.files._HOME": tmp_home,
        "genesis.dashboard.routes.files._ALLOWED_ROOTS": [gen, dot, tmp_home / ".claude"],
        "genesis.dashboard.routes.files._UPLOAD_DIR": dot / "uploads",
        # setup.py — /setup-status READS the marker (polled every load)
        "genesis.dashboard.routes.setup._SETUP_COMPLETE_MARKER": dot / "setup-complete",
        # backup.py — /backup/config WRITES secrets + toggles the real timer
        "genesis.dashboard.routes.backup._HOME": tmp_home,
        "genesis.dashboard.routes.backup._STATUS_FILE": dot / "backup_status.json",
        "genesis.dashboard.routes.backup._BACKUP_SCRIPT": gen / "scripts" / "backup.sh",
        "genesis.dashboard.routes.backup._BACKUP_LOG": gen / "logs" / "backup.log",
        "genesis.dashboard.routes.backup._BACKUP_DIR": tmp_home / "backups" / "genesis-backups",
        "genesis.dashboard.routes.backup._TIMER_DROPIN": (
            tmp_home / ".config" / "systemd" / "user" / "genesis-backup.timer.d" / "schedule.conf"
        ),
        # config.py — memory dir under ~/.claude
        "genesis.dashboard.routes.config._MEMORY_DIR": tmp_home
        / ".claude"
        / "projects"
        / "test"
        / "memory",
        # knowledge_upload.py — upload/completed dirs
        "genesis.dashboard.routes.knowledge_upload._UPLOAD_DIR": dot / "knowledge" / "inbox",
        "genesis.dashboard.routes.knowledge_upload._COMPLETED_DIR": dot / "knowledge" / "completed",
    }
    for dotted, value in _patches.items():
        monkeypatch.setattr(dotted, value, raising=False)


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

    Route-module home isolation: several ``routes/*`` modules bind host paths from
    ``Path.home()`` AT IMPORT (not ``GENESIS_HOME``), and the shell polls some of
    them on load — ``/updates/status`` WRITES, ``/files`` READS the real home,
    ``/setup-status`` READS. We redirect ``HOME`` (covers request-time
    ``Path.home()`` reads) AND patch every import-bound route global via
    ``_isolate_dashboard_home`` (see it for the full map + root-cause note).
    """
    monkeypatch.setenv("GENESIS_HOME", str(tmp_home))
    monkeypatch.setenv("HOME", str(tmp_home))
    monkeypatch.setattr("genesis.dashboard.auth._internal_token_cache", None, raising=False)
    monkeypatch.setattr(
        "genesis.dashboard.auth.get_or_create_secret_key",
        lambda: "test-secret-key-e2e",
    )
    _isolate_dashboard_home(monkeypatch, tmp_home)
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
    # Deferred import: the conftest must load without the optional ``.[browser]``
    # extra (see the module docstring). This fixture only runs when the directory
    # was collected, which happens only when playwright is importable.
    from playwright.sync_api import sync_playwright

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
