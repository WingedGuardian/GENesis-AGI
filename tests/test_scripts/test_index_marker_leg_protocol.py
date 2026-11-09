"""Real CLI transport, replay and the incomplete automatic protocol fence."""

import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest

from tests.test_scripts.test_index_marker_legs import SCRIPT
from tests.test_scripts.test_index_marker_legs import queue as queue  # noqa: F401


def _cli(*args):
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args],
        env=dict(os.environ),
        capture_output=True,
        timeout=20,
    )


def _inventory(directory):
    return {
        str(path.relative_to(directory)): (
            path.stat().st_ino,
            path.stat().st_mode,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in directory.iterdir()
        if path.is_file()
    }


@pytest.mark.parametrize(
    "name", ["space 雪", "tab\tpath", "line\npath", "trailing\n", "literal$()'\\"]
)
def test_real_cli_json_claim_and_settlement_preserve_literal_repository(queue, tmp_path, name):
    repo = str(tmp_path / name)
    written = _cli("write", "--repo", repo, "--tools", "both")
    assert written.returncode == 0, written.stderr
    listed = _cli("list-legs")
    assert listed.returncode == 0, listed.stderr
    rows = [json.loads(line) for line in listed.stdout.splitlines()]
    assert len(rows) == 2 and all(row["repo_path"] == repo for row in rows)
    h = queue.marker_hash(repo)
    claimed = _cli("claim", "--hash", h, "--provider", "gitnexus")
    assert claimed.returncode == 0, claimed.stderr
    claim = json.loads(claimed.stdout)
    assert set(claim["context"]) == queue._legs.CONTEXT and claim["context"]["repo_path"] == repo
    context = ["--context", json.dumps(claim["context"], sort_keys=True)]
    assert _cli("remember-outcome", *context, "--action", "consume").returncode == 0
    applied = _cli("apply-outcome", *context)
    assert applied.returncode == 0 and applied.stdout.strip() == b"consumed", applied.stderr
    assert [row["provider"] for row in queue.list_markers()] == ["cbm"]


def test_actual_cli_distinguishes_empty_claim_from_database_failure(queue, tmp_path):
    queue._prepare()
    empty = _cli("claim", "--hash", "0" * 16, "--provider", "cbm")
    assert empty.returncode == 1 and not empty.stdout
    queue.database_path().write_bytes(b"owned corrupt SQLite header")
    failed = _cli("list-legs")
    assert failed.returncode == 2 and not failed.stdout
    assert b"Traceback" in failed.stderr and b"DatabaseError" in failed.stderr


def test_busy_outcome_spool_has_complete_context_and_replays_once(queue, tmp_path):
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "both", "fast")
    claimed = queue.claim(queue.marker_hash(repo), provider="cbm")
    blocker = sqlite3.connect(queue.database_path(), isolation_level=None)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        spool = queue.remember_outcome(claimed["context"], "consume")
        raw = spool.read_bytes()
        event = json.loads(raw)
        assert event["version"] == 2 and event["kind"] == "outcome"
        assert {key: event[key] for key in claimed["context"]} == claimed["context"]
    finally:
        blocker.rollback()
        blocker.close()
    assert queue.apply_remembered_outcome(claimed["context"]) == "consumed"
    assert queue.apply_remembered_outcome(claimed["context"]) == "consumed"
    assert not spool.exists()
    assert [row["provider"] for row in queue.list_markers()] == ["gitnexus"]
    with closing(sqlite3.connect(queue.database_path())) as db:
        retained = db.execute(
            "SELECT raw_payload FROM legacy_imports WHERE content_sha256=?",
            (hashlib.sha256(raw).hexdigest(),),
        ).fetchone()
    assert bytes(retained[0]) == raw


def test_busy_current_enqueue_is_not_reclassified_as_legacy(queue, tmp_path):
    queue._prepare()
    repo = str(tmp_path / "current repo")
    blocker = sqlite3.connect(queue.database_path(), isolation_level=None)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        spool = queue.write_marker(repo, "both", "fast")
        event = json.loads(spool.read_bytes())
        assert set(event) == {"version", "kind", "request"} and event["version"] == 2
    finally:
        blocker.rollback()
        blocker.close()
    rows = queue.list_markers()
    assert len(rows) == 2 and all(not row["legacy_unqualified"] for row in rows)
    assert all(row["legacy_shared_attempts"] == [] for row in rows)


def test_old_unwrapped_enqueue_keeps_shared_debt_and_raw_evidence(queue, tmp_path):
    queue._prepare()
    repo = str(tmp_path / "old repo")
    h = queue.marker_hash(repo)
    data = dict(repo_path=repo, tools="both", mode="fast", requested_at=1, attempts=4)
    raw = json.dumps(data).encode()
    spool = queue.marker_dir() / f".spool-{h}-{'a' * 32}.spool"
    spool.write_bytes(raw)
    rows = queue.list_markers()
    assert len(rows) == 2 and all(row["attempts"] == 4 for row in rows)
    assert all(row["legacy_unqualified"] and row["legacy_shared_attempts"] == [4] for row in rows)
    with closing(sqlite3.connect(queue.database_path())) as db:
        assert bytes(db.execute("SELECT raw_payload FROM legacy_imports").fetchone()[0]) == raw


def test_readiness_is_read_only_and_never_enabled_by_a_flag(queue):
    queue._prepare()
    before = _inventory(queue.marker_dir())
    observed = _cli("automatic-ready")
    assert observed.returncode == queue.AUTOMATIC_PROTOCOL_INCOMPLETE
    assert json.loads(observed.stdout)["automatic_protocol_ready"] == 0
    assert _inventory(queue.marker_dir()) == before
    with closing(sqlite3.connect(queue.database_path())) as db:
        db.execute("UPDATE queue_meta SET value='1' WHERE key='automatic_protocol_ready'")
        db.commit()
    before = _inventory(queue.marker_dir())
    unsupported = _cli("automatic-ready")
    assert unsupported.returncode == 2 and not unsupported.stdout
    assert b"unsupported automatic queue protocol" in unsupported.stderr
    assert _inventory(queue.marker_dir()) == before


def test_real_runner_incomplete_protocol_never_claims_or_calls_provider(queue, tmp_path):
    queue.write_marker(str(tmp_path / "repo"), "both", "fast")
    called = tmp_path / "provider-called"
    fake = tmp_path / "owned-fake-entrypoint.sh"
    fake.write_text("#!/usr/bin/env bash\nprintf ran > " + shlex.quote(str(called)) + "\n")
    fake.chmod(0o700)
    before = _inventory(queue.marker_dir())
    runner = SCRIPT.parent.parent / "code_intel_runner.sh"
    result = subprocess.run(
        ["bash", str(runner)],
        env={**os.environ, "CODE_INTEL_ENTRYPOINT": str(fake)},
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0 and not called.exists(), result.stderr
    assert _inventory(queue.marker_dir()) == before
    assert (
        "protocol incomplete"
        in (queue.marker_dir().parent / "code-intelligence-runner.log").read_text()
    )


@pytest.mark.parametrize(
    "field", ["hash", "repo_path", "provider", "protocol", "execution_seq", "claim_id"]
)
@pytest.mark.parametrize("variant", ["missing", "wrong", "bool"])
def test_invalid_context_refuses_before_busy_fallback(queue, tmp_path, monkeypatch, field, variant):
    queue.write_marker(str(tmp_path / "repo"), "cbm", "fast")
    context = queue.claim(queue.marker_hash(str(tmp_path / "repo")), provider="cbm")["context"]
    if variant == "missing":
        context.pop(field)
    else:
        context[field] = True if variant == "bool" else "wrong"
    before = _inventory(queue.marker_dir())

    def unavailable():
        pytest.fail("invalid context reached database/busy fallback")

    monkeypatch.setitem(queue.remember_outcome.__globals__, "_prepare", unavailable)
    with pytest.raises(ValueError):
        queue.remember_outcome(context, "consume")
    assert _inventory(queue.marker_dir()) == before


@pytest.mark.parametrize("kind", ["extra", "missing", "duplicate", "bool-version", "wrong-kind"])
def test_closed_current_enqueue_envelope_keeps_poison_local(queue, tmp_path, kind):
    queue._prepare()
    repo = str(tmp_path / "repo")
    h = queue.marker_hash(repo)
    event = dict(
        version=2,
        kind="enqueue",
        request=dict(repo_path=repo, tools="both", mode="fast", requested_at=1, attempts=0),
    )
    if kind == "extra":
        event["request"]["unexpected"] = 1
    elif kind == "missing":
        event["request"].pop("attempts")
    elif kind == "bool-version":
        event["version"] = True
    elif kind == "wrong-kind":
        event["kind"] = "outcome"
    raw = json.dumps(event, separators=(",", ":")).encode()
    if kind == "duplicate":
        raw = raw.replace(b'"version":2', b'"version":1,"version":2')
    spool = queue.marker_dir() / f".spool-{h}-{'a' * 32}.spool"
    spool.write_bytes(raw)
    assert queue.list_markers() == [] and not spool.exists()
    with closing(sqlite3.connect(queue.database_path())) as db:
        disposition, evidence = db.execute(
            "SELECT disposition,raw_payload FROM legacy_imports"
        ).fetchone()
    assert disposition == "quarantined" and evidence == raw


def test_cli_duplicate_context_is_not_silently_accepted(queue, tmp_path):
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "cbm", "fast")
    context = queue.claim(queue.marker_hash(repo), provider="cbm")["context"]
    encoded = json.dumps(context, separators=(",", ":")).replace(
        '"protocol":2', '"protocol":1,"protocol":2'
    )
    result = _cli("remember-outcome", "--context", encoded, "--action", "consume")
    assert result.returncode == 2 and b"invalid-json" in result.stderr
    assert queue.get_inflight(context["hash"])["outcome_action"] is None
