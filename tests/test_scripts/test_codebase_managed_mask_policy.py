"""Actual CBM destinations retain shared unit policy at the renderer boundary."""
from __future__ import annotations

import os

import pytest

from tests.test_scripts.test_codebase_managed_templates import UNITS
from tests.test_scripts.test_codebase_managed_templates import render as render


@pytest.mark.parametrize("renderer", ["install.sh", "bootstrap.sh"])
@pytest.mark.parametrize("name", UNITS)
def test_existing_regular_cbm_destination_is_kept(render, renderer, name):
    units, run = render
    target = units / name
    target.write_text("[Service]\nExecStart=/operator/owned\n")
    before = target.stat()
    result = run(renderer)
    assert result.returncode == 0, result.stdout + result.stderr
    assert target.read_text() == "[Service]\nExecStart=/operator/owned\n"
    assert (target.stat().st_dev, target.stat().st_ino) == (before.st_dev, before.st_ino)


@pytest.mark.parametrize("name", UNITS)
def test_named_bootstrap_take_backs_up_link_and_preserves_foreign_bytes(render, name):
    units, run = render
    foreign = units.parent / "foreign-unit"
    foreign.write_text("foreign unit bytes")
    target = units / name
    target.symlink_to(foreign)
    other = units / next(unit for unit in UNITS if unit != name)
    other.symlink_to("/dev/null")
    other_inode = other.lstat().st_ino
    result = run("bootstrap.sh", take=name)
    assert result.returncode == 0, result.stdout + result.stderr
    assert foreign.read_text() == "foreign unit bytes"
    assert not target.is_symlink() and "genesis-managed" in target.read_text()
    assert other.is_symlink() and os.readlink(other) == "/dev/null"
    assert other.lstat().st_ino == other_inode
    backups = list((units.parent / ".genesis/deploy-backups").glob("*/" + name))
    assert len(backups) == 1 and backups[0].is_symlink()
    assert os.readlink(backups[0]) == str(foreign)


@pytest.mark.parametrize("name", UNITS)
@pytest.mark.parametrize("kind", ["mask", "foreign", "dangling"])
@pytest.mark.parametrize("take", ["named", "*"])
def test_install_keeps_links_despite_inherited_take(render, name, kind, take):
    units, run = render
    foreign = units.parent / "foreign-unit"
    if kind == "foreign":
        foreign.write_text("foreign unit bytes")
    destination = "/dev/null" if kind == "mask" else str(foreign)
    target = units / name
    target.symlink_to(destination)
    before = target.lstat().st_ino
    result = run("install.sh", take=name if take == "named" else take)
    assert result.returncode == 0, result.stdout + result.stderr
    assert target.is_symlink() and os.readlink(target) == destination
    assert target.lstat().st_ino == before and "kept" in result.stdout.lower()
    if kind == "foreign":
        assert foreign.read_text() == "foreign unit bytes"
    elif kind == "dangling":
        assert not foreign.exists()
