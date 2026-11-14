"""Prospective capacity observes the existing destination, including its limit."""

import runpy
from copy import deepcopy
from pathlib import Path

import pytest

from tests.test_scripts.test_code_intel_cbm_admission import GIB, _tree, admission
from tests.test_scripts.test_code_intel_scope_manager import manager, variant


def headroom(tmp_path):
    method = getattr(admission, "assess_headroom", None)
    assert callable(method), "recovery must share the actual scoped headroom formula"
    return method(
        tmp_path / "cg/user.slice",
        tmp_path / "cg",
        2,
        8 * GIB,
        2 * GIB,
        cache_reserve=2 * GIB,
        meminfo_path=tmp_path / "meminfo",
    )


def test_destination_itself_is_the_first_limit(tmp_path):
    _tree(tmp_path, parent_limit=12 * GIB)
    headroom(tmp_path)
    (tmp_path / "cg/user.slice/memory.max").write_text(str(10 * GIB))
    with pytest.raises(admission.AdmissionRefused, match="insufficient headroom"):
        headroom(tmp_path)


@pytest.mark.parametrize("fault", ["ancestor", "host", "missing-charge", "dirty", "bad-stat"])
def test_prospective_observation_preserves_every_shared_capacity_gate(tmp_path, fault):
    leaf, *_ = _tree(tmp_path, parent_limit=12 * GIB)
    parent = leaf.parent
    if fault == "ancestor":
        (tmp_path / "cg/memory.max").write_text(str(9 * GIB))
        (tmp_path / "cg/memory.current").write_text("0")
    elif fault == "host":
        (tmp_path / "meminfo").write_text("MemTotal: 67108864 kB\nMemAvailable: 1 kB\n")
    elif fault == "missing-charge":
        (parent / "memory.current").unlink()
    else:
        (parent / "memory.current").write_text(str(6 * GIB))
        (parent / "memory.stat").write_text(
            f"inactive_file {4 * GIB}\nactive_file 0\n"
            f"file_dirty {2 * GIB if fault == 'dirty' else 'bad'}\nfile_writeback 0\n"
        )
    with pytest.raises(admission.AdmissionRefused):
        headroom(tmp_path)


def test_prospective_cache_accounting_uses_same_retained_clean_reserve(tmp_path):
    leaf, *_ = _tree(tmp_path, parent_limit=12 * GIB, parent_current=4 * GIB)
    (leaf.parent / "memory.stat").write_text(
        f"inactive_file {4 * GIB}\nactive_file 0\nfile_dirty 0\nfile_writeback 0\n"
    )
    headroom(tmp_path)


def test_headroom_cannot_silently_skip_an_unmapped_destination(tmp_path):
    _tree(tmp_path)
    method = getattr(admission, "assess_headroom", None)
    assert callable(method), "shared destination headroom reader missing"
    with pytest.raises(admission.AdmissionRefused):
        method(
            tmp_path / "outside",
            tmp_path / "cg",
            2,
            8 * GIB,
            2 * GIB,
            meminfo_path=tmp_path / "meminfo",
        )


class SliceBus:
    def __init__(self):
        self.now, self.calls = 0.0, []
        self.name = "genesis-workload.slice"
        self.object = "/org/freedesktop/systemd1/unit/workload"
        self.unit = dict(
            Id=variant("s", self.name),
            LoadState=variant("s", "loaded"),
            InvocationID=variant("ay", [1] * 16),
            Following=variant("s", ""),
            ActiveState=variant("s", "active"),
            SubState=variant("s", "active"),
            Job=variant("(uo)", [0, "/"]),
        )
        self.group = "/user.slice/" + self.name

    def __call__(self, path, interface, method, *args, timeout):
        assert 0 < timeout <= 30
        self.calls.append((method, args))
        self.now += 0.01
        if method == "GetUnit":
            assert args == ("s", self.name)
            return variant("o", [self.object])
        # A replaced lookup may leave its former object readable. The production
        # observer must compare GetUnit again rather than rely on an RPC error.
        assert method == "GetAll" and path == "/org/freedesktop/systemd1/unit/workload"
        data = (
            self.unit
            if args == ("s", "org.freedesktop.systemd1.Unit")
            else dict(ControlGroup=variant("s", self.group))
        )
        assert args[1] in ("org.freedesktop.systemd1.Unit", "org.freedesktop.systemd1.Slice")
        return variant("a{sv}", [deepcopy(data)])


def slice_gate(bus):
    operation = manager(bus)
    method = getattr(operation, "require_slice_capacity", None)
    assert callable(method), "prospective recovery must observe the existing destination slice"
    return method


def test_existing_slice_is_observed_stably_around_capacity_callback():
    bus = SliceBus()
    run = slice_gate(bus)
    observed = []
    run(bus.name, observed.append)
    assert observed == [bus.group]
    assert [call[0] for call in bus.calls] == [
        "GetUnit",
        "GetAll",
        "GetAll",
        "GetAll",
        "GetAll",
        "GetUnit",
    ]


@pytest.mark.parametrize("fault", ["group", "identity", "state", "job", "object"])
def test_slice_drift_during_capacity_observation_refuses(fault):
    bus = SliceBus()
    run = slice_gate(bus)

    def observe(group):
        if fault == "group":
            bus.group = "/other.slice/" + bus.name
        elif fault == "identity":
            bus.unit["InvocationID"] = variant("ay", [2] * 16)
        elif fault == "state":
            bus.unit["ActiveState"] = variant("s", "deactivating")
        elif fault == "job":
            bus.unit["Job"] = variant("(uo)", [1, "/job/1"])
        else:
            bus.object += "replacement"

    with pytest.raises(ValueError):
        run(bus.name, observe)


@pytest.mark.parametrize(
    "fault",
    [
        "unloaded",
        "inactive",
        "alias",
        "job-bool",
        "group-empty",
        "group-relative",
        "group-traversal",
        "group-other",
        "group-nul",
    ],
)
def test_uncertain_slice_refuses_before_capacity_reader(fault):
    bus = SliceBus()
    run = slice_gate(bus)
    if fault == "unloaded":
        bus.unit["LoadState"] = variant("s", "not-found")
    elif fault == "inactive":
        bus.unit["ActiveState"] = variant("s", "inactive")
    elif fault == "alias":
        bus.unit["Following"] = variant("s", "other.slice")
    elif fault == "job-bool":
        bus.unit["Job"] = variant("(uo)", [False, "/"])
    else:
        bus.group = {
            "group-empty": "",
            "group-relative": bus.name,
            "group-traversal": "/x/../" + bus.name,
            "group-other": "/other.slice",
            "group-nul": "/x\0/" + bus.name,
        }[fault]

    def forbidden(group):
        raise AssertionError("uncertain destination must not be assessed")

    with pytest.raises(ValueError):
        run(bus.name, forbidden)


def test_capacity_refusal_is_never_swallowed_or_followed_by_manager_action():
    bus = SliceBus()
    run = slice_gate(bus)

    def refuse(group):
        raise admission.AdmissionRefused("synthetic destination pressure")

    with pytest.raises(admission.AdmissionRefused, match="synthetic"):
        run(bus.name, refuse)
    assert {call[0] for call in bus.calls} <= {"GetUnit", "GetAll"}


@pytest.mark.parametrize(
    "mounted,control",
    [
        ("/", "/user.slice/target.slice"),
        ("/container", "/container/user.slice/target.slice"),
        ("/container", "/user.slice/target.slice"),
    ],
)
def test_destination_mapping_reuses_the_existing_cgroup_namespace(
    tmp_path, monkeypatch, mounted, control
):
    root = tmp_path / "cg"
    group = root / "user.slice/target.slice"
    group.mkdir(parents=True)
    mount = tmp_path / "mountinfo"
    mount.write_text(f"1 0 0:1 {mounted} {root} rw - cgroup2 cgroup rw\n")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/lib"))
    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "scripts/lib/codebase_managed_unit.py")
    )
    method = namespace.get("resolve_control_group")
    assert callable(method), "capacity must share the existing physical cgroup mapper"
    monkeypatch.setitem(
        method.__globals__,
        "Path",
        lambda value: mount if value == "/proc/self/mountinfo" else Path(value),
    )
    monkeypatch.setitem(method.__globals__, "resolve_cgroup", lambda *args: (root, root, 2))
    assert method(control) == (group, root, 2)
