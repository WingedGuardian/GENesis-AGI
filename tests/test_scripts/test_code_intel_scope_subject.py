"""Synthetic proc/kernel reads for the original owned child; no manager/indexer."""

import os
import shutil
from pathlib import Path

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_execution import scope as scope


def _child(scope):
    probe, leaf, proc = scope
    pid = 12345 if os.getpid() != 12345 else 12346
    subject = proc / str(pid)
    shutil.copytree(proc / "self", subject)
    (subject / "stat").write_text(
        f"{pid} (early ) scope) S {os.getpid()} " + " ".join(["0"] * 17 + ["2000"]))
    return probe, leaf, proc, subject, pid


def test_explicit_subject_preserves_child_identity_without_changing_self(scope):
    probe, leaf, proc, subject, pid = _child(scope)
    facts = probe.capture(pid=pid)
    assert facts["reporter"] == {"pid": pid, "start_ticks": 2000,
                                 "boot_id": (proc / "sys/kernel/random/boot_id").read_text()}
    assert facts["leaf"]["inode"] == leaf.stat().st_ino
    assert probe.capture()["reporter"]["pid"] == os.getpid()
    with pytest.raises(ValueError, match="current reporter"):
        probe.finality(facts)


@pytest.mark.parametrize("pid", [True, False, 0, -1, "12345", 1.5, 2**32])
def test_invalid_explicit_subject_cannot_select_a_proc_path(scope, pid):
    with pytest.raises(ValueError, match="subject"):
        scope[0].capture(pid=pid)


@pytest.mark.parametrize("fault", ["foreign-parent", "wrong-pid", "missing-stat", "foreign-mount"])
def test_subject_requires_same_visible_mapping_and_original_direct_parent(scope, fault):
    probe, leaf, proc, subject, pid = _child(scope)
    path = subject / "stat"
    if fault == "foreign-parent":
        path.write_text(path.read_text().replace(f" S {os.getpid()} ", " S 1 "))
    elif fault == "wrong-pid":
        path.write_text(path.read_text().replace(str(pid), str(pid + 1), 1))
    elif fault == "missing-stat":
        path.unlink()
    else:
        path = subject / "mountinfo"
        path.write_text(path.read_text().replace("1 0 0:1", "2 0 0:1"))
    with pytest.raises((OSError, ValueError)):
        probe.capture(pid=pid)


@pytest.mark.parametrize("fault", ["start", "parent", "pid", "boot", "membership", "mount", "root"])
def test_original_subject_and_kernel_mapping_are_bracketed(scope, monkeypatch, fault):
    probe, leaf, proc, subject, pid = _child(scope)
    read = Path.read_text
    identity = probe.capture.__globals__["_identity"]
    changed = False

    def mutate(path, *args, **kwargs):
        nonlocal changed
        value = read(path, *args, **kwargs)
        if path == leaf / "memory.events.local" and not changed:
            changed = True
            if fault in ("start", "parent", "pid"):
                stat = read(subject / "stat")
                if fault == "start":
                    stat = stat.replace("2000", "2001")
                elif fault == "parent":
                    stat = stat.replace(f" S {os.getpid()} ", " S 1 ")
                else:
                    stat = stat.replace(str(pid), str(pid + 1), 1)
                (subject / "stat").write_text(stat)
            elif fault == "boot":
                (proc / "sys/kernel/random/boot_id").write_text("00000000-0000-0000-0000-000000000002")
            elif fault == "membership":
                (subject / "cgroup").write_text("0::/foreign.scope\n")
            elif fault == "mount":
                path = subject / "mountinfo"
                path.write_text(read(path).replace("1 0 0:1", "2 0 0:1"))
        return value

    def root_change(path):
        value = identity(path)
        return {**value, "inode": value["inode"] + 1} if fault == "root" and changed and path == leaf.parents[1] else value

    monkeypatch.setattr(Path, "read_text", mutate)
    monkeypatch.setitem(probe.capture.__globals__, "_identity", root_change)
    with pytest.raises((OSError, ValueError)):
        probe.capture(pid=pid)
    assert changed, "probe did not reach the mutation point"
