"""The corpus cache holds real commands, so its file mode is a security control.

``scripts/replay_guard_corpus.py`` caches every distinct Bash command this
install has ever run, verbatim, so it can replay them through a guard. That
file demonstrably contains secrets passed in argv (an inline ``SSHPASS=`` was
found in it), which is why it is written 0600 and lives outside the repo.

The mode was being requested in a way that silently did not apply. ``os.open``
honours its mode argument ONLY on the call that actually creates the file, so a
``--rebuild`` over a cache already sitting at 0644 rewrote the secrets into a
world-readable inode — while the confirmation line printed ``(mode 0600)``,
because that string was hard-coded rather than measured. The tightening helper
existed but was wired only to the LOAD path, never the rebuild path.

Both halves are locked here: the inode is tightened on rebuild, and the mode
the tool announces is read back off the file rather than asserted.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "replay_guard_corpus.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("replay_guard_corpus", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec so the module's own `from __future__`/dataclass
    # machinery resolves normally, matching how the script runs as __main__.
    sys.modules["replay_guard_corpus"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def rgc():
    return _load_module()


_ROWS = [("echo one", "/tmp"), ("echo two", "/tmp")]


@pytest.fixture
def cache(tmp_path, rgc, monkeypatch):
    """Point the module at a throwaway cache and stub the transcript walk.

    The real walk covers a multi-gigabyte tree that only grows (6.0 GB / 140,293
    unique pairs on the origin install 2026-09-10, against ~1.4 GB / 51,052 five
    days earlier), which is why no test ever runs it.
    """
    path = tmp_path / "guard-corpus.jsonl"
    monkeypatch.setattr(rgc, "_CACHE", path)
    monkeypatch.setattr(rgc, "_extract_commands", lambda: list(_ROWS))
    return path


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_rebuild_over_a_world_readable_cache_tightens_the_inode(cache, rgc):
    """The regression: rebuilding onto an existing 0644 file left it 0644.

    O_CREAT's mode is not applied when the file already exists, so the secrets
    were rewritten into an inode anyone on the box could read.
    """
    cache.write_text('"echo stale"\n')
    cache.chmod(0o644)

    rgc.load_corpus(rebuild=True)

    assert _mode(cache) == 0o600, (
        "rebuild left the corpus cache at "
        f"{_mode(cache):04o}; it holds verbatim commands including secrets"
    )


def test_a_freshly_created_cache_is_never_world_readable(cache, rgc):
    """The create path, which was already correct — pinned so it stays that way."""
    assert not cache.exists()

    rgc.load_corpus(rebuild=True)

    assert _mode(cache) == 0o600


def test_a_leftover_temp_from_an_interrupted_rebuild_is_never_adopted(cache, rgc):
    """This test changed subject, deliberately — do not read it as the old one.

    It used to assert that a leftover ``<cache>.tmp`` had its MODE tightened,
    because the rebuild reused that one fixed name and renamed whatever inode it
    found. The name is not reused any more (mkstemp is O_EXCL), which turns the
    mode question into a CONTENT question and a sharper one: a stale temp must
    contribute nothing to the new cache. Under the old fixed-name code that very
    inode was the one renamed onto it, so its bytes BECAME the corpus.
    """
    leftover = cache.with_name(cache.name + ".tmp")
    leftover.write_text('["echo interrupted", "/tmp"]\n')
    leftover.chmod(0o644)

    rows = rgc.load_corpus(rebuild=True)

    assert rows == _ROWS
    assert "echo interrupted" not in cache.read_text(), (
        "an interrupted rebuild's temp was adopted as the corpus"
    )
    assert _mode(cache) == 0o600


def test_the_announced_mode_is_measured_not_asserted(cache, rgc, monkeypatch, capsys):
    """The confirmation line must report the mode the file HAS, not a constant.

    A hard-coded ``(mode 0600)`` is what kept the original bug invisible: the
    one line a reader would check said the right thing while the file said
    something else. Defeating BOTH tightening paths — the leftover temp's mode
    and the explicit fchmod — proves the message tracks the file rather than the
    intent. With them in place both values are 0600 and any string would pass.
    """
    # RE-SEAMED. Stubbing os.fchmod no longer diverges: mkstemp creates at 0600
    # by construction, so with fchmod disabled the file is still 0600 and a
    # hard-coded string would pass. The seam moved one layer out — let the real
    # rename happen, then loosen the mode behind it. A hard-coded line would
    # still announce 0600 here.
    real_replace = rgc.os.replace

    def replace_then_loosen(src, dst):
        real_replace(src, dst)
        os.chmod(dst, 0o644)

    monkeypatch.setattr(rgc.os, "replace", replace_then_loosen)

    rgc.load_corpus(rebuild=True)

    err = capsys.readouterr().err
    assert _mode(cache) == 0o644, "the replace stub did not take effect"
    assert "(mode 0644)" in err, f"announced mode does not match the file: {err!r}"


def test_load_path_tightens_a_world_readable_cache(cache, rgc):
    """Reading a cache written before the 0600 default also repairs it."""
    cache.write_text('["echo one", "/tmp"]\n')
    cache.chmod(0o644)

    assert rgc.load_corpus() == [("echo one", "/tmp")]

    assert _mode(cache) == 0o600


def test_a_cache_without_the_cwd_field_is_rebuilt_not_replayed(cache, rgc, capsys):
    """A v1 cache holds bare command strings — no cwd.

    Replaying those means replaying every command from the repo root, which is
    the invalid measurement the format change exists to fix: a guard that asks
    "am I inside a worktree?" answers "no" every time and the rate silently
    under-counts. Accepting a v1 line for compatibility would restore that with
    every other test still green, so the loader must rebuild instead.
    """
    cache.write_text('"echo legacy"\n')

    result = rgc.load_corpus()

    assert result == _ROWS, "a v1 cache was replayed instead of rebuilt"
    assert "rebuilding" in capsys.readouterr().err


def test_each_command_is_replayed_from_the_directory_it_was_typed_in(cache, rgc):
    """The cwd reaches BOTH the payload and the process.

    The Python guards call os.getcwd() directly rather than reading the
    payload's cwd field, so threading it into the payload alone would look
    correct and change nothing. Asserting both is what makes the fix real.
    """
    seen = {}

    def fake_main():
        seen["process_cwd"] = os.getcwd()
        seen["payload_cwd"] = mod.read_payload()["cwd"]
        return 0

    mod = types.SimpleNamespace(main=fake_main, read_payload=lambda: None)

    before = os.getcwd()
    with fake_loaded(rgc, "fake_guard", mod):
        rgc._run_python_guard("fake_guard", "echo hi", "/tmp")

    assert seen["process_cwd"] == "/tmp", "the process cwd did not move"
    assert seen["payload_cwd"] == "/tmp", "the payload cwd did not move"
    assert os.getcwd() == before, "the replay left the process in the wrong directory"


def test_a_vanished_directory_resolves_to_the_repo_root(cache, rgc):
    """The resolver half of the honesty valve."""
    rgc._SUBSTITUTED_CWD["n"] = 0

    resolved = rgc._effective_cwd("/nonexistent/directory/from/an/old/session")

    assert resolved == str(rgc._REPO)
    assert rgc._SUBSTITUTED_CWD["n"] == 1


def test_the_pool_asks_for_fork_explicitly(cache, rgc, monkeypatch):
    """The pooled path must not inherit the platform's default start method.

    ``_probe`` resolves the guard out of the module-global ``GUARDS`` table, and
    ``_INLINE_BLOB`` is read once at import. A spawn/forkserver worker re-imports
    the module instead of inheriting it, so any entry registered at runtime is
    simply absent there. MEASURED on this box (python 3.12) with a module global
    set in the parent only:

        fork        -> ('HIT', 99)
        forkserver  -> KeyError
        spawn       -> KeyError

    ``_probe`` converts that KeyError into ``crashed=True``, so the whole run
    reports as invalid rather than failing loudly. Python 3.14 makes forkserver
    the Linux default while this project declares ``requires-python >=3.12``, so
    leaving the context implicit means the tool behaves differently on two
    supported interpreters.

    This asserts the REQUEST, not the outcome: reverting to a bare
    ``mp.Pool(jobs)`` never calls ``get_context`` and turns this red, which a
    behavioural assertion could not do while fork is still the 3.12 default.
    """
    import multiprocessing as mp

    asked: list[str] = []
    real_get_context = mp.get_context

    def recording_get_context(method=None):
        asked.append(method)
        return real_get_context(method)

    monkeypatch.setattr(mp, "get_context", recording_get_context)

    with fake_guard(
        rgc,
        "fake",
        lambda c, w: False,
        spawns_process=True,
        safety=rgc.replay_safe("a test double; it touches nothing"),
    ):
        rgc.replay("fake", [("echo hi", "/tmp")] * 4, show=0, jobs=4)

    assert "fork" in asked, (
        "replay() did not request the fork start method — a spawn/forkserver "
        f"worker cannot see runtime GUARDS entries. get_context calls: {asked}"
    )


@pytest.mark.parametrize("jobs", [1, 4])
def test_the_substitution_notice_is_actually_printed(cache, rgc, capsys, jobs):
    """The valve's CONSUMER, across both execution paths.

    The counter is a module global. For shell guards with jobs > 1 the probe
    runs in a FORKED CHILD, so the increment landed in the child's copy and the
    parent's stayed 0 — the notice never printed for the two guards that fan
    out. The previous test called the resolver directly and passed the whole
    time, which is exactly how a dead consumer ships: it pinned the counter, not
    the disclosure.
    """
    corpus = [("echo hi", "/nonexistent/gone-worktree")] * 4
    with fake_guard(
        rgc,
        "fake",
        lambda c, w: bool(rgc._effective_cwd(w) and False),
        spawns_process=jobs > 1,
        safety=rgc.replay_safe("a test double; it touches nothing"),
    ):
        rgc.replay("fake", corpus, show=0, jobs=jobs)

    out = capsys.readouterr().out
    assert "replayed from the repo root" in out, f"no substitution notice (jobs={jobs})"
    assert f"{len(corpus)}/{len(corpus)}" in out


@pytest.mark.parametrize("jobs", [1, 4])
def test_a_guard_that_only_crashes_cannot_report_a_clean_rate(cache, rgc, capsys, jobs):
    """A crash counted as a block let a guard that never ran once print 100%.

    The two paths also disagreed: the pool swallowed every exception while the
    serial loop let it propagate, so the SAME broken guard produced either a
    quotable measurement or a traceback depending only on the job count. This
    tool exists to produce numbers for PR bodies, so a well-formatted wrong rate
    is its worst failure mode.
    """

    def boom(cmd, cwd):
        raise FileNotFoundError("guard is not installed")

    corpus = [("echo hi", "/tmp")] * 4
    with fake_guard(
        rgc,
        "boom",
        boom,
        spawns_process=jobs > 1,
        safety=rgc.replay_safe("a test double; it only raises"),
    ):
        rgc.replay("boom", corpus, show=0, jobs=jobs)

    out = capsys.readouterr().out
    assert "RAISED" in out, f"a fully broken guard reported silently (jobs={jobs})"
    assert "not a measurement" in out


def test_a_cache_truncated_mid_rebuild_names_itself_and_recovers(cache, rgc, capsys):
    """An interrupted rebuild used to brick the tool.

    The build walks the whole transcript tree; a kill part-way leaves a truncated
    final line, and
    the next load died inside a list comprehension with a JSON error that named
    neither the cache nor the remedy. The file then had to be deleted by hand.
    """
    cache.write_text('["echo one", "/tmp"]\n["echo tw')

    result = rgc.load_corpus()

    assert result == _ROWS, "a corrupt cache was not rebuilt"
    err = capsys.readouterr().err
    assert "corrupt" in err and str(cache) in err


def test_the_guard_child_never_inherits_a_dispatched_session_flag(rgc, monkeypatch):
    """This test changed subject too, and the history is the reason it exists.

    It used to pin a GENESIS_DISCARD_SNAPSHOT_LOG redirect, added after a
    MEASURED finding: replaying `bash_safety` reached git_discard_guard through a
    delegation and wrote rows to the live recovery store, which is bounded and
    would have evicted real recovery history. That redirect is gone, because it
    was never sufficient — it moved the RECORDS but not the objects
    `git stash create` writes into whatever repository each row was recorded in.
    `bash_safety` is refused outright now, and the half-measure went with it.
    (The knob itself has since been retired upstream; git_discard_guard prints
    "GENESIS_DISCARD_SNAPSHOT_LOG is no longer read" and resolves
    GENESIS_DISCARD_SNAPSHOT_DIR instead. The reasoning is unchanged.)

    What still matters on this path is the one variable measured to change a
    VERDICT rather than a message: bash_safety_hook.sh exits 0 when
    `_in_genesis` is 1 and GENESIS_CC_SESSION is not exactly "1", so the same
    corpus yields a different rate depending on who ran the harness.
    """
    seen = {}

    def fake_run(argv, **kwargs):
        seen["env"] = kwargs.get("env") or {}

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(rgc.subprocess, "run", fake_run)
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")

    rgc._run_shell_guard(["bash", "/nonexistent-hook"], "echo hi", "/tmp")

    env = seen["env"]
    assert "GENESIS_CC_SESSION" not in env, (
        "the dispatched-session flag reached the guard child; the rate now "
        "depends on which session ran the harness"
    )
    # NOT a degenerate env={}. That would satisfy the line above while breaking
    # every guard it runs, and the failure would look like a guard bug.
    assert env.get("PATH"), "PATH was stripped from the child env"
    assert env.get("HOME"), "HOME was stripped from the child env"


@pytest.mark.parametrize("var", ["BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "BASH_XTRACEFD"])
def test_the_guard_child_never_inherits_a_shell_startup_channel(rgc, monkeypatch, var):
    """The shell's OWN startup channel, which is not a variable the guard reads.

    A non-interactive ``bash -c`` SOURCES $BASH_ENV before it runs anything, and
    this path spawns one bash per corpus row plus nested shells and command
    substitutions inside each. Inherited, an operator whose environment exports
    BASH_ENV would have their startup file executed a few hundred thousand times
    by a tool whose docstring says it replays local history. SHELLOPTS/BASHOPTS
    are the same channel by another door: exported, they switch options on in the
    child (errexit, xtrace, onecmd) and change what the guard DOES.

    Distinct from the GENESIS_CC_SESSION case above, which is a variable the hook
    itself reads. Nothing in the blob reads any of these — bash does.
    """
    seen = {}

    def fake_run(argv, **kwargs):
        seen["env"] = kwargs.get("env") or {}

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(rgc.subprocess, "run", fake_run)
    monkeypatch.setenv(var, "/tmp/would-be-sourced")

    rgc._run_shell_guard(["bash", "-c", "true"], "echo hi", "/tmp")

    assert var not in seen["env"], (
        f"{var} reached the guard child; replaying a shell guard would trigger "
        "the shell's startup path once per corpus row"
    )
    assert seen["env"].get("PATH"), "PATH was stripped from the child env"


def test_the_in_process_guard_never_inherits_the_hosts_argv(rgc, monkeypatch):
    """argv is ambient process state, and one of these guards branches on it.

    worktree_cwd_guard.main() selects its Enter/ExitWorktree classifiers on
    ``"--enter-worktree" in sys.argv``. replay() is a supported API, so for an
    imported caller sys.argv is whatever ITS operator typed — a pytest
    invocation, a wrapper script. Inherited, an unrelated flag puts every
    ordinary Bash row through the wrong classifier.

    Asserts three things, and the third is the one a refactor would silently
    break. The pin uses SLICE ASSIGNMENT rather than rebinding, because a guard
    written `from sys import argv` — or with a module-level `_ARGV = sys.argv` —
    holds the list itself and never sees a rebind, so it would keep reading the
    HOST's argv with the pin apparently in place and nothing to signal it. No
    guard in scripts/hooks/ is spelled that way today; the point is that the pin
    keeps working if one is.

    So identity is asserted alongside content. Under rebinding
    `sys.argv is host_argv` is False after the call, and a content-only
    assertion would pass either way — which would make this test unable to tell
    the two implementations apart, exactly the gap that lets a refactor through.
    """
    seen = {}

    def fake_main():
        seen["argv"] = list(sys.argv)
        return 0

    mod = types.SimpleNamespace(main=fake_main, read_payload=lambda: None)

    host_argv = ["pytest", "--enter-worktree", "-k", "something"]
    host_snapshot = list(host_argv)
    monkeypatch.setattr(sys, "argv", host_argv)

    with fake_loaded(rgc, "worktree_cwd_guard", mod):
        rgc._run_python_guard("worktree_cwd_guard", "echo hi", "/tmp")

    assert "--enter-worktree" not in seen["argv"], (
        "the host's argv reached the guard; --enter-worktree makes every row "
        f"block. guard saw: {seen['argv']}"
    )
    assert seen["argv"] == [str(rgc._HOOKS / "worktree_cwd_guard.py")], (
        f"the guard did not see its production argv: {seen['argv']}"
    )
    assert sys.argv == host_snapshot, "the host's argv content was not restored"
    assert sys.argv is host_argv, (
        "the pin REBOUND sys.argv instead of slice-assigning it — a guard that "
        "captured the list at import would still see the host's argv"
    )


def test_a_raising_restore_still_undoes_the_other_two_mutations(rgc, monkeypatch):
    """The `finally` must not abandon its own remaining restores.

    `_run_python_guard` mutates three pieces of ambient state and undoes them in
    one `finally`. If a restore that CAN throw runs before the others, one throw
    strands them — and `os.chdir(prior)` is exactly that: it raises when the
    invocation directory has been removed, which is not exotic in a harness whose
    subject is worktrees being removed.

    MEASURED before the fix, with chdir restoring first and unwrapped: the
    FileNotFoundError propagated out of the `finally`, `sys.argv` kept the pinned
    value and `mod.read_payload` kept the patched lambda. `_probe` catches that
    as one crash and the run CONTINUES — so every later row is classified against
    this row's payload while the report discloses a single crash. A silently
    wrong number is the one outcome this file exists to prevent, which is why the
    cannot-throw restores now go first and chdir goes last, suppressed.
    """

    def sentinel():
        return {"sentinel": True}

    mod = types.SimpleNamespace(main=lambda: 0, read_payload=sentinel)

    host_argv = ["pytest", "-k", "something"]
    host_snapshot = list(host_argv)
    monkeypatch.setattr(sys, "argv", host_argv)

    real_chdir = os.chdir
    calls = {"n": 0}

    def chdir_that_fails_on_the_way_back(path):
        calls["n"] += 1
        if calls["n"] == 1:  # into the row's directory — must succeed
            return real_chdir(path)
        raise FileNotFoundError(2, "No such file or directory", str(path))

    monkeypatch.setattr(rgc.os, "chdir", chdir_that_fails_on_the_way_back)

    before = real_chdir, os.getcwd()
    with fake_loaded(rgc, "restore_raiser", mod):
        rgc._run_python_guard("restore_raiser", "echo hi", "/tmp")

    assert calls["n"] == 2, "the restore chdir never ran, so nothing was proven"
    assert sys.argv == host_snapshot, (
        "a raising chdir restore stranded the argv restore — every later row "
        "would run with this row's pinned argv"
    )
    assert mod.read_payload is sentinel, (
        "a raising chdir restore stranded the read_payload restore — every later "
        "row would be classified against THIS row's payload"
    )
    real_chdir(before[1])


# ── replay-safety declaration ────────────────────────────────────────────────
#
# The exclusions used to live in prose comments that reasoned about WHICH GUARD
# was in the table. A delegation defeated exactly that: `bash_safety` is in the
# table and reaches `git_discard_guard`, which is not, through a CALLER. A
# comment cannot be defeated that way once what it governs is permission to run
# at all, declared per entry and enforced by the runner.


_UNSET = object()


@contextlib.contextmanager
def fake_guard(rgc, name, fn, *, spawns_process=False, safety=_UNSET, **extra):
    """Register a guard for one test and remove it afterwards.

    Replaces the old pattern of poking two globals (``GUARDS`` and
    ``_SHELL_GUARDS``), which could disagree. Passing no ``safety`` builds the
    record the way a careless newcomer would — that is the point of the first
    test below, so the default here must stay "argument omitted" rather than
    "declared unsafe".
    """
    kwargs = {"run": fn, "spawns_process": spawns_process, **extra}
    if safety is not _UNSET:
        kwargs["safety"] = safety
    rgc.GUARDS[name] = rgc.Guard(**kwargs)
    try:
        yield
    finally:
        rgc.GUARDS.pop(name, None)


@contextlib.contextmanager
def fake_loaded(rgc, name, mod):
    """Put a double in the guard-import cache for ONE test, then take it out.

    `_run_python_guard._loaded` is a process-global memo and the `rgc` fixture is
    module-scoped, so an entry written by one test is visible to every later one.
    That is harmless for an invented name and NOT harmless for a real guard name:
    a later test — or the same tests under `-k`, a different order, or a plugin
    that shuffles them — would silently run the double instead of the guard. The
    leak is latent today only because nothing downstream happens to look it up,
    which is a property of the current file rather than of the fixture.
    """
    loaded = rgc._run_python_guard._loaded
    had = name in loaded
    prior = loaded.get(name)
    loaded[name] = mod
    try:
        yield mod
    finally:
        if had:
            loaded[name] = prior
        else:
            loaded.pop(name, None)


def _run_cli(rgc, monkeypatch, *argv):
    """Invoke main() with argv, refusing to let it touch the real corpus."""
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", *argv])
    monkeypatch.setattr(
        rgc,
        "load_corpus",
        lambda **kw: pytest.fail("load_corpus ran — the refusal came too late"),
    )
    return rgc.main()


def test_a_guard_registered_without_a_safety_declaration_is_refused(rgc, monkeypatch, capsys):
    """THE test that discriminates this mechanism from the one that failed.

    A denylist-shaped fix — excluding `bash_safety` by name — would let this
    newcomer RUN, because it is on no list. Only a per-entry declaration whose
    DEFAULT is refusal catches a guard nobody thought about. The tripwire
    matters as much as the exit code: a refusal that still executes the guard
    has protected nothing.
    """
    ran = []

    def tripwire(cmd, cwd):
        ran.append((cmd, cwd))
        return True

    with fake_guard(rgc, "newcomer", tripwire):
        code = _run_cli(rgc, monkeypatch, "--guard", "newcomer")

    assert not ran, "an UNDECLARED guard was executed against the corpus"
    assert code == 2, (
        f"refusal exited {code}; a refusal that exits 0 lets a wrapper conclude "
        "the measurement succeeded"
    )
    printed = capsys.readouterr()
    assert "newcomer" in printed.out + printed.err


def test_replay_refuses_an_unsafe_guard_even_when_called_directly(rgc):
    """The second layer. The CLI refuses first, but importing this module and
    calling replay() must not be the way around the declaration — a bypass that
    needs no flag is still a bypass."""
    with (
        fake_guard(
            rgc, "nope", lambda c, w: True, safety=rgc.not_replay_safe("writes to a live repo")
        ),
        pytest.raises(RuntimeError, match="not replay-safe"),
    ):
        rgc.replay("nope", [("echo hi", "/tmp")], show=0, jobs=1)


def test_a_declared_unsafe_guard_is_refused_before_the_corpus_is_built(rgc, monkeypatch, capsys):
    """The corpus build walks the whole transcript tree. A refusal that arrives
    after it has spent
    those minutes is not a refusal, and the name stays in argparse `choices` so
    the answer is the reason rather than 'invalid choice'."""
    code = _run_cli(rgc, monkeypatch, "--guard", "bash_safety")

    assert code == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "git stash create" in err, "the refusal did not say what it protects against"


def test_an_in_process_guard_that_crashes_is_disclosed_not_scored(rgc, capsys):
    """FINDING A, and the one that mattered most — it affects the guards that
    REMAIN replayable.

    ``_run_python_guard`` caught Exception and returned True. The verdict was
    right (run_guard converts a crash to a block for these fail-closed guards)
    but it CONSUMED the exception, so ``_probe`` recorded crashed=False. An
    ImportError therefore crashed every row and printed a clean, quotable
    100.00% — while _probe's own docstring promised crashes were reported.
    """

    def boom_main():
        raise ImportError("shell_parse is not importable")

    crashy = types.SimpleNamespace(main=boom_main, read_payload=lambda: None)
    corpus = [("echo hi", "/tmp")] * 4
    with (
        fake_loaded(rgc, "crashy", crashy),
        fake_guard(
            rgc,
            "crashy",
            lambda c, w: rgc._run_python_guard("crashy", c, w),
            safety=rgc.replay_safe("a test double"),
        ),
    ):
        rgc.replay("crashy", corpus, show=0, jobs=1)

    out = capsys.readouterr().out
    assert "RAISED" in out, "a guard that crashed on EVERY row reported silently"
    assert "not a measurement" in out
    # The numerator is deliberately unchanged: production blocks on a crash too.
    # Only the disclosure is new, so a "fix" that drops crashes from the count
    # would be a different bug.
    assert "4/4" in out


def test_a_crashing_in_process_guard_still_restores_cwd_and_read_payload(rgc):
    """FORWARD PIN — this does NOT fail against today's code, and says so.

    Deleting the `except Exception` is only safe because the `finally` survives
    it. That block restores the process cwd and un-patches the guard module's
    read_payload; a leaked patch would classify every LATER command against this
    command's payload, silently. The obvious careless fix removes the `try` and
    the `finally` together.
    """

    def boom_main():
        raise RuntimeError("boom")

    def sentinel():
        return {"sentinel": True}

    mod = types.SimpleNamespace(main=boom_main, read_payload=sentinel)
    before = os.getcwd()

    with fake_loaded(rgc, "restorer", mod), pytest.raises(RuntimeError):
        rgc._run_python_guard("restorer", "echo hi", "/tmp")

    assert os.getcwd() == before, "a crashing guard left the process in its cwd"
    assert mod.read_payload is sentinel, "the payload patch leaked past the crash"


@pytest.mark.parametrize("jobs", [1, 4])
def test_a_timed_out_guard_is_disclosed_and_still_counted(rgc, capsys, jobs):
    """FINDING B. A timeout was already counted as a block — correctly, since
    production would block — but invisibly: crashed stayed False, so nothing
    distinguished "the guard blocked this" from "the guard never answered".

    The 4/4 assertion is load-bearing in the other direction: it stops a "fix"
    that drops timed-out rows from the corpus, which would quietly shrink the
    denominator instead of disclosing the problem.
    """

    def hang(cmd, cwd):
        raise subprocess.TimeoutExpired(cmd="guard", timeout=rgc._GUARD_TIMEOUT_S)

    corpus = [("echo hi", "/tmp")] * 4
    with fake_guard(
        rgc, "hang", hang, spawns_process=jobs > 1, safety=rgc.replay_safe("a test double")
    ):
        rgc.replay("hang", corpus, show=0, jobs=jobs)

    out = capsys.readouterr().out
    assert "TIMED OUT" in out, f"a guard that never answered was silent (jobs={jobs})"
    assert "4/4" in out
    # A hang and a crash have different fixes, so they must not share a verb.
    assert "RAISED" not in out


def test_two_rebuilds_never_share_a_temp_name(cache, rgc, monkeypatch):
    """A fixed `<cache>.tmp` was shared mutable state between processes. One
    string comparison, deliberately: racing two real rebuilds would be flaky and
    would prove less."""
    seen = []
    real_replace = rgc.os.replace

    def record(src, dst):
        seen.append(str(src))
        real_replace(src, dst)

    monkeypatch.setattr(rgc.os, "replace", record)

    rgc.load_corpus(rebuild=True)
    rgc.load_corpus(rebuild=True)

    assert len(seen) == 2
    assert seen[0] != seen[1], f"both rebuilds wrote through the same temp: {seen[0]}"


def test_a_second_rebuild_starting_mid_write_cannot_corrupt_the_first(cache, rgc, monkeypatch):
    """The interleave is FORCED, not raced — a scheduler-dependent test would be
    flaky and would not pin the ordering that actually broke.

    With one fixed temp name the second rebuild O_TRUNC'd the inode the first was
    still writing, and the first's os.replace then died FileNotFoundError after
    minutes of work.
    """
    real_replace = rgc.os.replace
    depth = []

    def replace_with_a_nested_rebuild(src, dst):
        depth.append(1)
        if len(depth) == 1:
            # A whole second rebuild completes while the first is mid-flight.
            rgc.load_corpus(rebuild=True)
        real_replace(src, dst)

    monkeypatch.setattr(rgc.os, "replace", replace_with_a_nested_rebuild)

    rows = rgc.load_corpus(rebuild=True)

    assert rows == _ROWS
    assert cache.exists() and cache.read_text().strip(), "the cache was left empty"
    assert _mode(cache) == 0o600


def test_list_shows_a_refused_guard_with_its_reason(rgc, monkeypatch, capsys):
    """A refused guard vanishing from --list is the absence-as-exclusion pattern
    this design replaced: absence teaches nothing, and nothing then reminds the
    next reader why."""
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", "--list"])

    assert rgc.main() == 0

    out = capsys.readouterr().out
    assert "bash_safety" in out and "REFUSED" in out
    assert "git stash create" in out
    assert "protected_paths" in out and "replayable" in out


def test_all_runs_the_safe_guards_and_names_the_refused_ones(rgc, monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(
        rgc,
        "GUARDS",
        {
            "safe_one": rgc.Guard(
                run=lambda c, w: bool(ran.append("safe_one")),
                safety=rgc.replay_safe("a test double"),
            ),
            "refused_one": rgc.Guard(
                run=lambda c, w: bool(ran.append("refused_one")) or True,
                safety=rgc.not_replay_safe("writes into a live repository"),
            ),
        },
    )
    monkeypatch.setattr(rgc, "load_corpus", lambda **kw: [("echo hi", "/tmp")])
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", "--all"])

    assert rgc.main() == 0

    assert "refused_one" not in ran, "--all executed a refused guard"
    assert "safe_one" in ran
    out = capsys.readouterr().out
    assert "NOT MEASURED: refused_one" in out, "a skipped guard was not disclosed"


def test_all_with_nothing_safe_exits_2(rgc, monkeypatch):
    """A run that measured nothing must not read as a clean sweep."""
    monkeypatch.setattr(
        rgc,
        "GUARDS",
        {"refused_one": rgc.Guard(run=lambda c, w: True, safety=rgc.not_replay_safe("writes"))},
    )
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", "--all"])
    monkeypatch.setattr(
        rgc, "load_corpus", lambda **kw: pytest.fail("corpus built for a run with no guards")
    )

    assert rgc.main() == 2


def test_an_empty_corpus_exits_2_instead_of_printing_a_clean_zero(rgc, monkeypatch, capsys):
    """The sibling of the refusal above, one level in.

    An unreadable transcript tree, an empty cache file, or a tree holding no Bash
    records all reach replay with ``corpus == []``. ``pct`` is defined as 0.0 when
    n == 0 and nothing crashes, so every selected guard printed
    ``blocked 0/0 (0.00%)`` and main() returned 0 — a measurement of NOTHING
    wearing the grammar of a clean sweep, on the output most likely to be pasted
    into a PR body.

    The assertion is on the EXIT CODE and the absence of a rate line, not on the
    prose: asserting the message alone would pass on a run that printed the
    refusal and then measured anyway.
    """
    ran = []
    monkeypatch.setattr(
        rgc,
        "GUARDS",
        {
            "safe_one": rgc.Guard(
                run=lambda c, w: ran.append((c, w)) or False,
                safety=rgc.replay_safe("a test double"),
            )
        },
    )
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", "--all"])
    monkeypatch.setattr(rgc, "load_corpus", lambda **kw: [])

    code = rgc.main()

    out = capsys.readouterr()
    assert code == 2, "an empty corpus exited 0 — a run that measured nothing"
    assert "0/0" not in out.out, "a 0/0 rate was printed for an empty corpus"
    assert not ran, "a guard was replayed against an empty corpus"
    assert "REFUSED" in out.err


@pytest.mark.parametrize(
    "argv",
    [
        ("--all", "--guard", "protected_paths"),
        ("--list", "--all"),
        ("--list", "--guard", "protected_paths"),
    ],
)
def test_conflicting_selectors_are_refused_rather_than_partly_honoured(rgc, monkeypatch, argv):
    """Every pairing produced a SUCCESSFUL PARTIAL RUN, which is the worst shape.

    ``--all --guard <name>`` ran that one guard and exited 0 while the closing
    ``NOT MEASURED`` line named only the REFUSED guards — so the other replayable
    guards were missing from the run AND from the disclosure, which is the one
    thing that line exists to prevent. ``--list`` with either simply won and
    exited 0 having measured nothing.

    argparse exits 2 on a mutually-exclusive violation, so this asserts SystemExit
    rather than a return value.
    """
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", *argv])
    monkeypatch.setattr(
        rgc,
        "load_corpus",
        lambda **kw: pytest.fail("the corpus was built for a conflicting selector"),
    )

    with pytest.raises(SystemExit) as exc:
        rgc.main()

    assert exc.value.code == 2


# ── review round 1 on this PR ────────────────────────────────────────────────


def test_the_protected_paths_declaration_does_not_deny_reading_the_environment(
    rgc, monkeypatch, capsys
):
    """The sharpest finding on this PR, whatever severity it was filed at.

    The declaration read "no environment reads". That is FALSE:
    `protected_paths_guard._expand` runs
    `os.path.expanduser(os.path.expandvars(token))` on each operand, `main()`
    expands again to spot a surviving `$`, and `_legacy_substring_block` /
    `_protected_dirs` / `_protected_files` each resolve
    `home = os.path.expanduser("~")`. The mechanism's entire value is that each
    entry carries VERIFIED evidence — a declaration with a false claim in it is
    worse than no declaration, because it reads as protection while being wrong.
    That is the failure this design replaced, committed inside the replacement.

    Cited by SYMBOL rather than line number, and this docstring is why the rule
    exists rather than an application of it: the version above named
    `:90`, `:225` and `:145/:154/:159`, and by the time a reviewer read it every
    one of the five had drifted — `:90` onto a tuple of glob characters. A
    docstring about stale evidence going stale is the whole argument for not
    citing positions.
    """
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", "--list"])
    rgc.main()

    out = capsys.readouterr().out
    protected = out.split("protected_paths")[1].split("worktree_cwd")[0]
    assert "no environment reads" not in protected
    assert "expandvars" in protected or "environment" in protected


def test_the_module_docstring_does_not_call_the_rate_benign(rgc):
    """This docstring is the CLI's --help text (ArgumentParser(description=__doc__)),
    so it is the most-read surface the tool has. Calling the result a
    "benign-block rate" recreated the exact misreading every printed rate is
    stamped UNCLASSIFIED to prevent — the corpus is every command anyone typed,
    dangerous ones included, and nothing in it has been classified."""
    assert "benign-block rate" not in rgc.__doc__.split("Deliberately NOT")[0]


def test_a_cache_row_of_the_wrong_shape_is_rebuilt_not_measured(cache, rgc, capsys):
    """A JSON OBJECT row is the dangerous one: `{"command": …, "cwd": …}`
    unpacked to the literal pair ("command", "cwd"), so the tool measured two
    words nobody typed and reported it as a rate. `42` and `["one"]` merely
    crashed the loader, which at least fails loudly."""
    cache.write_text('{"command": "rm -rf /", "cwd": "/tmp"}\n')

    rows = rgc.load_corpus()

    assert rows == _ROWS, "a structurally wrong cache row was replayed"
    assert ("command", "cwd") not in rows
    assert "rebuilding" in capsys.readouterr().err


@pytest.mark.parametrize("row", ["42", '["only-one"]'])
def test_other_wrong_cache_shapes_rebuild_rather_than_crash(cache, rgc, row):
    cache.write_text(row + "\n")

    assert rgc.load_corpus() == _ROWS


def test_one_malformed_transcript_record_does_not_abort_the_whole_walk(rgc, monkeypatch, tmp_path):
    """The walk covers the whole transcript tree. Every other malformed record
    here is skipped; a
    JSON line containing "Bash" but shaped as a LIST raised AttributeError from
    .get() and killed the entire rebuild — losing minutes of work to one bad
    line, at whatever point in the tree it happened to sit."""
    t = tmp_path / "projects"
    t.mkdir()
    good = {
        "cwd": "/tmp",
        "message": {
            "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "echo ok"}}]
        },
    }
    bad_record = '["Bash"]'
    bad_input = {
        "cwd": "/tmp",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": "Bash"}]},
    }
    (t / "s.jsonl").write_text(f"{bad_record}\n{json.dumps(bad_input)}\n{json.dumps(good)}\n")
    monkeypatch.setattr(rgc, "_TRANSCRIPTS", t)

    assert rgc._extract_commands() == [("echo ok", "/tmp")]


def test_a_blocked_sample_carries_the_directory_that_decided_it(rgc, capsys):
    """For both in-process guards the cwd DECIDES the verdict, so two
    identical-looking commands can be classified differently. A sample printed
    without it cannot be reproduced or argued with."""
    with fake_guard(rgc, "always", lambda c, w: True, safety=rgc.replay_safe("a test double")):
        rgc.replay("always", [("echo hi", "/some/recorded/dir")], show=5, jobs=1)

    out = capsys.readouterr().out
    assert "/some/recorded/dir" in out, "the sample dropped the cwd that decided it"


def test_a_run_with_no_valid_measurement_exits_2(rgc, monkeypatch, capsys):
    """A guard that crashed on every row still prints a number, and main()
    discarded replay()'s result and returned 0 — so a wrapper saw success while
    the output said in words that the figure is not a measurement. Refusals
    already exit 2 for exactly this reason."""

    def boom(cmd, cwd):
        raise FileNotFoundError("guard is not installed")

    monkeypatch.setattr(
        rgc,
        "GUARDS",
        {"boom": rgc.Guard(run=boom, safety=rgc.replay_safe("a test double"))},
    )
    monkeypatch.setattr(rgc, "load_corpus", lambda **kw: [("echo hi", "/tmp")])
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", "--all"])

    assert rgc.main() == 2

    err = capsys.readouterr().err
    assert "no valid measurement" in err


def test_a_negative_show_count_is_refused(rgc, monkeypatch):
    """`blocked[:-1]` prints every blocked command except the last. These are
    verbatim command lines that demonstrably contain secrets passed in argv, so
    a slipped minus sign is a corpus dump to a terminal or a captured log."""
    monkeypatch.setattr(
        sys, "argv", ["replay_guard_corpus.py", "--guard", "protected_paths", "--show", "-1"]
    )

    with pytest.raises(SystemExit) as excinfo:
        rgc.main()

    assert excinfo.value.code == 2


# ── round 2: findings from the adversarial audit of this session's fixes ──────


@pytest.mark.parametrize("jobs", ["0", "-4"])
def test_a_jobs_count_below_one_is_refused(rgc, monkeypatch, jobs):
    """The validation --show and --limit already had, and --jobs did not.

    `jobs > 1` is the pool test, so 0 or a negative silently takes the SERIAL
    path. On a shell guard that turns a ~14-minute pooled run into hours, with no
    message — a wrong RUNTIME rather than a wrong number, which is why nothing
    else in the output would have flagged it.
    """
    monkeypatch.setattr(
        sys, "argv", ["replay_guard_corpus.py", "--guard", "protected_paths", "--jobs", jobs]
    )

    with pytest.raises(SystemExit) as excinfo:
        rgc.main()

    assert excinfo.value.code == 2


def test_rebuild_with_list_says_it_is_doing_nothing(rgc, monkeypatch, capsys):
    """--list returns before load_corpus, so --rebuild on that path is a no-op.

    Ignoring a flag is defensible; ignoring it SILENTLY is not, because the
    operator who passed it is waiting for a rebuild that will never happen and
    the next measurement still reads the stale cache.
    """
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", "--list", "--rebuild"])

    assert rgc.main() == 0

    assert "--rebuild has no effect with --list" in capsys.readouterr().err


def test_the_blocked_sample_comes_from_the_outcome_not_the_index(rgc, capsys):
    """The pooled path must not recover its samples by position.

    It used to do `blocked.append(corpus[i - 1])`, which is correct ONLY because
    `imap` preserves order. A later switch to `imap_unordered` for speed would
    silently attribute every printed sample to the wrong command — and the
    samples are exactly what a human reads to turn a rate into a verdict, so the
    corruption would land in the one output that gets pasted into a PR.

    Pinned by making the guard block exactly ONE known row out of several.
    """
    corpus = [("echo alpha", "/tmp"), ("echo BLOCKME", "/tmp"), ("echo omega", "/tmp")]
    with fake_guard(
        rgc,
        "picky",
        lambda c, w: "BLOCKME" in c,
        safety=rgc.replay_safe("a test double"),
    ):
        result = rgc.replay("picky", corpus, show=5, jobs=1)

    out = capsys.readouterr().out
    assert result.blocked == 1
    assert "echo BLOCKME" in out, f"the sample did not name the blocked command: {out}"
    assert "echo alpha" not in out and "echo omega" not in out, (
        f"the sample named a command that was never blocked: {out}"
    )


def test_importing_the_module_survives_a_settings_file_with_no_inline_blob(tmp_path):
    """The blob is read LAZILY, so its absence cannot take down the whole tool.

    Read at import, a reworded hook or a malformed settings.json made
    `import replay_guard_corpus` exit — killing --list, whose entire job is
    EXPLAINING refusals, plus both in-process guards, replay() for library
    callers, and this test module. Five of six invocations never need the string;
    only the one guard that uses it should pay for it being gone.

    The script is COPIED into a throwaway repo layout with a blob-less
    settings.json and imported from there. Loading the real module and then
    reassigning `_REPO` would not test this at all: `_REPO` derives from
    `__file__` at import, so the real settings.json would already have been read
    successfully and an eager implementation would pass. The copy is what makes
    the import itself the thing under test.
    """
    fake_repo = tmp_path / "repo"
    (fake_repo / "scripts").mkdir(parents=True)
    (fake_repo / ".claude").mkdir(parents=True)
    (fake_repo / ".claude" / "settings.json").write_text('{"hooks": {}}')
    target = fake_repo / "scripts" / "replay_guard_corpus.py"
    target.write_text(_SCRIPT.read_text())

    spec = importlib.util.spec_from_file_location("rgc_blobless", target)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rgc_blobless"] = mod
    try:
        # THE PROPERTY: this line raised SystemExit before the read was made lazy.
        spec.loader.exec_module(mod)

        assert fake_repo == mod._REPO, "the copy did not resolve its own repo root"
        # --list is the path that must survive, because explaining refusals is
        # the whole reason a refused guard stays in the table.
        assert "inline_blob" in mod.GUARDS
        # ...and the one guard that genuinely needs the blob still fails loudly.
        with pytest.raises(SystemExit):
            mod._inline_blob()
    finally:
        sys.modules.pop("rgc_blobless", None)


# ── round 3: findings from the cross-model reviewer ───────────────────────────


def test_a_guard_that_exits_in_a_pool_worker_does_not_hang_the_run(cache, rgc, capsys):
    """A BaseException in a worker must come back as a crash, not a deadlock.

    `SystemExit` is a BaseException, so an `except Exception` in `_probe` misses
    it — and so does `multiprocessing.pool.worker`'s. The worker dies WITHOUT
    delivering a result and the parent blocks forever in `imap`'s `next()`.

    MEASURED against the real CLI in a checkout whose settings.json carries no
    inline blob: `--guard inline_blob --jobs 4` had to be killed at 25s, while
    `--jobs 1` exited cleanly at 1 — so the failure also depended on the host's
    core count, which is the platform-dependence the fork pin exists to remove.
    Strictly worse than the eager read it replaced: that exited, this never did.

    A guard is ALLOWED to exit rather than return — `run_guard` is built around
    exactly that — so this is the guard contract, not one guard's quirk.
    """

    def exits_instead_of_returning(cmd, cwd):
        raise SystemExit("the resource this guard needs is missing")

    corpus = [("echo hi", "/tmp")] * 4
    with fake_guard(
        rgc,
        "exiter",
        exits_instead_of_returning,
        spawns_process=True,
        safety=rgc.replay_safe("a test double"),
    ):
        result = rgc.replay("exiter", corpus, show=0, jobs=4)

    out = capsys.readouterr().out
    assert result.valid is False, "a run where every row exited reported as valid"
    assert "RAISED" in out, "the exits were not disclosed as crashes"
    assert f"{len(corpus)}/{len(corpus)}" in out


def test_a_guards_resource_is_resolved_in_the_parent_before_the_fan_out(cache, rgc):
    """`prepare` runs ONCE, in the parent, before any worker exists.

    Two properties in one hook. A resource resolved lazily inside `run` is
    resolved by EVERY worker — which made the fork comment's "the parent parses
    once and workers inherit it" false. And a resolution that RAISES is an
    ordinary exception in the parent, where it used to be an undelivered result
    and a blocked pool.
    """
    calls = []

    with fake_guard(
        rgc,
        "prepared",
        lambda c, w: False,
        spawns_process=True,
        safety=rgc.replay_safe("a test double"),
        prepare=lambda: calls.append(1),
    ):
        rgc.replay("prepared", [("echo hi", "/tmp")] * 8, show=0, jobs=4)

    assert calls == [1], f"prepare ran {len(calls)} times, expected exactly once"


def test_an_unreadable_cache_does_not_also_blame_the_v1_format(cache, rgc, capsys):
    """One cause per failure. The corrupt branch used to print two.

    The unreadable branches signalled themselves by putting `[None]` into `rows`,
    which then satisfied the v1-format check — so a cache truncated by an
    interrupted rebuild printed its real cause AND a second line asserting a
    false one, sending a reader after a format migration that does not exist.
    """
    cache.write_text('["echo one", "/tmp"]\n["echo tw')  # truncated mid-row

    assert rgc.load_corpus() == _ROWS  # recovery is unchanged: rebuild

    err = capsys.readouterr().err
    assert "corrupt" in err, "the real cause was not named"
    assert "predates the cwd field" not in err, (
        f"an unreadable cache was also blamed on the v1 format: {err!r}"
    )


def test_limit_zero_is_refused_rather_than_meaning_no_limit(rgc, monkeypatch):
    """`--limit 0` was falsy, so it ran the WHOLE corpus.

    Meanwhile `--show 0` means "show none". An operator smoke-testing the
    empty-corpus refusal with `--limit 0` got a full multi-minute run and no
    message. The default is None now, so omitted and zero are different things.
    """
    monkeypatch.setattr(
        sys, "argv", ["replay_guard_corpus.py", "--guard", "protected_paths", "--limit", "0"]
    )

    with pytest.raises(SystemExit) as excinfo:
        rgc.main()

    assert excinfo.value.code == 2
