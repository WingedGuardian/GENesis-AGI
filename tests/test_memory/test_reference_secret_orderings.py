"""Credential pairs under concurrency (#2426): every ordering of two writers'
statements, a writer stopped or cancelled at each await point, a delete or the
orphan sweep racing a write, and the secret_ref triggers. Split from
test_reference_secrets.py (the per-file cap); the helpers live there.

Fake credentials only, never real secrets.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import knowledge as kc
from genesis.db.schema import create_all_tables
from tests.test_memory.test_reference_secrets import (
    _VALUE,
    _body,
    _credential_units_without_a_secret,
    _ingest,
    _secret,
    _secret_rows,
    _store,
)


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


class _Conn:
    """A connection whose ``hook`` runs before each statement it matches, to
    stand in for what another coroutine does on the shared connection between
    two of ours."""

    def __init__(self, conn, prefix: str, hook):
        self._conn, self._prefix, self._hook, self.fired = conn, prefix, hook, 0

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *args, **kwargs):
        # Returned unawaited, as aiosqlite's own execute is, so the callers that
        # use it as a context manager still can.
        if not sql.lstrip().startswith(self._prefix) or self.fired:
            return self._conn.execute(sql, *args, **kwargs)
        self.fired += 1

        async def _hooked():
            await self._hook(self._conn)
            return await self._conn.execute(sql, *args, **kwargs)

        return _hooked()




@pytest.mark.parametrize("existing", [False, True])
async def test_a_stop_between_the_secret_and_the_body_leaves_no_body_without_its_secret(
    db,
    existing,
):
    """R1: a cancellation before ANY write of the upsert (enumerated: the first,
    the second, ... the last), then a commit by another writer, never leaves a
    credential body without its secret."""
    import asyncio

    if existing:
        await _ingest(db, _store(), _body())
    new_value = "user: example-admin, password: Next!Fake-4"

    class _CancelAt:
        def __init__(self, conn, n):
            self._conn, self._n, self.writes = conn, n, 0

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, sql, *args, **kwargs):
            if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
                self.writes += 1
                if self.writes == self._n:

                    async def _stop():
                        raise asyncio.CancelledError

                    return _stop()
            return self._conn.execute(sql, *args, **kwargs)

    async def _run(conn):
        await kc.upsert(
            conn,
            project_type="reference",
            domain="reference.credentials",
            source_doc="x",
            concept="Lab router admin",
            body=_body().replace(f"Value: {_VALUE}", f"Value: {kc.SECRET_PLACEHOLDER}"),
            secret_value=new_value,
            _commit=False,
        )

    stopped = 0
    for n in range(1, 10):
        conn = _CancelAt(db, n)
        try:
            await _run(conn)
        except asyncio.CancelledError:
            stopped += 1
        await db.commit()  # another coroutine's commit on the shared connection
        assert await _credential_units_without_a_secret(db) == [], f"stopped at write {n}"
        if conn.writes < n:
            break  # this run had fewer writes than n: every stop point was covered
        # Start each stop point from the same state.
        await db.execute("DELETE FROM knowledge_units WHERE concept = 'Lab router admin'")
        await db.execute("DELETE FROM reference_secrets")
        await db.execute("DELETE FROM knowledge_fts")
        if existing:
            await _ingest(db, _store(), _body())
        await db.commit()
    assert stopped >= 3  # the secret, the body and its FTS row were each a stop point


async def test_a_concurrent_insert_of_the_same_key_gets_the_secret_too(db):
    """R1: another writer inserts the key between the check and the write. The
    body lands on ITS row in the same statement that repoints it to this
    writer's secret, and the other writer's secret is retired."""
    held = _body().replace(f"Value: {_VALUE}", f"Value: {kc.SECRET_PLACEHOLDER}")

    async def _race(conn):
        await conn.execute(
            "INSERT INTO reference_secrets (id, value, created_at) VALUES ('w', 'other-Fake1', 'now')"
        )
        await conn.execute(
            "INSERT INTO knowledge_units (id, project_type, domain, source_doc, concept, body, "
            "ingested_at, secret_ref) VALUES ('winner', 'reference', 'reference.credentials', 'x', "
            "'Lab router admin', ?, 'now', 'w')",
            (held,),
        )

    wrapped = _Conn(db, "INSERT INTO knowledge_units", _race)
    actual, inserted = await kc.upsert(
        wrapped,
        id="loser",
        project_type="reference",
        domain="reference.credentials",
        source_doc="x",
        concept="Lab router admin",
        body=held,
        secret_value=_VALUE,
    )
    assert wrapped.fired == 1
    assert (actual, inserted) == ("winner", False)
    assert await _secret(db, "winner") == _VALUE
    assert await _secret(db, "loser") is None
    assert await _secret_rows(db) == 1  # the other writer's secret retired
    assert await _credential_units_without_a_secret(db) == []


async def test_a_concurrent_delete_never_leaves_a_body_without_its_secret(db):
    """R1, found in review: a delete between the check and the body write takes
    the row and its old secret; the unit write then re-creates the row, naming
    the fresh secret this writer inserted, which the delete never touched."""
    first = await _ingest(db, _store(), _body())
    held = (await kc.get(db, first))["body"]

    async def _delete(conn):
        await conn.execute("DELETE FROM knowledge_units WHERE id = ?", (first,))

    wrapped = _Conn(db, "INSERT INTO knowledge_units", _delete)
    await kc.upsert(
        wrapped,
        id=first,
        project_type="reference",
        domain="reference.credentials",
        source_doc="x",
        concept="Lab router admin",
        body=held,
        secret_value="rotated-Fake9!",
    )
    assert wrapped.fired == 1
    assert await _credential_units_without_a_secret(db) == []
    assert await _secret(db, first) == "rotated-Fake9!"


async def test_a_value_free_re_ingest_racing_a_delete_is_undone_and_refused(db):
    """A held-out body re-ingested with no value, while a delete runs: the
    re-created row would name a secret the delete retired, so the secret_ref
    trigger refuses the write and nothing is re-created."""
    first = await _ingest(db, _store(), _body())
    held = (await kc.get(db, first))["body"]

    async def _delete(conn):
        # As knowledge_crud.delete does it: the unit and its FTS row.
        await conn.execute("DELETE FROM knowledge_units WHERE id = ?", (first,))
        await conn.execute("DELETE FROM knowledge_fts WHERE unit_id = ?", (first,))

    wrapped = _Conn(db, "INSERT INTO knowledge_units", _delete)
    with pytest.raises(ValueError, match="deleted or repointed while this write ran"):
        await kc.upsert(
            wrapped,
            id=first,
            project_type="reference",
            domain="reference.credentials",
            source_doc="x",
            concept="Lab router admin",
            body=held,
        )
    assert await db.execute_fetchall("SELECT 1 FROM knowledge_units") == []
    assert await db.execute_fetchall("SELECT 1 FROM knowledge_fts") == []


# ─── the pairing invariant, over every ordering (rework v2) ──────────────────
#
# The shared connection serializes STATEMENTS, not transactions, so what two
# writers of one key can do to each other is fixed by the order their
# statements reach it. ``_Stepped`` makes each writer wait for its turn before
# every statement, so a schedule (a sequence of writer names) is one ordering;
# the tests run every ordering of two writers, of a writer and a delete, of a
# writer and the orphan sweep, and of a writer cancelled at each await point
# against another, then check the INVARIANT: every credential unit names a
# secret row, that row holds the value of the writer whose body the unit
# carries, and any other secret row is an orphan no unit names.


class _Schedule:
    """Hands out statement turns in the order of ``order``; a writer whose
    entries run out, or that has finished, runs free."""

    def __init__(self, order: list[str]):
        import asyncio

        self._order = list(order)
        self._done: set[str] = set()
        self._changed = asyncio.Condition()

    def _next(self) -> str | None:
        while self._order and self._order[0] in self._done:
            self._order.pop(0)
        return self._order[0] if self._order else None

    async def acquire(self, name: str) -> None:
        async with self._changed:
            await self._changed.wait_for(
                lambda: self._next() in (name, None) or name not in self._order
            )

    async def release(self, name: str) -> None:
        async with self._changed:
            if self._next() == name:
                self._order.pop(0)
            self._changed.notify_all()

    async def finish(self, name: str) -> None:
        async with self._changed:
            self._done.add(name)
            self._changed.notify_all()


class _Stepped:
    """``db`` for one writer: each statement waits for that writer's turn. With
    ``cancel_at=(k, when)`` the writer is cancelled at its k-th statement,
    ``before`` it runs or ``after`` it ran."""

    def __init__(self, db, name: str, schedule: _Schedule, cancel_at=None):
        self._db, self.name, self._schedule = db, name, schedule
        self._cancel_at, self.steps = cancel_at, 0

    def __getattr__(self, attr):
        return getattr(self._db, attr)

    async def _step(self, run):
        import asyncio

        self.steps += 1
        await self._schedule.acquire(self.name)
        try:
            if self._cancel_at == (self.steps, "before"):
                raise asyncio.CancelledError
            result = await run()
            if self._cancel_at == (self.steps, "after"):
                raise asyncio.CancelledError
            return result
        finally:
            await self._schedule.release(self.name)

    async def execute(self, sql, params=()):
        return await self._step(lambda: self._db.execute(sql, params))

    async def execute_fetchall(self, sql, params=()):
        return await self._step(lambda: self._db.execute_fetchall(sql, params))


_KEY = "Lab router admin"
_HELD = _body().replace(f"Value: {_VALUE}", f"Value: {kc.SECRET_PLACEHOLDER}")


def _value_of(writer: str) -> str:
    return f"password: {writer}-Fake9x"


async def _write(conn, writer: str, *, insert_only: bool = False):
    """One credential upsert of the key; ``source_doc`` names the body's writer."""
    return await kc.upsert(
        conn,
        project_type="reference",
        domain="reference.credentials",
        source_doc=f"writer-{writer}",
        concept=_KEY,
        body=_HELD,
        secret_value=_value_of(writer),
        insert_only=insert_only,
    )


async def _delete(conn, _writer: str):
    rows = await conn.execute_fetchall(
        "SELECT id FROM knowledge_units WHERE concept = ?",
        (_KEY,),
    )
    if rows:
        await kc.delete(conn, rows[0][0])


async def _sweep_now(conn, _writer: str):
    """The sweep with no age bound: it may take a secret whose unit is on its way."""
    await kc.sweep_orphan_secrets(conn, older_than_s=-60)


async def _new_shape_pairs(db) -> list[tuple[str, str | None]]:
    rows = await db.execute_fetchall(
        "SELECT k.source_doc, s.value FROM knowledge_units k "
        "LEFT JOIN reference_secrets s ON s.id = k.secret_ref WHERE k.concept = ?",
        (_KEY,),
    )
    return [(r[0].removeprefix("writer-"), r[1]) for r in rows]


async def _assert_invariant(db, pairs_fn=_new_shape_pairs, label="") -> None:
    pairs = await pairs_fn(db)
    assert len(pairs) <= 1, label
    for body_writer, value in pairs:
        assert value == _value_of(body_writer), f"{label}: body {body_writer} names {value!r}"
    if pairs_fn is _new_shape_pairs:
        # Every other secret row is an orphan, and no unit names a missing one.
        assert await _credential_units_without_a_secret(db) == [], label


async def _reset(db, seed: str | None) -> None:
    await db.execute("DELETE FROM knowledge_units")
    await db.execute("DELETE FROM knowledge_fts")
    await db.execute("DELETE FROM reference_secrets")
    await db.commit()
    if seed:
        await _write(db, seed)


async def _steps_of(db, op, name, seed) -> int:
    """How many statements ``op`` runs alone from the seeded state."""
    await _reset(db, seed)
    schedule = _Schedule([])
    conn = _Stepped(db, name, schedule)
    await op(conn, name)
    return conn.steps


def _orderings(a: str, n_a: int, b: str, n_b: int):
    import itertools

    for slots in itertools.combinations(range(n_a + n_b), n_a):
        order = [b] * (n_a + n_b)
        for i in slots:
            order[i] = a
        yield order


async def _run(db, ops, order, seed, cancel=None) -> dict:
    """Run ``ops`` ({name: op}) under one ordering; return {name: outcome}."""
    import asyncio

    await _reset(db, seed)
    schedule = _Schedule(order)

    async def one(name, op):
        conn = _Stepped(db, name, schedule, cancel.get(name) if cancel else None)
        try:
            return await op(conn, name)
        finally:
            await schedule.finish(name)

    names = list(ops)
    results = await asyncio.gather(
        *(one(n, ops[n]) for n in names),
        return_exceptions=True,
    )
    await db.commit()  # another coroutine's commit on the shared connection
    return dict(zip(names, results, strict=True))


async def _sweep_orderings(
    db, ops: dict, seed, pairs_fn=_new_shape_pairs, cancel=None, n_a: int | None = None,
) -> int:
    """Every ordering of the two ops' statements; returns how many ran. ``n_a``
    caps the first op's statements (a writer cancelled at its k-th runs k)."""
    (a, op_a), (b, op_b) = ops.items()
    n_a = n_a or await _steps_of(db, op_a, a, seed)
    n_b = await _steps_of(db, op_b, b, seed)
    runs = 0
    for order in _orderings(a, n_a, b, n_b):
        outcome = await _run(db, ops, order, seed, cancel)
        for result in outcome.values():
            # A cancellation is the one failure a run may show.
            if isinstance(result, BaseException) and not isinstance(result, asyncio_cancelled()):
                raise result
        await _assert_invariant(db, pairs_fn, f"{order} {cancel}")
        runs += 1
    return runs


def asyncio_cancelled():
    import asyncio

    return asyncio.CancelledError


@pytest.mark.parametrize("seed", [None, "S"])
async def test_every_ordering_of_two_writers_keeps_each_body_with_its_secret(db, seed):
    """4482018636, as an invariant: two upserts of one key, every ordering of
    their statements, from an absent key and from an existing one."""
    runs = await _sweep_orderings(db, {"A": _write, "B": _write}, seed)
    assert runs >= 20  # C(n_a + n_b, n_a) orderings ran, not a degenerate few


@pytest.mark.parametrize("seed", [None, "S"])
async def test_every_ordering_of_a_writer_and_a_delete_keeps_the_pair(db, seed):
    runs = await _sweep_orderings(db, {"A": _write, "D": _delete}, seed)
    assert runs >= 7  # an absent key: the delete is one read, at each of A's 7 gaps


@pytest.mark.parametrize("seed", [None, "S"])
async def test_a_sweep_that_takes_a_young_secret_refuses_the_unit_never_strands_it(db, seed):
    """The sweep with no age bound, at every point of a writer: where it takes
    the writer's new secret before the unit lands, the unit write is refused
    (the caller sees a failure), and no unit is left without its secret."""
    a, n_a = "A", await _steps_of(db, _write, "A", seed)
    n_w = await _steps_of(db, _sweep_now, "W", seed)
    refused = 0
    for order in _orderings(a, n_a, "W", n_w):
        outcome = await _run(db, {"A": _write, "W": _sweep_now}, order, seed)
        if isinstance(outcome["A"], ValueError):
            refused += 1
        elif isinstance(outcome["A"], BaseException):
            raise outcome["A"]
        await _assert_invariant(db, label=str(order))
    assert refused >= 1  # the window was really hit, and refused


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("seed", [None, "S"])
async def test_a_writer_cancelled_at_each_await_point_never_strands_or_crosses(db, seed, when):
    """A cancelled at its k-th statement (before it runs, or after), for every
    k, against a full writer B, in every ordering of their statements."""
    n_a = await _steps_of(db, _write, "A", seed)
    for k in range(1, n_a + 1):
        await _sweep_orderings(
            db,
            {"A": _write, "B": _write},
            seed,
            cancel={"A": (k, when)},
            n_a=k,
        )


async def _old_shape_write(conn, writer: str):
    """The two-table shape this replaced: the body first, then the secret keyed
    by the unit id, in separate statements."""
    await conn.execute(
        "INSERT INTO old_secrets_units (concept, source_doc) VALUES (?, ?) "
        "ON CONFLICT(concept) DO UPDATE SET source_doc = excluded.source_doc",
        (_KEY, f"writer-{writer}"),
    )
    rows = await conn.execute_fetchall(
        "SELECT rowid FROM old_secrets_units WHERE concept = ?",
        (_KEY,),
    )
    await conn.execute(
        "INSERT INTO old_secrets (unit_id, value) VALUES (?, ?) "
        "ON CONFLICT(unit_id) DO UPDATE SET value = excluded.value",
        (rows[0][0], _value_of(writer)),
    )


async def _old_shape_pairs(db) -> list[tuple[str, str | None]]:
    rows = await db.execute_fetchall(
        "SELECT u.source_doc, s.value FROM old_secrets_units u "
        "LEFT JOIN old_secrets s ON s.unit_id = u.rowid",
    )
    return [(r[0].removeprefix("writer-"), r[1]) for r in rows]


async def test_control_the_old_two_table_shape_fails_the_invariant(db):
    """The harness can see a crossing: the shape this replaced (secret keyed by
    the unit id, written after the body) breaks the invariant in some ordering
    of two writers."""
    await db.execute("CREATE TABLE old_secrets_units (concept TEXT UNIQUE, source_doc TEXT)")
    await db.execute("CREATE TABLE old_secrets (unit_id INTEGER PRIMARY KEY, value TEXT)")
    n = await _steps_of(db, _old_shape_write, "A", None)
    crossed = 0
    for order in _orderings("A", n, "B", n):
        await db.execute("DELETE FROM old_secrets_units")
        await db.execute("DELETE FROM old_secrets")
        await _run(db, {"A": _old_shape_write, "B": _old_shape_write}, order, None)
        try:
            await _assert_invariant(db, _old_shape_pairs)
        except AssertionError:
            crossed += 1
    assert crossed >= 1


# ─── the orphan sweep and the secret_ref triggers ────────────────────────────


async def _raw_secret(db, ref: str, value: str, created_at: str) -> None:
    await db.execute(
        "INSERT INTO reference_secrets (id, value, created_at) VALUES (?, ?, ?)",
        (ref, value, created_at),
    )


async def test_the_sweep_keeps_young_orphans_and_named_rows(db):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    old = (now - timedelta(hours=2)).isoformat()
    young = (now - timedelta(minutes=5)).isoformat()
    await _raw_secret(db, "old-orphan", "fake-1", old)
    await _raw_secret(db, "young-orphan", "fake-2", young)
    unit = await _ingest(db, _store(), _body())
    named = (await kc.get(db, unit))["secret_ref"]
    await db.execute(
        "UPDATE reference_secrets SET created_at = ? WHERE id = ?",
        (old, named),
    )
    assert await kc.sweep_orphan_secrets(db) == 1
    left = {r[0] for r in await db.execute_fetchall("SELECT id FROM reference_secrets")}
    assert left == {"young-orphan", named}
    assert await _secret(db, unit) == _VALUE


async def test_delete_runs_the_sweep(db):
    from datetime import UTC, datetime, timedelta

    await _raw_secret(
        db,
        "old-orphan",
        "fake-1",
        (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
    )
    unit = await _ingest(db, _store(), _body())
    await kc.delete(db, unit)
    assert await _secret_rows(db) == 0


_RAW_UNIT = (
    "INSERT INTO knowledge_units (id, project_type, domain, source_doc, concept, body, "
    "ingested_at, secret_ref) VALUES (?, 'reference', 'reference.credentials', 'x', ?, ?, "
    "'now', ?)"
)


async def test_a_credential_unit_naming_no_secret_is_refused(db):
    for ref in (None, "no-such-row"):
        with pytest.raises(aiosqlite.IntegrityError, match="names no stored secret"):
            await db.execute(_RAW_UNIT, ("u", "c", _HELD, ref))
    await _raw_secret(db, "s1", "fake-1", "now")
    await db.execute(_RAW_UNIT, ("u", "c", _HELD, "s1"))
    for ref in (None, "no-such-row"):
        with pytest.raises(aiosqlite.IntegrityError, match="names no stored secret"):
            await db.execute("UPDATE knowledge_units SET secret_ref = ? WHERE id = 'u'", (ref,))
    # Another kind is not a credential, and needs no secret.
    await db.execute(
        "INSERT INTO knowledge_units (id, project_type, domain, source_doc, concept, body, "
        "ingested_at) VALUES ('n', 'reference', 'reference.network', 'x', 'n', 'b', 'now')"
    )


async def test_repointing_or_deleting_a_unit_retires_its_secret_unless_shared(db):
    for ref in ("s1", "s2"):
        await _raw_secret(db, ref, f"fake-{ref}", "now")
    await db.execute(_RAW_UNIT, ("u1", "c1", _HELD, "s1"))
    await db.execute(_RAW_UNIT, ("u2", "c2", _HELD, "s1"))  # a shared ref
    await db.execute("UPDATE knowledge_units SET secret_ref = 's2' WHERE id = 'u1'")
    ids = lambda rows: {r[0] for r in rows}  # noqa: E731
    assert ids(await db.execute_fetchall("SELECT id FROM reference_secrets")) == {"s1", "s2"}
    await db.execute("DELETE FROM knowledge_units WHERE id = 'u2'")
    assert ids(await db.execute_fetchall("SELECT id FROM reference_secrets")) == {"s2"}
    await db.execute("DELETE FROM knowledge_units WHERE id = 'u1'")
    assert await _secret_rows(db) == 0


async def test_an_upsert_with_a_new_value_repoints_and_retires_the_old_secret(db):
    unit = await _ingest(db, _store(), _body())
    first_ref = (await kc.get(db, unit))["secret_ref"]
    await _ingest(db, _store(), _body(value="user: example-admin, password: Next!Fake-7"))
    row = await kc.get(db, unit)
    assert row["secret_ref"] != first_ref
    assert await _secret_rows(db) == 1
    assert len(row["secret_ref"]) == 32  # token_hex(16): random, not from the value


# ─── insert-only auto-capture (4484515141) ───────────────────────────────────


async def test_every_ordering_of_two_insert_only_writers_keeps_both_values_safe(db):
    """Two auto-captures of one key with different values: in every ordering
    exactly one lands on the key, the other is told the key is taken (and
    writes nothing), and the key's secret is the lander's."""
    ops = {
        "A": lambda c, n: _write(c, n, insert_only=True),
        "B": lambda c, n: _write(c, n, insert_only=True),
    }
    n_a = await _steps_of(db, ops["A"], "A", None)
    n_b = await _steps_of(db, ops["B"], "B", None)
    for order in _orderings("A", n_a, "B", n_b):
        outcome = await _run(db, ops, order, None)
        taken = [n for n, r in outcome.items() if isinstance(r, kc.CredentialKeyTaken)]
        landed = [n for n, r in outcome.items() if not isinstance(r, BaseException)]
        assert len(taken) == 1 and len(landed) == 1, (order, outcome)
        await _assert_invariant(db, label=str(order))
        assert (await _new_shape_pairs(db))[0][0] == landed[0]
        assert await _secret_rows(db) == 1  # the taken writer left no orphan


async def test_an_insert_only_write_never_changes_an_existing_key(db):
    await _write(db, "A")
    with pytest.raises(kc.CredentialKeyTaken):
        await _write(db, "B", insert_only=True)
    assert await _new_shape_pairs(db) == [("A", _value_of("A"))]
    # The same value is a no-op success.
    unit, inserted = await _write(db, "A", insert_only=True)
    assert inserted is False
    assert await _secret(db, unit) == _value_of("A")
    assert await _secret_rows(db) == 1
