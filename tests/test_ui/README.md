# `tests/test_ui/` — browser-level dashboard E2E (Playwright)

Real-browser end-to-end tests for the Genesis dashboard. A headless Chromium
(Playwright) drives the dashboard Flask app served on an ephemeral localhost
port. This is coverage the in-process `test_client` suites (`tests/test_dashboard/`)
cannot give: the Alpine.js app actually boots, static assets actually load, tab
routing actually runs, and the cookie login round-trips in a real browser.

## Running

Playwright + Chromium are the `.[browser]` extra (not part of `.[test]`):

```bash
pip install -e '.[browser]'
playwright install chromium
pytest tests/test_ui/ -v
```

If `playwright` isn't installed, the conftest sets `collect_ignore_glob` so the
whole directory **skips cleanly** during collection (a module-level `importorskip`
would instead raise a collection *error*, which is the bug this guard avoids).

## Why this is not in CI

`.github/workflows/ci.yml` runs `pytest` with `--ignore=tests/test_ui/`, and CI
installs only `.[test]` (no browser). So **CI green does not cover these tests** —
they are a **local / on-demand** harness (run them before a release, or on a
schedule). Two layers keep them from ever breaking the pipeline: the CI ignore,
and the `importorskip` guard.

> Trade-off: because they don't run in CI, they can bitrot. Wiring a dedicated
> browser-enabled CI job (install `.[browser]` + `playwright install chromium`,
> drop the ignore) is a deliberate follow-on decision, not done here.

## Scope

The app is built from the **real** production factory
(`StandaloneAdapter._create_flask_app`, see `conftest.py`) but **without** a live
runtime — so data-backed routes degrade to HTTP 503. These tests therefore assert
the UI **shell, tab routing, and auth** (not live data). Data-populated
assertions would need a booted runtime and belong in a separate, heavier harness.

The zero-JS-error assertion checks **uncaught exceptions** (`pageerror`) only, and
deliberately tolerates backend `console.error` 503s — those are the backend's
concern, not a dashboard UI fault.
