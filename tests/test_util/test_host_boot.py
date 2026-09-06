"""Tests for genesis.util.host_boot.

This primitive decides whether the Guardian drops a live escalation episode, so
its failure mode matters more than its happy path: `None` must mean "cannot
tell", never "rebooted". A `read_boot_id` that always returned falsy would make
`reset_if_rebooted` a permanent no-op and the whole feature silently dead —
measured as a GREEN mutation before these tests existed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from genesis.util import host_boot

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@pytest.fixture()
def fake_proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Repoint the module's `_PROC` seam at a fake tree and return the boot_id path."""
    boot_id = tmp_path / "sys" / "kernel" / "random" / "boot_id"
    boot_id.parent.mkdir(parents=True)
    monkeypatch.setattr(host_boot, "_PROC", str(tmp_path))
    return boot_id


def test_reads_the_value_and_strips_the_trailing_newline(fake_proc: Path) -> None:
    fake_proc.write_text("1a2b3c4d-0000-4111-8000-000000000001\n")
    assert host_boot.read_boot_id() == "1a2b3c4d-0000-4111-8000-000000000001"


def test_blank_content_reads_as_cannot_tell(fake_proc: Path) -> None:
    # An empty string would compare unequal to a stored id and be read as a
    # reboot, discarding a live episode. It must be None instead.
    fake_proc.write_text("   \n")
    assert host_boot.read_boot_id() is None


def test_missing_file_reads_as_cannot_tell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host_boot, "_PROC", str(tmp_path / "nonexistent"))
    assert host_boot.read_boot_id() is None


def test_undecodable_content_reads_as_cannot_tell(fake_proc: Path) -> None:
    # UnicodeDecodeError is a ValueError, not an OSError — the real /proc file is
    # ASCII, but the contract must hold for anything the seam can be pointed at.
    fake_proc.write_bytes(b"\xff\xfe not utf-8")
    assert host_boot.read_boot_id() is None


def test_the_real_kernel_path_yields_a_uuid() -> None:
    """Guards the PATH itself. Every test above would pass with a typo'd
    filename, since a missing file is a legitimate 'cannot tell'."""
    value = host_boot.read_boot_id()
    assert value is not None, "/proc/sys/kernel/random/boot_id was not readable"
    assert _UUID.match(value), f"not a boot UUID: {value!r}"


def test_the_value_is_stable_within_one_boot() -> None:
    # The whole design rests on this: two of the Guardian's oneshot processes in
    # the same boot must agree, or every tick would look like a reboot.
    assert host_boot.read_boot_id() == host_boot.read_boot_id()
