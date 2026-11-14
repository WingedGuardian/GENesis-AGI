"""Typed global census: synthetic manager and physical-absence reader only."""

from collections import Counter
from copy import deepcopy

import pytest

from tests.test_scripts.test_code_intel_scope_manager import UNIT, manager, variant


def row(name=UNIT + ".scope", state="inactive", obj="/org/freedesktop/systemd1/unit/owned"):
    return [
        name,
        "fixture",
        "loaded",
        state,
        "dead" if state == "inactive" else state,
        "",
        obj,
        0,
        "",
        "/",
    ]


class CensusBus:
    def __init__(self, rows):
        self.now, self.calls, self.reads, self.drift = 0.0, [], Counter(), None
        self.rows = deepcopy(rows)
        self.reply = variant("a(ssssssouso)", [deepcopy(rows)])
        self.units = {
            r[6]: dict(
                Id=variant("s", r[0]),
                LoadState=variant("s", "loaded"),
                InvocationID=variant("ay", [1] * 16),
                ActiveState=variant("s", r[3]),
                SubState=variant("s", r[4]),
                Following=variant("s", ""),
                Job=variant("(uo)", [0, "/"]),
            )
            for r in rows
        }
        self.groups = {
            r[6]: dict(ControlGroup=variant("s", "/user.slice/app-capped.slice/" + r[0]))
            for r in rows
        }

    def __call__(self, path, interface, method, *args, timeout):
        assert timeout > 0 and timeout <= 30
        self.calls.append((path, interface, method, args))
        self.now += 0.01
        self.reads[method, args] += 1
        if method == "ListUnitsByPatterns":
            assert args == ("asas", "0", "1", "code-intel-*.scope")
            return deepcopy(self.reply)
        if method == "GetUnit":
            (target,) = [r for r in self.rows if r[0] == args[1]]
            result = target[6]
            if self.drift == "object" and self.reads[method, args] > 1:
                result += "replacement"
            return variant("o", [result])
        assert method == "GetAll"
        unit = args == ("s", "org.freedesktop.systemd1.Unit")
        data = deepcopy(self.units[path] if unit else self.groups[path])
        if self.reads[method, args] > 1:
            if self.drift == "invocation" and unit:
                data["InvocationID"] = variant("ay", [2] * 16)
            elif self.drift == "state" and unit:
                data["ActiveState"] = variant("s", "active")
            elif self.drift == "job" and unit:
                data["Job"] = variant("(uo)", [1, "/org/freedesktop/systemd1/job/1"])
            elif self.drift == "group" and not unit:
                data["ControlGroup"] = variant("s", "")
        return variant("a{sv}", [data])


def gate(bus):
    operation = manager(bus)
    operation.deadline = 60
    method = getattr(operation, "require_no_batch", None)
    assert callable(method), "Q4b requires global physical-scope absence before reservation"
    return operation, method


@pytest.mark.parametrize(
    "count,state,empty_group",
    [
        (0, "inactive", False),
        (1, "inactive", False),
        (1, "failed", False),
        (1, "failed", True),
        (2, "inactive", False),
    ],
)
def test_complete_census_checks_both_providers_and_other_repositories(count, state, empty_group):
    rows = [
        row(state=state),
        row(
            "code-intel-bbbbbbbbbbbb-gitnexus-2.scope",
            state,
            "/org/freedesktop/systemd1/unit/other",
        ),
    ][:count]
    bus = CensusBus(rows)
    if empty_group:
        bus.groups[rows[0][6]]["ControlGroup"] = variant("s", "")
    _, run = gate(bus)
    checked = []
    run(checked.append)
    assert len(checked) == count
    assert {call[2] for call in bus.calls} <= {"ListUnitsByPatterns", "GetUnit", "GetAll"}
    assert bus.calls[0][3] == ("asas", "0", "1", "code-intel-*.scope")


@pytest.mark.parametrize(
    "state", ["active", "activating", "deactivating", "reloading", "maintenance", "unknown"]
)
def test_any_active_or_uncertain_batch_refuses_before_physical_reader(state):
    bus = CensusBus([row(state=state)])
    _, run = gate(bus)

    def forbidden(group):
        raise AssertionError("nonterminal manager state must refuse before kernel observation")

    with pytest.raises(ValueError):
        run(forbidden)


@pytest.mark.parametrize("state", ["inactive", "failed"])
@pytest.mark.parametrize("error", [ValueError, FileNotFoundError, PermissionError])
def test_terminal_state_never_hides_population_or_kernel_uncertainty(state, error):
    _, run = gate(CensusBus([row(state=state)]))

    def physical(group):
        raise error("synthetic populated subtree or unreadable events")

    with pytest.raises(error):
        run(physical)


@pytest.mark.parametrize(
    "fault",
    [
        "signature",
        "outer",
        "trailing-argument",
        "row-arity",
        "row-type",
        "bool-job",
        "job",
        "job-type",
        "job-path",
        "alias",
        "unloaded",
        "unknown-name",
        "object-path",
        "duplicate-name",
        "duplicate-object",
        "late-malformed",
    ],
)
def test_malformed_complete_census_never_becomes_empty_or_a_valid_prefix(fault):
    bus = CensusBus([row()])
    _, run = gate(bus)
    rows = bus.reply["data"][0]
    if fault == "signature":
        bus.reply["type"] = "as"
    elif fault == "outer":
        bus.reply["data"] = {}
    elif fault == "trailing-argument":
        bus.reply["data"].append([])
    elif fault == "row-arity":
        rows[0].append("extra")
    elif fault == "row-type":
        rows[0][1] = None
    elif fault == "bool-job":
        rows[0][7] = False
    elif fault == "job":
        rows[0][7] = 1
    elif fault == "job-type":
        rows[0][8] = "start"
    elif fault == "job-path":
        rows[0][9] = "/org/freedesktop/systemd1/job/1"
    elif fault == "alias":
        rows[0][5] = "other.scope"
    elif fault == "unloaded":
        rows[0][2] = "error"
    elif fault == "unknown-name":
        rows[0][0] = "code-intel-unfamiliar.scope"
    elif fault == "object-path":
        rows[0][6] = "relative/path"
    elif fault == "duplicate-name":
        rows.append(deepcopy(rows[0]))
    elif fault == "duplicate-object":
        rows.append(row("code-intel-bbbbbbbbbbbb-gitnexus-2.scope"))
    else:
        rows.append(["incomplete"])
    checked = []
    with pytest.raises(ValueError):
        run(checked.append)
    assert not checked


@pytest.mark.parametrize("drift", ["object", "invocation", "state", "job", "group"])
def test_original_terminal_identity_and_group_bracket_physical_absence(drift):
    bus = CensusBus([row(state="failed")])
    _, run = gate(bus)
    bus.drift = drift
    with pytest.raises(ValueError):
        run(lambda group: None)


@pytest.mark.parametrize("value", [False, [False, "/"], [0], None])
def test_job_property_requires_the_exact_typed_zero_job_pair(value):
    bus = CensusBus([row()])
    bus.units[bus.rows[0][6]]["Job"] = variant("(uo)", value)
    _, run = gate(bus)
    with pytest.raises(ValueError):
        run(lambda group: None)


def test_expired_census_cannot_return_an_empty_success():
    bus = CensusBus([])
    operation, run = gate(bus)
    operation.deadline = bus.now
    with pytest.raises(ValueError):
        run(lambda group: None)
    assert not bus.calls


@pytest.mark.parametrize(
    "fault",
    [
        "unit-id",
        "unit-alias",
        "unit-active",
        "unit-substate",
        "unit-load",
        "missing-active",
        "job-signature",
        "group-signature",
        "missing-group",
        "relative-group",
        "traversal-group",
        "foreign-group",
    ],
)
def test_terminal_header_and_scope_group_boundaries_refuse(fault):
    bus = CensusBus([row()])
    unit, group = bus.units[bus.rows[0][6]], bus.groups[bus.rows[0][6]]
    _, run = gate(bus)
    if fault == "unit-id":
        unit["Id"] = variant("s", "different.scope")
    elif fault == "unit-alias":
        unit["Following"] = variant("s", "different.scope")
    elif fault == "unit-active":
        unit["ActiveState"] = variant("s", "active")
    elif fault == "unit-substate":
        unit["SubState"] = variant("s", "abandoned")
    elif fault == "unit-load":
        unit["LoadState"] = variant("s", "not-found")
    elif fault == "missing-active":
        unit.pop("ActiveState")
    elif fault == "job-signature":
        unit["Job"] = variant("s", "0")
    elif fault == "group-signature":
        group["ControlGroup"] = variant("as", [])
    elif fault == "missing-group":
        group.pop("ControlGroup")
    elif fault == "relative-group":
        group["ControlGroup"] = variant("s", "slice/" + bus.rows[0][0])
    elif fault == "traversal-group":
        group["ControlGroup"] = variant("s", "/slice/../" + bus.rows[0][0])
    else:
        group["ControlGroup"] = variant("s", "/slice/different.scope")
    checked = []
    with pytest.raises(ValueError):
        run(checked.append)
    assert not checked
