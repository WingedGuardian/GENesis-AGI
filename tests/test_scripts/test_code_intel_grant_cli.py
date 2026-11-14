"""Real sealed metadata CLI; manager/capacity inputs are scratch substitutes."""

import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from tests.test_scripts.test_code_intel_gitnexus_metadata import selection as selection
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules


def cli_case(selection, tmp_path, *, schema5=True):
    original, modules, environment, cwd, activity, _, _ = selection
    root = original.root
    capacity = tmp_path / "capacity"
    group = capacity / "app-capped.slice"
    group.mkdir(parents=True)
    (capacity / "memory.max").write_text("max")
    (group / "memory.max").write_text(str(16 * 1024**3))
    (group / "memory.current").write_text("0")
    observed = tmp_path / "capacity-observed"
    scope = root / "scripts/lib/code_intel_scope.py"
    scope.write_text(scope.read_text() + f"""
class ScopeManager:
 def __init__(self,*args,**kwargs): pass
 def require_no_batch(self,physical): pass
 def require_slice_capacity(self,name,observe):
  assert name=='app-capped.slice'
  Path({str(observed)!r}).write_text('observed')
  observe('/app-capped.slice')
""")
    unit = root / "scripts/lib/codebase_managed_unit.py"
    unit.write_text(unit.read_text() + f"""
def resolve_control_group(control):
 assert control=='/app-capped.slice'
 return Path({str(group)!r}),Path({str(capacity)!r}),2
""")
    admission = root / "scripts/lib/code_intel_cbm_admission.py"
    admission.write_text(admission.read_text() + "\ndef _host_available(path): return 64*1024**3\n")
    git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    subprocess.run(["git", "-C", str(root), "add", "."], env=git_env,
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "core.hooksPath=/dev/null",
                    "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-qm", "inert metadata fixture"],
                   env=git_env, check=True, capture_output=True)
    source = sys.modules[type(original).__module__]
    snapshot = source.capture(root, "runner", original.buffers[source.SELF])
    queue = modules["queue"]
    queue._prepare()
    if schema5:
        with closing(queue._connect()) as db, queue._transaction(db):
            queue._recovery.upgrade(db, queue._legs, queue._native)
    queue.write_marker(str(root), "gitnexus", "full")
    lock = Path(environment["GENESIS_HOME"]) / "locks/code-intel-runner.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    prefix = ["/bin/bash", "--noprofile", "--norc", "-p", "-c",
              'exec 8>"$1"; /usr/bin/flock -n 8 || exit 97; shift; exec "$@"', "--", str(lock)]
    env = {**os.environ, **environment, "CODE_INTEL_INDEX_DISABLE": "0",
           "CODE_INTEL_WORKLOAD_SLICE": "0"}
    return snapshot, queue, prefix, env, cwd, activity, observed


def invoke(case, args, *, raw=False, extra_env=None):
    snapshot, _, prefix, env, cwd, _, _ = case
    environment = {**env, **(extra_env or {})}
    if raw:
        return subprocess.run([sys.executable, "-I", str(snapshot.root / "scripts/lib/index_marker.py"), *args],
                              cwd=cwd, env=environment, capture_output=True, timeout=30)
    with snapshot.sealed() as capsule:
        return subprocess.run([*prefix, *snapshot.resume_command("queue", *args)],
                              stdin=capsule, cwd=cwd, env=environment, capture_output=True, timeout=30)


def request(case):
    return ["request-retry", "--repo", str(case[0].root), "--provider", "gitnexus",
            "--origin", "pending", "--mode", "full", "--not-after-wall-ns",
            str(time.time_ns() + 3600 * 10**9), "--evidence-reference", "fixture://metadata",
            "--managed-config", "/unused"]


@pytest.mark.parametrize("route", ["captured", "raw", "schema4", "disabled"])
def test_sealed_issue_cli_writes_only_grant_metadata_and_keeps_readiness_negative(selection, tmp_path, route):
    case = cli_case(selection, tmp_path, schema5=route != "schema4")
    snapshot, queue, _, _, _, activity, observed = case
    before = queue.database_path().read_bytes()
    result = invoke(case, request(case), raw=route == "raw",
                    extra_env={"CODE_INTEL_INDEX_DISABLE": "1" if route == "disabled" else "0"})
    if route == "captured":
        assert result.returncode == 0, result.stderr.decode()
        grant = json.loads(result.stdout)
        assert grant["status"] == "issued" and grant["trial"] is None
        assert grant["source"]["files"] == snapshot.files and grant["source"]["commit"] == snapshot.commit
        assert observed.read_text() == "observed"
        with closing(queue._connect()) as db:
            assert queue._retry.load(db, grant["hash"], "gitnexus", queue._native) == grant
            assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM inflight").fetchone()[0] == 0
            assert db.execute("SELECT value FROM queue_meta WHERE key='automatic_protocol_ready'").fetchone()[0] == "0"
    else:
        assert result.returncode != 0
        assert b"captured" in result.stderr or b"schema5" in result.stderr or b"disabled" in result.stderr
        assert queue.database_path().read_bytes() == before and not observed.exists()
    assert not activity.exists()
    assert {row["attempts"] for row in queue.list_markers()} == {0}


@pytest.mark.parametrize("route", ["captured", "raw", "schema4", "wrong-digest"])
def test_actual_corrupt_digest_revoke_retains_capture_and_schema_guards(selection, tmp_path, route):
    case = cli_case(selection, tmp_path)
    _, queue, _, _, _, activity, _ = case
    issued = invoke(case, request(case))
    assert issued.returncode == 0, issued.stderr.decode()
    grant = json.loads(issued.stdout)
    raw = b"\xff"
    with closing(queue._connect()) as db, queue._transaction(db):
        db.execute("UPDATE provider_state SET grant=CAST(? AS TEXT) WHERE hash=? AND provider='gitnexus'",
                   (raw, grant["hash"]))
    status = invoke(case, ["status-recovery", "--repo", str(case[0].root), "--provider", "gitnexus"], raw=True)
    assert status.returncode == 0, status.stderr.decode()
    descriptor = json.loads(status.stdout)["grant"]
    assert descriptor["state"] == "invalid-grant" and descriptor["launch_authority"] is False
    digest = hashlib.sha256(b"text\0" + raw).hexdigest()
    assert descriptor["sha256"] == digest
    if route == "schema4":
        with closing(queue._connect()) as db:
            db.execute("PRAGMA user_version=4")
    before = queue.database_path().read_bytes()
    args = ["revoke-retry", "--repo", str(case[0].root), "--provider", "gitnexus",
            "--grant-sha256", "d" * 64 if route == "wrong-digest" else digest]
    result = invoke(case, args, raw=route == "raw")
    if route == "captured":
        assert result.returncode == 0, result.stderr.decode()
        assert json.loads(result.stdout) == dict(state="revoked-corrupt", sha256=digest, launch_authority=False)
        replay = invoke(case, args)
        assert replay.returncode == 0 and json.loads(replay.stdout) == dict(state="absent", launch_authority=False)
        with closing(queue._connect()) as db:
            assert db.execute("SELECT grant FROM provider_state WHERE hash=? AND provider='gitnexus'",
                              (grant["hash"],)).fetchone()[0] is None
    else:
        assert result.returncode != 0
        assert queue.database_path().read_bytes() == before
    assert not activity.exists() and {row["attempts"] for row in queue.list_markers()} == {0}


def test_selected_command_is_unavailable_in_metadata_prefix(selection, tmp_path):
    case = cli_case(selection, tmp_path)
    before = case[1].database_path().read_bytes()
    result = invoke(case, ["claim-selected", "--repo", str(case[0].root),
                           "--provider", "gitnexus", "--nonce", "b" * 32])
    assert result.returncode == 2 and b"invalid choice" in result.stderr
    assert case[1].database_path().read_bytes() == before
    assert not case[5].exists() and not case[6].exists()
