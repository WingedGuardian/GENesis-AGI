"""Tests for the shared migration file-discovery (db/_migration_discovery.py).

Extracted from the two migration runners; this exercises it directly (the
structural reviewer flagged it had only transitive coverage).

Fixtures here use REAL frozen legacy filenames rather than invented ones
(``0001_add_update_history.py``, not ``0001_a.py``). That is not incidental
tidiness: discovery now consults the enumerated frozen set, so an invented
legacy name is — correctly — a violation, and a test that used one would be
asserting against a fixture the production contract rejects.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest

from genesis.db._migration_discovery import discover_numbered_modules
from genesis.db._migration_ids import (
    DATA,
    DATA_MIGRATION_PATTERN,
    SCHEMA,
    SCHEMA_MIGRATION_PATTERN,
)

# Real members of the frozen set, used wherever a test needs a legacy file.
FIRST_LEGACY = "0001_add_update_history.py"
SECOND_LEGACY = "0002_add_eval_tables.py"
TENTH_LEGACY = "0010_bitemporal_memory.py"
LAST_LEGACY = "0093_entity_adjudication_approval.py"
FIRST_DATA = "d0001_origin_class_qdrant.py"


def _make(dir_, *names):
    for n in names:
        (dir_ / n).write_text("")


def test_matches_pattern_and_orders_by_id(tmp_path):
    _make(tmp_path, SECOND_LEGACY, FIRST_LEGACY, TENTH_LEGACY)
    out = discover_numbered_modules(tmp_path, SCHEMA)
    assert [mid for mid, _, _ in out] == ["0001", "0002", "0010"]  # zero-pad sort
    assert [stem for _, stem, _ in out] == [
        FIRST_LEGACY[:-3],
        SECOND_LEGACY[:-3],
        TENTH_LEGACY[:-3],
    ]


def test_frozen_legacy_ids_always_precede_timestamp_ids(tmp_path):
    """THE ordering invariant the whole mixed-width scheme rests on.

    Every legacy id starts with '0' and every timestamp id is a real calendar
    year >= TIMESTAMP_EPOCH_YEAR and so starts with '2', which is what a plain
    lexicographic sort needs to keep the frozen history ahead of all new work —
    in Python's `sorted()` here AND in SQLite's TEXT collation on the ledger.
    If this ever fails, a fresh install would replay history in the wrong order.
    """
    _make(
        tmp_path,
        "20260903200000_new.py",
        LAST_LEGACY,
        "20260101000000_earlier.py",
        FIRST_LEGACY,
    )
    out = discover_numbered_modules(tmp_path, SCHEMA)
    ids = [mid for mid, _, _ in out]
    assert ids == ["0001", "0093", "20260101000000", "20260903200000"]

    # The SQLite half, EXERCISED rather than asserted in prose (the docstring
    # used to claim it while the body never touched a database): both ledgers
    # key on TEXT PRIMARY KEY, whose default BINARY collation must agree with
    # Python's codepoint order. Rows go in reversed so a table that happened to
    # return insertion order would fail this.
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE m (id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO m VALUES (?)", [(i,) for i in reversed(ids)])
        assert [r[0] for r in conn.execute("SELECT id FROM m ORDER BY id")] == ids
    finally:
        conn.close()


def test_a_fork_legacy_id_above_2xxx_still_precedes_timestamps(tmp_path):
    """The ordering invariant must hold by CONSTRUCTION, not by leading digit.

    A raw filename sort keeps legacy ahead of timestamps only because the frozen
    set all begins with '0' and timestamps with '2'. A downstream FORK's own
    legacy-width id is runnable (DISALLOWED_LEGACY) and need not begin with '0':
    ``3000_fork_custom.py`` sorts AFTER a ``2026…`` timestamp by leading char, so
    a fresh clone would run it after the timestamp while the fork that authored
    it ran it before — divergent order across installs. discovery bands
    legacy-before-timestamp explicitly so a fork id in the 2xxx–9xxx range is
    ordered with the other legacy history, not scattered into new work.
    """
    _make(tmp_path, FIRST_LEGACY, "3000_fork_custom.py", "20260904120000_upstream.py")
    ids = [mid for mid, _, _ in discover_numbered_modules(tmp_path, SCHEMA)]
    assert ids == ["0001", "3000", "20260904120000"], ids
    # No legacy-width id may appear after any timestamp id.
    from genesis.db._migration_ids import is_valid_timestamp_id

    first_ts = next((i for i, x in enumerate(ids) if is_valid_timestamp_id(x)), len(ids))
    assert not [x for x in ids[first_ts:] if not is_valid_timestamp_id(x)], ids


def test_timestamp_ids_are_discovered_in_both_namespaces(tmp_path):
    """The runners' real patterns must accept the new width — a pattern that
    silently skipped it would mean the migration never runs, with no error."""
    schema_dir = tmp_path / "schema"
    data_dir = tmp_path / "data"
    schema_dir.mkdir()
    data_dir.mkdir()
    _make(schema_dir, "20260903200000_schema.py")
    _make(data_dir, "d20260903200000_data.py")
    assert [m for m, _, _ in discover_numbered_modules(schema_dir, SCHEMA)] == ["20260903200000"]
    assert [m for m, _, _ in discover_numbered_modules(data_dir, DATA)] == ["d20260903200000"]


@pytest.mark.parametrize(
    "bad_name",
    [
        "2026090320000_short.py",  # 13 digits
        "202609032000000_long.py",  # 15 digits
        "00000000000000_zero.py",  # 14 digits, not a calendar timestamp
        "20261301000000_month13.py",  # impossible month
        "20260932000000_day32.py",  # impossible day
        "19990101000000_preepoch.py",  # sorts before the frozen namespace
        "２０２６０９０４００００００_fullwidth.py",
        "d0001_misfiled_data_in_schema_dir.py",
        "dd20260904000000_doubled_prefix.py",  # ^d?\d never saw it — silent skip
        "D20260904000000_capitalised.py",  # same class, capitalised d
        "ｄ２０２６０９０４００００００_fullwidth_prefix.py",  # same IME generator, prefix position
        "20260904000000_case_mangled.PY",  # perfect id, Python never imports it
    ],
)
def test_unrunnable_files_raise_instead_of_being_skipped(tmp_path, bad_name):
    """A file that presents as a migration but cannot run must ABORT discovery.

    The previous version matched a regex and skipped whatever failed, so each
    of these was discovered by nothing and NEVER RAN — and the code that needed
    it deployed against a schema that never got the change. A duplicate id
    already aborts for a strictly smaller reason; a duplicate is at least loud.

    The full-width case is here because it regressed once already: the
    candidate net was written with ASCII ``[0-9]`` "for consistency" with the
    strict pattern, which made a full-width id fail to even PRESENT as a
    migration, so it was classified not-a-migration and the guard reported
    CLEAN — reproducing the exact silence the ASCII restriction was added to
    remove.
    """
    _make(tmp_path, FIRST_LEGACY, bad_name)
    with pytest.raises(RuntimeError) as excinfo:
        discover_numbered_modules(tmp_path, SCHEMA)
    message = str(excinfo.value)
    assert bad_name in message
    # The message has to be actionable, not merely present.
    assert "date -u +%Y%m%d%H%M%S" in message or "FROZEN_LEGACY_FILES" in message


@pytest.mark.parametrize("name", ["0094_fork_custom.py", "0092_gap.py"])
def test_a_disallowed_but_RUNNABLE_legacy_id_still_runs(tmp_path, caplog, name):
    """The runtime enforces RUNNABLE, never this repo's namespace POLICY.

    A 4-digit id outside our frozen set is well-formed, uniquely numbered, sorts
    correctly and applies cleanly — it breaks a rule of ours, not an invariant
    of the installation running it. Discovery sits on the boot-abort path
    (``runtime/init/db.py`` re-raises and ``_core`` stops bootstrap), so
    refusing here would leave a downstream fork carrying its own 4-digit
    migration with NO DATABASE after a pull, over a convention that was the only
    one available before this scheme existed. It runs, and warns; CI refuses it,
    which is where a policy about our namespace can be enforced without reaching
    into anyone else's install.
    """
    _make(tmp_path, FIRST_LEGACY, name)
    with caplog.at_level(logging.WARNING):
        out = discover_numbered_modules(tmp_path, SCHEMA)
    assert [stem for _, stem, _ in out] == [FIRST_LEGACY[:-3], name[:-3]]
    # Runs, but never silently: the warning names the file.
    assert any(name in r.getMessage() for r in caplog.records), caplog.records


def test_the_ci_guard_refuses_what_the_runtime_merely_warns_about(tmp_path):
    """The two layers must DISAGREE in exactly one direction, or the split is
    decorative. Pinned here so a future change cannot quietly align them by
    making the runtime strict again."""
    from genesis.db._migration_ids import DISALLOWED_LEGACY, RUNNABLE, classify

    assert classify(SCHEMA, "0094_fork_custom.py") == DISALLOWED_LEGACY
    assert DISALLOWED_LEGACY in RUNNABLE  # the runtime may execute it
    assert classify(SCHEMA, "2026090320000_short.py") not in RUNNABLE  # it may not


def test_an_unhandled_verdict_is_a_hard_error_not_an_accept(tmp_path, monkeypatch):
    """The residue of the residue.

    Both consumers used to switch on the two NEGATIVE verdicts and accept
    everything else, which is an accept-by-DEFAULT wearing a total-function
    docstring: every verdict added later is silently runnable. Adding
    DISALLOWED_LEGACY would have been accepted by both, in exactly the place the
    module claims that cannot happen.
    """
    import genesis.db._migration_ids as ids_mod

    _make(tmp_path, FIRST_LEGACY)
    monkeypatch.setattr(ids_mod, "classify", lambda ns, name: "a-verdict-from-the-future")
    with pytest.raises(RuntimeError, match="unhandled migration classification"):
        discover_numbered_modules(tmp_path, SCHEMA)


def test_a_valid_directory_does_not_raise(tmp_path):
    """The control for the test above: the same shape, all names legal."""
    _make(tmp_path, FIRST_LEGACY, "20260904120000_new.py")
    assert [m for m, _, _ in discover_numbered_modules(tmp_path, SCHEMA)] == [
        "0001",
        "20260904120000",
    ]


def test_ignores_files_that_never_looked_like_migrations(tmp_path):
    """Non-candidates stay ignored — the residue rule must not swallow the
    runner's own modules, which live in the same directory."""
    _make(
        tmp_path,
        FIRST_LEGACY,
        "README.md",
        "notes.txt",
        "_helper.py",
        "__init__.py",
        "runner.py",
        "stale_embedding_repair.py",
    )
    out = discover_numbered_modules(tmp_path, SCHEMA)
    assert [stem for _, stem, _ in out] == [FIRST_LEGACY[:-3]]


def test_a_migration_named_file_that_is_not_python_is_not_a_migration(tmp_path):
    """``0094_notes.txt`` is not a migration anyone expected to run."""
    _make(tmp_path, FIRST_LEGACY, "0094_notes.txt", "0094_compiled.pyc")
    out = discover_numbered_modules(tmp_path, SCHEMA)
    assert [stem for _, stem, _ in out] == [FIRST_LEGACY[:-3]]


def test_data_migration_pattern_is_distinct():
    """The d-prefixed pattern must NOT accept schema-style names, or vice versa.

    A pattern-level assertion: putting both kinds in one directory is now a
    violation (see the misfiled case above), so this can no longer be shown by
    discovering a mixed directory.
    """
    assert SCHEMA_MIGRATION_PATTERN.match("0001_schema.py")
    assert not SCHEMA_MIGRATION_PATTERN.match("d0001_data.py")
    assert DATA_MIGRATION_PATTERN.match("d0001_data.py")
    assert not DATA_MIGRATION_PATTERN.match("0001_schema.py")
    assert SCHEMA_MIGRATION_PATTERN.match("20260903200000_schema_ts.py")
    assert DATA_MIGRATION_PATTERN.match("d20260903200000_data_ts.py")


def test_the_strict_patterns_accept_only_ASCII_digits():
    """``[0-9]`` rather than ``\\d`` in the id field, asserted where it is claimed.

    ``\\d`` also matches non-ASCII decimal digits, whose codepoints sort nowhere
    near the ASCII ids the ordering invariant is stated over. Reverting the
    patterns to ``\\d`` leaves the whole suite green because
    ``is_valid_timestamp_id`` independently rejects non-ASCII — a correct green
    from a sibling layer, but it means nothing pinned the PATTERN's own
    contract, which is what the runners' discovery reads.
    """
    assert not SCHEMA_MIGRATION_PATTERN.match("２０２６０９０４００００００_x.py")
    assert not DATA_MIGRATION_PATTERN.match("d２０２６０９０４００００００_x.py")
    assert not SCHEMA_MIGRATION_PATTERN.match("２０２６_x.py")
    assert SCHEMA_MIGRATION_PATTERN.match("20260904000000_x.py")


def test_empty_dir_returns_empty(tmp_path):
    assert discover_numbered_modules(tmp_path, SCHEMA) == []


def test_the_real_migrations_directories_are_discoverable():
    """The acceptance case: the SHIPPING tree must survive its own contract.

    A guard that is clean only on fixtures is the failure this whole change
    exists to remove, so discovery runs against the real directories here.
    """
    from genesis.db.data_migrations import runner as data_runner
    from genesis.db.migrations import runner as schema_runner

    schema = discover_numbered_modules(schema_runner._MIGRATIONS_DIR, SCHEMA)
    data = discover_numbered_modules(data_runner._DATA_MIGRATIONS_DIR, DATA)
    assert len(schema) >= 92
    assert len(data) >= 11
    assert [m for m, _, _ in schema] == sorted(m for m, _, _ in schema)
    assert [m for m, _, _ in data] == sorted(m for m, _, _ in data)
