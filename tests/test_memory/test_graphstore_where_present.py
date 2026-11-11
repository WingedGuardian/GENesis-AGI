"""The where-present guard in ``graphstore_config.selection`` (D3).

The shipped default is ``mode: falkordb``. On an install with no engine unit
and no overlay choosing a mode, reads stay on networkx QUIETLY; an overlay that
sets ``mode: falkordb`` stays loud, and so does a unit that exists. Every case
runs against real files in a temp tree: a base config, the user overlay dir,
and a fake home holding (or not) the rendered unit.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from genesis import _config_overlay
from genesis.memory import graphstore_config as gc

QUIET = gc.ENGINE_NOT_PROVISIONED


@pytest.fixture
def tree(tmp_path, monkeypatch):
    base = tmp_path / "repo" / "config" / "graphstore.yaml"
    base.parent.mkdir(parents=True)
    base.write_text(yaml.safe_dump({"enabled": True, "mode": "falkordb"}))
    user_dir = tmp_path / "genesis_config"
    user_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(gc, "_base_path", lambda: base)
    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: user_dir)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GENESIS_FALKORDB_STORE_DISABLED", raising=False)
    gc.reset_config_cache()
    yield {"base": base, "overlay": user_dir / "graphstore.local.yaml", "home": home}
    gc.reset_config_cache()


def _unit(tree, *, enabled: bool = True) -> Path:
    """The engine unit as the installers leave it: rendered, and enabled
    (``default.target.wants`` link) unless ``enabled`` is False."""
    unit = tree["home"] / ".config" / "systemd" / "user" / "genesis-falkordb.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Unit]\n")
    if enabled:
        wants = unit.parent / "default.target.wants"
        wants.mkdir(exist_ok=True)
        (wants / unit.name).symlink_to(unit)
    return unit


def _overlay(tree, cfg: dict) -> None:
    path = tree["overlay"]
    path.write_text(yaml.safe_dump(cfg))
    # Move the key forward explicitly: a rewrite inside one filesystem tick is
    # what reset_config_cache exists for, and this test is about the key.
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_the_shipped_file_selects_falkordb():
    """Pin the shipped default itself, read from this repo's real file."""
    shipped = yaml.safe_load((gc.repo_root() / "config" / "graphstore.yaml").read_text())
    assert shipped["mode"] == "falkordb"
    assert shipped["enabled"] is True
    assert gc.DEFAULTS["mode"] == "falkordb", "the fallback matches the shipped file"


def test_shipped_falkordb_with_no_unit_is_a_quiet_networkx(tree):
    assert gc.selection() == ("networkx", QUIET)
    assert gc.effective_mode() == "networkx"


def test_shipped_falkordb_with_the_unit_selects_falkordb(tree):
    _unit(tree)
    assert gc.selection() == ("falkordb", None)


def test_an_overlay_choosing_falkordb_stays_loud_without_a_unit(tree):
    _overlay(tree, {"mode": "falkordb"})
    assert gc.mode_set_by_overlay() is True
    assert gc.selection() == ("falkordb", None)


def test_an_overlay_choosing_networkx_is_networkx_and_not_quiet(tree):
    _unit(tree)
    _overlay(tree, {"mode": "networkx"})
    assert gc.selection() == ("networkx", None)


def test_an_overlay_that_sets_only_enabled_leaves_the_mode_shipped(tree):
    _overlay(tree, {"enabled": True})
    assert gc.mode_set_by_overlay() is False
    assert gc.selection() == ("networkx", QUIET)


def test_the_kill_switch_and_a_disabled_lever_are_never_quiet(tree, monkeypatch):
    monkeypatch.setenv("GENESIS_FALKORDB_STORE_DISABLED", "1")
    assert gc.selection() == ("networkx", None)
    monkeypatch.delenv("GENESIS_FALKORDB_STORE_DISABLED")
    _overlay(tree, {"enabled": False})
    assert gc.selection() == ("networkx", None)


def test_the_cache_follows_the_overlay_and_the_unit(tree):
    assert gc.selection() == ("networkx", QUIET)
    _overlay(tree, {"mode": "falkordb"})
    assert gc.selection() == ("falkordb", None), "an overlay write must be seen next call"
    _overlay(tree, {"enabled": True})
    assert gc.selection() == ("networkx", QUIET)
    unit = _unit(tree)
    assert gc.selection() == ("falkordb", None), "a unit that appears must be seen next call"
    unit.unlink()
    assert gc.selection() == ("networkx", QUIET)


def test_a_broken_overlay_sets_no_mode(tree):
    tree["overlay"].write_text("mode: [unclosed\n")
    assert gc.mode_set_by_overlay() is False
    assert gc.selection() == ("networkx", QUIET)


def test_a_rendered_but_disabled_unit_is_not_provisioned(tree):
    """D3 review: installs rendered the unit and left it disabled before the
    default changed, and an operator opts out by disabling it. Neither may flip
    reads to an engine that is not armed: quiet networkx, not a loud fallback."""
    _unit(tree, enabled=False)
    assert gc.engine_unit_present() is False
    assert gc.selection() == ("networkx", QUIET)


def test_disabling_the_unit_takes_effect_on_the_next_read(tree):
    unit = _unit(tree)
    assert gc.selection() == ("falkordb", None)
    (unit.parent / "default.target.wants" / unit.name).unlink()
    assert gc.selection() == ("networkx", QUIET)
