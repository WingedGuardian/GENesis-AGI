"""The armed check, and the fail-open it exists to prevent.

`genesis-graph-project.timer` is enabled on EVERY install, because bootstrap.sh
enables every rendered timer except the backup one. So the projector's scheduled
entrypoint has to distinguish two things that both surface as
`GraphUnavailableError` out of `build()`:

* there is no graph engine on this install  -> nothing to do, exit 0
* there IS one and the projection failed    -> a real failure, exit 1

Collapsing those is the fail-open: an hourly unit that exits 0 on a genuine
failure means the staleness bound goes quiet exactly when it stops holding, and
a stale projection is indistinguishable from a current one from the engine's
side. That is why `armed()` is a POSITIVE check made BEFORE any projection is
attempted, rather than an `except` clause after one.
"""

from __future__ import annotations

import os

import pytest

from genesis.memory import graphstore_project as gp
from genesis.memory.graphstore import DatabaseUnreachable, GraphUnavailableError
from genesis.memory.graphstore_falkor import ProjectionInProgress


@pytest.fixture
def socket(tmp_path):
    """A socket path that exists. Contents are irrelevant — `armed()` asks the
    filesystem whether the engine was provisioned, not whether it answers."""
    p = tmp_path / "falkordb.sock"
    p.write_bytes(b"")
    return p


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    """Default the config to enabled, so each test varies ONE condition."""
    monkeypatch.setattr(gp, "load_config", lambda: {"enabled": True, "mode": "networkx"})


# ── armed(): one test per way an install can have nothing to project ──────────


def test_armed_when_the_socket_exists_and_the_client_is_installed(socket):
    ok, reason = gp.armed(socket)
    assert ok is True
    assert str(socket) in reason


@pytest.mark.parametrize(
    "enabled",
    [
        False,
        "false",  # a QUOTED false in YAML -> a TRUTHY string
        "False",
        "no",
        0,
        None,
        "true",  # not the BOOLEAN true: still not exactly True
    ],
)
def test_not_armed_when_the_master_switch_is_not_exactly_true(monkeypatch, socket, enabled):
    """`enabled` is read with `is not True`, matching `effective_mode()`.

    The original version of this test passed only `False` — the ONE value that
    a falsiness test and an identity test agree on, so it bound nothing and let
    the real defect through. MEASURED with `enabled: "false"`: reads were OFF
    (`effective_mode()` -> networkx) while a falsiness-based `armed()` returned
    True, so the timer ran hourly against a backend the operator had disabled.

    `"true"` is in the list deliberately: a string is not the boolean, and a
    value we cannot read as exactly True is one we decline to act on.
    """
    monkeypatch.setattr(gp, "load_config", lambda: {"enabled": enabled})
    ok, reason = gp.armed(socket)
    assert ok is False, f"enabled={enabled!r} must not arm the projector"
    assert "not enabled" in reason


def test_armed_only_for_the_boolean_true(monkeypatch, socket):
    """The complement, so the parametrize above cannot pass by refusing
    everything."""
    monkeypatch.setattr(gp, "load_config", lambda: {"enabled": True})
    ok, _ = gp.armed(socket)
    assert ok is True


def test_not_armed_when_there_is_no_socket(tmp_path):
    """The common case by a wide margin: an install that never provisioned an
    engine. A filesystem check, so it costs nothing on the installs where it is
    false."""
    ok, reason = gp.armed(tmp_path / "definitely-absent.sock")
    assert ok is False
    assert "no graph engine socket" in reason


def test_not_armed_when_the_client_library_is_missing(monkeypatch, socket):
    monkeypatch.setattr(gp.importlib.util, "find_spec", lambda name: None)
    ok, reason = gp.armed(socket)
    assert ok is False
    assert "client is not installed" in reason


def test_an_unreadable_config_is_not_armed(monkeypatch, socket):
    """Withholds the exception text, which can carry a path under the
    operator's home, and still names the failure."""

    def _boom():
        raise OSError("/home/someoperator/.genesis/config/graphstore.yaml is unreadable")

    monkeypatch.setattr(gp, "load_config", _boom)
    ok, reason = gp.armed(socket)
    assert ok is False
    assert "OSError" in reason
    assert "someoperator" not in reason


def test_a_socket_with_no_listener_is_still_armed(socket):
    """Deliberate direction, stated so nobody 'fixes' it into a quiet skip.

    `armed()` cannot tell a live engine from a stale socket file without
    connecting, and a stale socket is a BROKEN install: it should produce a loud
    hourly failure from `build()`, not be passed over as 'no engine here'.
    """
    ok, _ = gp.armed(socket)
    assert ok is True


# ── --if-armed: the exit-code contract the timer depends on ──────────────────


def _run(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["graphstore_project", *argv])
    return gp.main()


def test_if_armed_exits_zero_when_there_is_no_engine(monkeypatch, tmp_path, capsys):
    """The property that keeps a non-adopting install from having an hourly
    failing unit."""
    monkeypatch.setattr(gp, "falkordb_socket_path", lambda: tmp_path / "absent.sock")
    called = []
    monkeypatch.setattr(gp, "build", lambda *a, **k: called.append(1))

    assert _run(monkeypatch, ["--if-armed"]) == 0
    assert "nothing to project" in capsys.readouterr().out
    assert not called, "build() must not be attempted when the install is not armed"


def test_if_armed_still_fails_when_an_armed_install_cannot_project(monkeypatch, socket, capsys):
    """THE FAIL-OPEN GUARD, and the reason `armed()` is not an except clause.

    Engine present, projection fails. `build()` raises the SAME exception type
    that a missing engine raises, so an implementation that inferred
    'not armed' from the exception would exit 0 here and the hourly unit would
    report success while the projection silently rotted.
    """
    monkeypatch.setattr(gp, "falkordb_socket_path", lambda: socket)

    async def _fail(*_a, **_k):
        raise GraphUnavailableError("the engine refused the projection")

    monkeypatch.setattr(gp, "build", _fail)

    assert _run(monkeypatch, ["--if-armed"]) == 1
    out = capsys.readouterr().out
    assert "cannot project" in out
    assert "nothing to project" not in out


def test_a_manual_run_without_the_flag_still_reports_a_missing_engine(
    monkeypatch, tmp_path, capsys
):
    """The flag must not change the manual UX. Running this by hand on a box
    with no engine is a question, and the answer is the whole point of running
    it — so the default stays a loud exit 1."""
    monkeypatch.setattr(gp, "falkordb_socket_path", lambda: tmp_path / "absent.sock")

    async def _fail(*_a, **_k):
        raise GraphUnavailableError("cannot reach the graph engine at /nonexistent")

    monkeypatch.setattr(gp, "build", _fail)

    assert _run(monkeypatch, []) == 1
    assert "cannot project" in capsys.readouterr().out


# -- remediation routing: three failures, three different services -------------


def test_a_database_failure_does_not_blame_the_graph_engine(monkeypatch, socket, capsys):
    """Keyed on TYPE, not on message wording.

    Before this was a subclass, a database that could not be opened printed
    "the engine is not reachable over its socket - check genesis-falkordb" -
    a different subsystem from the one that failed, and the only instruction
    an unattended hourly journal entry gives its reader.
    """
    monkeypatch.setattr(gp, "falkordb_socket_path", lambda: socket)

    async def _db_fails(*_a, **_k):
        raise DatabaseUnreachable("the memory database at /x/y.db cannot be opened")

    monkeypatch.setattr(gp, "build", _db_fails)

    assert _run(monkeypatch, ["--if-armed"]) == 1
    out = capsys.readouterr().out
    assert "memory database could not be read" in out
    assert "genesis-falkordb" not in out
    assert "socket" not in out


def test_an_engine_failure_still_blames_the_engine(monkeypatch, socket, capsys):
    """The complement. Narrowing the database case must not swallow the case
    the original message was right about."""
    monkeypatch.setattr(gp, "falkordb_socket_path", lambda: socket)

    async def _engine_fails(*_a, **_k):
        raise GraphUnavailableError("cannot reach the graph engine at /x/z.sock")

    monkeypatch.setattr(gp, "build", _engine_fails)

    assert _run(monkeypatch, ["--if-armed"]) == 1
    out = capsys.readouterr().out
    assert "genesis-falkordb" in out
    assert "memory database could not be read" not in out


def test_the_database_error_is_still_a_graph_unavailable_error():
    """Subclassing is the compatibility promise: every existing caller catches
    GraphUnavailableError, and none of them should have to learn a new type."""
    assert issubclass(DatabaseUnreachable, GraphUnavailableError)


def test_a_collision_with_another_projector_is_a_no_op_not_a_failure(monkeypatch, socket, capsys):
    """Contention exits 0 and does NOT blame the engine.

    The cross-process publication lock is held whenever another projector is
    running — including the manual run this module's docstring recommends. That
    previously raised a bare GraphUnavailableError, so an hourly tick colliding
    with a manual run went RED and printed "check genesis-falkordb" about a
    service that was working perfectly. The work is being done by the other
    process; there is nothing to report.
    """
    monkeypatch.setattr(gp, "falkordb_socket_path", lambda: socket)

    async def _busy(*_a, **_k):
        raise ProjectionInProgress("another projection of 'genesis_memory' is already in progress")

    monkeypatch.setattr(gp, "build", _busy)

    assert _run(monkeypatch, ["--if-armed"]) == 0
    out = capsys.readouterr().out
    assert "skipping" in out
    assert "genesis-falkordb" not in out
    assert "cannot project" not in out


def test_a_db_read_failure_inside_the_store_also_routes_to_the_database(
    monkeypatch, socket, capsys
):
    """The path the first fix MISSED, and the reason the type is shared.

    `FalkorGraphStore._project_locked` converts every SQLite read failure into
    the graph error hierarchy itself, so it never reached `build()`'s generic
    `except Exception` where the private type used to be applied — the
    `except GraphUnavailableError: raise` above it won first. Only the
    database-OPEN path was covered, which a reviewer caught.

    Now the store raises the shared `DatabaseUnreachable` directly, so the type
    survives the re-raise and the remediation is right for both.
    """
    monkeypatch.setattr(gp, "falkordb_socket_path", lambda: socket)

    async def _store_read_fails(*_a, **_k):
        raise DatabaseUnreachable(
            "the memory database cannot be read — the projection cannot be built: no such table"
        )

    monkeypatch.setattr(gp, "build", _store_read_fails)

    assert _run(monkeypatch, ["--if-armed"]) == 1
    out = capsys.readouterr().out
    assert "memory database could not be read" in out
    assert "genesis-falkordb" not in out


def test_the_store_raises_the_shared_type_for_a_db_read_failure():
    """Binds the PRODUCER, not just this module's handling of it.

    Asserting only that `main()` routes the type correctly would pass even if
    the store went back to raising the generic error — which is precisely the
    defect that shipped. This pins the raise site itself.
    """
    import inspect

    from genesis.memory import graphstore_falkor

    src = inspect.getsource(graphstore_falkor.FalkorGraphStore._project_locked)
    assert "raise DatabaseUnreachable(" in src, (
        "_project_locked must raise the shared DatabaseUnreachable for SQLite "
        "read failures; raising the generic GraphUnavailableError sends "
        "operators to the graph engine for a database problem"
    )


# -- a config we cannot read is not consent ------------------------------------


def test_an_unparseable_config_is_not_armed(monkeypatch, socket):
    """`load_config()` absorbs a parse failure and returns DEFAULTS, and
    DEFAULTS["enabled"] is True — so without a validity check a corrupted
    overlay is indistinguishable from an operator enabling the backend.

    The realistic path: someone edits the overlay to write `enabled: false`,
    fat-fingers the YAML, and the projector reads the typo as permission.
    """
    monkeypatch.setattr(gp, "config_is_readable", lambda: False)
    ok, reason = gp.armed(socket)
    assert ok is False
    assert "did not parse" in reason


def test_config_is_readable_rejects_broken_yaml(tmp_path, monkeypatch):
    """Binds the accessor itself, not just this module's use of it."""
    from genesis.memory import graphstore_config as gc

    bad = tmp_path / "graphstore.yaml"
    bad.write_text("enabled: [unclosed\n", encoding="utf-8")
    monkeypatch.setattr(gc, "_base_path", lambda: bad)
    assert gc.config_is_readable() is False


def test_config_is_readable_treats_an_absent_file_as_fine(tmp_path, monkeypatch):
    """ABSENT is not CORRUPT, and conflating them would make 'no config' mean
    'do nothing' — a different and wrong reading for a fresh install."""
    from genesis.memory import graphstore_config as gc

    monkeypatch.setattr(gc, "_base_path", lambda: tmp_path / "nope.yaml")
    assert gc.config_is_readable() is True


# -- the staging-graph leak, and why a handler alone is not the fix ------------


def test_sigterm_is_routed_onto_the_cleanup_path(monkeypatch, tmp_path, capsys):
    """MEASURED: Python's DEFAULT SIGTERM handling runs neither `except
    BaseException` nor `finally`, while SIGINT runs both.

    The store deletes its staging graph only from an `except BaseException`, so
    a `TimeoutStartSec` expiry — delivered as SIGTERM — abandoned a full copy of
    the projection under a pid-keyed name no later tick would reuse. Routing
    SIGTERM onto KeyboardInterrupt puts it on the same path Ctrl+C already takes,
    which is the one measured to clean up correctly.

    Asserts the handler is INSTALLED and RAISES, rather than that a signal was
    delivered — delivery timing in a test process is its own flake.
    """
    import signal as _signal

    monkeypatch.setattr(gp, "falkordb_socket_path", lambda: tmp_path / "absent.sock")
    monkeypatch.setattr("sys.argv", ["graphstore_project", "--if-armed"])
    gp.main()

    handler = _signal.getsignal(_signal.SIGTERM)
    assert handler not in (_signal.SIG_DFL, _signal.SIG_IGN), (
        "SIGTERM is still on the default disposition, which unwinds nothing — "
        "a timed-out projection would abandon its staging graph"
    )
    with pytest.raises(KeyboardInterrupt):
        handler(_signal.SIGTERM, None)


def test_pid_alive_answers_alive_for_anything_ambiguous():
    """The two error directions are NOT symmetric, so this is asserted.

    A false DEAD destroys a projection another process is actively building; a
    false ALIVE leaves an orphan for the next sweep. Every uncertain case must
    therefore answer True.
    """
    from genesis.memory.graphstore_falkor import _pid_alive

    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(0) is True, "pid 0 is not a reapable process"
    assert _pid_alive(-1) is True, "a negative pid is a process GROUP, never ours to reap"
    # A pid that cannot exist on Linux (default pid_max is 4194304).
    assert _pid_alive(2**31 - 1) is False


@pytest.mark.asyncio
async def test_the_sweep_reaps_dead_pids_and_spares_everything_else(monkeypatch):
    """The backstop that covers what no signal handler can — SIGKILL, an OOM
    kill, a power loss.

    Engine-free: `_key_op` is stubbed, so this pins the SELECTION LOGIC, which
    is where the danger is. Deleting a live build's staging graph would corrupt
    a concurrent projection.
    """
    from genesis.memory.graphstore_falkor import FalkorGraphStore

    store = FalkorGraphStore(graph_key="swept")
    mine = os.getpid()
    keys = [
        f"swept_staging_{2**31 - 1}".encode(),  # dead -> reap
        f"swept_staging_{mine}".encode(),  # OURS -> never
        b"swept_staging_notanumber",  # not ours to guess at
        f"swept_staging_{os.getppid()}".encode(),  # alive -> spare
    ]
    deleted: list[str] = []

    async def _fake_key_op(op, *args, timeout=None, **kwargs):
        if op == "scan":
            return 0, keys
        if op == "delete":
            deleted.append(args[0])
            return 1
        raise AssertionError(f"unexpected op {op}")

    monkeypatch.setattr(store, "_key_op", _fake_key_op)
    removed = await store._sweep_orphan_staging()

    assert removed == 1
    assert deleted == [f"swept_staging_{2**31 - 1}"], (
        f"swept the wrong set: {deleted} — a live or unparseable key was reaped"
    )
