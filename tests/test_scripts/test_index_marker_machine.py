"""Closed machine records preserve literal data and distinguish predicates."""

import json
import os
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

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


def _fields(raw):
    assert raw.endswith(b"\0"), raw
    return raw[:-1].split(b"\0")


def test_actual_machine_list_keeps_current_validator_and_atomic_output(queue, tmp_path, monkeypatch, capsys):
    repo = str(tmp_path / "literal 雪\n")
    queue.write_marker(repo, "both", "fast")
    seen = []

    def reject(value):
        seen.append(value)
        raise ValueError("fixture current machine validator")

    monkeypatch.setitem(queue._pending_record.__globals__, "_machine_hash", reject)
    with pytest.raises(ValueError, match="fixture current machine validator"):
        queue.main(["list-legs", "--nul"])
    assert seen == [queue.marker_hash(repo)]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "name", ["space 雪", "tab\tpath", "line\npath", "trailing\n", "two\n\n", "literal$()'\\"]
)
def test_real_machine_claim_settlement_preserves_exact_repository(queue, tmp_path, name):
    repo = str(tmp_path / name)
    queue.write_marker(repo, "both", "fast")
    h = queue.marker_hash(repo)
    listed = _cli("list-legs", "--nul")
    assert listed.returncode == 0, listed.stderr
    fields = _fields(listed.stdout)
    assert len(fields) == 12
    assert {fields[4], fields[10]} == {b"cbm", b"gitnexus"}
    for offset in (0, 6):
        assert fields[offset : offset + 4] == [b"1", b"pending", h.encode(), repo.encode()]
        assert fields[offset + 5].isdigit() and int(fields[offset + 5]) >= 0
    claimed = _cli("claim", "--hash", h, "--provider", "gitnexus", "--nul")
    assert claimed.returncode == 0, claimed.stderr
    fields = _fields(claimed.stdout)
    assert len(fields) == 7 and fields[:6] == [
        b"1",
        b"claim",
        h.encode(),
        repo.encode(),
        b"gitnexus",
        b"fast",
    ]
    context = json.loads(fields[6])
    retained = queue.get_inflight(h)
    assert set(context) == queue._legs.CONTEXT
    assert context == {key: retained[key] for key in queue._legs.CONTEXT}
    assert (
        _cli("remember-outcome", "--context", fields[6].decode(), "--action", "consume").returncode
        == 0
    )
    applied = _cli("apply-outcome", "--context", fields[6].decode())
    assert applied.returncode == 0 and applied.stdout.strip() == b"consumed"
    assert [row["provider"] for row in queue.list_markers()] == ["cbm"]


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
def test_unclaimed_machine_predicate_has_complete_matching_acknowledgement(queue, provider):
    queue._prepare()
    h = "0" * 16
    absent = _cli("claim", "--hash", h, "--provider", provider, "--nul")
    assert absent.returncode == 1 and _fields(absent.stdout) == [
        b"1",
        b"unclaimed",
        h.encode(),
        provider.encode(),
    ]
    human = _cli("claim", "--hash", h, "--provider", provider)
    assert human.returncode == 1 and not human.stdout


@pytest.mark.parametrize("due", [False, True])
def test_escalation_machine_false_and_true_both_acknowledge_with_status_zero(queue, tmp_path, due):
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "cbm", "fast")
    h = queue.marker_hash(repo)
    if not due:
        queue.stamp_full(h)
    result = _cli("should-escalate", "--hash", h, "--nul")
    assert result.returncode == 0 and _fields(result.stdout) == [
        b"1",
        b"escalation",
        h.encode(),
        b"1" if due else b"0",
    ], result.stderr
    human = _cli("should-escalate", "--hash", h)
    assert human.returncode == (0 if due else 1) and not human.stdout


@pytest.mark.parametrize("command", ["claim", "should-escalate"])
@pytest.mark.parametrize("h", ["", "A" * 16, "0" * 15, "0" * 17, "../literal", "雪" * 16])
def test_invalid_machine_selector_refuses_before_initialization(queue, command, h):
    args = [command, "--hash", h, "--nul"]
    if command == "claim":
        args.extend(["--provider", "cbm"])
    result = _cli(*args)
    assert result.returncode == 2 and not result.stdout
    assert not queue.marker_dir().exists()


def test_complete_list_validation_precedes_first_output(queue, tmp_path, monkeypatch, capfd):
    queue.write_marker(str(tmp_path / "repo"), "cbm", "fast")
    first = queue.list_markers()[0]
    bad = {**first, "repo_path": str(tmp_path / "another")}
    monkeypatch.setitem(queue.main.__globals__, "list_markers", lambda: [first, bad])
    with pytest.raises((ValueError, RuntimeError)):
        queue.main(["list-legs", "--nul"])
    assert not capfd.readouterr().out


@pytest.mark.parametrize(
    "field,value", [("provider", "both"), ("mode", "unknown"), ("claim_id", "x")]
)
def test_claim_snapshot_is_checked_before_any_machine_output(
    queue, tmp_path, monkeypatch, capfd, field, value
):
    queue.write_marker(str(tmp_path / "repo"), "cbm", "fast")
    h = queue.marker_hash(str(tmp_path / "repo"))
    real = queue.claim(h, provider="cbm")
    bad = {**real, field: value}
    monkeypatch.setitem(queue.main.__globals__, "claim", lambda *args, **kwargs: bad)
    with pytest.raises((ValueError, RuntimeError)):
        queue.main(["claim", "--hash", h, "--provider", "cbm", "--nul"])
    assert not capfd.readouterr().out


@pytest.mark.parametrize(
    "requested,now,age", [(1e100, 100, 0), (-1e100, 100, 2**63 - 1), (-1e308, 1e308, 2**63 - 1)]
)
def test_list_age_clamps_before_integer_conversion_without_rewriting_time(
    queue, tmp_path, monkeypatch, requested, now, age
):
    queue.write_marker(str(tmp_path / "repo"), "cbm", "fast")
    with queue._write_db() as db:
        db.execute("UPDATE pending SET requested_at=?", (requested,))
    monkeypatch.setitem(
        queue.list_markers.__globals__,
        "time",
        SimpleNamespace(time=lambda: now, monotonic=queue.time.monotonic, sleep=queue.time.sleep),
    )
    rows = queue.list_markers()
    assert len(rows) == 1 and rows[0]["age_s"] == age and rows[0]["requested_at"] == requested
    with sqlite3.connect(queue.database_path()) as db:
        assert db.execute("SELECT requested_at FROM pending").fetchone()[0] == requested


def test_complete_machine_list_matches_independent_database_population(queue, tmp_path):
    for name, tools in (("one", "cbm"), ("two\n", "gitnexus"), ("three\t雪", "both")):
        queue.write_marker(str(tmp_path / name), tools, "fast")
    with sqlite3.connect(queue.database_path()) as db:
        expected = set(db.execute("SELECT hash,repo_path,provider FROM pending"))
    result = _cli("list-legs", "--nul")
    assert result.returncode == 0, result.stderr
    fields = _fields(result.stdout)
    assert len(fields) == 6 * len(expected)
    observed = {
        tuple(value.decode() for value in fields[i + 2 : i + 5]) for i in range(0, len(fields), 6)
    }
    assert observed == expected


@pytest.mark.parametrize("failure", ["write", "flush"])
def test_machine_output_failure_propagates_instead_of_acknowledging(queue, monkeypatch, failure):
    class BrokenOutput:
        def write(self, value):
            if failure == "write":
                raise BrokenPipeError("owned broken machine output")
            return len(value)

        def flush(self):
            if failure == "flush":
                raise OSError("owned machine flush failure")

    monkeypatch.setitem(
        queue._write_machine.__globals__,
        "sys",
        SimpleNamespace(stdout=SimpleNamespace(buffer=BrokenOutput())),
    )
    with pytest.raises(OSError, match="owned"):
        queue._write_machine([("1", "unclaimed", "0" * 16, "cbm")])


def test_moved_repository_with_a_symlink_keeps_the_list_and_its_claim_valid(queue, tmp_path):
    """Stored spellings are never re-resolved; one moved repo cannot stall the queue."""
    kept, moved = tmp_path / "kept", tmp_path / "moved"
    kept.mkdir()
    moved.mkdir()
    queue.write_marker(str(kept), "cbm", "fast")
    queue.write_marker(str(moved), "cbm", "fast")
    moved.rename(tmp_path / "elsewhere")
    moved.symlink_to(tmp_path / "elsewhere")
    listed = _cli("list-legs", "--nul")
    assert listed.returncode == 0, listed.stderr
    assert len(_fields(listed.stdout)) == 12
    h = queue.marker_hash(str(kept))
    claimed = _cli(
        "claim", "--hash", queue._legs.stored_hash(str(moved)), "--provider", "cbm", "--nul"
    )
    assert claimed.returncode == 0, claimed.stderr
    context = _fields(claimed.stdout)[6].decode()
    consumed = _cli("consume", "--context", context)
    assert consumed.returncode == 0, consumed.stderr
    assert [row["hash"] for row in queue.list_markers()] == [h]


def test_busy_queue_machine_commands_exit_75_with_no_output(queue, tmp_path):
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "cbm", "fast")
    h = queue.marker_hash(repo)
    holder = sqlite3.connect(queue.database_path(), isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        for args in (
            ["list-legs", "--nul"],
            ["claim", "--hash", h, "--provider", "cbm", "--nul"],
            ["should-escalate", "--hash", h, "--nul"],
        ):
            result = _cli(*args)
            assert result.returncode == 75 and not result.stdout, (args, result)
    finally:
        holder.rollback()
        holder.close()
    assert queue.get_inflight(h) is None


def test_busy_automatic_ready_reports_the_busy_status(queue, tmp_path):
    queue.write_marker(str(tmp_path / "repo"), "cbm", "fast")
    holder = sqlite3.connect(queue.database_path(), isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        result = _cli("automatic-ready")
    finally:
        holder.rollback()
        holder.close()
    assert result.returncode == 75 and not result.stdout, result


def test_repository_moved_before_conversion_keeps_its_stored_spelling(queue, tmp_path):
    """Conversion keeps a stored spelling whose hash matches, never re-resolving it."""
    from tests.test_scripts.test_index_marker_leg_migration import _old_database

    db, hashes = _old_database(queue, tmp_path, 2)
    db.close()
    h, repo = hashes[0]
    (tmp_path / "relocated").mkdir()
    os.symlink(tmp_path / "relocated", repo)
    queue.convert_queue()
    listed = _cli("list-legs", "--nul")
    assert listed.returncode == 0, listed.stderr
    fields = _fields(listed.stdout)
    assert [
        fields[i + 3].decode() for i in range(0, len(fields), 6) if fields[i + 2].decode() == h
    ] == [repo, repo]
    claimed = _cli("claim", "--hash", h, "--provider", "cbm", "--nul")
    assert claimed.returncode == 0, claimed.stderr


def test_claim_context_check_never_consults_the_passed_resolver(queue, tmp_path):
    """The resolver argument is kept for call compatibility only; it is never used."""
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "cbm", "fast")
    context = queue.claim(queue.marker_hash(repo), provider="cbm")["context"]

    def resolver(_repo):
        raise AssertionError("validate_context consulted a live resolver")

    assert queue._legs.validate_context(context, resolver) == context
