"""The daily retention step for the hook audit stores.

Run as a subprocess, the way ``disk_hygiene.sh`` runs it, because the properties that
matter are process-level: it must never exit non-zero (a failing prune must not skip
the rest of the groom) and it must no-op cleanly on a store that has never been
written.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "prune_hook_audit_logs.py"
_HYGIENE = _REPO / "scripts" / "disk_hygiene.sh"


#: Shell builtins that execute the CONTENT of a file rather than reading it.
_EXEC_WORDS = frozenset({"eval", "source", "."})


def _executes_file_content(line: str) -> frozenset[str]:
    """Which execution builtins appear as COMMAND WORDS in one shell line.

    ONE implementation, used by both the scan of the real loader and the test that
    locks this predicate's own coverage. They were briefly two copies, and the
    duplicate made the lock vacuous: mutating the scan's predicate left the lock
    green because it was checking a different function.

    Tokens via ``shlex``, never string prefixes. A prefix test reads only the first
    word of a line, so ``if source "$path"; then`` walks past it, and it keys on a
    single space, so ``eval\\t"$v"`` does too — both demonstrated against the
    previous version of this check (CodeRabbit, PR #1609). Reaching for a canonical
    tokenizer rather than hand-rolling shell semantics is the house rule, and it
    applies in a test as much as in a guard.
    """
    try:
        tokens = shlex.split(line, comments=True)
    except ValueError:
        # A line shlex cannot read (unbalanced quotes across a continuation) is a
        # line this cannot clear — fail toward FLAGGING, never toward passing.
        tokens = line.replace("\t", " ").split()
    return _EXEC_WORDS.intersection(tokens)


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )


def _seed(d: Path, n: int, size: int = 1000, *, age_s: float = 3600) -> None:
    """Seed a store with n records, BACKDATED by default.

    The mtime is set deliberately, not left at "now": the pruner refuses to
    delete a file touched inside its active-writer grace window, because two
    overlapping flushes mean the earlier writer's file is not the newest NAME and
    was otherwise an ordinary deletion candidate (Codex P2, PR #1609). Fixtures
    that write everything in the same millisecond are the one shape real stores
    never have — a store being trimmed by the daily timer holds files hours old —
    so backdating is what makes these tests model the thing they test. Pass
    ``age_s=0`` to exercise the protection itself.
    """
    d.mkdir(parents=True, exist_ok=True)
    when = time.time() - age_s
    for i in range(n):
        body = json.dumps({"seeded": i, "pad": "x" * max(size - 30, 1)}) + "\n"
        f = d / f"20200101T000000_{i:06d}Z-1-0.jsonl"
        f.write_text(body)
        os.utime(f, (when, when))


def test_an_oversize_store_is_trimmed_to_the_bound(tmp_path):
    store = tmp_path / "store"
    _seed(store, 20, size=1000)
    before = sum(f.stat().st_size for f in store.glob("*.jsonl"))
    r = _run(str(store), "--max-bytes", "5000")
    assert r.returncode == 0, r.stderr
    after = sum(f.stat().st_size for f in store.glob("*.jsonl"))
    assert after <= 5000 < before
    assert "removed" in r.stdout


def test_the_newest_record_survives_a_trim(tmp_path):
    """Restating the writer's invariant at the process level: a live flush is always
    the newest name, so the pruner must never be able to take it."""
    store = tmp_path / "store"
    _seed(store, 20, size=1000)
    newest = sorted(store.glob("*.jsonl"))[-1]
    assert _run(str(store), "--max-bytes", "10").returncode == 0
    assert newest.exists()


def test_a_store_under_the_bound_is_left_alone(tmp_path):
    store = tmp_path / "store"
    _seed(store, 3, size=100)
    before = {f.name: f.stat().st_mtime_ns for f in store.glob("*.jsonl")}
    assert _run(str(store), "--max-bytes", "1000000").returncode == 0
    assert {f.name: f.stat().st_mtime_ns for f in store.glob("*.jsonl")} == before


def test_a_missing_store_is_a_clean_no_op(tmp_path):
    r = _run(str(tmp_path / "never-used"))
    assert r.returncode == 0
    assert "absent" in r.stdout
    assert not (tmp_path / "never-used").exists()


def test_a_file_where_a_store_should_be_reports_and_still_exits_zero(tmp_path):
    """Best-effort is a process-level contract: a broken store must not abort the
    groom, so this reports and exits 0 rather than raising."""
    bogus = tmp_path / "a-file"
    bogus.write_text("not a directory")
    r = _run(str(bogus))
    assert r.returncode == 0
    assert "absent" in r.stdout or "error" in r.stderr


def test_several_stores_are_each_trimmed(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _seed(a, 20, size=1000)
    _seed(b, 20, size=1000)
    assert _run(str(a), str(b), "--max-bytes", "5000").returncode == 0
    for store in (a, b):
        assert sum(f.stat().st_size for f in store.glob("*.jsonl")) <= 5000


def test_disk_hygiene_actually_invokes_the_pruner():
    """Wiring, not behaviour: the step is worthless if the groom never runs it.

    A text assertion because running ``main()`` live would reap worktrees.
    """
    body = _HYGIENE.read_text()
    assert "prune_hook_audit_logs.py" in body, "the groom does not invoke the pruner"
    main_body = body.split("\nmain() {", 1)[-1] if "\nmain() {" in body else body
    assert "prune_hook_audit_logs.py" in main_body, "the invocation is outside main()"


def test_a_recently_written_file_is_not_trimmed(tmp_path):
    """The protection itself. Two overlapping flushes mean the EARLIER writer's
    file is not the newest name — so excluding only `files[-1]` left it an
    ordinary deletion candidate, and unlinking it made that writer return a path
    that no longer exists as a successful write (Codex P2, PR #1609)."""
    store = tmp_path / "store"
    _seed(store, 20, size=1000, age_s=0)  # everything written just now
    r = _run(str(store), "--max-bytes", "10")
    assert r.returncode == 0, r.stderr
    assert len(list(store.glob("*.jsonl"))) == 20, (
        "the pruner deleted files a concurrent flush could still be writing"
    )


def test_old_files_are_still_trimmed_when_a_new_one_exists(tmp_path):
    """CONTROL, and the one that stops the grace window becoming a no-op: a store
    with one fresh file and many old ones must still shrink."""
    store = tmp_path / "store"
    _seed(store, 20, size=1000)  # backdated
    fresh = store / "20990101T000000_000000Z-1-0.jsonl"
    fresh.write_text(json.dumps({"fresh": True}) + "\n")
    r = _run(str(store), "--max-bytes", "5000")
    assert r.returncode == 0, r.stderr
    assert fresh.exists(), "the newest file was taken"
    assert sum(f.stat().st_size for f in store.glob("*.jsonl")) <= 5100


def test_an_unwritable_store_is_reported_not_silently_skipped(tmp_path):
    """A store the pruner cannot write reports "removed 0 byte(s)" — byte-identical
    to "already under bound" — while it grows forever.

    `trim_dir_by_size` skips an unlink it cannot perform, without a word. That is
    reachable in practice, not hypothetical: `resolve_store_dir` honours any
    ABSOLUTE override, and the hygiene unit runs under `ProtectSystem=strict` with
    `ReadWritePaths=%h`, so a store configured outside $HOME is readable and
    unwritable exactly here. It is the same silent-unbounded-store failure this
    prune path exists to end, one layer down.
    """
    store = tmp_path / "store"
    _seed(store, 20, size=1000)
    before = len(list(store.glob("*.jsonl")))
    os.chmod(store, 0o500)  # readable + traversable, not writable
    try:
        r = _run(str(store), "--max-bytes", "10")
    finally:
        os.chmod(store, 0o700)
    assert r.returncode == 0, "a prune failure must never abort the rest of the groom"
    assert "NOT WRITABLE" in r.stderr, f"the failure was silent: {r.stdout!r} {r.stderr!r}"
    assert "removed 0 byte(s)" not in r.stdout, (
        "an unwritable store must not report the same line as a store under its bound"
    )
    assert len(list(store.glob("*.jsonl"))) == before


def test_a_writable_store_still_reports_the_ordinary_line(tmp_path):
    """The control. Without it the assertion above passes against a pruner that
    calls every store unwritable and trims nothing at all."""
    store = tmp_path / "store"
    _seed(store, 20, size=1000)
    r = _run(str(store), "--max-bytes", "5000")
    assert r.returncode == 0, r.stderr
    assert "NOT WRITABLE" not in r.stderr
    assert "removed" in r.stdout
    assert sum(f.stat().st_size for f in store.glob("*.jsonl")) <= 5000


def test_the_hygiene_groom_reads_the_store_knobs_without_executing_secrets_env():
    """The trim needs two variables that live in `secrets.env`, and the unit
    deliberately does NOT load that file.

    MEASURED on systemd 255: an `EnvironmentFile=` overrides `Environment=`
    regardless of the order the directives appear in, so loading secrets.env into
    the hygiene unit would silently replace the gh/git PATH that unit pins — and
    hand every provider key to a oneshot that runs `rm -rf`. The knobs are read by
    name in the script instead, and never by `eval`/`source`, because that file is
    the one place on the box holding every credential.
    """
    unit = (_REPO / "scripts" / "systemd" / "genesis-disk-hygiene.service.template").read_text()
    # DIRECTIVES only. The unit explains in a comment why this is absent, and a bare
    # substring scan matches that explanation — a test that fails on its own
    # rationale teaches the next person to delete the rationale.
    directives = [ln.strip() for ln in unit.splitlines() if not ln.lstrip().startswith("#")]
    assert not any(ln.startswith("EnvironmentFile") for ln in directives), (
        "loading secrets.env here overrides the unit's own pinned PATH (systemd 255)"
    )
    assert any(ln.startswith("Environment=PATH=") for ln in directives), (
        "the pinned PATH this test protects is gone, so the assertion above guards nothing"
    )

    body = _HYGIENE.read_text()
    for knob in ("GENESIS_MERGE_OVERRIDE_DIR", "GENESIS_DISCARD_SNAPSHOT_DIR"):
        assert knob in body, f"the groom never resolves {knob}"
    assert "_load_store_knob() {" in body, (
        "the loader is gone, so the scan below would silently cover the whole file"
    )
    loader = body.split("_load_store_knob() {", 1)[-1].split("\n    }", 1)[0]
    # CODE lines only. Scanning prose too makes any comment containing ". " fail this,
    # which trains the next person to delete the comment rather than keep the property.
    code = [
        ln.strip() for ln in loader.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
    ]
    assert code, "the loader body is all comments — it cannot be doing the work"
    for ln in code:
        hit = _executes_file_content(ln)
        assert not hit, f"the knob loader executes secrets.env content ({sorted(hit)}): {ln!r}"


def test_the_exec_predicate_catches_the_shapes_a_prefix_test_missed():
    """The guard's own guard: lock the bypasses, and the loader's real lines.

    The previous version of the assertion above tested string PREFIXES, so it read
    only the first word of a line and keyed on a single space. Three shapes walked
    through it (CodeRabbit, PR #1609). Locking them here means a future
    simplification of that predicate fails loudly instead of quietly going blind —
    the must-not-fire half matters just as much, since a predicate that flags every
    line would satisfy the must-catch half on its own.

    Calls the SHARED `_executes_file_content`, not a local copy. A first draft
    reimplemented it here, and MEASURED: reverting the real predicate to first-word
    matching left this test green, because it was locking a different function.
    """

    def flags(line: str) -> bool:
        return bool(_executes_file_content(line))

    for line in (
        'if source "$path"; then',
        'eval\t"$value"',
        'if .\t"$path"; then',
        'eval "$(cat "$REPO_DIR/secrets.env")"',
        'source "$REPO_DIR/secrets.env"',
        '. "$REPO_DIR/secrets.env"',
        'x=1; eval "$y"',
    ):
        assert flags(line), f"a bypass shape is not caught: {line!r}"

    for line in (
        'local key="$1"',
        '[ -f "$REPO_DIR/secrets.env" ] || return 0',
        'line="$(grep -aE "^${key}=" "$REPO_DIR/secrets.env" | tail -1)" || line=""',
        'val="${line#*=}"',
        '[ -n "$val" ] && export "$key=$val"',
        "return 0",
    ):
        assert not flags(line), f"an ordinary loader line is flagged: {line!r}"


def test_the_backup_mirror_prune_is_gated_on_a_successful_listing():
    """A failed listing and an empty store must not lead to the same action.

    `find … 2>/dev/null` inside a process substitution discards both the errors and
    the exit status, so a store that cannot be listed produced an EMPTY live-name
    set — indistinguishable from a store with no records. The mirror loop then read
    every mirrored record as deleted upstream and removed the lot: the last
    known-good copies, destroyed by the loop whose own comment says that must not
    happen (CodeRabbit Major, PR #1609).

    A WIRING check, and said plainly rather than dressed up: driving the real
    failure end-to-end needs the full backup harness in test_backup_dr_signal.py
    (git remote, gpg, NAS stubs) for one branch. What it pins is that the listing's
    status is captured and that the destructive loop sits behind it.
    """
    body = (_REPO / "scripts" / "backup.sh").read_text()
    assert "_AUDIT_LISTED=false" in body, "the listing's exit status is not captured"
    section = body.split("Backing up hook audit stores", 1)[-1].split("--- 7.", 1)[0]
    assert section, "the audit-store section could not be located"
    guard = section.split("if ! $_AUDIT_LISTED; then", 1)
    assert len(guard) == 2, "the mirror prune is not gated on a successful listing"
    # The destructive loop must live on the ELSE side of that gate.
    assert "_AUDIT_DROPPED=$(( _AUDIT_DROPPED + 1 ))" in guard[1], (
        "the mirror-delete loop is not inside the successful-listing branch"
    )


def test_the_mirror_reconciliation_is_one_pass_not_one_grep_per_file():
    """Reconciling the mirror against the live store must not rescan per file.

    The loop ran a fresh `grep -qxF` for every mirrored record, each one rescanning
    the entire live-name string. That is quadratic in a store whose whole shape is
    one file per flush — and the advertised 5 MB bound still holds tens of
    thousands of these small records, so the cost arrives exactly at the boundary
    the store is documented to support, delaying the scheduled backup there
    (Codex P2, PR #1609).

    A STRUCTURAL check, in the same spirit as the gating test above and said just
    as plainly: driving the real cost needs the full backup harness for one loop.
    What it pins is that the per-file rescan is gone and a single-pass membership
    set replaced it — the semantics are unchanged, so a behavioural test would pass
    against both shapes and prove nothing about the thing that was wrong.
    """
    body = (_REPO / "scripts" / "backup.sh").read_text()
    section = body.split("Backing up hook audit stores", 1)[-1].split("--- 7.", 1)[0]
    assert section, "the audit-store section could not be located"
    assert 'grep -qxF "$_bbase"' not in section, (
        "the per-file rescan is back: every mirrored record re-greps the whole "
        "live-name string"
    )
    assert "declare -A _AUDIT_LIVE_SET" in section, (
        "no single-pass membership set — reconciliation is not linear"
    )
    assert '${_AUDIT_LIVE_SET[$_bbase]:-}' in section, (
        "the delete decision does not consult the membership set"
    )
