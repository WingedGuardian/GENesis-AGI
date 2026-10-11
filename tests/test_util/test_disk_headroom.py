"""genesis.util.disk_headroom: the free space a write can actually use, measured by
the disk guardian's own dg_measure (statvfs, tightened by a btrfs quota)."""

from __future__ import annotations

import logging
import shutil

import pytest

from genesis.util import disk_headroom


def _fake_guardian(tmp_path, body: str):
    lib = tmp_path / "disk_guardian.sh"
    lib.write_text(f"dg_measure() {{\n{body}\n}}\n")
    return lib


def test_the_guardians_effective_figure_is_used(tmp_path, monkeypatch):
    lib = _fake_guardian(tmp_path, 'echo "1234 99999 1 - - btrfs 5"')
    monkeypatch.setattr(disk_headroom, "_GUARDIAN_LIB", lib)
    assert disk_headroom.effective_free_bytes(tmp_path) == 1234 * 1024**2


def test_a_path_not_made_yet_is_measured_where_it_will_land(tmp_path, monkeypatch):
    seen = tmp_path / "seen"
    lib = _fake_guardian(tmp_path, f'printf %s "$1" > "{seen}"; echo "7 7 0 - - ext4 0"')
    monkeypatch.setattr(disk_headroom, "_GUARDIAN_LIB", lib)
    assert disk_headroom.effective_free_bytes(tmp_path / "a" / "b") == 7 * 1024**2
    assert seen.read_text() == str(tmp_path)


@pytest.mark.parametrize("body", ["return 1", 'echo "not-a-number 1 0"', ":"])
def test_an_unusable_answer_falls_back_to_statvfs_and_says_so(tmp_path, monkeypatch, caplog, body):
    monkeypatch.setattr(disk_headroom, "_warned", False)
    monkeypatch.setattr(disk_headroom, "_GUARDIAN_LIB", _fake_guardian(tmp_path, body))
    with caplog.at_level(logging.WARNING, logger=disk_headroom.__name__):
        got = disk_headroom.effective_free_bytes(tmp_path)
    assert (
        got == shutil.disk_usage(tmp_path).free
        or abs(got - shutil.disk_usage(tmp_path).free) < 64 * 1024**2
    )
    assert "statvfs" in caplog.text


def test_a_missing_guardian_library_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_headroom, "_GUARDIAN_LIB", tmp_path / "absent.sh")
    assert disk_headroom.effective_free_bytes(tmp_path) > 0


def test_nothing_measurable_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_headroom, "_GUARDIAN_LIB", tmp_path / "absent.sh")

    def fail(path):
        raise OSError("no statvfs")

    monkeypatch.setattr(disk_headroom.shutil, "disk_usage", fail)
    assert disk_headroom.effective_free_bytes(tmp_path) is None


def test_the_real_guardian_measures_this_filesystem(tmp_path):
    """Against the shipped scripts/lib/disk_guardian.sh: on a filesystem with no
    quota its figure is statvfs's, to the MiB it reports in."""
    got = disk_headroom.effective_free_bytes(tmp_path)
    assert got is not None and got > 0
    assert got <= shutil.disk_usage(tmp_path).free + 1024**2


def test_usage_is_the_guardians_free_and_total(tmp_path, monkeypatch):
    """When a quota binds, the guardian reports the quota as the total too; a floor
    taken as a share of the pool would refuse every write on such an install."""
    monkeypatch.setattr(
        disk_headroom, "_GUARDIAN_LIB", _fake_guardian(tmp_path, 'echo "40960 51200 1 - - btrfs 9"')
    )
    assert disk_headroom.effective_usage(tmp_path) == (40960 * 1024**2, 51200 * 1024**2)


def test_the_fallback_warns_once_per_process(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(disk_headroom, "_warned", False)
    monkeypatch.setattr(disk_headroom, "_GUARDIAN_LIB", tmp_path / "absent.sh")
    with caplog.at_level(logging.WARNING, logger=disk_headroom.__name__):
        disk_headroom.effective_free_bytes(tmp_path)
        disk_headroom.effective_free_bytes(tmp_path)
    assert caplog.text.count("statvfs") == 1
