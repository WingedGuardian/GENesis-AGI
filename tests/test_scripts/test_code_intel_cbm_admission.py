"""Destination-scope admission tests for Codebase batch indexing."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_HELPER = Path(__file__).resolve().parents[2] / "scripts/lib/code_intel_cbm_admission.py"
_SPEC = importlib.util.spec_from_file_location("code_intel_cbm_admission", _HELPER)
assert _SPEC and _SPEC.loader
admission = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(admission)

GIB = 1024**3
UNIT = "code-intel-123456abcdef-cbm-4242"


def _tree(tmp_path: Path, *, parent_limit: int = 12 * GIB,
          parent_current: int = GIB) -> tuple[Path, Path, Path, Path]:
    mount = tmp_path / "cg"
    leaf = mount / "user.slice" / f"{UNIT}.scope"
    leaf.mkdir(parents=True)
    (mount / "memory.max").write_text("max\n")
    parent = leaf.parent
    (parent / "memory.max").write_text(f"{parent_limit}\n")
    (parent / "memory.current").write_text(f"{parent_current}\n")
    (parent / "memory.stat").write_text(
        "inactive_file 0\nactive_file 0\nfile_dirty 0\nfile_writeback 0\n"
    )
    (leaf / "memory.max").write_text(f"{4 * GIB}\n")
    (leaf / "memory.swap.max").write_text("0\n")
    self_cgroup = tmp_path / "self.cgroup"
    self_cgroup.write_text(f"0::/user.slice/{UNIT}.scope\n")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(f"35 24 0:31 / {mount} rw - cgroup2 cgroup rw\n")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 67108864 kB\nMemAvailable: 66060288 kB\n")
    return leaf, self_cgroup, mountinfo, meminfo


def _assess(tmp_path: Path) -> None:
    admission.assess(
        4 * GIB, 2 * GIB, UNIT,
        self_path=tmp_path / "self.cgroup",
        mountinfo_path=tmp_path / "mountinfo",
        meminfo_path=tmp_path / "meminfo",
    )


def test_scope_and_shared_ancestor_with_capacity_admit(tmp_path):
    _tree(tmp_path)
    _assess(tmp_path)


@pytest.mark.parametrize("current", [7 * GIB, 9 * GIB])
def test_parent_pressure_refuses_even_with_room_in_job_scope(tmp_path, current):
    _tree(tmp_path, parent_current=current)
    with pytest.raises(admission.AdmissionRefused, match="insufficient headroom"):
        _assess(tmp_path)


def test_malformed_cache_statistics_cannot_make_full_parent_look_empty(tmp_path):
    leaf, *_ = _tree(tmp_path, parent_current=9 * GIB)
    (leaf.parent / "memory.stat").write_text(
        f"inactive_file 08\nactive_file {8 * GIB}\n"
        "file_dirty 0\nfile_writeback 0\n"
    )
    with pytest.raises(admission.AdmissionRefused, match="insufficient headroom"):
        _assess(tmp_path)


def test_only_clean_file_cache_above_reserve_can_create_headroom(tmp_path):
    leaf, *_ = _tree(tmp_path, parent_limit=10 * GIB, parent_current=6 * GIB)
    stat = leaf.parent / "memory.stat"
    stat.write_text(f"inactive_file {4 * GIB}\nactive_file 0\n"
                    "file_dirty 0\nfile_writeback 0\n")
    _assess(tmp_path)
    stat.write_text(f"inactive_file {4 * GIB}\nactive_file 0\n"
                    f"file_dirty {2 * GIB}\nfile_writeback 0\n")
    with pytest.raises(admission.AdmissionRefused, match="insufficient headroom"):
        _assess(tmp_path)


@pytest.mark.parametrize("property_file,value", [
    ("memory.max", "2147483648\n"),
    ("memory.swap.max", "1\n"),
])
def test_wrong_scope_limit_or_swap_refuses(tmp_path, property_file, value):
    leaf, *_ = _tree(tmp_path)
    (leaf / property_file).write_text(value)
    with pytest.raises(admission.AdmissionRefused):
        _assess(tmp_path)


def test_wrong_scope_identity_refuses(tmp_path):
    _tree(tmp_path)
    (tmp_path / "self.cgroup").write_text("0::/user.slice/other.scope\n")
    (tmp_path / "cg/user.slice/other.scope").mkdir()
    with pytest.raises(admission.AdmissionRefused, match="outside its expected scope"):
        _assess(tmp_path)


def test_page_rounded_scope_limit_is_admitted_and_budgeted(tmp_path):
    page = os.sysconf("SC_PAGE_SIZE")
    cap = 4 * GIB + 1
    effective = 4 * GIB + page
    leaf, *_ = _tree(tmp_path, parent_limit=effective + 3 * GIB - 1,
                     parent_current=GIB - 1)
    (leaf / "memory.max").write_text(f"{effective}\n")
    admission.assess(
        cap, 2 * GIB, UNIT,
        self_path=tmp_path / "self.cgroup",
        mountinfo_path=tmp_path / "mountinfo",
        meminfo_path=tmp_path / "meminfo",
    )
    (leaf.parent / "memory.current").write_text(f"{GIB}\n")
    with pytest.raises(admission.AdmissionRefused, match="insufficient headroom"):
        admission.assess(
            cap, 2 * GIB, UNIT,
            self_path=tmp_path / "self.cgroup",
            mountinfo_path=tmp_path / "mountinfo",
            meminfo_path=tmp_path / "meminfo",
        )
    (leaf / "memory.max").write_text(f"{effective + page}\n")
    with pytest.raises(admission.AdmissionRefused, match="differs"):
        admission.assess(
            cap, 2 * GIB, UNIT,
            self_path=tmp_path / "self.cgroup",
            mountinfo_path=tmp_path / "mountinfo",
            meminfo_path=tmp_path / "meminfo",
        )
    (leaf / "memory.max").write_text(f"{4 * GIB}\n")
    (leaf.parent / "memory.current").write_text(f"{GIB}\n")
    admission.assess(
        cap, 2 * GIB, UNIT,
        self_path=tmp_path / "self.cgroup",
        mountinfo_path=tmp_path / "mountinfo",
        meminfo_path=tmp_path / "meminfo",
    )


def test_broad_mount_exposes_tighter_ancestor(tmp_path):
    leaf, _, mountinfo, _ = _tree(tmp_path, parent_limit=7 * GIB,
                                  parent_current=2 * GIB)
    narrow = tmp_path / "narrow"
    narrow.symlink_to(leaf, target_is_directory=True)
    with mountinfo.open("a") as out:
        out.write(f"36 24 0:31 /user.slice/{UNIT}.scope {narrow} "
                  "rw - cgroup2 cgroup rw\n")
    with pytest.raises(admission.AdmissionRefused, match="insufficient headroom"):
        _assess(tmp_path)


def test_duplicate_alias_of_same_cgroup_is_not_ambiguous(tmp_path):
    _, _, mountinfo, _ = _tree(tmp_path)
    alias = tmp_path / "cg-alias"
    alias.symlink_to(tmp_path / "cg", target_is_directory=True)
    with mountinfo.open("a") as out:
        out.write(f"36 24 0:31 / {alias} rw - cgroup2 cgroup rw\n")
    _assess(tmp_path)


def test_missing_scope_limit_refuses(tmp_path):
    leaf, *_ = _tree(tmp_path)
    (leaf / "memory.max").unlink()
    with pytest.raises(admission.AdmissionRefused, match="cannot read"):
        _assess(tmp_path)


def test_unmounted_membership_refuses(tmp_path):
    _, _, mountinfo, _ = _tree(tmp_path)
    mountinfo.write_text("35 24 0:31 / /missing rw - cgroup2 cgroup rw\n")
    with pytest.raises(admission.AdmissionRefused, match="cannot map"):
        _assess(tmp_path)


def test_namespace_relative_membership_maps_to_subtree_mount(tmp_path):
    _, self_cgroup, mountinfo, _ = _tree(tmp_path)
    self_cgroup.write_text(f"0::/user.slice/{UNIT}.scope\n")
    mountinfo.write_text(
        f"35 24 0:31 /container {tmp_path / 'cg'} rw - cgroup2 cgroup rw\n"
    )
    _assess(tmp_path)


def test_missing_parent_charge_refuses(tmp_path):
    leaf, *_ = _tree(tmp_path)
    (leaf.parent / "memory.current").unlink()
    with pytest.raises(admission.AdmissionRefused, match="cannot read"):
        _assess(tmp_path)


def test_host_headroom_is_separate_bound(tmp_path):
    _tree(tmp_path)
    (tmp_path / "meminfo").write_text(
        "MemTotal: 67108864 kB\nMemAvailable: 5242880 kB\n"
    )
    with pytest.raises(admission.AdmissionRefused, match="host available"):
        _assess(tmp_path)


@pytest.mark.parametrize("available_kb", [
    "999999999999999", "9007199254740992", "9223372036854775807",
    "18446744073709551616", "1" + "0" * 39,
])
def test_unrepresentable_host_available_memory_refuses(tmp_path, available_kb):
    _tree(tmp_path)
    (tmp_path / "meminfo").write_text(
        f"MemTotal: {available_kb} kB\nMemAvailable: {available_kb} kB\n"
    )
    with pytest.raises(admission.AdmissionRefused):
        _assess(tmp_path)


@pytest.mark.parametrize("value", ["08", "02147483648", "9" * 19, "-1"])
def test_noncanonical_or_oversized_capacity_input_refuses(value):
    with pytest.raises(admission.AdmissionRefused):
        admission.number(value, "test input")


def test_v1_memory_and_memsw_scope_limits_are_checked(tmp_path):
    leaf, self_cgroup, mountinfo, _ = _tree(tmp_path)
    (leaf / "memory.limit_in_bytes").write_text(f"{4 * GIB}\n")
    (leaf / "memory.memsw.limit_in_bytes").write_text(f"{4 * GIB}\n")
    (leaf.parent / "memory.limit_in_bytes").write_text(f"{12 * GIB}\n")
    (tmp_path / "cg/memory.limit_in_bytes").write_text("9223372036854771712\n")
    (leaf.parent / "memory.usage_in_bytes").write_text(f"{GIB}\n")
    (leaf.parent / "memory.stat").write_text(
        "total_inactive_file 0\ntotal_active_file 0\n"
        "total_dirty 0\ntotal_writeback 0\n"
    )
    self_cgroup.write_text(f"7:cpu,memory:/user.slice/{UNIT}.scope\n")
    mountinfo.write_text(f"35 24 0:31 / {tmp_path / 'cg'} "
                         "rw - cgroup cgroup rw,cpu,memory\n")
    _assess(tmp_path)
    (leaf / "memory.memsw.limit_in_bytes").write_text(f"{5 * GIB}\n")
    with pytest.raises(admission.AdmissionRefused, match="permits swap"):
        _assess(tmp_path)
