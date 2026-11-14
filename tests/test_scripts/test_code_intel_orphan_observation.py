"""Truthful old-owner lifetime observations; no process signalling or waits."""

import runpy
from pathlib import Path

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence

ROOT = Path(__file__).resolve().parents[2]
BOOT = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def old_owner(tmp_path, evidence):
    proc = tmp_path / "proc"
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text(BOOT)
    directory = proc / "123"
    directory.mkdir()
    directory.joinpath("stat").write_text(
        "123 (name with ) and spaces) " + " ".join(["S", "1"] + ["0"] * 17 + ["100"])
    )
    namespace = runpy.run_path(str(ROOT / "scripts/lib/code_intel_scope.py"))
    method = namespace.get("owner_ended")
    def observe(*args, **kwargs):
        assert callable(method), "supervised orphan closure needs positive exact-owner death proof"
        return method(*args, **kwargs)

    return observe, dict(pid=123, start_ticks=100, boot_id=BOOT), proc, evidence


@pytest.mark.parametrize("state", list("RSDTtWKPI"))
def test_matching_live_states_including_uninterruptible_sleep_are_not_dead(old_owner, state):
    method, owner, proc, proof = old_owner
    path = proc / "123/stat"
    path.write_text(path.read_text().replace(") S ", ") " + state + " "))
    assert method(owner, proof, proc=proc) is False


@pytest.mark.parametrize("state", ["Z", "X", "x"])
def test_terminal_leader_state_does_not_prove_process_end(old_owner, state):
    method, owner, proc, proof = old_owner
    path = proc / "123/stat"
    path.write_text(path.read_text().replace(") S ", ") " + state + " "))
    assert method(owner, proof, proc=proc) is False
    with pytest.raises(ValueError, match="ended"):
        proof["_process_state"](proc / "123", 123)


@pytest.mark.parametrize("state", ["RS", "SD", "ZT", "Xx"])
@pytest.mark.parametrize("start_ticks", [100, 200])
def test_multicharacter_state_never_proves_lifetime_end(old_owner, state, start_ticks):
    method, owner, proc, proof = old_owner
    path = proc / "123/stat"
    path.write_text(
        path.read_text().replace(") S ", ") " + state + " ").replace("100", str(start_ticks))
    )
    with pytest.raises(ValueError, match="state"):
        method(owner, proof, proc=proc)


@pytest.mark.parametrize("case", ["absent", "reused", "reboot"])
def test_conclusive_original_lifetime_end(old_owner, case):
    method, owner, proc, proof = old_owner
    if case == "absent":
        (proc / "123/stat").unlink()
        (proc / "123").rmdir()
    elif case == "reused":
        path = proc / "123/stat"
        path.write_text(path.read_text().replace("100", "200"))
    else:
        (proc / "sys/kernel/random/boot_id").write_text(BOOT[:-1] + "2")
    assert method(owner, proof, proc=proc) is True


@pytest.mark.parametrize(
    "case", ["short", "pid", "start", "missing-stat", "unreadable", "boot-drift", "state"]
)
def test_missing_or_malformed_evidence_never_becomes_owner_death(old_owner, case, monkeypatch):
    method, owner, proc, proof = old_owner
    path = proc / "123/stat"
    if case == "short":
        path.write_text("123 (name) Z 1")
    elif case == "pid":
        path.write_text(path.read_text().replace("123 ", "124 ", 1))
    elif case == "start":
        path.write_text(path.read_text().replace("100", "bad"))
    elif case == "state":
        path.write_text(path.read_text().replace(") S ", ") invalid "))
    elif case == "missing-stat":
        path.unlink()
    else:
        original = Path.read_text

        def read(current, *args, **kwargs):
            if current == path:
                if case == "unreadable":
                    raise PermissionError("synthetic proc restriction")
                (proc / "sys/kernel/random/boot_id").write_text(BOOT[:-1] + "2")
            return original(current, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises((OSError, ValueError)):
        method(owner, proof, proc=proc)


@pytest.mark.parametrize(
    "field,value", [("pid", True), ("pid", 0), ("start_ticks", 0), ("boot_id", "bad")]
)
def test_invalid_original_identity_has_no_observation_authority(old_owner, field, value):
    method, owner, proc, proof = old_owner
    with pytest.raises(ValueError):
        method({**owner, field: value}, proof, proc=proc)
