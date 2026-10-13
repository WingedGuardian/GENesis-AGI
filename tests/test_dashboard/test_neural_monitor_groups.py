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


def _grid_positions() -> dict[str, list[tuple[int, int]]]:
    block = _TEMPLATE[
        _TEMPLATE.index("const GRID_POSITIONS") : _TEMPLATE.index("const GRID_COLS")
    ]
    return {
        key: [(int(c), int(r)) for c, r in re.findall(r"\[(\d+),\s*(\d+)\]", cells)]
        for key, cells in re.findall(r"^\s*(\w+):\s*(\[\[.*\]\]),?\s*$", block, re.M)
    }


def _group_sites() -> dict[str, list[str]]:
    block = _TEMPLATE[
        _TEMPLATE.index("const SUBSYSTEM_GROUPS") : _TEMPLATE.index("function aggregateStatus")
    ]
    keys = re.findall(r"^    (\w+):\s*\{", block, re.M)
    lists = re.findall(r"sites:\s*\[([^\]]*)\]", block)
    assert len(keys) == len(lists)
    return {k: re.findall(r"['\"]([^'\"]+)['\"]", lst) for k, lst in zip(keys, lists, strict=True)}


def test_the_grid_parser_sees_the_map():
    """Every entry line in GRID_POSITIONS must parse: an entry wrapped across
    lines would otherwise be skipped, and the tests below would pass without
    checking it."""
    block = _TEMPLATE[
        _TEMPLATE.index("const GRID_POSITIONS") : _TEMPLATE.index("const GRID_COLS")
    ]
    block = block[block.index("{") : block.index("};")]
    entries = re.findall(r"^\s*\w+:", block, re.M)
    assert len(_grid_positions()) == len(entries) >= 10


def test_every_hand_placed_group_has_one_cell_per_site():
    """Short, and the renderer moves the whole group into the flow rows and
    leaves its hand cells empty. Long, and a cell sits unused."""
    groups = _group_sites()
    wrong = {
        key: (len(cells), len(groups.get(key, [])))
        for key, cells in _grid_positions().items()
        if len(cells) != len(groups.get(key, []))
    }
    assert not wrong, f"(cells, sites) per group in GRID_POSITIONS: {wrong}"


def test_no_two_hand_placed_sites_share_a_cell():
    cells = [c for v in _grid_positions().values() for c in v]
    assert len(cells) == len(set(cells)), sorted({c for c in cells if cells.count(c) > 1})
    assert all(1 <= col <= 7 for col, _ in cells)


def test_each_hand_placed_group_is_one_connected_block():
    for key, cells in _grid_positions().items():
        todo, seen = [cells[0]], {cells[0]}
        while todo:
            col, row = todo.pop()
            for nxt in ((col + 1, row), (col - 1, row), (col, row + 1), (col, row - 1)):
                if nxt in cells and nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        assert seen == set(cells), f"{key} is split: {sorted(set(cells) - seen)}"
