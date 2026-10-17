"""scripts/uninstall.sh stops and disables every systemd user unit a template installs.

Host cleanup delegates to the guarded container transaction, which has one
ordinary-unit list plus managed Codebase retirement. Derive the expected set
from scripts/systemd/*.template so neither route can omit an installed unit.
Known gaps are named, each with the issue that owns it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "scripts" / "systemd"

# Pre-existing gaps, tracked in #3225; remove a name once uninstall covers it.
_KNOWN_GAPS = {
    "genesis-cc-align.timer",
    "genesis-cc-align.service",
    "genesis-code-intel-freeze.service",
    "agent-zero.service",
}
# Started per instance on demand and never enabled: stopped by instance name, not disabled.
_INSTANCE_TEMPLATES = {"genesis-disk-hygiene-pressure@.service"}
# This service must retire through the fail-closed guard, before shell cleanup.
_MANAGED_UNITS = {"genesis-cbm-query.service"}


def _code() -> str:
    text = (REPO / "scripts" / "uninstall.sh").read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _between(code: str, start: str, end: str) -> str:
    i = code.index(start)
    return code[i : code.index(end, i)]


def _sites() -> dict[str, str]:
    code = _code()
    return {
        "shared stop+disable": _between(
            # The trailing backslash: a shorter loop over the watchdog alone
            # (the pre-teardown pause) starts with the same words.
            code,
            "for unit in genesis-watchdog.timer genesis-watchdog.service \\\n",
            'safe_disable_service "$unit"',
        ),
    }


def _units() -> list[str]:
    return sorted(
        p.name.removesuffix(".template")
        for p in TEMPLATES.glob("*.template")
        if p.name.endswith((".service.template", ".timer.template"))
    )


def test_the_scan_finds_the_shared_site_and_the_templates():
    """Denominators: an empty site or template list would make the test vacuous."""
    sites = _sites()
    assert all("genesis-server.service" in s for s in sites.values()), sites.keys()
    assert len(_units()) >= 20


def test_each_site_is_only_its_own_block():
    """A slice that ran into another site would credit it with that site's units."""
    sites = _sites()
    text = sites["shared stop+disable"]
    assert "safe_disable_service" not in text
    assert "PRESSURE_UNIT=" not in text
    assert "container_exec" not in text
    assert "incus exec" not in text


@pytest.mark.parametrize("site", ["shared stop+disable"])
def test_every_installed_unit_reaches_every_uninstall_site(site):
    text = _sites()[site]
    missing = [
        u
        for u in _units()
        if u not in _KNOWN_GAPS | _INSTANCE_TEMPLATES | _MANAGED_UNITS and u not in text
    ]
    assert not missing, f"{site} does not stop/disable: {missing}"


def test_host_cleanup_delegates_to_the_shared_guarded_transaction():
    host = _between(
        _code(),
        'log "Cleaning Genesis inside container',
        'ok "Genesis removal complete (via incus exec)"',
    )
    assert 'incus exec "$CONTAINER_NAME" -- /usr/bin/su --login' in host
    assert 'codebase_managed.py" uninstall -- --genesis-only --non-interactive' in host
    assert 'genesis-uninstall "$CONTAINER_GENESIS_HOME"' in host
    assert "systemctl --user stop" not in host
    assert "systemctl --user disable" not in host


def test_managed_units_retire_before_the_shared_shell_cleanup():
    managed = (REPO / "scripts/codebase_managed.py").read_text(encoding="utf-8")
    assert set(_units()) >= _MANAGED_UNITS
    assert 'BACKEND = "genesis-cbm-query.service"' in managed
    retirement = _between(managed, "def retire_managed()", "def report_retained_state()")
    assert '("disable", BACKEND)' in retirement
    assert '("stop", BACKEND)' in retirement
    assert '("stop", SLICE)' in retirement
    assert "require_quiescent(unit)" in retirement
    transaction = _between(managed, "def uninstall(", "def read_document(")
    assert transaction.index("retire_managed()") < transaction.index("os.execv(")


def test_the_browser_stack_timer_goes_before_its_service():
    """A timer stopped after its service can start the service again mid-teardown."""
    for name, text in _sites().items():
        timer = text.index("genesis-browser-stack.timer")
        service = text.index("genesis-browser-stack.service")
        assert timer < service, name


def test_known_gaps_are_still_gaps():
    """Each exemption must still be needed; a fixed one comes off the list."""
    sites = _sites()
    for unit in _KNOWN_GAPS:
        assert (TEMPLATES / f"{unit}.template").exists(), unit
        assert not all(re.search(re.escape(unit), s) for s in sites.values()), unit
