"""Queued route/provider inputs retain the old standalone admission defaults."""

import pytest

from tests.test_scripts.test_code_intel_cbm_admission import GIB, UNIT, _tree, admission
from tests.test_scripts.test_code_intel_execution import CAP
from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_execution import scope as scope


def _move(scope, provider, parent):
    probe, leaf, proc = scope
    unit = probe.unit.replace("-cbm-", "-" + provider + "-")
    target = leaf.parents[1] / parent / (unit + ".scope")
    target.parent.mkdir(exist_ok=True)
    leaf.rename(target)
    membership = f"0::/{parent}/{unit}.scope\n"
    (proc / "self/cgroup").write_text(membership)
    (proc / "self/task" / str(admission.os.getpid()) / "cgroup").write_text(membership)
    return unit, proc


@pytest.mark.parametrize("provider,route,parent", [
    ("cbm", "workload", "genesis-workload.slice"),
    ("gitnexus", "workload", "genesis-workload.slice"),
    ("gitnexus", "capped", "app-capped.slice"),
    ("cbm", "inherit", "session-test.slice"),
])
def test_explicit_queued_profile_reads_actual_parent_and_keeps_scope_checks(scope, evidence, provider, route, parent):
    unit, proc = _move(scope, provider, parent)
    probe = evidence["ScopeProbe"](unit, CAP, proc=proc, route=(route, parent))
    facts = probe.capture()
    assert facts["leaf"]["path"].endswith(parent + "/" + unit + ".scope")
    assert facts["memory_max"] == CAP and facts["swap_max"] == 0
    assert probe.finality(facts)["processes"] == [admission.os.getpid()]


@pytest.mark.parametrize("provider,route,parent", [
    ("cbm", "capped", "app-capped.slice"),
    ("gitnexus", "inherit", "session-test.slice"),
    ("gitnexus", "capped", "app.slice"),
    ("cbm", "workload", "app.slice"),
    ("cbm", "other", "app.slice"),
    ("cbm", "inherit", "../app.slice"),
])
def test_queued_profile_cannot_authorize_a_different_route(scope, evidence, provider, route, parent):
    unit, proc = _move(scope, provider, "app.slice")
    with pytest.raises(ValueError, match="invalid queued scope route or cap|queued route does not match provider"):
        evidence["ScopeProbe"](unit, CAP, proc=proc, route=(route, parent))


def test_valid_queued_route_still_requires_its_actual_parent(scope, evidence):
    unit, proc = _move(scope, "cbm", "app.slice")
    probe = evidence["ScopeProbe"](unit, CAP, proc=proc, route=("workload", "genesis-workload.slice"))
    with pytest.raises(ValueError, match="outside its explicitly reserved slice and scope"):
        probe.capture()


def test_omitted_queue_route_still_requires_workload_slice(scope, evidence):
    unit, proc = _move(scope, "cbm", "app.slice")
    with pytest.raises(ValueError, match="outside"):
        evidence["ScopeProbe"](unit, CAP, proc=proc).capture()


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("current,allowed", [(GIB, True), (9 * GIB, False)])
def test_explicit_provider_uses_same_real_ancestor_budget(tmp_path, provider, current, allowed):
    leaf, member, mounts, host = _tree(tmp_path, parent_current=current)
    unit = UNIT.replace("-cbm-", "-" + provider + "-")
    leaf.rename(leaf.with_name(unit + ".scope"))
    member.write_text(member.read_text().replace(UNIT, unit))
    kwargs = dict(self_path=member, mountinfo_path=mounts, meminfo_path=host, provider=provider)
    if allowed:
        admission.assess(4 * GIB, 2 * GIB, unit, **kwargs)
    else:
        with pytest.raises(admission.AdmissionRefused, match="insufficient headroom"):
            admission.assess(4 * GIB, 2 * GIB, unit, **kwargs)


def test_omitted_provider_still_rejects_gitnexus_scope(tmp_path):
    leaf, member, mounts, host = _tree(tmp_path)
    unit = UNIT.replace("-cbm-", "-gitnexus-")
    leaf.rename(leaf.with_name(unit + ".scope"))
    member.write_text(member.read_text().replace(UNIT, unit))
    with pytest.raises(admission.AdmissionRefused, match="invalid expected"):
        admission.assess(4 * GIB, 2 * GIB, unit, self_path=member, mountinfo_path=mounts, meminfo_path=host)


@pytest.mark.parametrize("provider", ["both", "", None, True, "cbm|gitnexus"])
def test_unknown_provider_never_widens_admission(tmp_path, provider):
    leaf, member, mounts, host = _tree(tmp_path)
    with pytest.raises(admission.AdmissionRefused, match="provider"):
        admission.assess(4 * GIB, 2 * GIB, UNIT, provider=provider,
                         self_path=member, mountinfo_path=mounts, meminfo_path=host)
