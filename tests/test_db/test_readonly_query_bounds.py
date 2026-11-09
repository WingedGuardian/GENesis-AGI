"""Regression controls for retained-result, output and deadline boundaries."""

import argparse
import contextlib
import io
import json
import sqlite3
import sys
import time
from types import SimpleNamespace

import pytest

from genesis.db import query_cli, readonly_query
from genesis.db.readonly_query import QueryTimeout, run_readonly_query
from genesis.env import genesis_db_path as _real_genesis_db_path


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "home"))
    p = tmp_path / "scratch.db"
    with sqlite3.connect(p) as c:
        c.execute("create table t (n integer)")
        c.execute("insert into t values (7)")
    monkeypatch.setenv("GENESIS_DB_PATH", str(p))
    # The suite globally replaces this resolver; restore it only with owned paths.
    monkeypatch.setattr("genesis.env.genesis_db_path", _real_genesis_db_path)
    return p


def cli(argv):
    parser = argparse.ArgumentParser()
    query_cli.add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["db", "query", *argv])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = args.func(args)
    return code, out.getvalue(), err.getvalue()


def test_positive_read_and_write_refusal(scratch):
    code, out, _ = cli(["--db", str(scratch), "--json", "select n from t"])
    assert code == 0 and json.loads(out) == [{"n": 7}]
    code, _, err = cli(["--db", str(scratch), "delete from t"])
    assert code == 4 and "refused" in err
    with sqlite3.connect(scratch) as c:
        assert c.execute("select n from t").fetchall() == [(7,)]


def test_duplicate_json_names_are_not_silently_overwritten(scratch):
    code, out, err = cli(["--db", str(scratch), "--json", "select 1 as id, 2 as id"])
    assert code == 1 and not out and "duplicate" in err.lower()


@pytest.mark.parametrize("separator", [8232, 8233])
def test_unicode_separators_cannot_create_extra_splitlines_rows(scratch, separator):
    sep = chr(separator)
    code, out, _ = cli(["--db", str(scratch), f'select char({separator}) as "a{sep}b"'])
    assert code == 0
    assert len(out.splitlines()) == 2
    assert sep not in out


def test_explicit_empty_db_does_not_read_the_default(scratch):
    code, out, err = cli(["--db", "", "select n from t"])
    assert code in (1, 2) and not out and err


@pytest.mark.parametrize("use_default", [False, True])
def test_unknown_user_paths_return_a_controlled_error(scratch, monkeypatch, use_default):
    bad = "~genesis_test_nonexistent_user_6f870a95/db"
    if use_default:
        monkeypatch.setenv("GENESIS_DB_PATH", bad)
    args = [] if use_default else ["--db", bad]
    code, out, err = cli([*args, "select 1"])
    assert code == 1 and not out and err and "Traceback" not in err


def test_overlong_path_keeps_the_original_error_boundary(scratch):
    code, out, err = cli(["--db", str(scratch.parent / ("x" * 300)), "select 1"])
    assert code == 1 and not out and err


def test_short_query_deadline_limits_exclusive_lock_wait(scratch, monkeypatch):
    monkeypatch.setenv("GENESIS_DB_BUSY_TIMEOUT_MS", "700")
    writer = sqlite3.connect(scratch)
    try:
        writer.execute("begin exclusive")
        started = time.monotonic()
        with pytest.raises(QueryTimeout):
            run_readonly_query(scratch, "select n from t", timeout_s=0.05)
        assert time.monotonic() - started < 0.5
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.parametrize("value", [None, 7, 1.5, "x" * 512, b"x" * 512])
@pytest.mark.parametrize("max_rows", [200, None])
def test_many_individually_legal_values_exceed_result_budget(scratch, monkeypatch, value, max_rows):
    monkeypatch.setattr(readonly_query, "MAX_VALUE_BYTES", 2048)
    monkeypatch.setattr(readonly_query, "MAX_RESULT_BYTES", 4000, raising=False)
    before = scratch.read_bytes()
    sql = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r WHERE n<40) SELECT ? AS v FROM r"
    with pytest.raises(sqlite3.DataError, match="result.*(budget|limit)"):
        run_readonly_query(scratch, sql, (value,), max_rows=max_rows)
    assert scratch.read_bytes() == before
    assert run_readonly_query(scratch, "select 7").rows == [(7,)]


def test_result_accounting_boundary_and_discarded_lookahead(scratch, monkeypatch):
    baseline = run_readonly_query(scratch, "select 7 as n")
    budget = (
        sys.getsizeof([])
        + sys.getsizeof(baseline.columns)
        + sum(sys.getsizeof(c) for c in baseline.columns)
        + sys.getsizeof([None])
        + sys.getsizeof((7,))
        + sys.getsizeof(7)
    )
    monkeypatch.setattr(readonly_query, "MAX_RESULT_BYTES", budget, raising=False)
    assert run_readonly_query(scratch, "select 7 as n").rows == [(7,)]
    result = run_readonly_query(
        scratch, "select 7 as n union all select zeroblob(1800)", max_rows=1
    )
    assert result.rows == [(7,)] and result.truncated
    monkeypatch.setattr(readonly_query, "MAX_RESULT_BYTES", budget - 1)
    with pytest.raises(sqlite3.DataError, match="result.*(budget|limit)"):
        run_readonly_query(scratch, "select 7 as n")


def test_empty_result_still_charges_its_containers_and_columns(scratch, monkeypatch):
    baseline = run_readonly_query(scratch, "select 7 as n where 0")
    budget = (
        sys.getsizeof([])
        + sys.getsizeof(baseline.columns)
        + sum(sys.getsizeof(c) for c in baseline.columns)
    )
    monkeypatch.setattr(readonly_query, "MAX_RESULT_BYTES", budget, raising=False)
    assert run_readonly_query(scratch, "select 7 as n where 0").rows == []
    monkeypatch.setattr(readonly_query, "MAX_RESULT_BYTES", budget - 1)
    with pytest.raises(sqlite3.DataError, match="result.*(budget|limit)"):
        run_readonly_query(scratch, "select 7 as n where 0")


def test_duplicate_json_empty_result_is_refused_but_tsv_and_case_distinct_are_preserved(scratch):
    code, out, err = cli(["--db", str(scratch), "--json", "select 1 as n, 2 as n where 0"])
    assert code == 1 and not out and "duplicate" in err.lower()
    code, out, _ = cli(["--db", str(scratch), "select 1 as n, 2 as n"])
    assert code == 0 and out == "n\tn\n1\t2\n"
    code, out, _ = cli(["--db", str(scratch), "--json", "select 1 as n, 2 as N"])
    assert code == 0 and json.loads(out) == [{"n": 1, "N": 2}]


@pytest.mark.parametrize("separator", [8232, 8233])
def test_tsv_separator_escaping_preserves_exact_values(scratch, separator):
    sep = chr(separator)
    code, out, _ = cli(["--db", str(scratch), f'select char({separator}) as "a{sep}b"'])
    escape = f"\\u{separator:04x}"
    assert code == 0 and out == f"a{escape}b\n{escape}\n"


def test_json_prepares_one_row_before_converting_later_rows(scratch, monkeypatch):
    events = []
    convert = query_cli._json_value

    def recording_value(value):
        events.append(("convert", value))
        return convert(value)

    class Sink(io.StringIO):
        def write(self, text):
            events.append(("write", text))
            return super().write(text)

    monkeypatch.setattr(query_cli, "_json_value", recording_value)
    parser = argparse.ArgumentParser()
    query_cli.add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        ["db", "query", "--db", str(scratch), "--json", "select 101 as n union all select 202"]
    )
    out = Sink()
    with contextlib.redirect_stdout(out):
        assert args.func(args) == 0
    assert json.loads(out.getvalue()) == [{"n": 101}, {"n": 202}]
    first = events.index(("convert", 101))
    second = events.index(("convert", 202))
    assert any(kind == "write" and "101" in text for kind, text in events[first + 1 : second])


def test_result_budget_failure_has_no_partial_cli_output(scratch, monkeypatch):
    monkeypatch.setattr(readonly_query, "MAX_RESULT_BYTES", 800, raising=False)
    code, out, err = cli(
        ["--db", str(scratch), "--json", "select zeroblob(512) as v union all select zeroblob(512)"]
    )
    assert code == 1 and not out and "result" in err.lower()


@pytest.mark.parametrize(
    "code", [sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY_SNAPSHOT, sqlite3.SQLITE_LOCKED]
)
@pytest.mark.parametrize("expired", [False, True])
def test_only_expired_timeout_categories_become_query_timeout(scratch, monkeypatch, code, expired):
    clock = [100.0]
    err = sqlite3.OperationalError("controlled database condition")
    err.sqlite_errorcode = code
    err.sqlite_errorname = "SQLITE_INTERRUPT" if code == sqlite3.SQLITE_INTERRUPT else "SQLITE_BUSY"
    real_connect = sqlite3.connect
    closed = []

    class Connection:
        def __init__(self, *args, **kwargs):
            self.inner = real_connect(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def execute(self, sql, *args):
            if sql == "select n from t":
                clock[0] += 11 if expired else 0
                raise err
            return self.inner.execute(sql, *args)

        def close(self):
            self.inner.close()
            closed.append(True)

    monkeypatch.setattr(readonly_query, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(
        readonly_query, "sqlite3", SimpleNamespace(**(vars(sqlite3) | {"connect": Connection}))
    )
    if expired:
        with pytest.raises(QueryTimeout):
            run_readonly_query(scratch, "select n from t", timeout_s=10)
    else:
        with pytest.raises(sqlite3.OperationalError) as caught:
            run_readonly_query(scratch, "select n from t", timeout_s=10)
        assert caught.value is err
    assert closed == [True]


def test_busy_policy_shorter_than_deadline_keeps_original_busy(scratch, monkeypatch):
    monkeypatch.setenv("GENESIS_DB_BUSY_TIMEOUT_MS", "10")
    with sqlite3.connect(scratch) as writer:
        writer.execute("begin exclusive")
        try:
            with pytest.raises(sqlite3.OperationalError) as caught:
                run_readonly_query(scratch, "select n from t", timeout_s=5)
            assert not isinstance(caught.value, QueryTimeout)
        finally:
            writer.rollback()


@pytest.mark.parametrize("timeout", [None, 0])
def test_disabled_timeout_preserves_ordinary_reads(scratch, timeout):
    assert run_readonly_query(scratch, "select n from t", timeout_s=timeout).rows == [(7,)]


def test_explicit_path_wins_over_malformed_default(scratch, monkeypatch):
    monkeypatch.setenv("GENESIS_DB_PATH", "~genesis_test_nonexistent_user_6f870a95/db")
    code, out, _ = cli(["--db", str(scratch), "--json", "select n from t"])
    assert code == 0 and json.loads(out) == [{"n": 7}]


def test_omitted_path_uses_owned_default(scratch):
    code, out, _ = cli(["--json", "select n from t"])
    assert code == 0 and json.loads(out) == [{"n": 7}]


@pytest.mark.parametrize("sql", ["delete from t", "select 1; select 2", "select missing from t"])
def test_existing_errors_keep_precedence_after_clock_advances(scratch, monkeypatch, sql):
    clock = [100.0]
    real_connect = sqlite3.connect
    closed = []

    class Connection:
        def __init__(self, *args, **kwargs):
            self.inner = real_connect(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def execute(self, statement, *args):
            try:
                return self.inner.execute(statement, *args)
            finally:
                if statement == sql:
                    clock[0] += 11

        def close(self):
            self.inner.close()
            closed.append(True)

    monkeypatch.setattr(readonly_query, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(
        readonly_query, "sqlite3", SimpleNamespace(**(vars(sqlite3) | {"connect": Connection}))
    )
    with pytest.raises(sqlite3.DatabaseError) as caught:
        run_readonly_query(scratch, sql, timeout_s=10)
    assert not isinstance(caught.value, QueryTimeout)
    if sql.startswith(("delete", "select 1;")):
        assert isinstance(caught.value, readonly_query.ReadOnlyViolation)
    else:
        assert "no such column" in str(caught.value)
    assert closed == [True]


@pytest.mark.parametrize("phase", ["execute", "fetch"])
@pytest.mark.parametrize("has_row", [False, True])
def test_successful_operations_recheck_deadline_even_at_end_of_rows(
    scratch, monkeypatch, phase, has_row
):
    clock = [100.0]
    real_connect = sqlite3.connect
    closed = []
    sql = "select n from t" + ("" if has_row else " where 0")

    class Cursor:
        def __init__(self, cursor):
            self.inner = cursor

        def __getattr__(self, name):
            original = getattr(self.inner, name)
            if name not in {"fetchone", "fetchmany", "fetchall"}:
                return original

            def fetch(*args):
                value = original(*args)
                if phase == "fetch":
                    clock[0] += 11
                return value

            return fetch

    class Connection:
        def __init__(self, *args, **kwargs):
            self.inner = real_connect(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def execute(self, statement, *args):
            cursor = self.inner.execute(statement, *args)
            if statement == sql:
                if phase == "execute":
                    clock[0] += 11
                return Cursor(cursor)
            return cursor

        def close(self):
            self.inner.close()
            closed.append(True)

    monkeypatch.setattr(readonly_query, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(
        readonly_query, "sqlite3", SimpleNamespace(**(vars(sqlite3) | {"connect": Connection}))
    )
    with pytest.raises(QueryTimeout):
        run_readonly_query(scratch, sql, timeout_s=10)
    assert closed == [True]


def test_quarantine_precedes_an_expired_execution_budget(scratch, monkeypatch):
    from genesis.db import integrity

    clock = [100.0]
    admitted = readonly_query.assert_admitted

    def quarantine(path):
        clock[0] += 11
        return admitted(path)

    monkeypatch.setattr(integrity, "database_is_quarantined", lambda _path: True)
    monkeypatch.setattr(readonly_query, "assert_admitted", quarantine)
    monkeypatch.setattr(readonly_query, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with pytest.raises(integrity.DatabaseIntegrityError):
        run_readonly_query(scratch, "select n from t", timeout_s=10)


def test_optional_missing_path_diagnostic_preserves_original_error(scratch, monkeypatch):
    class BrokenDiagnosticPath:
        def __init__(self, _value):
            pass

        def expanduser(self):
            raise RuntimeError("controlled diagnostic failure")

    monkeypatch.setattr(query_cli, "Path", BrokenDiagnosticPath)
    code, out, err = cli(["--db", str(scratch), "select missing from t"])
    assert code == 1 and not out and "no such column" in err
    assert "diagnostic failure" not in err


@pytest.mark.parametrize("expired", [False, True])
def test_connection_construction_timeout_is_classified(scratch, monkeypatch, expired):
    clock = [100.0]
    error = sqlite3.OperationalError("controlled construction busy")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY

    def connect(*args, **kwargs):
        clock[0] += 11 if expired else 0
        raise error

    monkeypatch.setattr(readonly_query, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(
        readonly_query, "sqlite3", SimpleNamespace(**(vars(sqlite3) | {"connect": connect}))
    )
    with pytest.raises(QueryTimeout if expired else sqlite3.OperationalError) as caught:
        run_readonly_query(scratch, "select n from t", timeout_s=10)
    if not expired:
        assert caught.value is error


def test_setup_failure_closes_connection_and_preserves_original(scratch, monkeypatch):
    real_connect = sqlite3.connect
    closed = []
    error = sqlite3.OperationalError("controlled setup failure")

    class Connection:
        def __init__(self, *args, **kwargs):
            self.inner = real_connect(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def setconfig(self, *args):
            raise error

        def close(self):
            self.inner.close()
            closed.append(True)

    monkeypatch.setattr(
        readonly_query, "sqlite3", SimpleNamespace(**(vars(sqlite3) | {"connect": Connection}))
    )
    with pytest.raises(sqlite3.OperationalError) as caught:
        run_readonly_query(scratch, "select n from t")
    assert caught.value is error and closed == [True]
