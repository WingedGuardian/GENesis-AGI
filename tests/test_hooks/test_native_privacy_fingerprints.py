"""Snapshot binding and a real catastrophic-regex worker deadline."""
import asyncio
import sys
from pathlib import Path

import pytest

from tests.conftest import private_module
from tests.test_hooks.test_native_push_privacy import CANARY, SOURCE, _record
from tests.test_hooks.test_native_push_privacy import native_world as _native_world


@pytest.fixture
def native_world(tmp_path, request):
    return _native_world.__wrapped__(tmp_path, request)


def _scan_module(monkeypatch, world):
    repo, env, git, commit, invoke = world
    for key, value in env.items():
        if key in ("HOME", "GENESIS_RELEASE_FINGERPRINTS") or key.startswith("GIT_"):
            monkeypatch.setenv(key, value)
    monkeypatch.chdir(repo)
    monkeypatch.syspath_prepend(str(SOURCE / "scripts/hooks"))
    module = private_module("native_fingerprint_snapshot", SOURCE / "scripts/hooks/push_privacy_scan.py")
    monkeypatch.setattr(module, "SCAN_SECONDS", 3.0)
    return module


def test_scan_snapshot_is_bound_to_actual_fingerprint_bytes(native_world, monkeypatch):
    repo, env, git, commit, invoke = native_world
    module = _scan_module(monkeypatch, native_world)
    scan = module._Scan(SOURCE, ["public", "fixture"], b"", str(repo / ".git"))
    before = scan.result("CLEAN").identity
    Path(env["GENESIS_RELEASE_FINGERPRINTS"]).write_text("different-fixture\n")
    assert scan.text(CANARY.encode(), "fixture-location")
    fresh = module._Scan(SOURCE, ["public", "fixture"], b"", str(repo / ".git"))
    assert not fresh.text(CANARY.encode(), "fixture-location")
    assert before != fresh.result("CLEAN").identity


@pytest.mark.parametrize("known_first", [False, True])
def test_bounded_real_regex_and_first_known_match(native_world, monkeypatch, known_first):
    repo, env, git, commit, invoke = native_world
    module = _scan_module(monkeypatch, native_world)
    Path(env["GENESIS_RELEASE_FINGERPRINTS"]).write_text((CANARY + "\n" if known_first else "") + "(a+)+$\n")
    oid = commit((CANARY if known_first else "") + "a" * 48 + "!")
    result = asyncio.run(module.scan(SOURCE, ["public", "git@github.com:fixture/public.git"], _record(oid), common_dir=str(repo / ".git")))
    assert result.verdict == ("TIER_A" if known_first else "COULD_NOT_SCAN")


def test_missing_canonical_import_refuses_without_raw_diagnostics(native_world, tmp_path):
    import subprocess

    repo, env, git, commit, invoke = native_world
    empty = tmp_path / "unavailable-runtime"
    empty.mkdir()
    # -S removes site packages; clear the fixture source PYTHONPATH too.
    env = dict(env, PYTHONPATH="")
    result = subprocess.run([sys.executable, "-S", str(SOURCE / "scripts/hooks/push_privacy_scan.py"),
                             str(empty), str(repo / ".git"), "--", "public", "https://user:synthetic-pass@github.com/fixture/public.git"],
                            cwd=empty, env=env, input=_record(commit("safe")), capture_output=True, timeout=10)
    assert result.returncode == 2 and b"COULD_NOT_SCAN" in result.stderr
    assert b"Traceback" not in result.stderr and b"synthetic-pass" not in result.stderr
