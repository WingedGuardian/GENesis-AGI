"""A missing Scrapling must be loud, and an available one silent.

`scrapling.fetchers` imports browser-automation packages (playwright and
browserforge, among others) even for the plain AsyncFetcher. When one is
missing the import fails and web/fetch.py falls back to plain httpx, which
works but loses TLS impersonation; that used to happen with no trace. The fetch
tests patch `_HAS_SCRAPLING` off, which is why the logging needs tests of its own.
"""

from __future__ import annotations

import importlib
import logging

# `genesis.web` re-exports a function named `fetch`, so import the module by path.
fetch = importlib.import_module("genesis.web.fetch")


def test_missing_scrapling_is_logged_once(monkeypatch, caplog):
    monkeypatch.setattr(fetch, "_HAS_SCRAPLING", False)
    monkeypatch.setattr(
        fetch, "_SCRAPLING_IMPORT_ERROR", ImportError("No module named 'browserforge'")
    )
    monkeypatch.setattr(fetch, "_scrapling_failure_logged", False)
    with caplog.at_level(logging.WARNING, logger="genesis.web.fetch"):
        fetch.WebFetcher()
        fetch.WebFetcher()
    warnings = [r for r in caplog.records if "Scrapling is unavailable" in r.getMessage()]
    assert len(warnings) == 1
    assert "browserforge" in warnings[0].getMessage()
    # The remedy must be a command that exists in this tree: the repo's own
    # browser extra. AsyncFetcher's import pulls playwright and browserforge;
    # the extra declares playwright, and camoufox requires browserforge.
    # scrapling's own `fetchers` extra is not named because it pins exact
    # playwright/patchright versions (0.4.7: playwright==1.58.0) outside the
    # repo's declared browser dependencies.
    message = warnings[0].getMessage()
    assert "pip install -e '.[browser]'" in message
    assert "scrapling[fetchers]" not in message
    # No path to a script that is not in the tree.
    assert "scripts/" not in message


def test_browser_extra_carries_what_the_warning_promises():
    """The warning promises `.[browser]` supplies playwright and browserforge.

    playwright is declared directly; browserforge arrives through camoufox
    (camoufox 0.4.11 requires browserforge>=1.2.1,<2). Read the declaration
    itself so a change to the extra that drops both routes fails here rather
    than leaving the log message pointing at a remedy that no longer works.
    CI installs only `.[test]`, so this reads pyproject.toml, not the venv.
    """
    import tomllib
    from importlib.metadata import PackageNotFoundError, requires
    from pathlib import Path

    pyproject = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    extra = " ".join(pyproject["project"]["optional-dependencies"]["browser"]).lower()
    assert "playwright" in extra
    assert "browserforge" in extra or "camoufox" in extra
    if "browserforge" not in extra:
        try:
            camoufox_requires = requires("camoufox") or []
        except PackageNotFoundError:
            return  # not installed here; the declaration check above still ran
        assert any(r.lower().startswith("browserforge") for r in camoufox_requires)


def test_available_scrapling_logs_nothing(monkeypatch, caplog):
    monkeypatch.setattr(fetch, "_HAS_SCRAPLING", True)
    monkeypatch.setattr(fetch, "_scrapling_failure_logged", False)
    with caplog.at_level(logging.WARNING, logger="genesis.web.fetch"):
        fetch.WebFetcher()
    assert not [r for r in caplog.records if "Scrapling is unavailable" in r.getMessage()]
