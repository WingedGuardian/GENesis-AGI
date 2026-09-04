"""The migration-id CI guard (scripts/check_migration_prefixes.py).

Two rules: no duplicate prefix in either namespace, and no NEW hand-allocated
4-digit id — that namespace is frozen to the exact set that existed on
2026-09-03 (0001..0091, d0001..d0011), enforced at BOTH ends plus its size.

The both-ends part is load-bearing and was learned the hard way: a one-sided
"above the freeze mark" rule let ``0000`` through, and ``0000`` is the worst
possible id to miss — legacy-width, duplicating nothing, and ordering-divergent
in the maximal way (on a fresh install it sorts first and runs before ``0001``;
on an existing install every legacy id is already applied, so it runs last).
The size check pins contiguity, so a DELETED legacy migration cannot silently
free its id for re-allocation.

The guard replaced a shell glob+grep that could not survive mixed-width ids in
either direction — MEASURED 2026-09-03: the 4-digit glob made timestamp
migrations invisible to the guard, and widening the glob while keeping
``grep -oP '^\\d{4}'`` truncated every ``20260903…`` id to ``2026``, so two
distinct migrations reported as duplicates of each other.

TWO LEVELS OF TEST HERE, and the second exists because the first cannot see
CI's environment. Most cases call ``check()`` directly against a fake tree.
``test_ci_command_line_*`` instead runs the script the way ci.yml runs it —
bare interpreter, no venv, no PYTHONPATH — because the first version of this
guard path-imported the RUNNER modules (which carry module-scope
``import aiosqlite``) and therefore exited 2 on every PR, enforcing nothing,
while every check()-level test passed inside the project venv. That test is
also the only thing asserting violations reach a NONZERO EXIT: mutating
``return 1`` to ``return 0`` in ``main()`` left the whole suite green, printing
red annotations on a passing job.

Filesystem-only: no network, no DB, no gh.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO / "scripts" / "check_migration_prefixes.py"
_spec = importlib.util.spec_from_file_location("check_migration_prefixes", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["check_migration_prefixes"] = _mod
_spec.loader.exec_module(_mod)


def _fake_repo(schema: list[str], data: list[str], *, tmp_path: Path) -> Path:
    """A minimal tree with both migration dirs and the REAL id-contract module.

    The contract is copied verbatim from the repo so the guard binds to the
    genuine patterns AND the genuine freeze window — a fixture that hand-wrote
    either would be testing the fixture, which is the exact defect this script
    exists to remove.
    """
    for rel in ("src/genesis/db/migrations", "src/genesis/db/data_migrations"):
        (tmp_path / rel).mkdir(parents=True)
    (tmp_path / "src/genesis/db/_migration_ids.py").write_text(
        (_REPO / "src/genesis/db/_migration_ids.py").read_text()
    )
    for name in schema:
        (tmp_path / "src/genesis/db/migrations" / name).write_text("")
    for name in data:
        (tmp_path / "src/genesis/db/data_migrations" / name).write_text("")
    return tmp_path


def _full_legacy(
    schema_extra: list[str] | None = None, data_extra: list[str] | None = None
) -> tuple[list[str], list[str]]:
    """The complete frozen window (0001..0091 / d0001..d0011), plus extras.

    Contiguity is enforced, so a fixture laying down only a couple of legacy
    files is itself a violation — every freeze/duplicate case must start from
    the real window or it would trip the contiguity rule by accident and pass
    for the wrong reason.
    """
    schema = [f"{i:04d}_legacy.py" for i in range(1, 92)] + (schema_extra or [])
    data = [f"d{i:04d}_legacy.py" for i in range(1, 12)] + (data_extra or [])
    return schema, data


def test_real_repo_is_clean():
    """The acceptance baseline: the guard must pass on the ACTUAL tree, or it
    is measuring something other than what ships."""
    assert _mod.check(_REPO) == []


def test_clean_mixed_tree_passes(tmp_path):
    repo = _fake_repo(
        *_full_legacy(["20260903200000_new.py"], ["d20260903200000_new.py"]),
        tmp_path=tmp_path,
    )
    assert _mod.check(repo) == []


def test_duplicate_timestamp_prefix_is_caught(tmp_path):
    """The residual the freeze cannot remove: two authors CAN hit the same
    second. Rare, but both runners still raise on it, so the guard still checks."""
    repo = _fake_repo(
        *_full_legacy(["20260903200000_a.py", "20260903200000_b.py"]), tmp_path=tmp_path
    )
    out = _mod.check(repo)
    assert any("duplicate prefix '20260903200000'" in v for v in out)


def test_duplicate_legacy_prefix_still_caught(tmp_path):
    schema, data = _full_legacy()
    repo = _fake_repo(schema + ["0001_duplicate.py"], data, tmp_path=tmp_path)
    out = _mod.check(repo)
    assert any("duplicate prefix '0001'" in v for v in out)


def test_a_NEW_legacy_prefix_ABOVE_the_window_is_refused(tmp_path):
    """The freeze rule — the whole point. 0092 is the id a human would
    hand-allocate next, and allocating is what made two branches collide."""
    repo = _fake_repo(*_full_legacy(["0092_hand_allocated.py"]), tmp_path=tmp_path)
    out = _mod.check(repo)
    freeze = [v for v in out if "allocates a NEW legacy-width id" in v]
    assert any("0092" in v for v in freeze)
    assert any("date -u +%Y%m%d%H%M%S" in v for v in freeze)  # names the remedy


def test_a_NEW_legacy_prefix_BELOW_the_window_is_refused(tmp_path):
    """0000 is legacy-width, duplicates nothing, and slipped a one-sided
    `prefix > mark` rule. It is also the maximally order-divergent id: first on
    a fresh install, last on an existing one."""
    repo = _fake_repo(*_full_legacy(["0000_hand_allocated.py"]), tmp_path=tmp_path)
    out = _mod.check(repo)
    # The FREEZE-WINDOW arm specifically. Asserting on "0000" + "frozen" alone
    # passed under a one-sided mutation, because the contiguity message names
    # both too — a test that fires for the wrong reason is not a lock.
    assert any(
        "0000" in v and "allocates a NEW legacy-width id" in v for v in out
    )


def test_a_NEW_legacy_data_prefix_is_refused(tmp_path):
    repo = _fake_repo(*_full_legacy(data_extra=["d0012_new.py"]), tmp_path=tmp_path)
    out = _mod.check(repo)
    assert any("d0012" in v and "allocates a NEW legacy-width id" in v for v in out)


def test_a_NEW_legacy_data_prefix_below_the_window_is_refused(tmp_path):
    repo = _fake_repo(*_full_legacy(data_extra=["d0000_hand.py"]), tmp_path=tmp_path)
    out = _mod.check(repo)
    assert any("d0000" in v and "allocates a NEW legacy-width id" in v for v in out)


def test_a_DELETED_legacy_migration_is_caught(tmp_path):
    """Deleting a legacy file frees its id for silent re-allocation — the one
    way the frozen set can change without anyone deciding to change it."""
    schema, data = _full_legacy()
    repo = _fake_repo([s for s in schema if not s.startswith("0050")], data, tmp_path=tmp_path)
    out = _mod.check(repo)
    # The CONTIGUITY arm specifically — the mirror of the freeze-window tests.
    assert any("should hold exactly 91" in v for v in out)
    assert not any("allocates a NEW legacy-width id" in v for v in out)


def test_existing_legacy_ids_are_not_flagged(tmp_path):
    """The freeze must not fire on the ids that already exist — a guard that
    reddens the untouched tree gets disabled by whoever hits it next."""
    repo = _fake_repo(*_full_legacy(), tmp_path=tmp_path)
    assert _mod.check(repo) == []


def test_an_empty_directory_is_an_anomaly_not_a_pass(tmp_path):
    """91 migrations exist; a scan finding none did not describe the directory.
    Reporting CLEAN there is the seen-nothing-so-no-problem failure."""
    _, data = _full_legacy()
    repo = _fake_repo([], data, tmp_path=tmp_path)
    out = _mod.check(repo)
    assert any("no files matched" in v for v in out)


def test_a_missing_directory_fails_closed_not_clean(tmp_path):
    """The id contract lives OUTSIDE the migration dirs now, so a missing
    directory no longer fails at load — it must be a violation, never a scan
    that quietly examined nothing and reported clean."""
    schema, data = _full_legacy()
    repo = _fake_repo(schema, data, tmp_path=tmp_path)
    for f in (repo / "src/genesis/db/data_migrations").iterdir():
        f.unlink()
    (repo / "src/genesis/db/data_migrations").rmdir()
    out = _mod.check(repo)
    assert any("directory not found" in v and "nothing was scanned" in v for v in out)


def test_plumbing_failure_exits_2_never_0(tmp_path, monkeypatch, capsys):
    """A guard that cannot run must not report clean. Exit 2 is distinct from
    exit 1 (real violations) so CI can tell them apart."""
    monkeypatch.setattr(sys, "argv", ["check", "--repo-root", str(tmp_path / "nope")])
    assert _mod.main() == 2
    assert "could not run" in capsys.readouterr().err


def test_clean_run_exits_0_and_says_so(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check", "--repo-root", str(_REPO)])
    assert _mod.main() == 0
    assert "CLEAN" in capsys.readouterr().out


def _run_as_ci(repo_root: Path) -> subprocess.CompletedProcess:
    """Run the script EXACTLY as ci.yml does: a bare interpreter with no venv,
    no PYTHONPATH, and nothing pip-installed."""
    return subprocess.run(
        [sys.executable, "-E", "-S", str(_SCRIPT), "--repo-root", str(repo_root)],
        capture_output=True,
        text=True,
        cwd=str(_REPO),
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(repo_root)},
    )


def test_ci_command_line_runs_at_all_on_the_real_tree():
    """BLOCKER regression: the first version of this guard bound to the runner
    modules, which import aiosqlite at module scope, so it exited 2 on every PR
    in a job that installs nothing — while every check()-level test passed
    inside the venv. The id contract is stdlib-only so this stays true."""
    proc = _run_as_ci(_REPO)
    assert proc.returncode == 0, f"exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert "CLEAN" in proc.stdout


def test_ci_command_line_exits_NONZERO_on_a_violation(tmp_path):
    """The block/pass wiring, which no check()-level test can reach: mutating
    main()'s `return 1` to `return 0` printed every ::error:: annotation and
    still exited 0 — red annotations on a green job, the most convincing form
    of reporting clean when it is not."""
    repo = _fake_repo(*_full_legacy(["0092_hand_allocated.py"]), tmp_path=tmp_path)
    proc = _run_as_ci(repo)
    assert proc.returncode == 1, f"exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert "0092" in proc.stdout and "frozen" in proc.stdout
