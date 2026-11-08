"""Native publication checks use owned Git objects and synthetic private values."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2]
CANARY = "SYNTHETIC_PRIVATE_NATIVE_VALUE"
PUBLIC = "git@github.com:fixture/public.git"


@pytest.fixture
def native_world(tmp_path, request):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(HOME=str(tmp_path), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="protocol.allow",
               GIT_CONFIG_VALUE_0="never")

    def git(*args, data=None):
        return subprocess.run(["git", "--no-replace-objects", "-C", str(repo), *args], env=env,
                              input=data, capture_output=True, timeout=5, check=True).stdout

    git("-c", "init.templateDir=", "init", "-q", "-b", "main", "--object-format=" + getattr(request, "param", "sha1"))
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    config = tmp_path / ".genesis/config"
    config.mkdir(parents=True)
    (config / "genesis.yaml").write_text("github:\n  user: fixture\n  public_repo: public\n")
    fingerprints = tmp_path / "fingerprints.txt"
    fingerprints.write_text(CANARY + "\n")
    env["GENESIS_RELEASE_FINGERPRINTS"] = str(fingerprints)
    hooks = repo / "scripts/hooks"
    hooks.mkdir(parents=True)
    for name in ("sync-hooks.sh", "pre-push", "pre_push_chain.py", "pre_push_gates.sh",
                 "push_privacy_scan.py", "push_destinations.py", "push_privacy_ack.py"):
        if (SOURCE / "scripts/hooks" / name).exists():
            shutil.copy2(SOURCE / "scripts/hooks" / name, hooks / name)
    (repo / "src").symlink_to(SOURCE / "src", target_is_directory=True)
    (repo / ".venv").symlink_to(Path(sys.executable).parent.parent, target_is_directory=True)

    def commit(message, *, text="safe content\n", filename="sample.txt", parent=None):
        blob = git("hash-object", "-w", "--stdin", data=text.encode()).strip()
        tree = git("mktree", "-z", data=b"100644 blob " + blob + b"\t" + filename.encode() + b"\0").strip()
        args = ["commit-tree", tree.decode(), "-m", message]
        if parent:
            args.extend(["-p", parent])
        return git(*args).decode().strip()

    def invoke(oid, remote_oid=None):
        installed = subprocess.run(["bash", str(hooks / "sync-hooks.sh")], cwd=repo, env=env,
                                   capture_output=True, timeout=10)
        assert installed.returncode == 0, installed.stderr
        data = f"refs/heads/topic {oid} refs/heads/topic {remote_oid or '0' * len(oid)}\n".encode()
        return subprocess.run([str(repo / ".git/hooks/pre-push"), "public", PUBLIC], cwd=repo,
                              env=env, input=data, capture_output=True, timeout=70)

    return repo, env, git, commit, invoke


@pytest.mark.parametrize("surface", ["line", "message", "path"])
def test_installed_native_chain_blocks_exact_values(native_world, surface):
    repo, env, git, commit, invoke = native_world
    oid = commit(CANARY if surface == "message" else "safe message",
                 text=CANARY + "\n" if surface == "line" else "safe content\n",
                 filename=CANARY if surface == "path" else "sample.txt")
    result = invoke(oid)
    assert result.returncode == 2, result.stderr
    assert b"TIER_A" in result.stderr
    assert CANARY.encode() not in result.stdout + result.stderr


def test_canonical_fingerprint_first_match_is_opt_in(tmp_path):
    from genesis.contribution import sanitize

    fingerprints = tmp_path / "fingerprints.txt"
    fingerprints.write_text(CANARY + "\n")
    parsed = sanitize._ParsedDiff(["sample"], [("sample", 1, CANARY), ("sample", 2, CANARY)], False, 0)
    assert len(sanitize._check_fingerprints(parsed, fingerprints)) == 2
    assert len(sanitize._check_fingerprints(parsed, fingerprints, stop_after_first=True)) == 1


def _record(oid, remote=None, ref="refs/heads/topic"):
    return f"{ref} {oid} {ref} {remote or '0' * len(oid)}\n".encode()


def _worker(world, data, *, destination=PUBLIC):
    repo, env, git, commit, invoke = world
    result = subprocess.run([sys.executable, str(SOURCE / "scripts/hooks/push_privacy_scan.py"), "--worker",
                             str(SOURCE), str(repo / ".git"), "--", "public", destination],
                            cwd=repo, env=env, input=data, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert CANARY.encode() not in result.stdout + result.stderr
    return json.loads(result.stdout)


def test_known_remote_excludes_only_its_ancestry_and_rewind_changes_identity(native_world):
    repo, env, git, commit, invoke = native_world
    base = commit("base")
    private = commit("known old commit", text=CANARY + "\n", parent=base)
    tip = commit("safe tip", parent=private)
    clean = _worker(native_world, _record(tip, private))
    rewind = _worker(native_world, _record(tip, base))
    assert clean["verdict"] == "CLEAN" and rewind["verdict"] == "TIER_A"
    assert clean["identity"] != rewind["identity"]


def test_new_ref_does_not_subtract_private_origin_or_net_diff(native_world):
    repo, env, git, commit, invoke = native_world
    private = commit("private history", text=CANARY + "\n")
    tip = commit("scrubbed tip", parent=private)
    git("remote", "add", "origin", "git@github.com:fixture/private.git")
    git("update-ref", "refs/remotes/origin/main", private)
    result = _worker(native_world, _record(tip))
    assert result["verdict"] == "TIER_A"
    assert result["degraded"] == ["public-base-unproved"]


@pytest.mark.parametrize("data", [b"", b"(delete) " + b"0" * 40 + b" refs/heads/topic " + b"1" * 40 + b"\n"])
def test_empty_and_deletion_only_are_clean_without_fingerprints(native_world, data):
    repo, env, git, commit, invoke = native_world
    Path(env["GENESIS_RELEASE_FINGERPRINTS"]).unlink()
    assert _worker(native_world, data)["verdict"] == "CLEAN"


@pytest.mark.parametrize("malformed", [b"partial", b"x " + b"0" * 40 + b" y not-an-oid\n", b"\n", b"\r\n"])
def test_malformed_native_population_is_unknown(native_world, malformed):
    result = _worker(native_world, malformed)
    assert result["verdict"] == "COULD_NOT_SCAN"
    assert result["reasons"] == ["native-input"]


def test_deletion_and_every_tuple_field_remain_in_identity(native_world):
    repo, env, git, commit, invoke = native_world
    oid = commit("safe")
    first = _record(oid)
    populations = [first, first + _record("0" * 40, oid, "refs/heads/deleted"),
                   first.replace(b"refs/heads/topic", b"refs/heads/other", 1),
                   first.rsplit(b" ", 1)[0] + b" " + b"1" * 40 + b"\n",
                   _record(oid, ref="refs/heads/other"), first + _record(oid, ref="refs/heads/second")]
    results = [_worker(native_world, data) for data in populations]
    assert all(result["verdict"] == "CLEAN" for result in results)
    assert len({result["identity"] for result in results}) == len(populations)


def test_private_destination_is_outside_public_scope(native_world):
    repo, env, git, commit, invoke = native_world
    oid = commit(CANARY, text=CANARY + "\n")
    result = _worker(native_world, _record(oid), destination="git@github.com:fixture/private.git")
    assert result["verdict"] == "CLEAN" and not result["findings"]


@pytest.mark.parametrize("state", ["missing", "empty", "comments", "fifo"])
def test_unavailable_fingerprints_are_unknown_not_known_value_hits(native_world, state):
    repo, env, git, commit, invoke = native_world
    path = Path(env["GENESIS_RELEASE_FINGERPRINTS"])
    if state in ("missing", "fifo"):
        path.unlink()
        if state == "fifo":
            os.mkfifo(path)
    else:
        path.write_text("# fixture comment\n" if state == "comments" else "")
    result = _worker(native_world, _record(commit("safe")))
    assert result["verdict"] == "COULD_NOT_SCAN" and result["reasons"] == ["fingerprint-unavailable"]
    assert result["findings"] == []


def test_annotated_tag_message_is_checked_before_peeling(native_world):
    repo, env, git, commit, invoke = native_world
    oid = commit("safe")
    raw = f"object {oid}\ntype commit\ntag fixture\ntagger Fixture <fixture@example.invalid> 1 +0000\n\n{CANARY}\n".encode()
    tag = git("hash-object", "-w", "-t", "tag", "--stdin", data=raw).decode().strip()
    assert _worker(native_world, _record(tag))["verdict"] == "TIER_A"
