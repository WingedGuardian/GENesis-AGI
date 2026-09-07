"""Tests for the FalkorDB provisioning collector.

The load-bearing property is the CONTRACT with the posture rule: the rule in
awareness/loop.py reads exactly two keys out of this collector's ``metrics``,
and a rename on either side would disarm the only alert this feature has —
silently, because a rule that reads a missing key simply never fires.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from genesis.awareness import loop as _loop
from genesis.infra_profile.collectors import CONTAINER_COLLECTORS
from genesis.infra_profile.collectors.falkordb_facts import (
    _installed_versions,
    collect_falkordb,
)


def _run(monkeypatch, home: Path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return asyncio.run(collect_falkordb())


def test_unprovisioned_box_reports_absence_without_failing(monkeypatch, tmp_path):
    """The common case on every install that never adopts the engine."""
    result = _run(monkeypatch, tmp_path)

    assert result.name == "falkordb"
    assert result.facts["unit_present"] is False
    assert result.facts["module_installed"] is False
    assert result.facts["module_versions"] == []
    assert result.metrics["socket_present"] is False


def test_volatile_state_is_in_metrics_not_facts(monkeypatch, tmp_path):
    """facts are HASHED; a value that flips on restart would bill an LLM call.

    infra_profile/types.py puts "states" on the metrics side explicitly. This
    pins the split so a future edit cannot quietly move them back.
    """
    result = _run(monkeypatch, tmp_path)

    assert "unit_active_state" in result.metrics
    assert "socket_present" in result.metrics
    assert "unit_active_state" not in result.facts
    assert "socket_present" not in result.facts
    # Enablement IS configuration — deliberate, slow-changing — so it stays.
    assert "unit_enabled" in result.facts


def test_the_posture_rule_reads_the_keys_this_collector_emits(monkeypatch, tmp_path):
    """The contract nothing else checks.

    A rename on either side leaves the rule reading a key that is never
    present, so it silently never fires. Wire the real collector output into
    the real rule rather than asserting on hand-written strings.
    """
    result = _run(monkeypatch, tmp_path)
    section = {"status": "ok", "facts": result.facts, "metrics": dict(result.metrics)}

    # Healthy-but-unarmed: silent.
    assert "falkordb_socket_missing" not in _loop._infra_missing_protections(
        {"sections": {"falkordb": section}}
    )

    # Drift, expressed through the collector's OWN key names.
    section["metrics"]["unit_active_state"] = "active"
    section["metrics"]["socket_present"] = False
    assert "falkordb_socket_missing" in _loop._infra_missing_protections(
        {"sections": {"falkordb": section}}
    )


def test_a_version_dir_without_a_module_is_not_installed(tmp_path):
    """An interrupted download leaves the dir; presence of the FILE is truth."""
    deps = tmp_path / "deps"
    (deps / "4.20.4").mkdir(parents=True)
    assert _installed_versions(deps) == []

    (deps / "4.20.4" / "falkordb.so").write_bytes(b"x")
    assert _installed_versions(deps) == ["4.20.4"]


def test_missing_deps_root_is_empty_not_an_error(tmp_path):
    assert _installed_versions(tmp_path / "nope") == []


def test_collector_is_registered():
    """An unregistered collector produces no section, so the rule never fires."""
    assert collect_falkordb in CONTAINER_COLLECTORS


def test_collector_degrades_its_own_section_never_the_refresh(monkeypatch, tmp_path):
    """An unguarded failure must come back as a failed SECTION, not an exception.

    The service gathers collectors with return_exceptions, but a collector that
    raises still loses its whole section silently; returning SectionResult.failed
    is what makes the failure visible.
    """
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    def _boom(*_a, **_k):
        raise OSError("simulated")

    monkeypatch.setattr(Path, "is_file", _boom)
    result = asyncio.run(collect_falkordb())
    assert result.name == "falkordb"
    assert result.status != "ok"


def test_an_unreadable_deps_dir_reads_as_no_modules(tmp_path):
    """Not the same as a failure: a dir we cannot list holds no module we can use.

    `_installed_versions` swallows OSError deliberately, so the section stays
    ok with an empty list. That is the honest answer and it keeps a permissions
    quirk in ~/.genesis from degrading the whole section.
    """
    deps = tmp_path / "deps"
    deps.mkdir()
    deps.chmod(0o000)
    try:
        assert _installed_versions(deps) == []
    finally:
        deps.chmod(0o755)
