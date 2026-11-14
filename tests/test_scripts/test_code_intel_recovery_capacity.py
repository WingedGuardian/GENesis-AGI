"""Prospective capacity observes the existing destination, including its limit."""

import runpy
from pathlib import Path

import pytest

from tests.test_scripts.test_code_intel_cbm_admission import GIB, _tree, admission


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
