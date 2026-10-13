"""scripts/uninstall.sh stops and disables every systemd user unit a template installs.

The three lists it does that from (the in-container loop, the host-driven stop list
and the host-driven disable loop) are kept by hand, so the expected set here is
derived from scripts/systemd/*.template: a unit added without reaching all three
fails. Known gaps are named, each with the issue that owns it.
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


def _code() -> str:
    text = (REPO / "scripts" / "uninstall.sh").read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _between(code: str, start: str, end: str) -> str:
    i = code.index(start)
    return code[i : code.index(end, i)]


def _last_before(code: str, start: str, end: str) -> str:
    """From the LAST ``start`` before ``end``: the same stop line also pauses the
    watchdog far earlier, and a slice from there would take in the other sites."""
    j = code.index(end)
    return code[code.rindex(start, 0, j) : j]


def _sites() -> dict[str, str]:
    code = _code()
    return {
        "in-container stop+disable": _between(
            # The trailing backslash: a shorter loop over the watchdog alone
            # (the pre-teardown pause) starts with the same words.
            code,
            "for unit in genesis-watchdog.timer genesis-watchdog.service \\\n",
            'safe_disable_service "$unit"',
        ),
        "host-driven stop": _last_before(
            code,
            "systemctl --user stop genesis-watchdog.timer genesis-watchdog.service",
            "for u in genesis-server.service",
        ),
        "host-driven disable": _between(
            code, "for u in genesis-server.service", 'systemctl --user disable \\"\\$u\\"'
        ),
    }


def _units() -> list[str]:
    return sorted(
        p.name.removesuffix(".template")
        for p in TEMPLATES.glob("*.template")
        if p.name.endswith((".service.template", ".timer.template"))
    )


def test_the_scan_finds_the_three_sites_and_the_templates():
    """Denominators: an empty site or template list would make the test vacuous."""
    sites = _sites()
    assert all("genesis-server.service" in s for s in sites.values()), sites.keys()
    assert len(_units()) >= 20


def test_each_site_is_only_its_own_block():
    """A slice that ran into another site would credit it with that site's units."""
    sites = _sites()
    assert "safe_disable_service" not in sites["host-driven stop"]
    assert "PRESSURE_UNIT=" not in sites["host-driven stop"]
    assert "container_exec" not in sites["host-driven stop"]
    assert "systemctl --user disable" not in sites["host-driven stop"]


@pytest.mark.parametrize(
    "site", ["in-container stop+disable", "host-driven stop", "host-driven disable"]
)
def test_every_installed_unit_reaches_every_uninstall_site(site):
    text = _sites()[site]
    missing = [
        u
        for u in _units()
        if u not in _KNOWN_GAPS and u not in _INSTANCE_TEMPLATES and u not in text
    ]
    assert not missing, f"{site} does not stop/disable: {missing}"


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
