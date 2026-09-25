"""Tests for genesis.util.tmp.big_tmp_dir — the dedicated large-temp directory.

Large runtime producers (yt-dlp audio, STT uploads, git worktrees, eval artifacts)
must keep their temp OFF ~/.genesis/cc-tmp (the watchgod-policed 'oxygen' folder) by
passing dir=big_tmp_dir(). These verify the helper resolves + creates the right dir
and that tempfile actually honors it.
"""

import contextlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from genesis.util.tmp import big_tmp_dir, should_redirect_pytest_basetemp


def test_default_is_home_tmp_and_is_created(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GENESIS_BIG_TMP", raising=False)
    d = big_tmp_dir()
    assert d == str(tmp_path / "tmp")
    assert Path(d).is_dir(), "big_tmp_dir must create the directory"


def test_honors_env_override(tmp_path, monkeypatch):
    override = tmp_path / "custom-big-tmp"
    monkeypatch.setenv("GENESIS_BIG_TMP", str(override))
    d = big_tmp_dir()
    assert d == str(override)
    assert Path(d).is_dir()


def test_tempfile_lands_under_big_tmp_dir(tmp_path, monkeypatch):
    """A NamedTemporaryFile created with dir=big_tmp_dir() lives under it — the exact
    mechanism the runtime large-producers use to keep temp off cc-tmp."""
    monkeypatch.setenv("GENESIS_BIG_TMP", str(tmp_path / "big"))
    d = big_tmp_dir()
    with tempfile.NamedTemporaryFile(dir=d, suffix=".x") as f:
        assert Path(f.name).parent == Path(d)


def _load_conftest():
    """Load the real tests/conftest.py as a standalone module.

    Fresh each time: these arms drive `pytest_configure` directly, and module
    state from one must not leak into the next.
    """
    import importlib.util

    conftest_path = Path(__file__).resolve().parents[1] / "conftest.py"
    spec = importlib.util.spec_from_file_location("_genesis_conftest_probe", conftest_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── should_redirect_pytest_basetemp — the (pure, side-effect-free) decision ──
#
# Contract as of 2026-09-24: ALLOWLIST polarity. Redirect by DEFAULT; exempt only
# an explicit --basetemp and CI. The previous contract redirected only when TMPDIR
# already resolved to cc-tmp, which left every other entry path writing into the
# 512 MB tmpfs at /tmp -- the incident this replaced it.


def test_redirect_by_default():
    """The default case, and the one the old predicate got wrong: nothing special
    about the environment at all -> redirect. This is the whole point."""
    assert should_redirect_pytest_basetemp(None, None) is True


def test_noredirect_when_explicit_basetemp_passed():
    """An explicit --basetemp always wins -- never override the caller. Holds even
    on CI, and holds ahead of the CI check (order matters: a caller who named a
    location gets it regardless of where they are)."""
    assert should_redirect_pytest_basetemp("/some/where", None) is False
    assert should_redirect_pytest_basetemp("/some/where", "true") is False


def test_noredirect_on_ci():
    """CI keeps its own ample temp and may have a read-only $HOME. This is the
    behaviour the old predicate got for free from `TMPDIR unset -> False`; now it
    is stated, because the default flipped."""
    assert should_redirect_pytest_basetemp(None, "true") is False
    assert should_redirect_pytest_basetemp(None, "1") is False
    assert should_redirect_pytest_basetemp(None, "TRUE") is False


def test_ci_falsey_spellings_are_not_ci():
    """`CI=false` is the conventional way tooling opts a run OUT of CI behaviour.
    Reading it as "on CI" would invert the operator's intent."""
    for value in ("false", "False", "0", "no", "off", " false ", ""):
        assert should_redirect_pytest_basetemp(None, value) is True, value


def test_predicate_is_pure_no_filesystem_side_effect(tmp_path, monkeypatch):
    """The predicate must NOT create ~/tmp (or anything) -- that side-effect on the
    no-op path was the regression an earlier refactor fixed. Asserted on the
    REDIRECT path too, which is now the default: purity is what lets the caller
    decide whether it can afford the filesystem work."""
    monkeypatch.setenv("GENESIS_BIG_TMP", str(tmp_path / "should-not-exist"))
    assert should_redirect_pytest_basetemp(None, None) is True  # redirect path
    assert should_redirect_pytest_basetemp("/x", None) is False  # no-op path
    assert not (tmp_path / "should-not-exist").exists()


def test_call_site_falls_back_when_the_target_is_unwritable(tmp_path, monkeypatch):
    """The read-only-$HOME failure mode, end to end through the real conftest.

    CI detection is an optimisation, not a correctness requirement: an
    unrecognised CI reaches the filesystem block, and an OSError escaping
    pytest_configure would kill collection for the WHOLE suite. It must fall
    back to pytest's default instead -- exactly the behaviour that environment
    has today -- and must SAY SO, because a silent fall-back lands the temp tree
    back on whatever pytest's default is.

    The exception raised here is FileExistsError, from `big_tmp_dir()` itself
    (src/genesis/util/tmp.py:30) -- the FIRST statement in the guarded block,
    not the leaf mkdir. Stated because an earlier version of this docstring
    named NotADirectoryError from the wrong line; a real read-only home raises
    PermissionError. All three are OSError, which is the class that matters.
    """
    mod = _load_conftest()

    blocker = tmp_path / "ro-home"
    blocker.write_text("not a directory")
    monkeypatch.setenv("GENESIS_BIG_TMP", str(blocker))
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("GENESIS_PYTEST_LOCK", "0")

    config = SimpleNamespace(option=SimpleNamespace(basetemp=None))
    with pytest.warns(RuntimeWarning, match="could not place pytest basetemp"):
        mod.pytest_configure(config)  # must not raise
    try:
        assert config.option.basetemp is None, "must leave pytest's default in place"
        assert not hasattr(config, "_genesis_basetemp_cleanup")
    finally:
        mod.pytest_unconfigure(config)  # release the box lock this acquired


def test_call_site_actually_redirects(tmp_path, monkeypatch):
    """THE POSITIVE CONTROL, and the arm whose absence let the feature ship
    untested: every other arm here calls the predicate with literals, so the
    redirect itself could be DELETED with the file still green (measured --
    removing `config.option.basetemp = target` left 12/12 passing).

    Asserts the effect, not the decision: basetemp is moved to a per-pid leaf
    under GENESIS_BIG_TMP, and the directory exists.
    """
    mod = _load_conftest()

    big = tmp_path / "big"
    monkeypatch.setenv("GENESIS_BIG_TMP", str(big))
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("GENESIS_PYTEST_LOCK", "0")

    config = SimpleNamespace(option=SimpleNamespace(basetemp=None))
    mod.pytest_configure(config)
    try:
        expected = big / "pytest" / str(os.getpid())
        assert config.option.basetemp == str(expected)
        assert expected.is_dir()
        assert config._genesis_basetemp_cleanup == str(expected)
    finally:
        mod.pytest_unconfigure(config)


def test_call_site_reads_the_CI_variable(tmp_path, monkeypatch):
    """Pins WHICH environment variable the call site consults. Measured: with
    the call site reading a non-existent name instead of `CI`, every arm in
    this file stayed green -- the predicate tests cannot see the wiring."""
    mod = _load_conftest()

    big = tmp_path / "big"
    monkeypatch.setenv("GENESIS_BIG_TMP", str(big))
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GENESIS_PYTEST_LOCK", "0")

    config = SimpleNamespace(option=SimpleNamespace(basetemp=None))
    mod.pytest_configure(config)
    try:
        assert config.option.basetemp is None, "CI must be exempt, read from $CI"
        assert not (big / "pytest").exists(), "the no-op path must create nothing"
    finally:
        mod.pytest_unconfigure(config)


# The MODE SPACE, not one mode. The first version of these arms used a single
# fixture shape (0o500 dir, 0o400 file) -- which happened to be the one mode the
# first implementation handled -- and four single-element mutations survived it.
# 0o000 is the mode this suite actually chmods at more than twenty sites, and it
# is the one that made the first implementation raise TypeError out of
# pytest_configure. The "everything leaks" control is not a fourth mode but the
# per-arm inline one: each arm first proves the plain `ignore_errors` form
# CANNOT remove its fixture, so a pass is about _force_rmtree rather than about
# the fixture being trivially removable.
_SEALED_MODES = (0o500, 0o000, 0o300)


def _dead_pid() -> int:
    """A pid that is provably not running: spawn and reap a trivial child.

    NOT a hardcoded 999999 -- `/proc/sys/kernel/pid_max` is 4194304 here, so a
    literal is reusable and the arm would go vacuous the day it is reused.
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    for _ in range(50):
        if not Path(f"/proc/{proc.pid}").exists():
            return proc.pid
        time.sleep(0.02)
    raise AssertionError("pid never became free; arm would be vacuous")


def _sealed_leaf(leaf: Path, mode: int) -> Path:
    """A basetemp leaf whose inner directory is chmod-ed to `mode`."""
    sealed = leaf / "gh-sealed"
    sealed.mkdir(parents=True)
    (sealed / "config.yml").write_text("token: x")
    (sealed / "config.yml").chmod(0o400)
    sealed.chmod(mode)
    return sealed


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits under test")
@pytest.mark.parametrize("mode", _SEALED_MODES)
def test_cleanup_removes_a_leaf_containing_an_unwritable_directory(tmp_path, mode):
    """The leak that shipped, and the reason the suite's temp tree grew forever.

    `shutil.rmtree(..., ignore_errors=True)` CANNOT remove a tree holding a
    `0o500` directory -- unlinking needs write+execute on the PARENT -- and
    `ignore_errors` discards the PermissionError silently. MEASURED on this
    install before the fix: 55 leaves in ~/tmp/pytest, all 55 with dead PIDs
    (every one reap-eligible) and all 55 containing an unwritable directory;
    none had ever been removed.

    The producer is an ordinary autouse fixture (tests/test_cc/conftest.py
    seals a gh config read-only), so this is the common case, not a corner.
    """
    mod = _load_conftest()

    leaf = tmp_path / "pytest" / "999001"
    sealed = _sealed_leaf(leaf, mode)
    try:
        # Control: prove the plain form really cannot do it, so a pass below is
        # about _force_rmtree and not about the fixture being trivially removable.
        shutil.rmtree(str(leaf), ignore_errors=True)
        assert leaf.exists(), f"mode {mode:04o} fixture is removable — arm proves nothing"

        mod._force_rmtree(str(leaf))
        assert not leaf.exists(), f"mode {mode:04o}: an unwritable dir still strands the leaf"
    finally:
        if sealed.exists():
            sealed.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits under test")
def test_cleanup_never_raises_a_non_oserror_out_of_config(tmp_path):
    """REGRESSION. The first implementation retried inside an `onexc` handler by
    calling `func(path)`. CPython passes `os.open` there (shutil.py:682, :781)
    and `os.close` (:692, :712, :808) — neither takes a single path — so a
    `0o000` directory produced `TypeError: open() missing required argument
    'flags'`. TypeError is NOT an OSError, so it escaped every `except OSError`
    around it, out of `pytest_configure`, turning every run in the repository
    into an INTERNALERROR — permanently, since the crash precedes the removal.

    pytest's own handler guards this with an allowlist
    (`_pytest/pathlib.py:101`). This asserts the whole class is gone, not that
    one signature was special-cased.
    """
    mod = _load_conftest()
    leaf = tmp_path / "pytest" / "999002"
    sealed = _sealed_leaf(leaf, 0o000)
    try:
        mod._force_rmtree(str(leaf))  # must not raise ANYTHING
    finally:
        if sealed.exists():
            sealed.chmod(0o700)


def test_cleanup_never_invokes_the_failing_function(tmp_path):
    """The mechanism behind the arm above, pinned. A handler that calls `func`
    at all reopens the TypeError class for whichever signature it did not think
    of; the fix is a chmod PRE-PASS, so no handler ever calls it.

    Asserted over the AST rather than the text. A regex over the source matched
    the word inside this function's own docstring — a check that fires on prose
    is a check that gets deleted the next time someone edits a comment.
    """
    import ast

    src = (Path(__file__).resolve().parents[1] / "conftest.py").read_text()
    fn = next(
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "_force_rmtree"
    )
    offenders = [
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "func"
    ]
    assert not offenders, (
        f"conftest.py:{offenders}: an onexc handler is calling `func` again. "
        "CPython passes os.open (shutil.py:682, :781) and os.close (:692, :712, "
        ":808) there, and neither takes a single path — the resulting TypeError "
        "is not an OSError and escapes pytest_configure."
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits under test")
def test_cleanup_does_not_chmod_outside_the_tree(tmp_path):
    """It relaxes permissions to delete, so the blast radius is the finding.
    Only DIRECTORIES inside the tree may be touched: never the parent above it,
    never a hardlink's shared inode, never a symlink's target."""
    mod = _load_conftest()

    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("z")
    outside_file.chmod(0o400)
    outside_dir = tmp_path / "outside-dir"
    # NOT EMPTY, and that is the point. With no children, os.walk yields
    # `dirnames == []` for it and nothing is chmod-ed, so the victim's own mode
    # survives and the arm passes even with `followlinks=True` -- MEASURED, a
    # one-character mutation that all 22 arms missed. The nested dirs are what
    # a followed link would actually relax.
    (outside_dir / "sub1" / "sub2").mkdir(parents=True)
    (outside_dir / "sub1" / "sub2").chmod(0o500)
    (outside_dir / "sub1").chmod(0o500)
    outside_dir.chmod(0o500)

    leaf = tmp_path / "pytest" / "999003"
    sealed = leaf / "gh-sealed"
    sealed.mkdir(parents=True)
    (sealed / "config.yml").write_text("token: x")
    (sealed / "config.yml").chmod(0o400)
    # INSIDE the sealed dir, deliberately. Placed at the leaf root instead, the
    # first rmtree pass unlinks it before the chmod pre-pass ever walks the
    # tree, and the arm silently stops testing the symlink guard at all
    # (MEASURED: deleting the guard left this arm green).
    (sealed / "points-out").symlink_to(outside_dir)
    os.link(outside_file, tmp_path / "pytest" / "hardlinked.txt")
    sealed.chmod(0o500)

    parent_before = stat.S_IMODE(os.stat(tmp_path).st_mode)
    try:
        mod._force_rmtree(str(leaf))
        assert stat.S_IMODE(os.stat(tmp_path).st_mode) == parent_before, "chmod-ed above the tree"
        assert stat.S_IMODE(os.stat(outside_file).st_mode) == 0o400, (
            "mutated a hardlinked inode — only directories should ever be chmod-ed"
        )
        assert stat.S_IMODE(os.stat(outside_dir).st_mode) == 0o500, "chmod-ed through a symlink"
        # The NESTED modes are the sensitive ones: a followed link relaxes the
        # children while leaving the top-level victim untouched, so checking only
        # the obvious target misses it.
        assert stat.S_IMODE(os.stat(outside_dir / "sub1").st_mode) == 0o500, (
            "walked THROUGH a symlink and relaxed a directory outside the tree"
        )
        assert outside_dir.exists(), "followed a symlink out of the tree and deleted its target"
    finally:
        for d in (outside_dir / "sub1" / "sub2", outside_dir / "sub1", outside_dir):
            with contextlib.suppress(OSError):
                d.chmod(0o700)
        if sealed.exists():
            sealed.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits under test")
def test_cleanup_removes_a_leaf_whose_own_root_is_unwritable(tmp_path):
    """The leaf directory ITSELF sealed, not a child of it.

    Every other arm here nests the unwritable directory, so the pre-pass's
    grant on the tree ROOT was unbound — MEASURED: deleting it left the suite
    green. os.walk cannot list an unreadable root either, so without that first
    grant the loop never runs at all and nothing below is reached.
    """
    mod = _load_conftest()

    leaf = tmp_path / "pytest" / "999004"
    (leaf / "inner").mkdir(parents=True)
    (leaf / "inner" / "f.txt").write_text("x")
    leaf.chmod(0o000)
    try:
        shutil.rmtree(str(leaf), ignore_errors=True)
        assert leaf.exists(), "fixture is removable — this arm proves nothing"

        mod._force_rmtree(str(leaf))
        assert not leaf.exists(), "a sealed leaf ROOT still strands the tree"
    finally:
        if leaf.exists():
            leaf.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits under test")
def test_cleanup_warns_when_it_cannot_finish(tmp_path):
    """The "LOUD when it fails" half of the contract, which was unbound —
    MEASURED: deleting the final `warnings.warn` left all 22 arms green. A
    silently leaked leaf is the exact defect this function replaced, so the
    report is not decoration.

    The undeletable thing is a directory owned by another user (/proc/1, which
    this uid cannot chmod or remove) reached through a bind of the tree: not
    available here, so the fixture instead makes the leaf's PARENT read-only,
    which stops the leaf itself being unlinked.
    """
    leaf = tmp_path / "pytest" / "999005"
    leaf.mkdir(parents=True)
    (leaf / "f.txt").write_text("x")
    parent = leaf.parent
    parent.chmod(0o500)  # the leaf cannot be removed: no write on its parent
    try:
        mod = _load_conftest()
        with pytest.warns(RuntimeWarning, match="could not fully remove"):
            mod._force_rmtree(str(leaf))
        assert leaf.exists()
    finally:
        parent.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits under test")
def test_cleanup_warning_survives_an_interpreter_level_error_filter(tmp_path):
    """REGRESSION. `RuntimeWarning` is an Exception, not an OSError, so under
    `PYTHONWARNINGS=error` or `python -W error` the report itself became an
    INTERNALERROR out of pytest_configure — the same catastrophic route as the
    round-2 TypeError, wearing a different class, and self-perpetuating because
    the crash precedes the removal.

    Note `pytest -W error` does NOT reproduce it: pytest applies its own -W
    inside a catch_warnings block, later than configure time. The interpreter
    level is the one that bites, which is why this arm sets the filter directly.
    """
    import warnings as _w

    leaf = tmp_path / "pytest" / "999006"
    leaf.mkdir(parents=True)
    (leaf / "f.txt").write_text("x")
    parent = leaf.parent
    parent.chmod(0o500)
    try:
        mod = _load_conftest()
        with _w.catch_warnings():
            _w.simplefilter("error")  # every warning becomes an exception
            mod._force_rmtree(str(leaf))  # must NOT raise
    finally:
        parent.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits under test")
def test_cleanup_removes_a_sealed_directory_nested_inside_another(tmp_path):
    """The DEPTH axis. Every other arm is `leaf/<one sealed dir>/<file>`, and a
    single sealed directory is granted from the root's own `dirnames` whatever
    order the walk runs in — so `topdown=False` passed all 22 arms. Only a
    sealed directory INSIDE a sealed directory distinguishes them: bottom-up,
    the outer one is still unreadable when the walk tries to list it.
    """
    mod = _load_conftest()

    leaf = tmp_path / "pytest" / "999007"
    inner = leaf / "a" / "b"
    inner.mkdir(parents=True)
    (inner / "f.txt").write_text("x")
    inner.chmod(0o000)
    (leaf / "a").chmod(0o000)
    try:
        shutil.rmtree(str(leaf), ignore_errors=True)
        assert leaf.exists(), "fixture is removable — this arm proves nothing"

        mod._force_rmtree(str(leaf))
        assert not leaf.exists(), "a sealed directory nested in another still strands the tree"
    finally:
        for d in (leaf / "a" / "b", leaf / "a"):
            with contextlib.suppress(OSError):
                d.chmod(0o700)


def test_cleanup_unlinks_a_leaf_that_is_itself_a_symlink(tmp_path):
    """REGRESSION. `_grant` and `os.walk` both act on the ROOT unconditionally,
    and both follow a symlink there (os.chmod always; os.walk follows its TOP
    regardless of `followlinks`). The reaper selects leaves by NAME — an
    all-digits directory whose pid is dead — and never checks the type, so a
    leaf that is a link is reachable rather than theoretical: it would relax
    permissions across a tree outside this one, delete nothing, and leak
    forever while warning on every run.
    """
    mod = _load_conftest()

    target = tmp_path / "somewhere-else"
    (target / "kept").mkdir(parents=True)
    (target / "kept" / "f.txt").write_text("x")
    link = tmp_path / "pytest" / "999008"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)

    mod._force_rmtree(str(link))

    assert not link.exists() and not link.is_symlink(), "the link itself was not removed"
    assert target.exists(), "followed the link and deleted its target"
    assert (target / "kept" / "f.txt").exists()


def test_cleanup_removes_a_leaf_that_is_a_DANGLING_symlink(tmp_path):
    """A leaf that is a broken link. Covered by the symlink-root guard, not by
    the `lexists` check — stated precisely because the obvious reading is the
    other way round, and MEASURED: reverting `lexists` to `exists` leaves this
    arm green while removing the root guard turns it red."""
    mod = _load_conftest()

    link = tmp_path / "pytest" / "999009"
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / "does-not-exist")

    mod._force_rmtree(str(link))

    assert not link.is_symlink(), "a dangling leaf link was left in place"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits under test")
def test_unconfigure_removes_its_own_unwritable_leaf(tmp_path, monkeypatch):
    """The OTHER leak path, through the real session-exit hook rather than the
    helper. Paired with the reaper arm because the two call sites leak
    independently: fixing one and not the other still grows the tree forever,
    and an arm that calls `_force_rmtree` directly cannot tell them apart.
    """
    mod = _load_conftest()

    big = tmp_path / "big"
    monkeypatch.setenv("GENESIS_BIG_TMP", str(big))
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("GENESIS_PYTEST_LOCK", "0")

    config = SimpleNamespace(option=SimpleNamespace(basetemp=None))
    mod.pytest_configure(config)
    leaf = Path(config.option.basetemp)
    sealed = _sealed_leaf(leaf, 0o000)
    try:
        mod.pytest_unconfigure(config)
        assert not leaf.exists(), (
            "session exit left its own basetemp behind — this is how 55 leaves "
            "accumulated in ~/tmp/pytest"
        )
    finally:
        if sealed.exists():
            sealed.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits under test")
def test_reaper_removes_a_dead_pid_leaf_with_an_unwritable_directory(tmp_path):
    """Same defect through the reaper, which is the path that had leaked 55
    times. Pairs a provably-dead pid with a LIVE one so a pass cannot come from
    the reaper simply deleting everything."""
    mod = _load_conftest()

    base = tmp_path / "pytest"
    dead = base / str(_dead_pid())
    live = base / str(os.getpid())
    sealed = [_sealed_leaf(leaf, 0o000) for leaf in (dead, live)]
    try:
        mod._reap_stale_pytest_basetemps(str(base))
        assert not dead.exists(), "dead-pid leaf with an unwritable dir was not reaped"
        assert live.exists(), "reaped a LIVE run's leaf"
    finally:
        for sd in sealed:
            if sd.exists():
                sd.chmod(0o700)


def test_pytest_unconfigure_removes_per_pid_basetemp(tmp_path):
    """The conftest's pytest_unconfigure deletes the per-pid basetemp it recorded,
    so ~/tmp/pytest/<pid> dirs can't accumulate (the retention-leak fix). Tests the
    real conftest functions loaded fresh."""
    import importlib.util
    from types import SimpleNamespace

    conftest_path = Path(__file__).resolve().parents[1] / "conftest.py"
    spec = importlib.util.spec_from_file_location("_genesis_conftest_probe", conftest_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    leaf = tmp_path / "pytest" / "12345"
    leaf.mkdir(parents=True)
    (leaf / "scratch.txt").write_text("x")
    mod.pytest_unconfigure(SimpleNamespace(_genesis_basetemp_cleanup=str(leaf)))
    assert not leaf.exists(), "per-pid basetemp not cleaned at unconfigure"
    # No-op safe when nothing was recorded (e.g. the redirect never fired).
    mod.pytest_unconfigure(SimpleNamespace())


def test_reap_stale_pytest_basetemps(tmp_path):
    """The startup sweep reaps per-PID leaves from abnormal exits (dead PID),
    spares live runs (this process), and ignores non-PID / pid<=1 names — so a
    SIGKILL/crash can't leak the basetemp (pytest_unconfigure never runs then)."""
    import importlib.util
    import subprocess

    conftest_path = Path(__file__).resolve().parents[1] / "conftest.py"
    spec = importlib.util.spec_from_file_location("_genesis_conftest_probe2", conftest_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    base = tmp_path / "pytest"
    base.mkdir()
    # A genuinely-dead PID: spawn + reap a trivial process, then reuse its PID.
    proc = subprocess.Popen(["true"])
    proc.wait()
    dead = base / str(proc.pid)
    dead.mkdir()
    (dead / "scratch").write_text("x")
    live = base / str(os.getpid())
    live.mkdir()
    non_pid = base / "not-a-pid"
    non_pid.mkdir()
    init = base / "1"
    init.mkdir()
    # Unexpected on-disk state the reaper must survive without raising (it would
    # otherwise break collection for the WHOLE suite):
    superscript = base / "²"  # '²'.isdigit() True but int('²') → ValueError
    superscript.mkdir()
    huge = base / ("9" * 40)  # os.kill(10**40, 0) → OverflowError (not an OSError)
    huge.mkdir()

    mod._reap_stale_pytest_basetemps(str(base))  # must NOT raise

    assert not dead.exists(), "dead-PID leaf must be reaped"
    assert live.exists(), "live-PID (this process) must be spared"
    assert non_pid.exists(), "non-PID name must be ignored"
    assert init.exists(), "pid<=1 must be skipped (never signal 0/1)"
    assert superscript.exists(), "unicode-digit name must be skipped, not crash"
    assert huge.exists(), "out-of-range PID name must be spared, not crash"
    # No crash on a missing base dir.
    mod._reap_stale_pytest_basetemps(str(tmp_path / "does-not-exist"))
