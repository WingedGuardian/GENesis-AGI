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

import pytest

from genesis.memory import graphstore_project as gp
from genesis.memory.graphstore import GraphUnavailableError
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
        raise gp._DatabaseUnreachable("the memory database at /x/y.db cannot be opened")

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
    assert issubclass(gp._DatabaseUnreachable, GraphUnavailableError)


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
