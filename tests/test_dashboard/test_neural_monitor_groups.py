"""The neural monitor's subsystem map must account for every call site.

SUBSYSTEM_GROUPS in the template is a hand-maintained list. It drifted: 25
routing call sites, and several ids known to the call-site metadata, rendered
only in an "Other" catch-all card. These tests make drift a CI failure
instead of something noticed on the dashboard.

Scope: the sources a site can be known from STATICALLY — the routing config
and the call-site metadata. An id that exists only as a historical run row
cannot be enumerated here; the template's "Other" card remains the runtime
safety net for that case, and a retired id belongs in the metadata as
DEPRECATED_REMOVED so the snapshot stops resurrecting it.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from genesis.observability._call_site_meta import _CALL_SITE_META

_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE = (_ROOT / "src/genesis/dashboard/templates/neural_monitor.html").read_text()


def _grouped_sites() -> list[str]:
    block = _TEMPLATE[
        _TEMPLATE.index("const SUBSYSTEM_GROUPS") : _TEMPLATE.index("function aggregateStatus")
    ]
    lists = re.findall(r"sites:\s*\[([^\]]*)\]", block)
    return [site for lst in lists for site in re.findall(r"['\"]([^'\"]+)['\"]", lst)]


def _named_sites() -> set[str]:
    block = _TEMPLATE[_TEMPLATE.index("const SITE_META") : _TEMPLATE.index("// Category colors")]
    return set(re.findall(r"^\s*['\"]([^'\"]+)['\"]:\s*\{", block, re.M))


def test_the_parser_sees_the_groups():
    """Guard against a regex that silently matches nothing."""
    assert len(_grouped_sites()) > 60


def test_every_routing_call_site_is_grouped():
    config = yaml.safe_load((_ROOT / "config/model_routing.yaml").read_text())
    missing = sorted(set(config["call_sites"]) - set(_grouped_sites()))
    assert not missing, f"add to SUBSYSTEM_GROUPS in neural_monitor.html: {missing}"


def test_every_live_metadata_site_is_grouped():
    """Entries marked `tile: False` are aliases or work tags, not call sites:
    they never produce call-site data, so grouping them would add a
    permanently grey tile."""
    live = {
        k
        for k, v in _CALL_SITE_META.items()
        if v.get("status_reason") != "DEPRECATED_REMOVED" and v.get("tile") is not False
    }
    missing = sorted(live - set(_grouped_sites()))
    assert not missing, f"add to SUBSYSTEM_GROUPS, or mark DEPRECATED_REMOVED if retired: {missing}"


def test_no_site_is_in_two_groups():
    sites = _grouped_sites()
    assert len(sites) == len(set(sites)), sorted({s for s in sites if sites.count(s) > 1})


def test_every_grouped_site_has_a_display_name():
    """Without one, getMeta() falls back to the raw id on the tile."""
    missing = sorted(set(_grouped_sites()) - _named_sites())
    assert not missing, f"add a SITE_META entry: {missing}"


def test_no_retired_site_is_still_grouped():
    retired = {
        k for k, v in _CALL_SITE_META.items() if v.get("status_reason") == "DEPRECATED_REMOVED"
    }
    assert not (retired & set(_grouped_sites()))


def test_no_non_tile_entry_is_grouped():
    """An alias or tag given a tile would render as a permanent 'unknown' dot."""
    non_tiles = {k for k, v in _CALL_SITE_META.items() if v.get("tile") is False}
    assert not (non_tiles & set(_grouped_sites()))
