"""Local-only object reads must not trigger Git promisor retrieval."""
import json
import os
import subprocess
import sys

import pytest

from tests.test_hooks.test_native_privacy_public_base import public_world as _public_world
from tests.test_hooks.test_native_push_privacy import SOURCE, _record, _worker


@pytest.fixture
def public_world(tmp_path):
    return _public_world.__wrapped__(tmp_path)


def _promisor(world, mode):
    world.git(world.client, "remote", "add", "owned-promisor", "git@github.com:fixture/public.git")
    if mode == "filter":
        world.git(world.client, "config", "remote.owned-promisor.partialclonefilter", "blob:none")
    elif mode == "extension":
        world.git(world.client, "config", "core.repositoryformatversion", "1")
        world.git(world.client, "config", "extensions.partialClone", "owned-promisor")
    elif mode == "included":
        included = world.root / "included.conf"
        included.write_text('[remote "owned-promisor"]\n promisor = true\n')
        world.git(world.client, "config", "include.path", str(included))
    elif mode == "worktree":
        world.git(world.client, "config", "extensions.worktreeConfig", "true")
        world.git(world.client, "config", "--worktree", "remote.owned-promisor.promisor", "true")
    elif mode == "environment":
        world.env.update(GIT_CONFIG_COUNT="3", GIT_CONFIG_KEY_2="remote.owned-promisor.promisor", GIT_CONFIG_VALUE_2="true")
    else:
        world.git(world.client, "config", "remote.owned-promisor.promisor", "true" if mode != "false" else "false")
        if mode == "true-then-false":
            world.git(world.client, "config", "--add", "remote.owned-promisor.promisor", "false")


def _native(world, data=None, destination="git@github.com:fixture/public.git"):
    return subprocess.run([sys.executable, str(SOURCE / "scripts/hooks/push_privacy_scan.py"),
                           str(SOURCE), str(world.client / ".git"), "--", "public", destination],
                          cwd=world.client, env=world.env,
                          input=_record(world.outgoing) if data is None else data,
                          capture_output=True, timeout=12)


def _capability_shim(world, capable=False):
    """Model only flag support; every object lookup still executes actual Git."""
    directory = world.root / "git-bin"
    directory.mkdir()
    path = directory / "git"
    receipt = world.root / "git-argv.jsonl"
    path.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                    f"with open({str(receipt)!r}, 'a') as log: log.write(json.dumps([sys.argv[1:], os.environ.get('GIT_NO_LAZY_FETCH')]) + '\\n')\n"
                    "args = sys.argv[1:]\n"
                    "if '--no-lazy-fetch' in args:\n"
                    + ("    args.remove('--no-lazy-fetch')\n" if capable else "    raise SystemExit(129)\n")
                    + "os.execv('/usr/bin/git', ['/usr/bin/git', *args])\n")
    path.chmod(0o755)
    world.env["PATH"] = str(directory) + os.pathsep + world.env["PATH"]
    return receipt


@pytest.mark.parametrize("mode", ["promisor", "filter", "extension", "included", "worktree", "environment", "true-then-false"])
def test_unqualified_partial_clone_refuses_before_object_access(public_world, mode):
    world = public_world
    remote_only = world.commit(world.root / "public.git", "remote-only", world.main)
    world.advertise(remote_only, None)
    _promisor(world, mode)
    _capability_shim(world)
    world.env["GIT_NO_LAZY_FETCH"] = "0"
    before = world.snapshot()
    result = _native(world)
    assert result.returncode == 2 and b"COULD_NOT_SCAN" in result.stderr
    assert world.snapshot() == before
    assert world.transport() == []
    worker = _worker((world.client, world.env, None, None, None), _record(world.outgoing))
    assert worker["reasons"] == ["local-only-objects-unavailable"]


@pytest.mark.parametrize("mode", ["promisor", "filter", "extension"])
@pytest.mark.parametrize("present", [False, True])
def test_qualified_flag_model_never_fetches_and_preserves_local_proof(public_world, mode, present):
    world = public_world
    if not present:
        remote_only = world.commit(world.root / "public.git", "remote-only", world.main)
        world.advertise(remote_only, None)
    _promisor(world, mode)
    receipt = _capability_shim(world, capable=True)
    world.env["GIT_NO_LAZY_FETCH"] = "0"
    before = world.snapshot()
    result = _native(world)
    assert result.returncode == (0 if present else 2), result.stderr
    if not present:
        assert b"TIER_A" in result.stderr
    assert world.snapshot() == before
    assert len(world.transport()) == 1
    calls = [json.loads(line) for line in receipt.read_text().splitlines()]
    assert all(environment == "1" for args, environment in calls)
    reads = [args for args, environment in calls if any(x in args for x in ("cat-file", "rev-list", "diff-tree", "ls-tree"))]
    assert reads and all("--no-lazy-fetch" in args for args in reads)


@pytest.mark.parametrize("mode", ["none", "false"])
def test_unqualified_full_clone_still_uses_native_public_base(public_world, mode):
    world = public_world
    if mode == "false":
        _promisor(world, mode)
    _capability_shim(world)
    before = world.snapshot()
    result = _native(world)
    assert result.returncode == 0, result.stderr
    assert world.snapshot() == before and len(world.transport()) == 1


@pytest.mark.parametrize("population", ["empty", "deletion", "private"])
def test_partial_clone_nonpublic_or_no_object_population_stays_untouched(public_world, population):
    world = public_world
    _promisor(world, "promisor")
    _capability_shim(world)
    before = world.snapshot()
    data = b"" if population == "empty" else _record("0" * 40, world.outgoing) if population == "deletion" else None
    result = _native(world, data, "git@github.com:fixture/private.git" if population == "private" else "git@github.com:fixture/public.git")
    assert result.returncode == 0 and world.snapshot() == before and not world.transport()


def test_scanner_bootstraps_trusted_siblings_with_safe_path(public_world):
    world = public_world
    world.env["PYTHONSAFEPATH"] = "1"
    before = world.snapshot()
    result = _native(world)
    assert result.returncode == 0, result.stderr
    assert world.snapshot() == before
