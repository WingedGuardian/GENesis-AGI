"""Shared test fixtures for Genesis v3."""

# ── sys.path guard: tests must import from THIS worktree, not from main ──
# The venv has an editable install (``pip install -e .``) whose ``.pth``
# file adds ``/home/ubuntu/genesis/src`` — the MAIN worktree's src — to
# ``sys.path`` at interpreter startup. Without this guard, running
# ``pytest`` from a sibling worktree collects tests from the worktree's
# ``tests/`` directory but imports ``genesis.*`` from main's source tree.
# The tests silently lie: they report PASS/FAIL against the wrong code.
#
# This block inserts the current worktree's own ``src`` at ``sys.path``
# position 0 before any test collects. pytest loads conftest.py during
# the collection phase, before any test module runs, and before the
# fixtures below import ``genesis.*``. Position 0 beats the editable
# install's path.
#
# Safety:
# - In the main worktree ``_WORKTREE_SRC`` resolves to the same directory
#   as the editable install's ``.pth``-injected path. The guard removes
#   and re-inserts that path at position 0 — a reorder, not a true no-op,
#   but semantically equivalent because only one ``genesis/`` package
#   exists on ``sys.path``. Import resolution is unchanged.
# - In a sibling worktree it shadows the editable install so tests resolve
#   against the worktree's source tree, which is what every test author
#   expects.
# - This is the structural fix for the 2026-04-10 worktree-test-isolation
#   footgun: before this guard, every sibling-worktree test run needed an
#   explicit ``PYTHONPATH=src`` prefix or it silently tested main instead.
import sys
from pathlib import Path

_WORKTREE_SRC = Path(__file__).resolve().parent.parent / "src"
if _WORKTREE_SRC.is_dir():
    _src_str = str(_WORKTREE_SRC)
    if _src_str in sys.path:
        # Already present but may not be at position 0 — move it to the
        # front so it shadows anything the editable install injected.
        sys.path.remove(_src_str)
    sys.path.insert(0, _src_str)

import os  # noqa: E402

import aiosqlite  # noqa: E402
import pytest  # noqa: E402

# ── Safety: prevent os.killpg(1, ...) from killing all processes ─────────
_real_killpg = os.killpg


def _safe_killpg(pgid: int, sig: int) -> None:
    """Safety wrapper that blocks os.killpg with pgid <= 1."""
    if pgid <= 1:
        raise ValueError(
            f"BLOCKED: os.killpg({pgid}, {sig}) would kill all user processes. "
            "Always set mock_proc.pid to an explicit value > 1 in tests."
        )
    _real_killpg(pgid, sig)


os.killpg = _safe_killpg  # type: ignore[assignment]


# ── Redirect pytest's temp tree OFF the watchgod-policed cc-tmp ───────────
# A CC session's TMPDIR is ``~/.genesis/cc-tmp`` (set by scripts/cc-slot.sh),
# and pytest's ``tmp_path``/basetemp default under ``$TMPDIR``. Left alone, a
# broad suite dumps hundreds of MB of ``pytest-of-<user>/`` into that
# budget-policed dir and trips ``genesis-tmp-watchgod`` (stuck-ORANGE churn).
# This steers pytest's own basetemp to ``~/tmp`` (``big_tmp_dir``) instead —
# WITHOUT touching the process ``TMPDIR`` (which would desync CC's
# TMPDIR/CLAUDE_CODE_TMPDIR). Runs at config time, before any ``tmp_path``
# fixture resolves. No-op on CI (TMPDIR unset) and when ``--basetemp`` is
# passed explicitly. See ``genesis.util.tmp.should_redirect_pytest_basetemp``.
def _reap_stale_pytest_basetemps(pytest_base: str) -> None:
    """Remove per-PID basetemp dirs left by runs that exited abnormally (SIGKILL/
    host crash — ``pytest_unconfigure`` never ran). A leaf is stale iff its name
    is an integer PID that is no longer alive; LIVE PIDs (concurrent runs) are
    spared, so this never touches another in-flight suite. Best-effort — this is
    the safety net that makes cleanup survive abnormal exits (``pytest_unconfigure``
    handles the normal path)."""
    import shutil

    try:
        names = os.listdir(pytest_base)
    except OSError:
        return
    for name in names:
        # isdecimal (NOT isdigit): isdigit is True for superscripts like '²' whose
        # int() raises ValueError — this loop must never raise out of
        # pytest_configure (that would break collection for the WHOLE suite,
        # strictly worse than the leak it prevents).
        if not name.isdecimal():
            continue
        pid = int(name)
        if pid <= 1:
            continue  # our leaf is always our own PID (>1); never signal 0/1
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            shutil.rmtree(os.path.join(pytest_base, name), ignore_errors=True)  # dead → reap
        except (OSError, OverflowError):
            # alive-but-not-ours (PermissionError), transient, or an out-of-PID-range
            # name (os.kill(10**30,0) → OverflowError, which is NOT an OSError) → spare.
            pass


def _is_introspection_only(config) -> bool:
    """Modes that print and exit without running tests (--help/--fixtures/--markers).

    Kept as a predicate rather than inlined so the exemption set is one
    reviewable list; see the caller for why these must not be blocked.
    """
    opt = config.option
    return any(
        getattr(opt, name, False)
        for name in ("help", "showfixtures", "show_fixtures_per_test", "markers", "version")
    )


def pytest_configure(config):
    # ── Box-wide serialization ─────────────────────────────────────────────
    # Acquire BEFORE anything else, and before the basetemp early-return
    # below, so every exit path from this function is already governed.
    #
    # This is the choke point every run of THIS repo's suite shares, whatever
    # launched it and from whichever worktree: the CC concurrent-test guard
    # sees Bash-tool invocations only, so cron jobs, plain SSH shells and
    # background sessions reach pytest without ever passing it. (The gauntlet
    # scores FOREIGN fixture projects with their own rootdir, so this conftest
    # never loads for it — it acquires the same lock itself.) Non-blocking and
    # fail-open by contract, so a fault here can never stop the suite from
    # running — see genesis.util.pytest_lock.
    from genesis.util import pytest_lock
    from genesis.util.tmp import big_tmp_dir, should_redirect_pytest_basetemp

    lock = pytest_lock.acquire()
    config._genesis_pytest_lock = lock
    if lock.blocked and not _is_introspection_only(config):
        pytest.exit(lock.message, returncode=pytest_lock.EXIT_LOCK_HELD)
    elif lock.blocked:
        # Introspection modes run _do_configure() OUTSIDE wrap_session, so an
        # Exit raised here escapes as a pluggy traceback (MEASURED: --help and
        # --markers exit 1 with a traceback; --fixtures discards the status and
        # exits 0, so a blocked call looks like it SUCCEEDED and listed
        # nothing). They also collect nothing and run no tests, so there is no
        # resource to govern — let them through.
        lock.release()

    # Decide FIRST (pure, no I/O) — only touch the filesystem when we actually
    # redirect, so the no-op / CI / explicit-`--basetemp` path never creates
    # `~/tmp` (which would break a read-only-home or CI run during config).
    if not should_redirect_pytest_basetemp(
        current_basetemp=config.option.basetemp,
        tmpdir_env=os.environ.get("TMPDIR"),
        home=os.path.expanduser("~"),
    ):
        return
    # Scope the leaf per-process. pytest CLEARS an explicit basetemp at session
    # start and roots tmp_path directly under it (no pytest-of-<user> numbered
    # rotation), so two concurrent runs sharing one path would rmtree each other's
    # live temp. The CC concurrent-test guard only covers Bash-tool pytest —
    # autonomy/gauntlet subprocesses bypass it — so the per-pid leaf is what keeps
    # simultaneous runs isolated.
    pytest_base = os.path.join(big_tmp_dir(), "pytest")
    # Reap dead-PID leaves from prior abnormal exits BEFORE creating ours, so the
    # ~/tmp/pytest dir can't accumulate (the daily hygiene job only prunes direct
    # children of ~/tmp, whose mtime every run refreshes — so it never ages out).
    _reap_stale_pytest_basetemps(pytest_base)
    target = os.path.join(pytest_base, str(os.getpid()))
    Path(target).mkdir(parents=True, exist_ok=True)
    config.option.basetemp = target
    config._genesis_basetemp_cleanup = target


def pytest_unconfigure(config):
    """Remove the per-process basetemp tree this session created. pytest wipes an
    explicit basetemp at session START but never at exit, and each run gets a new
    PID, so without this the ``~/tmp/pytest/<pid>`` dirs would accumulate (the
    daily hygiene job only prunes direct children of ``~/tmp``, whose mtime every
    new run refreshes — so it never ages out). Best-effort."""
    import shutil

    target = getattr(config, "_genesis_basetemp_cleanup", None)
    if target:
        shutil.rmtree(target, ignore_errors=True)

    # Release the box-wide lock LAST, so it spans the whole session including
    # this cleanup. (flock also drops on process death, so a crash cannot wedge
    # the box — this is the orderly path, not the only one.)
    lock = getattr(config, "_genesis_pytest_lock", None)
    if lock is not None:
        lock.release()


# ── Safety: prevent tests from polluting production circuit breaker state ──
@pytest.fixture(autouse=True)
def _isolate_circuit_breaker_state(tmp_path, monkeypatch):
    """Redirect circuit breaker state file to tmp_path for all tests."""
    import genesis.routing.circuit_breaker as cb_mod

    monkeypatch.setattr(cb_mod, "_STATE_FILE", tmp_path / "cb_state.json")


# ── Safety: prevent tests from writing REAL durable alerts ──────────────────
@pytest.fixture(autouse=True)
def _isolate_alert_queue(tmp_path):
    """Redirect the durable alert-queue root to tmp for ALL tests.

    ``_alert_flap`` / ``_alert_starved`` (watchdog) and the alert-drain init
    resolve their queue root via ``env.alert_queue_root()``; patching that one
    resolver keeps any test reaching an alert path from writing a REAL
    ``~/.genesis/alerts/queue`` entry that the live server drains to the owner's
    Telegram (this happened — test-sized 'flap-damping' backoff values were
    delivered as real incidents). Surgical: only the alert-queue root, NOT
    ``GENESIS_HOME`` globally, so config/state reads are untouched. The HOST
    guardian queue (``config.state_path``) is a separate path, unaffected.

    Uses a fixture-OWNED ``MonkeyPatch`` (not the shared ``monkeypatch``
    fixture) so a test that calls ``monkeypatch.undo()`` mid-body cannot revert
    this suite-isolation patch and re-expose the real queue — mirrors
    ``_isolate_user_config_dir``.
    """
    mp = pytest.MonkeyPatch()
    mp.setattr(
        "genesis.env.alert_queue_root",
        lambda: tmp_path / "alerts" / "queue",
    )
    yield
    mp.undo()


# ── Safety: prevent tests from writing to the REAL genesis.db ────────────────
@pytest.fixture(autouse=True)
def _isolate_genesis_db_path(tmp_path):
    """Redirect ``env.genesis_db_path()`` to tmp for ALL tests.

    Same class as ``_isolate_alert_queue``: code paths that resolve the DB
    lazily (e.g. the settings gate-disable critical-observation write via
    ``get_raw_db(genesis_db_path())``) would otherwise write REAL rows the
    live server pages to the owner's Telegram when tests run from the main
    tree (from a worktree they silently mint a stray ``data/genesis.db``
    instead — measured 2026-08-19). Tests that need a DB construct their own
    in-memory connection; none legitimately resolve the install DB.
    """
    mp = pytest.MonkeyPatch()
    mp.setattr(
        "genesis.env.genesis_db_path",
        lambda: tmp_path / "isolated-genesis.db",
    )
    yield
    mp.undo()


# ── Safety: isolate tests from the install's config overlays ──
@pytest.fixture(autouse=True)
def _isolate_user_config_dir(tmp_path):
    """Neutralize BOTH overlay vectors so tests never read install-local state.

    ``config/*.local.yaml`` overlays are install-local (e.g. a voice-live
    install arms ``voice_act.local.yaml`` ``mode: live``). Every loader that
    calls ``merge_local_overlay`` resolves them from two locations, and BOTH
    leak into tests unpatched — green on CI (no overlays), red or falsely green
    on a live install:

    1. **User dir** (``~/.genesis/config/*.local.yaml``) — absolute, so it leaks
       in the main tree AND in a sibling worktree. Patched by redirecting
       ``_user_config_dir`` to an empty per-test dir.
    2. **Repo-relative sibling** (``<repo>/config/*.local.yaml``) — gitignored,
       present only in the main tree, so a worktree test run structurally can't
       catch it. When a loader is called with a real repo config path (e.g.
       ``load_ego_config()`` with no args → ``repo_root()/config/ego.yaml``),
       ``_resolve_overlay_path`` falls back to the real sibling. We wrap the
       resolver so any overlay that resolves INSIDE the real repo config dir is
       redirected to the empty dir. tmp-rooted overlays (the overlay tests'
       own fixtures, and ``test_config_save``-style tests) are never touched.

    ``security/immunity.py`` binds ``_user_config_dir`` at module level (its own
    reference), so it is patched separately. The guards in
    tests/test_config_overlay.py keep this list complete and catch any NEW
    hand-rolled ``.local.yaml`` resolver that bypasses ``merge_local_overlay``.
    Independent resolvers (routing, recon, mcp settings) hold their own logic
    and are tracked for consolidation (follow-up); the MCP ``_USER_CONFIG_DIR``
    constants are excluded as too import-heavy for an every-test fixture — their
    tests self-isolate.

    Uses a fixture-OWNED ``MonkeyPatch`` rather than the shared ``monkeypatch``
    fixture: a test that calls ``monkeypatch.undo()`` mid-body (e.g.
    ``test_learned_knobs``) would otherwise also revert THIS suite-isolation
    patch — and a subsequent config read in that test would hit the real overlay.
    An independent instance we undo in teardown is immune to that.
    """
    from genesis import _config_overlay
    from genesis.env import repo_root
    from genesis.security import immunity

    mp = pytest.MonkeyPatch()
    user_dir = tmp_path / "user-config-isolated"
    mp.setattr(_config_overlay, "_user_config_dir", lambda: user_dir)
    mp.setattr(immunity, "_user_config_dir", lambda: user_dir)

    # Vector 2: neutralize the repo-relative sibling fallback.
    _orig_resolve = _config_overlay._resolve_overlay_path

    def _sandboxed_resolve(base_path):
        result = _orig_resolve(base_path)
        try:
            resolved = result.resolve()
        except OSError:  # pragma: no cover - defensive (symlink loops)
            return result
        # Resolve the real config dir LAZILY (repo_root() reads GENESIS_REPO_ROOT
        # per call), so a test that re-points GENESIS_REPO_ROOT after fixture
        # setup is still honored — and the deterministic regression test can aim
        # a synthetic overlay at it.
        real_config_dir = (repo_root() / "config").resolve()
        if resolved.is_relative_to(real_config_dir):
            # A real install-local overlay — redirect to the guaranteed-absent
            # isolated dir so the loader falls through to shipped defaults.
            return user_dir / result.name
        return result

    mp.setattr(_config_overlay, "_resolve_overlay_path", _sandboxed_resolve)
    # immunity.py binds `_resolve_overlay_path` at MODULE level (a by-name
    # import), so its own reference must be patched too — patching only
    # _config_overlay's attribute does not reach it (record_demotion() would
    # otherwise still fall back to the real config/ws3_immunity.local.yaml
    # sibling). Lazy importers (memory/graph_expansion, ledger/learned_knobs)
    # re-resolve against the patched _config_overlay each call, so they need no
    # separate patch. The guard test tracks module-level importers of both names.
    mp.setattr(immunity, "_resolve_overlay_path", _sandboxed_resolve)
    try:
        yield
    finally:
        mp.undo()


@pytest.fixture(autouse=True)
def _isolate_ledger_write_failures():
    """Reset the ledger writer + grader failure counters around every test.

    ``genesis.ledger.writers._write_failures`` (P1b) and the P2 grader's
    ``_metric_vanished`` / ``_grade_failed`` are process-global Counters —
    correct for production (they accumulate since process start, read by
    ``_compute_alerts``), but they leak across tests: a hook/grader-failure
    test would otherwise make an unrelated health-alert test see a stray
    ``ledger:write_failed`` / ``ledger:grade_failed`` alert. Clear before and
    after each test.
    """
    from genesis.ego import proposals as _ego_proposals
    from genesis.ledger import cells as _ledger_cells
    from genesis.ledger import grader as _ledger_grader
    from genesis.ledger import writers as _ledger_writers

    _ledger_writers._write_failures.clear()
    _ledger_grader._reset_grade_failure_counts_for_tests()
    _ledger_cells._reset_cell_counters_for_tests()
    _ego_proposals._reset_arbitration_failures_for_tests()
    yield
    _ledger_writers._write_failures.clear()
    _ledger_grader._reset_grade_failure_counts_for_tests()
    _ledger_cells._reset_cell_counters_for_tests()
    _ego_proposals._reset_arbitration_failures_for_tests()


@pytest.fixture
async def db():
    """In-memory SQLite database with all tables created and seeded."""
    from genesis.db.connection import SerializedConnection
    from genesis.db.schema import create_all_tables, seed_data

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    await create_all_tables(conn)
    await seed_data(conn)
    await conn.commit()
    wrapped = SerializedConnection(conn)
    yield wrapped
    await wrapped.close()


@pytest.fixture(autouse=True)
def _guard_db_crud_not_mocked():
    """Pin the test that leaks a Mock onto a real ``genesis.db.crud`` function.

    A bare ``obs_crud.create = AsyncMock()`` — assigning a mock to a real module
    attribute *without* ``monkeypatch``/``patch`` — is never restored. It then
    silently poisons the shared ``db`` fixture for the rest of the session:
    inserts return a truthy Mock but write nothing, so a distant victim test
    reads 0 rows and fails mysteriously (cost us a multi-session hunt). This
    guard makes the leak fail at the *offending* test instead.

    Scoped to ``observations`` (the proven hotspot + highest-traffic crud
    module). Autouse fixtures tear down *after* explicitly-requested fixtures,
    so a legitimate ``monkeypatch.setattr(obs_crud, …)`` is already restored
    when this check runs — no false positives. Cost: one ``isinstance`` sweep
    of one small module's namespace per test.

    Caveat: a *session*/*module*-scoped fixture that patches ``obs_crud`` and is
    still active during a later function-scoped test's teardown would trip this
    guard (no such fixture exists today). Use function scope, or set the mock on
    a local object, if you ever need one.
    """
    from unittest.mock import Mock

    import genesis.db.crud.observations as obs_crud

    yield
    leaked = sorted(
        name
        for name, obj in vars(obs_crud).items()
        if not name.startswith("__") and isinstance(obj, Mock)
    )
    if leaked:
        raise AssertionError(
            "Test leaked unittest.mock object(s) onto real module "
            f"genesis.db.crud.observations: {leaked}. Use monkeypatch.setattr "
            "or `with patch(...)` so the patch is restored, or set the mock on a "
            "local mock object — never assign to the real module attribute."
        )


@pytest.fixture
async def empty_db():
    """In-memory SQLite database with tables but no seed data."""
    from genesis.db.connection import SerializedConnection
    from genesis.db.schema import create_all_tables

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    await create_all_tables(conn)
    await conn.commit()
    wrapped = SerializedConnection(conn)
    yield wrapped
    await wrapped.close()
