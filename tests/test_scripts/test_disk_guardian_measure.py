"""scripts/lib/disk_guardian.sh — measurement and tiering.

The watchgod tiers on the SMALLEST of several "room left" figures, and every
one of them is optional. These tests drive the real shell functions against a
fixture mountinfo + a fixture /sys/fs/btrfs tree, so they run on any CI box
(no btrfs needed). statvfs itself comes from the real tmp_path filesystem; the
fixture mountinfo claims that filesystem is btrfs so the quota and allocation
branches are reached.

The measured case these pin: on a live install df reported 208 GB free while
the container's btrfs quota left 140 GB — a df-only guardian trips ~68 GB late.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "disk_guardian.sh"
UUID = "0f0f0f0f-1111-2222-3333-444444444444"
MIB = 1024 * 1024


def _mount_point(p: Path) -> str:
    return subprocess.run(
        ["stat", "-c", "%m", str(p)], capture_output=True, text=True, check=True
    ).stdout.strip()


def _statvfs_mb(p: Path) -> tuple[int, int]:
    st = os.statvfs(p)
    return st.f_bavail * st.f_frsize // MIB, st.f_blocks * st.f_frsize // MIB


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n")


def _fixture(
    tmp_path: Path,
    *,
    fstype="btrfs",
    subvolid=256,
    source=None,
    quota=None,
    excl_quota=None,
    devices=None,
    alloc=None,
    meta=None,
):
    """Build a mountinfo claiming tmp_path's mount is `fstype`, plus a sysfs tree."""
    target = tmp_path / "watched"
    target.mkdir()
    mnt = _mount_point(target)
    src = source or f"/dev/disk/by-uuid/{UUID}"
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"20 1 0:40 / {mnt.replace(' ', chr(92) + '040')} rw,relatime shared:1 - "
        f"{fstype} {src} rw,space_cache=v2,subvolid={subvolid},subvol=/x\n"
    )
    sysfs = tmp_path / "sysfs"
    fs = sysfs / UUID
    (fs / "allocation").mkdir(parents=True)
    for dev, sectors in (devices or {"sdz1": 400 * 1024 * 2048}).items():
        _write(fs / "devices" / dev / "size", sectors)
    a = alloc or {"data": 100 * 1024 * MIB, "metadata": 6 * 1024 * MIB, "system": 8 * MIB}
    for k, v in a.items():
        _write(fs / "allocation" / k / "disk_total", v)
    mu, mt = meta or (2 * 1024 * MIB, 3 * 1024 * MIB)
    _write(fs / "allocation" / "metadata" / "bytes_used", mu)
    _write(fs / "allocation" / "metadata" / "total_bytes", mt)
    q = fs / "qgroups" / f"0_{subvolid}"
    flags = 0
    if quota is not None:
        flags |= 1
        _write(q / "max_referenced", quota[0])
        _write(q / "referenced", quota[1])
    if excl_quota is not None:
        flags |= 2
        _write(q / "max_exclusive", excl_quota[0])
        _write(q / "exclusive", excl_quota[1])
    _write(q / "limit_flags", flags)
    return target, mountinfo, sysfs


def _run(snippet: str, mountinfo: Path, sysfs: Path, tmp_path: Path) -> str:
    env = dict(
        os.environ,
        DG_MOUNTINFO=str(mountinfo),
        DG_SYSFS_BTRFS=str(sysfs),
        DG_STATE_DIR=str(tmp_path / "state"),
        HOME=str(tmp_path),
    )
    proc = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\nsource '{_LIB}'\n{snippet}"], env=env, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _measure(target, mountinfo, sysfs, tmp_path):
    out = _run(f"dg_measure '{target}'", mountinfo, sysfs, tmp_path).split()
    free, total, quota, unalloc, meta, fstype = out[:6]  # 7th: usage for the rate
    return int(free), int(total), int(quota), unalloc, meta, fstype


def test_quota_tighter_than_statvfs_wins(tmp_path):
    # 1 GiB quota with 100 MiB referenced: whatever the real disk has free,
    # the effective free space is 924 MiB of 1024.
    target, mi, sy = _fixture(tmp_path, quota=(1024 * MIB, 100 * MIB))
    sv_free, sv_total = _statvfs_mb(target)
    assert sv_free > 924, "fixture precondition: the real fs must have more room than the quota"
    free, total, quota, *_ = _measure(target, mi, sy, tmp_path)
    assert (free, total, quota) == (924, 1024, 1)


def test_no_quota_reports_statvfs(tmp_path):
    target, mi, sy = _fixture(tmp_path)
    sv_free, sv_total = _statvfs_mb(target)
    free, total, quota, *_ = _measure(target, mi, sy, tmp_path)
    assert quota == 0
    assert total == sv_total
    assert abs(free - sv_free) <= 2  # the real fs may move by a block between reads


def test_quota_larger_than_the_disk_does_not_inflate(tmp_path):
    # A quota bigger than the filesystem must never REPORT more room than statvfs.
    target, mi, sy = _fixture(tmp_path, quota=(10**15, 0))
    sv_free, sv_total = _statvfs_mb(target)
    free, total, *_ = _measure(target, mi, sy, tmp_path)
    assert total == sv_total
    assert free <= sv_free + 2


def test_exceeded_quota_reads_zero_not_negative(tmp_path):
    target, mi, sy = _fixture(tmp_path, quota=(100 * MIB, 150 * MIB))
    free, total, quota, *_ = _measure(target, mi, sy, tmp_path)
    assert (free, total, quota) == (0, 100, 1)


def test_exclusive_limit_is_a_second_wall(tmp_path):
    target, mi, sy = _fixture(
        tmp_path, quota=(2048 * MIB, 100 * MIB), excl_quota=(512 * MIB, 400 * MIB)
    )
    free, total, quota, *_ = _measure(target, mi, sy, tmp_path)
    assert (free, total, quota) == (112, 512, 1)


def test_unenforced_quota_is_ignored(tmp_path):
    # Limit values present but limit_flags 0: btrfs is not enforcing them.
    target, mi, sy = _fixture(tmp_path)
    q = sy / UUID / "qgroups" / "0_256"
    _write(q / "max_referenced", 10 * MIB)
    _write(q / "referenced", 9 * MIB)
    free, _total, quota, *_ = _measure(target, mi, sy, tmp_path)
    assert quota == 0 and free > 10


def test_unallocated_and_metadata(tmp_path):
    # 400 GiB device; 100 GiB data + 6 GiB metadata (raw, e.g. DUP) + 8 MiB system.
    target, mi, sy = _fixture(tmp_path)
    *_, unalloc, meta, fstype = _measure(target, mi, sy, tmp_path)
    assert fstype == "btrfs"
    assert int(unalloc) == 400 * 1024 - 100 * 1024 - 6 * 1024 - 8
    assert int(meta) == 66


def test_multi_device_sums_raw_sizes(tmp_path):
    target, mi, sy = _fixture(
        tmp_path, devices={"sdz1": 100 * 1024 * 2048, "sdy1": 100 * 1024 * 2048}
    )
    *_, unalloc, _meta, _ = _measure(target, mi, sy, tmp_path)
    assert int(unalloc) == 200 * 1024 - 106 * 1024 - 8


def test_device_matched_by_name_when_source_is_not_by_uuid(tmp_path):
    dev = tmp_path / "dev" / "sdz1"
    dev.parent.mkdir()
    dev.write_text("")
    target, mi, sy = _fixture(tmp_path, source=str(dev), quota=(1024 * MIB, 0))
    free, total, quota, *_ = _measure(target, mi, sy, tmp_path)
    assert (total, quota) == (1024, 1)


def test_ambiguous_device_refuses_rather_than_guesses(tmp_path):
    dev = tmp_path / "dev" / "sdz1"
    dev.parent.mkdir()
    dev.write_text("")
    target, mi, sy = _fixture(tmp_path, source=str(dev), quota=(1024 * MIB, 0))
    other = sy / "99999999-aaaa-bbbb-cccc-dddddddddddd"
    _write(other / "devices" / "sdz1" / "size", 1)
    free, total, quota, unalloc, meta, _ = _measure(target, mi, sy, tmp_path)
    assert quota == 0 and unalloc == "-" and meta == "-"


def test_non_btrfs_skips_every_btrfs_input(tmp_path):
    target, mi, sy = _fixture(tmp_path, fstype="ext4", quota=(1024 * MIB, 0))
    free, total, quota, unalloc, meta, fstype = _measure(target, mi, sy, tmp_path)
    assert (quota, unalloc, meta, fstype) == (0, "-", "-", "ext4")


def test_missing_sysfs_degrades_to_statvfs(tmp_path):
    target, mi, sy = _fixture(tmp_path, quota=(1024 * MIB, 0))
    free, total, quota, unalloc, meta, _ = _measure(target, mi, tmp_path / "nope", tmp_path)
    sv_free, sv_total = _statvfs_mb(target)
    assert (quota, unalloc, meta, total) == (0, "-", "-", sv_total)


def test_garbage_sysfs_values_are_skipped_not_zero(tmp_path):
    target, mi, sy = _fixture(tmp_path, quota=(1024 * MIB, 0))
    _write(sy / UUID / "qgroups" / "0_256" / "referenced", "banana")
    _write(sy / UUID / "allocation" / "data" / "disk_total", "")
    free, total, quota, unalloc, *_ = _measure(target, mi, sy, tmp_path)
    assert quota == 0, "an unparseable quota must be skipped, never read as full"
    assert unalloc == "-"


# ── tiers ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "free,total,expect",
    [
        (50_000, 100_000, "green"),
        (15_000, 100_000, "green"),
        (14_999, 100_000, "yellow"),
        (7_999, 100_000, "orange"),
        (3_071, 100_000, "red"),  # absolute floor 3 GiB beats 3 %
        (8_999, 300_000, "red"),  # 3 % of 300 GB = 9000 MB floor; one under it
        (9_000, 300_000, "orange"),  # exactly at the floor is not below it
        (400, 512, "green"),  # a 512 MB tmpfs can be green: the 3 GiB floor is capped
        (120, 512, "green"),  # 23 % free of a small fs is not an emergency
        # Small filesystems keep a real ORANGE band: RED sits strictly below
        # the 8 % ORANGE line (review finding — a 10 % cap alone made them
        # jump from YELLOW straight to RED).
        # RED floor = min(max(3 %, 3 GiB), 3/4 of the ORANGE line).
        (38, 512, "orange"),
        (29, 512, "red"),
        (1846, 2048, "green"),  # the live cc-tmp: 2 GiB quota, 1.8 GiB free
        (300, 2048, "yellow"),
        (160, 2048, "orange"),
        (125, 2048, "orange"),
        (120, 2048, "red"),
        (2500, 25_600, "yellow"),
        (2000, 25_600, "orange"),
        (1600, 25_600, "orange"),
        (1500, 25_600, "red"),
        (8_401, 280_000, "orange"),  # a big disk keeps its 3 % floor (8.4 GB)
        (8_399, 280_000, "red"),
    ],
)
def test_floor_tier(tmp_path, free, total, expect):
    out = _run(f"dg_floor_tier {free} {total} - -", tmp_path / "mi", tmp_path, tmp_path)
    assert out == expect


def test_metadata_exhaustion_is_red_even_with_free_data(tmp_path):
    assert _run("dg_floor_tier 200000 400000 900 85", tmp_path, tmp_path, tmp_path) == "red"
    assert _run("dg_floor_tier 200000 400000 900 50", tmp_path, tmp_path, tmp_path) == "green"
    assert _run("dg_floor_tier 200000 400000 5000 95", tmp_path, tmp_path, tmp_path) == "green"


@pytest.mark.parametrize(
    "eta,expect",
    [
        ("-", "green"),
        ("5000", "green"),
        ("359", "yellow"),
        ("59", "orange"),
        ("9", "red"),
        ("0", "red"),
    ],
)
def test_eta_tier(tmp_path, eta, expect):
    assert _run(f"dg_eta_tier {eta}", tmp_path, tmp_path, tmp_path) == expect


def test_rate_smooths_a_burst_and_tracks_a_runaway(tmp_path):
    snippet = """
    dg_rate_update k 1000 1000 >/dev/null          # baseline
    echo "burst=$(dg_rate_update k 1350 1030)"      # 350 MB in 30 s, once
    echo "calm=$(dg_rate_update k 1350 1060)"
    """
    out = dict(line.split("=") for line in _run(snippet, tmp_path, tmp_path, tmp_path).splitlines())
    assert int(out["burst"]) == 210  # 0.3 * 700 MB/min
    assert int(out["calm"]) < int(out["burst"])
    # a sustained 3 GB/min runaway converges toward it within a few polls
    snippet2 = (
        "t=2000; u=5000; dg_rate_update r $u $t >/dev/null\n"
        + "".join("t=$((t+30)); u=$((u+1500)); r=$(dg_rate_update r $u $t)\n" for _ in range(6))
        + "echo $r"
    )
    assert int(_run(snippet2, tmp_path, tmp_path, tmp_path)) > 2400


def test_rate_ignores_a_stale_gap(tmp_path):
    snippet = "dg_rate_update g 100 1000 >/dev/null; dg_rate_update g 90000 9000"
    assert _run(snippet, tmp_path, tmp_path, tmp_path) == "0"


def test_eta_from_rate(tmp_path):
    assert _run("dg_eta_min 6000 100", tmp_path, tmp_path, tmp_path) == "60"
    assert _run("dg_eta_min 6000 0", tmp_path, tmp_path, tmp_path) == "-"
    assert _run("dg_eta_min 6000 -50", tmp_path, tmp_path, tmp_path) == "-"


# ── architect-review fixes (reservations, metadata, burst cap, binding) ──


def test_reservations_count_against_the_quota(tmp_path):
    target, mi, sy = _fixture(tmp_path, quota=(1024 * MIB, 100 * MIB))
    q = sy / UUID / "qgroups" / "0_256"
    _write(q / "rsv_data", 50 * MIB)
    _write(q / "rsv_meta_pertrans", 70 * MIB)
    _write(q / "rsv_meta_prealloc", 4 * MIB)
    free, total, quota, *_ = _measure(target, mi, sy, tmp_path)
    assert (free, total, quota) == (800, 1024, 1)


def test_an_inconsistent_quota_tree_is_not_trusted(tmp_path):
    target, mi, sy = _fixture(tmp_path, quota=(1024 * MIB, 100 * MIB))
    _write(sy / UUID / "qgroups" / "inconsistent", 1)
    free, total, quota, *_ = _measure(target, mi, sy, tmp_path)
    assert quota == 0 and total > 1024


def test_metadata_counts_reserved_and_pinned(tmp_path):
    target, mi, sy = _fixture(tmp_path, meta=(2 * 1024 * MIB, 4 * 1024 * MIB))
    md = sy / UUID / "allocation" / "metadata"
    _write(md / "bytes_reserved", 512 * MIB)
    _write(md / "bytes_pinned", 512 * MIB)
    *_, meta, _ = _measure(target, mi, sy, tmp_path)
    assert int(meta) == 75


def test_metadata_inside_twice_the_global_reserve_reads_full(tmp_path):
    target, mi, sy = _fixture(tmp_path, meta=(3 * 1024 * MIB, 4 * 1024 * MIB))
    _write(sy / UUID / "allocation" / "global_rsv_size", 512 * MIB + 1)
    *_, meta, _ = _measure(target, mi, sy, tmp_path)
    assert int(meta) == 100


@pytest.mark.parametrize("floor,eta,expect", [
    ("green", "red", "yellow"),      # a burst at high free space only logs
    ("green", "orange", "yellow"),
    ("yellow", "red", "orange"),
    ("orange", "red", "red"),        # the same rate on a tight disk is RED
    ("red", "green", "red"),         # the floor alone always counts
    ("yellow", "green", "yellow"),
])
def test_eta_escalates_at_most_one_level(tmp_path, floor, eta, expect):
    assert _run(f"dg_tier {floor} {eta}", tmp_path, tmp_path, tmp_path) == expect


def test_rate_resets_when_the_binding_limit_changes(tmp_path):
    snippet = """
    dg_rate_update b 1000 1000 fs >/dev/null
    dg_rate_update b 1100 1030 fs >/dev/null
    dg_rate_update b 90000 1060 quota
    """
    assert _run(snippet, tmp_path, tmp_path, tmp_path) == "0"


def test_every_function_survives_errexit_on_its_false_paths(tmp_path):
    """The daemon runs under set -euo pipefail: a function that ENDS on a false
    test returns non-zero and takes the whole daemon down. Exercise every
    function's quiet path under -e (the harness already sets it)."""
    snippet = """
    dg_floor_tier 900 1000 - - >/dev/null
    dg_eta_tier - >/dev/null
    dg_eta_min 10 0 >/dev/null
    dg_tier green green >/dev/null
    dg_measure / >/dev/null
    dg_mount_line / >/dev/null || true
    echo survived
    """
    assert _run(snippet, tmp_path / "nope", tmp_path / "nope", tmp_path) == "survived"


def test_the_rate_input_excludes_quota_reservations(tmp_path):
    """Reservations swing by gigabytes between commits; the growth rate must
    not read them as writes (7th field = usage WITHOUT reservations), while
    the headroom still subtracts them."""
    target, mi, sy = _fixture(tmp_path, quota=(1024 * MIB, 100 * MIB))
    _write(sy / UUID / "qgroups" / "0_256" / "rsv_meta_pertrans", 300 * MIB)
    out = _run(f"dg_measure '{target}'", mi, sy, tmp_path).split()
    assert out[0] == "624"   # headroom counts the reservation
    assert out[6] == "100"   # usage for the rate does not


def test_a_roomier_quota_does_not_bind(tmp_path):
    """Review finding (round 1): a quota with MORE headroom than the pool under
    it is not the binding limit. The figures, the quota flag and the rate
    input (7th field) must all come from statvfs, or other subvolumes filling
    the shared pool would never move the growth rate."""
    target, mi, sy = _fixture(tmp_path)
    sv_free, sv_total = _statvfs_mb(target)
    # 10 MiB referenced under a limit 1 GiB beyond what the pool has free.
    lim = (sv_free + 1024) * MIB + 10 * MIB
    q = sy / UUID / "qgroups" / "0_256"
    _write(q / "max_referenced", lim)
    _write(q / "referenced", 10 * MIB)
    _write(q / "limit_flags", 1)
    out = _run(f"dg_measure '{target}'", mi, sy, tmp_path).split()
    free, total, quota, used = int(out[0]), int(out[1]), int(out[2]), int(out[6])
    assert quota == 0, "the pool binds, not the quota"
    assert total == sv_total
    assert abs(free - sv_free) <= 2
    assert abs(used - (sv_total - sv_free)) <= 2, "rate input is the pool's usage, not the qgroup's 10 MiB"


def test_a_tighter_quota_supplies_every_figure(tmp_path):
    """Control for the test above: when the quota IS the closest wall, free,
    total and the rate input all come from the qgroup."""
    target, mi, sy = _fixture(tmp_path, quota=(1024 * MIB, 100 * MIB))
    out = _run(f"dg_measure '{target}'", mi, sy, tmp_path).split()
    assert (out[0], out[1], out[2], out[6]) == ("924", "1024", "1", "100")


def test_each_binding_keeps_its_own_rate_series(tmp_path):
    """Review finding: near a quota's crossover the closer wall alternates
    poll to poll. One shared series reset on every flip and never produced a
    rate; per-binding series each keep tracking a real runaway."""
    snippet = """
    dg_rate_update b 1000 1000 fs >/dev/null
    dg_rate_update b 500 1030 quota >/dev/null
    dg_rate_update b 1300 1060 fs >/dev/null
    dg_rate_update b 800 1090 quota >/dev/null
    dg_rate_update b 1600 1120 fs
    """
    rate = int(_run(snippet, tmp_path, tmp_path, tmp_path))
    assert rate > 100, f"a steady 300 MB/min fill must register while the binding alternates (got {rate})"
