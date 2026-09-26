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

import contextlib  # noqa: E402
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


# ── Redirect pytest's temp tree onto disk, off every policed temp dir ─────
# pytest's ``tmp_path``/basetemp default under ``$TMPDIR``, and both places that
# resolves to here are small: a CC session's ``~/.genesis/cc-tmp`` (set by
# scripts/cc-slot.sh, budget-policed by ``genesis-tmp-watchgod``), and — with
# ``TMPDIR`` unset or ``/tmp`` — a 512 MB tmpfs, i.e. RAM. Left alone, a broad
# suite dumps hundreds of MB into one of them and pages the operator (MEASURED
# 2026-09-24: 255 MB of basetemp in the 512 MB RAM disk).
# This steers pytest's own basetemp to ``~/tmp`` (``big_tmp_dir``) instead —
# WITHOUT touching the process ``TMPDIR`` (which would desync CC's
# TMPDIR/CLAUDE_CODE_TMPDIR). Runs at config time, before any ``tmp_path``
# fixture resolves. Redirects by DEFAULT; no-ops on CI and when ``--basetemp``
# is passed. See ``genesis.util.tmp.should_redirect_pytest_basetemp``.
def _warn_without_escalating(message: str) -> None:
    """Emit a RuntimeWarning that CANNOT be turned into an exception.

    Both call sites run inside ``pytest_configure``, where any escaping
    exception is an INTERNALERROR that kills collection for the entire
    repository — and because the crash happens before the cleanup completes,
    the next run hits it again. That failure has now been reached twice by two
    different exception types (a ``TypeError`` from an ``onexc`` handler, then
    this), so the fix is the BOUNDARY rather than another predicate.

    ``RuntimeWarning`` is an ``Exception``, not an ``OSError``, so the
    ``except OSError`` around the caller does not stop it. MEASURED: under
    ``PYTHONWARNINGS=error`` (and ``python -W error``) a real pytest run dies
    with ``INTERNALERROR ... RuntimeWarning``. Note ``pytest -W error`` does
    NOT do this — pytest applies its own ``-W`` inside a ``catch_warnings``
    block, later than configure time — which is exactly why the interpreter-level
    form is easy to miss, and why the person most likely to have it exported is
    the one debugging this very warning.

    ``simplefilter("always")`` inside ``catch_warnings`` restores the global
    filter state on exit, so this neither leaks a filter nor suppresses anyone
    else's.
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn(message, RuntimeWarning, stacklevel=3)


def _force_rmtree(path: str) -> None:
    """Remove a tree even when it contains unreadable or unwritable directories.

    ``shutil.rmtree(..., ignore_errors=True)`` CANNOT do this, and that is not a
    corner case here. Unlinking a file needs write+execute on its PARENT
    directory, so a single ``0o500`` dir strands the whole subtree — and
    ``ignore_errors`` discards the PermissionError without a trace, so the
    failure is perfectly silent.

    MEASURED 2026-09-25 on this install before the fix: ``~/tmp/pytest`` held 55
    leaves, all 55 with dead PIDs (so the reaper considered every one of them
    eligible) and all 55 containing an unwritable directory. Nothing had ever
    removed a single one of them. The producer is ordinary rather than exotic:
    ``tests/test_cc/conftest.py`` has an AUTOUSE fixture whose sealed gh config
    the code under test chmods read-only, and this suite chmods ``0o000`` at
    more than twenty sites. Those restore in a ``finally:`` — which is exactly
    what does not run on the SIGKILL this reaper exists for.

    WHY A PRE-PASS RATHER THAN A RETRY HANDLER, which is the whole design and
    was got wrong once. The obvious shape is an ``onexc`` handler that chmods
    and calls ``func(path)`` again. It does not work, for two independent
    reasons, both read from CPython 3.12's ``shutil`` rather than assumed:

    * ``onexc`` is invoked with the function that failed, and that is NOT always
      a one-path call. ``shutil.py:682`` and ``:781`` pass ``os.open`` (which
      needs ``(path, flags)``) and ``:692/:712/:808`` pass ``os.close`` (which
      needs a descriptor). Calling either with a single path raises
      ``TypeError`` — which is not an ``OSError``, so it escapes every
      ``except OSError`` around it, propagates out of ``pytest_configure``, and
      turns every run in the repository into an ``INTERNALERROR``. pytest's own
      handler guards exactly this with an allowlist
      (``_pytest/pathlib.py:101``: ``if func not in (os.rmdir, os.remove,
      os.unlink)``), which a reimplementation is very likely to drop, because
      the allowlist looks like defensive noise rather than the load-bearing line.
    * Even with the allowlist, retrying cannot fix an unreadable directory.
      After ``onexc(os.open, ...)`` at ``:682``, ``_rmtree_safe_fd`` falls past
      the ``else:`` and never descends — so the subtree is orphaned whatever the
      handler did. pytest's own ``rm_rf`` cannot remove a ``0o000`` directory
      either; it emits ``PytestWarning: (rm_rf) error removing ...`` and leaves
      it. Only granting the permission BEFORE the walk reaches the directory
      works.

    So: attempt the cheap removal first, and only if something survives, walk
    the tree top-down granting ``u+rwx`` on each directory before ``os.walk``
    descends into it, then remove again. ``topdown=True`` is what makes that
    possible — the descent into ``dirnames`` happens after the loop body, so
    chmod-ing them in the body is in time.

    No ``onexc`` handler ever calls ``func``, so the ``TypeError`` class above
    cannot recur. Nothing outside ``path`` is ever chmod-ed. Symlinks are never
    followed (``followlinks=False``, and directory symlinks are skipped
    explicitly) so a link inside the tree cannot be used to relax permissions on
    a target outside it, and a hardlink's shared inode is never touched because
    only DIRECTORIES are chmod-ed.

    Best-effort, and LOUD when it fails: anything still present after both
    passes is warned about rather than left silent, because a silently leaked
    leaf is the exact defect this replaced.
    """
    import shutil
    import stat

    def _swallow(func, failed_path, exc):
        # Deliberately does NOTHING — in particular it never calls `func`. See
        # the docstring: `func` may be `os.open`/`os.close`, whose signatures a
        # single-path call does not satisfy, and the resulting TypeError is not
        # an OSError and would escape pytest_configure.
        return

    # A SYMLINK AS THE ROOT defeats both guards below, so it is handled before
    # them. `os.chmod` follows links, and `os.walk` follows its TOP regardless of
    # `followlinks` (os.py:344 vs :401) — so a leaf that is a link would relax
    # permissions on, and walk into, a tree outside this one. The reaper selects
    # leaves by NAME (an all-digits dead pid) and never checks the type, so this
    # is reachable rather than theoretical. Unlink the link; never its target.
    if os.path.islink(path):
        with contextlib.suppress(OSError):
            os.unlink(path)
        return

    shutil.rmtree(path, onexc=_swallow)
    # lexists, not exists: `exists` follows symlinks and reports False for a
    # DANGLING one, so a leaf left behind as a broken link would read as a clean
    # removal. Belt-and-braces rather than load-bearing, and labelled as such
    # because the distinction is invisible: the symlink-root guard above already
    # handles every dangling leaf this function is actually given, so the only
    # way here is a TOCTOU (a directory swapped for a broken link between that
    # check and this one). No test binds it — MEASURED: reverting to `exists`
    # leaves the suite green — and it is kept because it is free and correct,
    # not because anything proves it necessary.
    if not os.path.lexists(path):
        return

    # Something survived — grant traversal+write top-down, then try once more.
    def _grant(target: str) -> None:
        # Suppressed, not ignored: a directory we cannot chmod is reported by the
        # warning at the end rather than raised out of pytest_configure.
        with contextlib.suppress(OSError):
            os.chmod(target, os.stat(target).st_mode | stat.S_IRWXU)

    _grant(path)
    for dirpath, dirnames, _files in os.walk(path, topdown=True, followlinks=False):
        for name in dirnames:
            child = os.path.join(dirpath, name)
            if os.path.islink(child):
                continue  # never chmod through a link — the target may be outside
            _grant(child)

    shutil.rmtree(path, onexc=_swallow)
    if os.path.lexists(path):
        _warn_without_escalating(
            f"genesis: could not fully remove the pytest temp tree at {path}; "
            "something under it is undeletable by this user. It will be retried "
            "on the next run."
        )


def _reap_stale_pytest_basetemps(pytest_base: str) -> None:
    """Remove per-PID basetemp dirs left by runs that exited abnormally (SIGKILL/
    host crash — ``pytest_unconfigure`` never ran). A leaf is stale iff its name
    is an integer PID that is no longer alive; LIVE PIDs (concurrent runs) are
    spared, so this never touches another in-flight suite. Best-effort — this is
    the safety net that makes cleanup survive abnormal exits (``pytest_unconfigure``
    handles the normal path)."""
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
            _force_rmtree(os.path.join(pytest_base, name))  # dead → reap
        except (OSError, OverflowError):
            # alive-but-not-ours (PermissionError), transient, or an out-of-PID-range
            # name (os.kill(10**30,0) → OverflowError, which is NOT an OSError) → spare.
            pass


def _is_introspection_only(config) -> bool:
    """Modes that print and exit without running tests (--help/--fixtures/--markers).

    Kept as a predicate rather than inlined so the exemption set is one
    reviewable list; see the caller for why these must not be blocked.

    ``version`` is deliberately NOT in that list, which is not obvious. MEASURED
    across two pytest versions, because the project pins only ``pytest>=8.0`` and
    the behaviour differs between them:

    * ``--version`` — ``Config.main()`` short-circuits it (it matches the literal
      token at count 1) before the infrastructure starts, so we never run.
      Same on 9.0.3 and 9.1.1.
    * ``--version --version`` / ``-VV`` — ``helpconfig.pytest_cmdline_main``
      returns at ``version > 1``, before ``_do_configure()``. Again never run.
    * ``-V`` — VERSION-DEPENDENT, and that is the whole reason this note exists.
      On 9.0.3 neither branch fires (``Config.main`` matches the *string*
      ``--version``, and ``version == 1`` is not ``> 1``), so it falls through to
      ``wrap_session`` and runs the WHOLE SUITE. On 9.1.1 pytest answers it early
      and this hook never loads.

    Removing ``version`` is right under EITHER behaviour: where ``-V`` never
    reaches us the entry is simply dead, and where it does reach us it is a full
    unserialized suite that exempting would wave straight past the lock — the
    opposite of the point. It reads like a harmless entry, which is why the
    measurement is written down rather than left to the next reader's intuition,
    and why the test guarding it asserts the safety PROPERTY (``-V`` never runs a
    session past a held lock) rather than either version's mechanism. An earlier
    version of that test encoded 9.0.3's behaviour and failed on CI's 9.1.1.
    """
    opt = config.option
    return any(
        getattr(opt, name, False)
        for name in ("help", "showfixtures", "show_fixtures_per_test", "markers")
    )


def pytest_configure(config):
    # ── Box-wide serialization ─────────────────────────────────────────────
    # Acquire BEFORE anything else, and before the basetemp early-return
    # below, so every exit path from this function is already governed.
    #
    # This is the choke point every run of THIS repo's suite shares, whatever
    # launched it and from whichever worktree — nothing else sits on the path of
    # a cron job, a plain SSH shell or a background session. (The gauntlet
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
    # `~/tmp` (which would break a read-only-home run during config).
    if not should_redirect_pytest_basetemp(
        current_basetemp=config.option.basetemp,
        ci_env=os.environ.get("CI"),
    ):
        return
    # Scope the leaf per-process. pytest CLEARS an explicit basetemp at session
    # start and roots tmp_path directly under it (no pytest-of-<user> numbered
    # rotation), so two concurrent runs sharing one path would rmtree each other's
    # live temp. The box lock now serializes runs that load this conftest, but a
    # deliberate override (GENESIS_PYTEST_LOCK=0) or a foreign-rootdir run can
    # still overlap, so the per-pid leaf remains what keeps their temp isolated.
    #
    # FAIL OPEN on any filesystem error. The predicate above redirects by default
    # and exempts CI by reading `$CI`, so an unrecognised CI with a read-only
    # `$HOME` would reach this block — and an OSError escaping `pytest_configure`
    # kills collection for the WHOLE suite, which is strictly worse than the RAM
    # disk this redirect exists to protect. Falling back leaves pytest's own
    # default, i.e. exactly the behaviour that environment has today. This is what
    # makes CI detection an optimisation rather than a correctness requirement.
    # BROAD by design, and this is the second layer rather than the first. The
    # narrow `except OSError` below was correct for the filesystem calls it was
    # written for, and twice now something in this block has raised a class it
    # does not cover (a TypeError out of an onexc handler; a RuntimeWarning
    # escalated by an interpreter-level filter). Cleanup is best-effort by
    # contract, and an exception escaping pytest_configure kills collection for
    # the whole repository — so at THIS boundary, breadth is the safer error.
    # Each specific cause is still fixed at its source; this stops the next one
    # being catastrophic rather than cosmetic.
    try:
        pytest_base = os.path.join(big_tmp_dir(), "pytest")
        # Reap dead-PID leaves from prior abnormal exits BEFORE creating ours, so
        # the ~/tmp/pytest dir can't accumulate (the daily hygiene job only prunes
        # direct children of ~/tmp, whose mtime every run refreshes — so it never
        # ages out).
        _reap_stale_pytest_basetemps(pytest_base)
        target = os.path.join(pytest_base, str(os.getpid()))
        Path(target).mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001 — see the note above the try
        # Say so. A silent fall-back lands the temp tree back on whatever
        # pytest's default is — which on an install with a tmpfs /tmp is the
        # RAM disk this redirect exists to avoid. The operator's next signal
        # would otherwise be the incident recurring.
        _warn_without_escalating(
            f"genesis: could not place pytest basetemp on disk ({exc}); "
            "falling back to pytest's default, which may be a small tmpfs. "
            "Set GENESIS_BIG_TMP to a writable on-disk directory."
        )
        return
    config.option.basetemp = target
    config._genesis_basetemp_cleanup = target


def pytest_unconfigure(config):
    """Remove the per-process basetemp tree this session created. pytest wipes an
    explicit basetemp at session START but never at exit, and each run gets a new
    PID, so without this the ``~/tmp/pytest/<pid>`` dirs would accumulate (the
    daily hygiene job only prunes direct children of ``~/tmp``, whose mtime every
    new run refreshes — so it never ages out). Best-effort.

    Uses :func:`_force_rmtree`, not ``shutil.rmtree(ignore_errors=True)`` — see
    that function for why the plain form silently leaks every tree this suite
    produces."""
    target = getattr(config, "_genesis_basetemp_cleanup", None)
    try:
        if target:
            _force_rmtree(target)
    finally:
        # Release the box-wide lock LAST, so it spans the whole session including
        # this cleanup — but in a `finally`, so a cleanup that raises cannot leave
        # the lock held and block every other test run on the machine. (flock also
        # drops on process death, so a crash cannot wedge the box permanently;
        # this is the orderly path, not the only one.)
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


# ── Safety: prevent tests from writing REAL merge-override audit rows ───────
@pytest.fixture(autouse=True)
def _isolate_override_log(tmp_path):
    """Redirect the merge gate's override STORE to tmp for ALL tests.

    Any test that drives ``git_push_guard`` with an override sigil in the command
    now produces a durable audit row, and the default path is the live log the
    operator reads. MEASURED before this existed: four rows describing a PR that
    does not exist — one blocked command retried during development — reached the
    real store and had to be removed by hand. A store nobody isolated records
    fiction before it records anything true.

    Repo-wide rather than under ``tests/test_hooks/``: ``tests/test_scripts/``
    also drives these hooks (some as subprocesses), and a bare local
    ``git merge x  # merge-to-main-override`` is enough to write a row — no PR,
    no network. Set on the environment so subprocess-launched hooks inherit it.

    Uses a fixture-OWNED ``MonkeyPatch`` (not the shared ``monkeypatch``
    fixture) so a test that calls ``monkeypatch.undo()`` mid-body cannot revert
    this suite-isolation patch and re-expose the real log — mirrors
    ``_isolate_alert_queue``.
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("GENESIS_MERGE_OVERRIDE_DIR", str(tmp_path / "merge_overrides"))
    # The discard guard's recovery store, for the SAME reason and by the same
    # argument: tests/test_scripts/ drives that hook too, some as subprocesses, and
    # its live default is ~/.genesis/git_discard_snapshots. Today only
    # test_git_discard_guard.py sets it locally, so nothing leaks — this is here so
    # the NEXT test that drives that guard cannot write to the operator's real
    # recovery store. Isolating one of two sibling stores was the gap.
    mp.setenv("GENESIS_DISCARD_SNAPSHOT_DIR", str(tmp_path / "git_discard_snapshots"))
    # And the SUPERSEDED knob, which is config the operator may still carry. It no
    # longer steers the store, but setting it now makes the guard print a migration
    # notice — so leaving it inherited means the suite's stderr depends on the
    # developer's environment. Same gap as the sibling store above, one level up:
    # isolating the store but not the config that talks about it.
    mp.delenv("GENESIS_DISCARD_SNAPSHOT_LOG", raising=False)
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


def private_module(name: str, path):
    """Load ``path`` as a PRIVATE module without leaking ``name`` to everyone.

    A test wanting its own instance of a script registers it in ``sys.modules``
    before ``exec_module`` so that anything the module imports BY ITS OWN NAME
    during exec resolves to THIS copy rather than a previously-registered one
    (measured: a self-importing module sees the private instance). The trap is
    leaving it registered afterwards.

    Registering BEFORE exec is also what lets a ``@dataclass`` decorate at all,
    when the module carries ``from __future__ import annotations``:
    ``dataclasses._is_type`` dereferences ``sys.modules.get(cls.__module__)``,
    which is ``None`` for an unregistered module. Do not take that on trust from
    this docstring — ``tests/test_private_module.py`` locks it, and deleting the
    ``sys.modules[name] = mod`` line below fails there.

    Leaving the name registered is the leak this exists to prevent: pytest
    imports every test module at COLLECTION, so the last registration wins for
    the session, and a ``monkeypatch.setattr`` can then land on a different
    object than a call-time ``from <name> import ...`` resolves. Two locks in
    ``tests/test_hooks/test_escalation_cap.py`` assert exactly that for
    ``review_state`` and ``review_scope``.

    Prefer this over hand-rolling register/exec/restore: N call sites each
    remembering to restore is a convention, and conventions break one instance
    at a time.

    LIMIT, and it is real, because the instruction to prefer this helper routes
    you into it. A class defined in the loaded module resolves its string
    annotations against whatever ``sys.modules`` holds AFTER the restore, and the
    restore has TWO branches with different — and differently dangerous —
    outcomes for an annotation naming a module-level symbol:

    * name previously UNBOUND -> the entry is popped, and
      ``typing.get_type_hints`` (plus anything built on it: pydantic,
      ``inspect.signature(eval_str=True)``) raises ``NameError``. Loud.
    * name previously BOUND -> the PREVIOUS object is put back, so resolution
      SUCCEEDS against the canonical module and returns a same-named class from
      a different module object. Silent, and worse for that reason.

    ``dataclasses.fields`` is unaffected in both. Documented rather than locked,
    deliberately: the test that once pinned these two branches had to exec every
    carrier under ``scripts/`` to find them, which pulled a module-level
    ``load_dotenv(override=True)`` into the test process and replaced environment
    variables for everything collected after it — a worse defect than the caveat
    it was verifying. An earlier revision of this paragraph also asserted the
    first outcome unconditionally, which is wrong about the second — and the
    second is the SILENT one, which is why it is written down here.

    So if the script under test needs late annotation resolution, it is not a
    private-module candidate. (An earlier revision also claimed the opposite of
    the limit entirely — "5/5, no failures" — from four modules picked by hand
    for being easy to exec rather than from the population. Recorded because the
    convenience sample IS the failure mode, and it becomes invisible the moment
    it is written as a number.)
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - real files always resolve
        raise ImportError(f"cannot load {name} from {path}")
    mod = importlib.util.module_from_spec(spec)
    sentinel = object()
    previous = sys.modules.get(name, sentinel)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        # Restore in BOTH directions: put back what was there, or remove the
        # entry entirely if the name was previously unbound. Leaving our copy
        # registered when nothing was there before is the same leak, one step
        # removed — the next importer would silently get this private instance.
        if previous is sentinel:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    return mod


def require_access_denied(path) -> None:
    """Skip unless THIS process is actually stopped by ``path``'s mode bits.

    Probes the path that was chmod'd, not some writable neighbour of it — a
    probe aimed at the wrong path reports "denied" from a directory nobody
    restricted, which skips every run and pins nothing. Directories are probed
    by listing, files by opening, because those are the operations the callers
    rely on being refused.

    Any test that chmods a directory or file and then asserts on the failure is
    resting on a premise the environment can void: root and anything holding
    CAP_DAC_OVERRIDE write straight through mode bits, and CI containers
    routinely run as root. There the operation SUCCEEDS, and the test either
    fails for an unrelated reason or — worse — passes vacuously, having
    asserted that nothing went wrong in a run where nothing was ever blocked.

    Shared rather than repeated: the same premise underpins several chmod-based
    tests across the suite, and a lesson applied only where it was learned
    leaves the rest of the population exactly as it was.

    Restores nothing and mutates nothing — call it AFTER chmod, before the
    assertions, and let the caller's own ``finally`` restore the mode.
    """
    import pathlib

    p = pathlib.Path(path)
    try:
        if p.is_dir():
            list(p.iterdir())
        else:
            p.open("rb").close()
    except OSError:
        return  # genuinely denied — the premise holds
    pytest.skip(f"this process reads through mode bits on {p} (root / CAP_DAC_OVERRIDE)")


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


#: The breadcrumb's path, and the pid of the pytest session that OWNS it.
ACTIVE_TEST_FILE_ENV = "GENESIS_ACTIVE_TEST_FILE"
ACTIVE_TEST_OWNER_ENV = "GENESIS_ACTIVE_TEST_OWNER"


def _claim_active_test_breadcrumb():
    """Take the breadcrumb for THIS process, unless a parent pytest already has it.

    The path arrives through ``os.environ``, and this repo's own suite launches
    NESTED pytest runs that inherit it -- ``test_pytest_lock`` and
    ``test_proactive_hook_bounded_output`` both spawn a child with
    ``{**os.environ, ...}``. Without an owner every one of those children writes
    ITS node ids over the outer session's file, so after the child exits a hard
    crash in the still-running outer test is reported under the child's last
    test: a confident, wrong name, which is worse than no name at all.

    The owner pid is exported, so a child inherits it and sees a value that is
    not its own pid -- that comparison, not the mere presence of a variable, is
    what makes a nested run stand down. The idiom already exists here:
    ``pytest_lock``'s HELD_ENV does the same job for the box-wide lock, and its
    tests pop it precisely so a child stops mistaking itself for the outer run.

    LIMIT, stated rather than discovered later: under ``pytest-xdist`` every
    worker is a child that inherits the owner, so all of them stand down and no
    breadcrumb is written. The suite does not use xdist (it is not a declared
    dependency), and a wrong name is worse than a missing one, so standing down
    is the right direction to fail -- but a future ``-n`` would need a
    per-worker path rather than this claim.
    """
    if not os.environ.get(ACTIVE_TEST_FILE_ENV):
        return
    if os.environ.get(ACTIVE_TEST_OWNER_ENV):
        return  # inherited: an outer pytest owns it, and we are nested
    os.environ[ACTIVE_TEST_OWNER_ENV] = str(os.getpid())


def _owns_active_test_breadcrumb():
    """True only for the process that claimed the breadcrumb.

    Re-read from the environment on every write rather than cached, so a test
    that manipulates these variables sees the effect it asked for, and so the
    failure direction is silence rather than a wrong name.
    """
    return os.environ.get(ACTIVE_TEST_OWNER_ENV) == str(os.getpid())


# Guarded like everything else on this path. The hook below is documented
# best-effort throughout, and this is the one line that runs at IMPORT time --
# where a raise is not a failed test, it is a collection error that takes the
# whole suite down. A breadcrumb is a diagnostic; it never gets to be fatal.
with contextlib.suppress(Exception):  # see the best-effort note above
    _claim_active_test_breadcrumb()


def pytest_runtest_logstart(nodeid, location):
    """Record the test about to run, for a crash that never writes a report.

    CI drops ``-v`` because one line per test truncated the step log at ~44% of
    the suite, which left a red run naming no failing test at all. The junit
    report carries the names instead -- except when pytest never gets to write
    it, which is exactly what a segfault, an OOM kill or ``os._exit`` inside a
    test does. This is the channel that survives that: rewritten before every
    test and fsynced, so the last successful write names the test the crash
    happened in.

    INERT unless ``GENESIS_ACTIVE_TEST_FILE`` is set, so a local run pays
    nothing, and inert in a NESTED pytest run that inherited the variable from
    an outer session (see ``_claim_active_test_breadcrumb``). Best-effort
    throughout -- a breadcrumb that could fail the suite it exists to diagnose
    would be a poor trade.
    """
    path = os.environ.get(ACTIVE_TEST_FILE_ENV)
    if not path or not _owns_active_test_breadcrumb():
        return
    try:
        # surrogateescape, and `except Exception`, because a node id is not
        # guaranteed to be encodable. On POSIX a filename carrying a non-UTF-8
        # byte reaches this hook as a surrogate (os.fsdecode(b"tests/\xff.py")),
        # and a strict write raises UnicodeEncodeError -- which `except OSError`
        # does NOT catch, so a best-effort diagnostic would abort pytest with an
        # internal error BEFORE the test ran. The whole point of this hook is to
        # be readable after a crash; being the crash is the one outcome it may
        # not have.
        with open(path, "w", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(f"{nodeid}\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:  # noqa: BLE001 - see above: this may never fail the suite
        pass
