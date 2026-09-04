"""The migration-id CI guard (scripts/check_migration_prefixes.py).

Three rules: every file in a migrations directory classifies as a legal
migration (the residue is a violation), no duplicate id in either namespace,
and every frozen legacy file is still present under its own name.

The guard used to check properties of the id SET — no duplicates, min/max inside
a window — and four independent review findings came out of that one choice,
because a set-property is blind in two directions. It could not see a file the
pattern failed to match (a 13-digit id was skipped in silence, in the guard AND
in discovery, so the migration NEVER RAN), and it was invariant under mutations
that preserve the endpoints (``0092`` sits inside ``0001..0093`` yet does not
exist, so a hand-allocated ``0092_new.py`` passed; renaming a frozen file left
the id set identical; deleting every legacy file left the legacy loop empty,
which read as clean). Stating the contract as a total function over filenames is
what closes all four, and closes the ones nobody has thought of yet.

Fixtures bind to the REAL frozen set via the contract module rather than
generating plausible names, because a generated ``0001_legacy.py`` is not a
frozen filename and the guard is right to reject it — a fixture that hand-wrote
the set would be testing the fixture.

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

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO / "scripts" / "check_migration_prefixes.py"
_spec = importlib.util.spec_from_file_location("check_migration_prefixes", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["check_migration_prefixes"] = _mod
_spec.loader.exec_module(_mod)

_IDS = _mod._load_id_contract(_REPO)


def _fake_repo(schema: list[str], data: list[str], *, tmp_path: Path) -> Path:
    """A minimal tree with both migration dirs and the REAL id-contract module.

    The contract is copied verbatim from the repo so the guard binds to the
    genuine patterns AND the genuine frozen set — a fixture that hand-wrote
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
    """The frozen legacy files as they ACTUALLY exist, plus extras.

    Read from the contract, not generated: the real schema set runs 0001..0091
    and then 0093 — a GAP at 0092, left by a rename — and it was exactly that
    gap that a range check could not express, admitting a brand-new
    hand-allocated ``0092_new.py``.
    """
    schema = sorted(_IDS.FROZEN_LEGACY_FILES[_IDS.SCHEMA])
    data = sorted(_IDS.FROZEN_LEGACY_FILES[_IDS.DATA])
    return schema + (schema_extra or []), data + (data_extra or [])


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


def test_duplicate_timestamp_id_is_caught(tmp_path):
    """The residual the freeze cannot remove: two authors CAN hit the same
    second. Rare, but both runners still raise on it, so the guard still checks."""
    repo = _fake_repo(
        *_full_legacy(["20260903200000_a.py", "20260903200000_b.py"]),
        tmp_path=tmp_path,
    )
    out = _mod.check(repo)
    assert any("duplicate id '20260903200000'" in v for v in out)


def test_a_second_file_claiming_a_frozen_id_is_caught(tmp_path):
    """The legacy half of "duplicate id", which the freeze now subsumes.

    Two files can no longer share a LEGACY id without one of them being outside
    the frozen set — the set holds one filename per id — so this is reported as
    an illegal legacy id rather than reaching the duplicate arm. The duplicate
    arm still matters for TIMESTAMP ids, where two authors really can collide
    (covered above); this test pins that the file is refused either way, which
    is the property that actually protects the tree.
    """
    schema, data = _full_legacy()
    repo = _fake_repo(schema + ["0001_duplicate.py"], data, tmp_path=tmp_path)
    out = _mod.check(repo)
    assert any("0001_duplicate.py" in v for v in out), out


@pytest.mark.parametrize(
    "bad",
    [
        "0094_hand_allocated.py",  # the id a human would allocate next
        "0000_hand_allocated.py",  # below the old window; maximally order-divergent
        "0092_gap.py",  # INSIDE the old lo..hi range, absent from the set
    ],
)
def test_a_new_hand_allocated_legacy_id_is_refused(tmp_path, bad):
    """The freeze rule — the whole point. Allocating is what made two branches
    collide, so no new legacy-width id is admissible in any position.

    ``0092`` is the case a range check structurally could not catch: it lies
    between the endpoints, so only the enumerated set can tell it is not real.
    """
    repo = _fake_repo(*_full_legacy([bad]), tmp_path=tmp_path)
    out = _mod.check(repo)
    freeze = [v for v in out if "allocates the legacy-width id" in v]
    assert any(bad in v for v in freeze), out
    assert any("date -u +%Y%m%d%H%M%S" in v for v in freeze)  # names the remedy


@pytest.mark.parametrize("bad", ["d0012_new.py", "d0000_hand.py"])
def test_a_new_hand_allocated_legacy_data_id_is_refused(tmp_path, bad):
    repo = _fake_repo(*_full_legacy(data_extra=[bad]), tmp_path=tmp_path)
    out = _mod.check(repo)
    assert any(bad in v and "allocates the legacy-width id" in v for v in out)


@pytest.mark.parametrize(
    "bad",
    [
        "2026090320000_short.py",  # 13 digits
        "202609032000000_long.py",  # 15 digits
        "00000000000000_zero.py",  # 14 digits, not a calendar timestamp
        "20261301000000_month13.py",
        "20260932000000_day32.py",
        "19990101000000_preepoch.py",  # would sort before the frozen namespace
        "２０２６０９０４００００００_fullwidth.py",
        "d0001_misfiled_data_in_schema_dir.py",
    ],
)
def test_a_file_that_cannot_run_is_a_violation_not_a_skip(tmp_path, bad):
    """The finding the set-shaped guard could not see at all.

    Each of these was previously skipped by ``if m:`` — in the guard AND in
    discovery — so the migration was found by nothing, never ran, and the code
    needing it deployed against a schema that never got the change. Silent, and
    indistinguishable from a clean tree.
    """
    repo = _fake_repo(*_full_legacy([bad]), tmp_path=tmp_path)
    out = _mod.check(repo)
    assert any(bad in v for v in out), out


def test_deleting_a_frozen_migration_is_caught(tmp_path):
    """An id-set check cannot see this: min, max and count are all unchanged.

    The COUNT is asserted, not just the victim's presence in the message.
    Mutating the guard so it never records what it saw makes it report all 92
    frozen files as missing — which still contains the victim's name, so an
    ``assert victim in message`` passes while the guard has gone completely
    blind. A test that cannot tell one missing file from ninety-two is not
    locking the behaviour it claims to.
    """
    schema, data = _full_legacy()
    victim = "0051_entity_layer.py"
    repo = _fake_repo([s for s in schema if s != victim], data, tmp_path=tmp_path)
    out = _mod.check(repo)
    missing = [v for v in out if "frozen legacy file(s) missing" in v]
    assert len(missing) == 1, out
    assert "1 frozen legacy file(s) missing" in missing[0], missing
    assert victim in missing[0], missing
    # A file that is still present must not be named.
    assert "0050_canonicalize_bitemporal_ts.py" not in missing[0], missing


def test_renaming_a_frozen_migration_is_caught(tmp_path):
    """``0050_old.py`` -> ``0050_new.py`` leaves the id set IDENTICAL, while
    installs that already applied ``0050`` skip the replacement forever and
    fresh installs run it — two schemas, diverging, with nothing to notice."""
    schema, data = _full_legacy()
    victim = "0050_canonicalize_bitemporal_ts.py"
    renamed = [s for s in schema if s != victim] + ["0050_renamed.py"]
    repo = _fake_repo(renamed, data, tmp_path=tmp_path)
    out = _mod.check(repo)
    assert any(victim in v and "missing" in v for v in out), out
    assert any("0050_renamed.py" in v for v in out), out


def test_deleting_every_legacy_migration_is_caught(tmp_path):
    """The empty-legacy hole: with one timestamp migration present the id map is
    non-empty, so the legacy loop simply had nothing to iterate and reported
    clean."""
    _, data = _full_legacy()
    repo = _fake_repo(["20260904120000_only.py"], data, tmp_path=tmp_path)
    out = _mod.check(repo)
    assert any("frozen legacy file(s) missing" in v for v in out), out


def test_the_frozen_legacy_set_is_closed(tmp_path):
    """The set's SIZE is pinned, so growing it cannot be a quiet side effect.

    This is a speed bump, not enforcement, and the distinction is worth stating.
    The guard reads FROZEN_LEGACY_FILES from the commit under test — it takes no
    base ref and shells out to no git — so a single PR can add ``0094_x.py`` AND
    add ``"0094_x.py"`` to the set, and every rule passes. Only an out-of-repo
    reference (a merge-base diff, or a required human review) could actually
    close it.

    A merge-base diff was considered and declined: it needs the base ref in CI
    (``fetch-depth: 0``) and would mean exec'ing a source file from another ref.
    An unmet CI dependency is the exact bug that already bit this guard once —
    it path-imported aiosqlite-bearing modules and exited 2 on every PR while
    every test passed inside the venv — so buying enforcement with a new CI
    dependency is a poor trade here.

    What this DOES buy: the addition can no longer be silent. Anyone growing the
    set must also change a number in a test whose name says the set is closed,
    which is a deliberate act with a message attached at the moment it matters.
    The error text deliberately does not advertise the set as a remedy either.

    If you are here because this test failed: you are hand-allocating a
    migration id, which is the thing this whole scheme removed. Use
    ``date -u +%Y%m%d%H%M%S`` instead. The only legitimate reason to proceed is
    a legacy-id migration that was authored before this scheme and has already
    merged to the default branch.
    """
    assert len(_IDS.FROZEN_LEGACY_FILES[_IDS.SCHEMA]) == 92
    assert len(_IDS.FROZEN_LEGACY_FILES[_IDS.DATA]) == 11


def test_a_future_timestamp_id_is_refused(tmp_path):
    """The ordering invariant's ceiling — the side nobody looked at.

    The floor is guarded in the contract because a year >= the epoch forces a
    leading '2'. Nothing bounded the top, and the typo that reaches it is more
    likely than the widths that WERE caught: one wrong digit turns 2026 into
    2926, a perfectly valid id that sorts after every migration anyone writes
    for the next nine centuries — silent in discovery, in the guard, and in the
    runner.
    """
    repo = _fake_repo(*_full_legacy(["29260903200000_typo_year.py"]), tmp_path=tmp_path)
    out = _mod.check(repo)
    assert any("FUTURE timestamp id" in v and "29260903200000" in v for v in out), out


def test_a_recent_past_timestamp_is_not_flagged_as_future(tmp_path):
    """The control: the ceiling must not redden ordinary work. A guard that
    fires on a normal migration gets disabled by whoever hits it next."""
    repo = _fake_repo(*_full_legacy(["20200101000000_old_but_fine.py"]), tmp_path=tmp_path)
    assert _mod.check(repo) == []


def test_a_migration_in_a_subdirectory_is_caught(tmp_path):
    """Discovery is flat (importlib resolves a flat package), so a nested file
    is discovered by nothing — the same never-runs silence as a mistyped width,
    in the one shape a flat scan structurally cannot see."""
    schema, data = _full_legacy()
    repo = _fake_repo(schema, data, tmp_path=tmp_path)
    nested = repo / "src/genesis/db/migrations/helpers"
    nested.mkdir()
    (nested / "20260904120000_buried.py").write_text("")
    out = _mod.check(repo)
    assert any("subdirectory" in v and "20260904120000_buried.py" in v for v in out), out


def test_existing_legacy_ids_are_not_flagged(tmp_path):
    """The freeze must not fire on the ids that already exist — a guard that
    reddens the untouched tree gets disabled by whoever hits it next."""
    repo = _fake_repo(*_full_legacy(), tmp_path=tmp_path)
    assert _mod.check(repo) == []


def test_the_runners_own_modules_are_not_migrations(tmp_path):
    """The residue rule must not swallow files that never presented as
    migrations — they share the directory with every migration."""
    schema, data = _full_legacy()
    repo = _fake_repo(
        schema + ["__init__.py", "runner.py", "__main__.py"],
        data + ["__init__.py", "runner.py", "_util.py", "stale_embedding_repair.py"],
        tmp_path=tmp_path,
    )
    assert _mod.check(repo) == []


def test_an_empty_directory_is_an_anomaly_not_a_pass(tmp_path):
    """92 migrations exist; a scan finding none did not describe the directory.
    Reporting CLEAN there is the seen-nothing-so-no-problem failure."""
    _, data = _full_legacy()
    repo = _fake_repo([], data, tmp_path=tmp_path)
    out = _mod.check(repo)
    assert any("no runnable migration found" in v for v in out), out


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
    inside the venv. The id contract is stdlib-only so this stays true, and
    ``datetime`` (added for timestamp validation) keeps it true."""
    proc = _run_as_ci(_REPO)
    assert proc.returncode == 0, f"exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert "CLEAN" in proc.stdout


def test_ci_command_line_exits_NONZERO_on_a_violation(tmp_path):
    """The block/pass wiring, which no check()-level test can reach: mutating
    main()'s `return 1` to `return 0` printed every ::error:: annotation and
    still exited 0 — red annotations on a green job, the most convincing form
    of reporting clean when it is not."""
    repo = _fake_repo(*_full_legacy(["0094_hand_allocated.py"]), tmp_path=tmp_path)
    proc = _run_as_ci(repo)
    assert proc.returncode == 1, f"exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert "0094" in proc.stdout and "frozen" in proc.stdout


# --- Immutability against what is ALREADY MERGED (Codex P1, PR #1678) -------
# FROZEN_LEGACY_FILES is a hand-maintained snapshot of one closed set. It cannot
# grow to cover timestamp migrations — they are unbounded — so the moment the
# first one merges, a later PR can delete it or rename it to another perfectly
# valid timestamp and every other rule still reports CLEAN. "Already merged" is
# a fact only git holds, so the guard asks git.


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )
    return out.stdout


def _committed_repo(tmp_path: Path, *, extra_schema: list[str] | None = None) -> tuple[Path, str]:
    """A fake repo whose CURRENT tree is committed. Returns (repo, base_sha)."""
    schema, data = _full_legacy(extra_schema or [])
    repo = _fake_repo(schema, data, tmp_path=tmp_path)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo, _git(repo, "rev-parse", "HEAD").strip()


# A real, well-formed timestamp id — the namespace the enumerated list can
# never cover, which is the whole point of the finding.
_MERGED_TS = "20260101000000_add_a_column.py"


def test_deleting_an_already_merged_timestamp_migration_is_caught(tmp_path):
    """THE acceptance replay, and it shows the hole and the fix in one run.

    Nothing in the enumerated frozen set mentions this file — the set is closed
    and covers only legacy ids — so EVERY other rule reports clean while existing
    installs keep its id in their ledger and skip the replacement forever. The
    first assertion pins that: without a base there is nothing to notice with,
    which is the state the finding describes rather than a bug in this test. The
    second is what the base buys.
    """
    repo, base = _committed_repo(tmp_path, extra_schema=[_MERGED_TS])
    (repo / "src/genesis/db/migrations" / _MERGED_TS).unlink()

    assert _mod.check(repo) == [], (
        "the deletion is invisible without a base — if this ever fails, some "
        "other rule now covers it and this test is no longer measuring the base"
    )
    violations = _mod.check(repo, base_ref=base)
    assert any(_MERGED_TS in v and "already-merged" in v for v in violations), violations


def test_renaming_an_already_merged_timestamp_migration_is_caught(tmp_path):
    """A rename is a delete plus an add, so it needs no rename detection to be
    caught — and it is the more dangerous shape, because the id set still has
    the same size and the body still exists to read."""
    repo, base = _committed_repo(tmp_path, extra_schema=[_MERGED_TS])
    d = repo / "src/genesis/db/migrations"
    (d / _MERGED_TS).rename(d / "20260101000000_add_a_column_v2.py")

    violations = _mod.check(repo, base_ref=base)
    assert any(_MERGED_TS in v for v in violations), violations


def test_an_unchanged_tree_is_clean_against_its_own_base(tmp_path):
    """CONTROL. The rule must not fire on the ordinary case, or it fails every
    PR and gets switched off."""
    repo, base = _committed_repo(tmp_path, extra_schema=[_MERGED_TS])
    assert _mod.check(repo, base_ref=base) == []


def test_adding_a_new_timestamp_migration_is_clean(tmp_path):
    """CONTROL, and the one that matters most: the rule freezes what exists, it
    does not freeze the directory."""
    repo, base = _committed_repo(tmp_path, extra_schema=[_MERGED_TS])
    (repo / "src/genesis/db/migrations" / "20260202000000_new_work.py").write_text("")
    assert _mod.check(repo, base_ref=base) == []


def test_a_malformed_file_on_the_base_may_be_removed(tmp_path):
    """Deleting a file that never RAN is a fix, not a regression — a 13-digit id
    is discovered by nothing, so no install has it in a ledger. Filtering the
    base list through the same classify() keeps the rule from defending files it
    should be reporting."""
    repo, base = _committed_repo(tmp_path)
    d = repo / "src/genesis/db/migrations"
    (d / "2026010100000_short.py").write_text("")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add a malformed file")
    base2 = _git(repo, "rev-parse", "HEAD").strip()
    (d / "2026010100000_short.py").unlink()

    assert _mod.check(repo, base_ref=base2) == []


def test_an_unreadable_explicit_base_is_a_violation_not_a_pass(tmp_path):
    """A guard TOLD to compare, that then compares nothing, is the silent pass
    this whole script exists to remove — a shallow CI checkout would produce
    exactly this and read as clean."""
    repo, _ = _committed_repo(tmp_path)
    violations = _mod.check(repo, base_ref="no-such-ref-ffffffff")
    assert violations and all("could not be read" in v for v in violations), violations


def test_no_base_at_all_degrades_instead_of_failing(tmp_path):
    """CONTROL on the fail direction. A fork running this from a tarball has no
    base to compare against and must not be failed for it — the rule is skipped,
    every other rule still applies."""
    schema, data = _full_legacy()
    repo = _fake_repo(schema, data, tmp_path=tmp_path)  # never git-init'ed
    assert _mod.check(repo) == []


# --- The ID is ASCII; the DESCRIPTION is not (Codex P2, PR #1678) -----------


@pytest.mark.parametrize("name", ["0094_café.py", "20260101000000_修復.py"])
def test_a_unicode_description_is_a_legal_migration(tmp_path, name):
    """Only the id participates in ordering and uniqueness, so only the id needs
    a codepoint guarantee. Restricting the description too was collateral, and
    it broke names that import fine today: a fork carrying one would have had
    discovery start RAISING — aborting database init on the schema side — over a
    naming preference of ours.

    `0094_café.py` is a hand-allocated legacy id, so the guard still refuses it
    as a POLICY violation; what must not happen is MALFORMED, which means "no
    runner will ever discover this".
    """
    assert _IDS.classify(_IDS.SCHEMA, name) != _IDS.MALFORMED


def test_a_unicode_digit_in_the_ID_is_still_malformed(tmp_path):
    """The control on the other side, and the reason the two halves differ: a
    full-width digit sorts nowhere near the ASCII ids the ordering invariant is
    stated over, so it must stay a violation."""
    assert _IDS.classify(_IDS.SCHEMA, "２０２６０１０１０００００_x.py") == _IDS.MALFORMED
