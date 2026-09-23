"""Tests for the FalkorDB provisioning collector.

The load-bearing property is the CONTRACT with the posture rule: the rule in
awareness/loop.py reads exactly two keys out of this collector's ``metrics``,
and a rename on either side would disarm the only alert this feature has —
silently, because a rule that reads a missing key simply never fires.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from genesis.awareness import loop as _loop
from genesis.infra_profile.collectors import CONTAINER_COLLECTORS, falkordb_facts
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

    An earlier version of this docstring claimed a raising collector "loses its
    whole section silently". That is FALSE: service.py:183-193 catches the
    exception, derives the name from `collector.__name__`, logs it, and appends
    SectionResult.failed. Returning the failed section here is still right --
    it carries a specific reason instead of a repr, and it keeps the collector
    honest about its own contract -- but it is a clarity win, not the last line
    of defence the old wording implied.
    """
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    def _boom(*_a, **_k):
        raise OSError("simulated")

    monkeypatch.setattr(Path, "is_file", _boom)
    result = asyncio.run(collect_falkordb())
    assert result.name == "falkordb"
    assert result.status != "ok"


class _StubProc:
    """asyncio subprocess stand-in that records whether it was reaped."""

    def __init__(self, *, communicate_error=None, stdout=b"", returncode=0):
        self._communicate_error = communicate_error
        self._stdout = stdout
        self.returncode = None
        self._final_rc = returncode
        self.killed = False
        self.waited = False

    async def communicate(self):
        if self._communicate_error is not None:
            raise self._communicate_error
        self.returncode = self._final_rc
        return (self._stdout, b"")

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waited = True
        self.returncode = -9
        return self.returncode


def _with_unit(
    monkeypatch,
    home: Path,
    proc=None,
    systemctl="/usr/bin/systemctl",
    bus=True,
):
    """Render the unit file and control what `systemctl show` does.

    Every outside edge is pinned deliberately. The spawn is stubbed even when
    a test expects no spawn at all -- so that a regression in the `which`
    guard fails as a SANDBOX VIOLATION rather than quietly running a real
    `systemctl --user` against the developer's own session and reporting
    whatever that box happens to be doing. `XDG_RUNTIME_DIR` is set for the
    same reason: the bus probe must never read the real one.
    """
    unit = home / ".config" / "systemd" / "user" / "genesis-falkordb.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Unit]\n")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(falkordb_facts.shutil, "which", lambda _n: systemctl)

    runtime_dir = home / "run"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    if bus:
        (runtime_dir / "bus").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))

    async def _spawn(*_a, **_k):
        if proc is None:
            raise AssertionError(
                "test spawned a REAL systemctl -- the sandbox leaked"
            )
        return proc

    monkeypatch.setattr(falkordb_facts.asyncio, "create_subprocess_exec", _spawn)
    return unit


def test_a_systemctl_failure_degrades_the_section_rather_than_the_fact(monkeypatch, tmp_path):
    """An unreadable probe must fail the SECTION, never answer `unit_enabled: None`.

    `unit_enabled` is a HASHED fact. `_merge_section` keeps the prior facts and
    hash for a non-ok section -- its comment calls that "no phantom drift" --
    so propagating the failure costs nothing. Swallowing it into a None would
    instead flip a hashed fact, billing a drift observation plus an LLM
    annotation regeneration on the way out and again on the way back, for a
    transient hiccup that changed no configuration at all.
    """
    proc = _StubProc(communicate_error=OSError("bus unreachable"))
    _with_unit(monkeypatch, tmp_path, proc)

    result = asyncio.run(collect_falkordb())

    assert result.status != "ok", "an unverifiable probe reported itself as ok"
    assert result.name == "falkordb"


def test_a_nonzero_systemctl_exit_is_a_failed_probe(monkeypatch, tmp_path):
    """`systemctl show` exits 0 even for a unit that does not exist (measured),
    so a nonzero exit cannot mean "absent" -- it means we could not ask."""
    proc = _StubProc(stdout=b"", returncode=1)
    _with_unit(monkeypatch, tmp_path, proc)

    result = asyncio.run(collect_falkordb())

    assert result.status != "ok"


def test_no_systemctl_at_all_is_a_fact_not_a_failure(monkeypatch, tmp_path):
    """A box without systemd has no unit state, and that is an answer.

    The distinction the fix turns on: absent is knowable, unreadable is not.
    Collapsing them would leave every non-systemd install permanently errored.
    """
    _with_unit(monkeypatch, tmp_path, proc=None, systemctl=None)

    result = asyncio.run(collect_falkordb())

    assert result.status == "ok"
    assert result.facts["unit_enabled"] is None
    assert result.metrics["unit_active_state"] is None


def test_a_failed_probe_reaps_its_subprocess(monkeypatch, tmp_path):
    """A child we stop reading from is still a child this process must reap.

    Returning without killing and waiting leaves a zombie behind on every
    refresh that hits the failure -- unbounded, on a path that runs hourly.
    """
    proc = _StubProc(communicate_error=OSError("boom"))
    _with_unit(monkeypatch, tmp_path, proc)

    asyncio.run(collect_falkordb())

    assert proc.killed, "subprocess was never killed"
    assert proc.waited, "subprocess was never reaped"


def test_a_probe_timeout_reaps_and_degrades(monkeypatch, tmp_path):
    """The timeout path is the one most likely to fire, so pin it separately.

    Asserting only `status != "ok"` made this a duplicate of the OSError test:
    deleting the whole `except TimeoutError` clause left it green, because the
    generic handler re-raises and degrades the section just the same
    (mutation-verified). The timeout-specific MESSAGE is what distinguishes
    the branch, so that is what this asserts.
    """
    proc = _StubProc(communicate_error=TimeoutError())
    _with_unit(monkeypatch, tmp_path, proc)

    result = asyncio.run(collect_falkordb())

    assert result.status != "ok"
    assert "timed out" in (result.error or ""), result.error
    assert proc.killed and proc.waited


def test_a_readable_probe_still_reports_its_states(monkeypatch, tmp_path):
    """The equivalence lock: propagating failures must not blind the happy path."""
    proc = _StubProc(stdout=b"active\nenabled\n", returncode=0)
    _with_unit(monkeypatch, tmp_path, proc)

    result = asyncio.run(collect_falkordb())

    assert result.status == "ok"
    assert result.facts["unit_enabled"] == "enabled"
    assert result.metrics["unit_active_state"] == "active"


def test_a_cancelled_refresh_still_reaps_its_subprocess(monkeypatch, tmp_path):
    """CancelledError is not an Exception, so `except Exception` would miss it.

    Mutation-verified: narrowing the handler to `except Exception` left every
    other test in this file green while reintroducing the leak. This is the
    only thing holding that choice.
    """
    proc = _StubProc(communicate_error=asyncio.CancelledError())
    _with_unit(monkeypatch, tmp_path, proc)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(collect_falkordb())

    assert proc.killed, "a cancelled refresh left its child running"
    assert proc.waited


def test_a_box_with_no_user_bus_keeps_the_facts_it_could_read(monkeypatch, tmp_path):
    """No user manager is a standing property, not a failure to report.

    Erroring here would be permanent on that class of box, and `_merge_section`
    has no prior facts to fall back on, so four facts read straight off the
    filesystem would be discarded because one sub-probe was unanswerable.
    """
    proc = _StubProc(stdout=b"", returncode=1)
    _with_unit(monkeypatch, tmp_path, proc, bus=False)

    result = asyncio.run(collect_falkordb())

    assert result.status == "ok"
    assert result.facts["unit_present"] is True
    assert result.facts["unit_enabled"] is None
    assert "socket_path" in result.facts


def test_a_live_bus_makes_a_nonzero_exit_a_real_failure(monkeypatch, tmp_path):
    """The other side of that split: with a bus reachable, we could ask and
    did not get an answer, so the section is genuinely unverifiable."""
    proc = _StubProc(stdout=b"", returncode=1)
    _with_unit(monkeypatch, tmp_path, proc, bus=True)

    result = asyncio.run(collect_falkordb())

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
