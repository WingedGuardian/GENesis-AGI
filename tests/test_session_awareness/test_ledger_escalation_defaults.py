"""The escalation provenance default, in both places it ships.

`load_config` overlays the repo yaml on top of DEFAULTS, so a value present in
one and missing from the other is a default that silently never applies on a
fresh install. Both must name the owner-commitment values, and only those.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from genesis.db.crud.session_charters import CALLER_SETTABLE_ADDED_BY
from genesis.session_awareness import ledger_escalation_config as cfg_mod

_EXPECTED = {"foreground", "relayed"}
_YAML = Path(__file__).resolve().parents[2] / "config" / "ledger_escalation.yaml"


def test_module_default_escalates_owner_commitments_only():
    assert set(cfg_mod.DEFAULTS["escalate_added_by"]) == _EXPECTED


def test_shipped_yaml_matches_the_module_default():
    shipped = yaml.safe_load(_YAML.read_text())["escalate_added_by"]
    assert set(shipped) == _EXPECTED


def test_fresh_install_config_resolves_to_the_default(monkeypatch):
    """No overlay (a fresh install): the merged config yields the default."""
    monkeypatch.setattr(cfg_mod, "_base_path", lambda: _YAML)
    monkeypatch.setattr(cfg_mod, "merge_local_overlay", lambda base, _path: base)
    assert cfg_mod.escalate_added_by(cfg_mod.load_config()) == frozenset(_EXPECTED)


def test_relayed_is_caller_settable():
    """An escalating value nobody can set would be inert."""
    assert "relayed" in CALLER_SETTABLE_ADDED_BY
