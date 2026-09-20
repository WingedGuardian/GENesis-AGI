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
import dataclasses
import errno
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import time
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


@pytest.fixture(autouse=True)
def _isolate_corpus_paths(tmp_path, rgc):
    """Keep EVERY test in this module off the operator's real home.

    `main()` calls `_cache_provenance()`, which stats `_CACHE` and walks
    `_TRANSCRIPTS` — both module-level, both under `~` by default. Two
    PRE-EXISTING tests reach `main()` without the `cache` fixture
    (`test_all_runs_the_safe_guards_and_names_the_refused_ones`,
    `test_a_run_with_no_valid_measurement_exits_2`), so adding that call made
    them sweep the operator's REAL transcript tree and stat the real cache —
    MEASURED on the origin install at 12,047 files and 75 MB, which turned two
    unit tests' runtime into a function of whose machine they ran on. It also
    falsified the `cache` fixture's own docstring claim that no test ever walks
    the real tree.

    Autouse and module-wide rather than a patch on those two, because the
    POLARITY is the point: a test written next year that reaches `main()`
    inherits the isolation instead of silently re-acquiring the leak. Tests that
    want a populated tree still override `_TRANSCRIPTS` themselves.

    Uses a fixture-OWNED `MonkeyPatch`, mirroring `tests/conftest.py`'s
    `_isolate_alert_queue`, so a test calling `monkeypatch.undo()` mid-body
    cannot revert suite isolation and re-expose the real paths.
    """
    mp = pytest.MonkeyPatch()
    home = tmp_path / "isolated-home"
    (home / ".claude" / "projects").mkdir(parents=True)
    (home / ".genesis" / "output").mkdir(parents=True)
    mp.setattr(rgc, "_CACHE", home / ".genesis" / "output" / "guard-corpus.jsonl")
    mp.setattr(rgc, "_TRANSCRIPTS", home / ".claude" / "projects")
    yield
    mp.undo()


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


def _double_safe(rgc, why: str = "a test double"):
    """A replay-safe declaration for a guard that exists only inside one test.

    Built through `ReplaySafety` directly, because there is no longer a
    constructor that returns `safe=True`: replay permission is withheld for the
    whole shipped table pending #2036. That withhold is a statement about which
    guards in `GUARDS` may run; it is not a claim that the replay MACHINERY is
    broken, and these doubles are what exercise that machinery — pool fan-out,
    crash disclosure, timeouts, argv pinning. #2036 needs all of it working.

    So the record still carries a `safe` field and `replay()` still honours it;
    what no longer exists is a way for a SHIPPED declaration to set it.
    """
    return rgc.ReplaySafety(safe=True, why=why)


def _double_unsafe(rgc, why: str = "a test double"):
    """The refusing sibling of `_double_safe`, same reasoning."""
    return rgc.not_replay_safe(why)


def _list_sections(out: str) -> dict[str, str]:
    """Split `--list` output into one block per guard, keyed by name.

    Keyed on the HEADER LINE (`<name>` at column 0, then `replayable`/`REFUSED`)
    rather than on a bare substring search. The substring version broke the
    moment a declaration legitimately mentioned another guard's module — and it
    broke SILENTLY, handing the assertion a slice of the wrong guard's text,
    which is the failure shape this file exists to catch elsewhere.
    """
    sections: dict[str, str] = {}
    current: str | None = None
    for line in out.splitlines():
        header = re.fullmatch(r"(\S+)\s+(replayable|REFUSED)", line)
        if header:
            current = header.group(1)
            sections[current] = ""
        elif current and line.startswith("    "):
            sections[current] += line + "\n"
        elif line and not line.startswith(" "):
            current = None
    return sections


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
        safety=_double_safe(rgc, "a test double; it touches nothing"),
    ):
        rgc.replay("fake", [("echo hi", "/tmp")] * 4, jobs=4)

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
        safety=_double_safe(rgc, "a test double; it touches nothing"),
    ):
        rgc.replay("fake", corpus, jobs=jobs)

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
        safety=_double_safe(rgc, "a test double; it only raises"),
    ):
        rgc.replay("boom", corpus, jobs=jobs)

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
            rgc, "nope", lambda c, w: True, safety=_double_unsafe(rgc, "writes to a live repo")
        ),
        pytest.raises(RuntimeError, match="not replay-safe"),
    ):
        rgc.replay("nope", [("echo hi", "/tmp")], jobs=1)


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
            safety=_double_safe(rgc, "a test double"),
        ),
    ):
        rgc.replay("crashy", corpus, jobs=1)

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
        rgc, "hang", hang, spawns_process=jobs > 1, safety=_double_safe(rgc, "a test double")
    ):
        rgc.replay("hang", corpus, jobs=jobs)

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


def test_list_shows_every_guard_with_its_reason(rgc, monkeypatch, capsys):
    """A refused guard vanishing from --list is the absence-as-exclusion pattern
    this design replaced: absence teaches nothing, and nothing then reminds the
    next reader why.

    Asserted through `_list_sections`, which keys on the HEADER LINE, rather
    than by searching the whole output for a word. The bare-substring version
    went VACUOUS the moment no guard was replayable: `assert "replayable" in
    out` kept passing on the word's appearance inside a BLIND_SPOTS sentence,
    and mutating the header literal to garbage left every test green. An
    assertion that cannot tell the header from the prose was not checking the
    header.
    """
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", "--list"])

    assert rgc.main() == 0

    out = capsys.readouterr().out
    sections = _list_sections(out)
    assert set(sections) == set(rgc.GUARDS), (
        f"--list must show every guard; missing {set(rgc.GUARDS) - set(sections)}"
    )
    # The header carries the verdict, and with the permission withheld every
    # verdict is REFUSED. `_list_sections` only matches `<name> replayable` or
    # `<name> REFUSED`, so reaching this line already proves a real header.
    for name in rgc.GUARDS:
        assert f"{name:16s} REFUSED" in out, f"{name} has no REFUSED header line"
    assert "git stash create" in sections["git_discard"], "the refusal lost its reason"

    # Re-homed from a deleted test. The blind spots come from ONE list so the
    # prose and the output cannot disagree, and that claim is made in the module
    # docstring and in SKILL.md — but the only assertion pinning it lived inside
    # a test removed whole for its OTHER half. MEASURED: with the pin gone,
    # deleting the print loop entirely left all 66 tests green, so the tool's
    # published limits could stop being published in silence.
    for spot in rgc.BLIND_SPOTS:
        assert spot.split(".")[0] in out, f"--list stopped printing a blind spot: {spot[:60]}"


def test_all_runs_the_safe_guards_and_names_the_refused_ones(rgc, monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(
        rgc,
        "GUARDS",
        {
            "safe_one": rgc.Guard(
                run=lambda c, w: bool(ran.append("safe_one")),
                safety=_double_safe(rgc, "a test double"),
            ),
            "refused_one": rgc.Guard(
                run=lambda c, w: bool(ran.append("refused_one")) or True,
                safety=_double_unsafe(rgc, "writes into a live repository"),
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
        {"refused_one": rgc.Guard(run=lambda c, w: True, safety=_double_unsafe(rgc, "writes"))},
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
                safety=_double_safe(rgc, "a test double"),
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


def test_rebuild_alone_is_a_reachable_path(rgc, monkeypatch, capsys, cache):
    """The corpus build must stay reachable now that every guard is refused.

    `--guard` and `--all` return BEFORE `load_corpus()` on the refusal path, so
    once nothing is replayable they can no longer build the cache — and
    `--rebuild --list` used to send the operator to exactly those two modes.
    That left no path at all, while the module help still advertised the corpus
    build. Advertising an operation no code path can perform is this file's own
    named failure, committed inside it.
    """
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", "--rebuild"])

    assert rgc.main() == 0, "--rebuild alone must not be refused"

    assert cache.exists() and cache.read_text().strip(), "the cache was not built"
    # stderr, not stdout: the notice interpolates the absolute cache path, which
    # embeds the operator's home directory, and stdout is the surface this
    # tool's output gets pasted from. This test's subject is that the path is
    # REACHABLE — the stream is incidental to it.
    assert "corpus rebuilt" in capsys.readouterr().err


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
    protected = _list_sections(out)["protected_paths"]
    assert "no environment reads" not in protected
    assert "expandvars" in protected or "environment" in protected
    # There used to be a third assertion here, on a machine-derived
    # `reads_env=True` printed beside the prose. The walk that produced it is
    # gone — it decided an open-set question by reading — so what pins this
    # declaration now is the CITATION check: the prose above quotes
    # `os.path.expanduser(os.path.expandvars(token))` from `_expand`, and
    # `verify_declarations` re-resolves that fragment against the executable
    # source. The claim is anchored; it is just anchored to something checkable.
    assert "cites:" in protected, "the corrected prose must still be citation-anchored"


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
        {"boom": rgc.Guard(run=boom, safety=_double_safe(rgc, "a test double"))},
    )
    monkeypatch.setattr(rgc, "load_corpus", lambda **kw: [("echo hi", "/tmp")])
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", "--all"])

    assert rgc.main() == 2

    err = capsys.readouterr().err
    assert "no valid measurement" in err


# ── round 2: findings from the adversarial audit of this session's fixes ──────


@pytest.mark.parametrize("jobs", ["0", "-4"])
def test_a_jobs_count_below_one_is_refused(rgc, monkeypatch, jobs):
    """The validation --limit already had, and --jobs did not.

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
        safety=_double_safe(rgc, "a test double"),
    ):
        result = rgc.replay("exiter", corpus, jobs=4)

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
        safety=_double_safe(rgc, "a test double"),
        prepare=lambda: calls.append(1),
    ):
        rgc.replay("prepared", [("echo hi", "/tmp")] * 8, jobs=4)

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

    An operator smoke-testing the
    empty-corpus refusal with `--limit 0` got a full multi-minute run and no
    message. The default is None now, so omitted and zero are different things.
    """
    monkeypatch.setattr(
        sys, "argv", ["replay_guard_corpus.py", "--guard", "protected_paths", "--limit", "0"]
    )

    with pytest.raises(SystemExit) as excinfo:
        rgc.main()

    assert excinfo.value.code == 2


# ── round 3: the guard must come from THIS checkout ───────────────────────────


def test_the_guard_is_loaded_from_this_checkout_not_the_import_cache(rgc, monkeypatch):
    """`__import__(name)` returns whatever a host process imported first.

    That is the failure mode with the worst timing: `replay()` is a supported API
    and worktree-based guard development is the reason to call it, so a process
    that already imported `protected_paths_guard` from ANOTHER checkout gets that
    module back and the harness reports a rate for the changed guard while having
    measured the unchanged one. Nothing downstream can tell.

    Pins the load path, not the absence of a bug: a decoy under the BARE name must
    be ignored in favour of the file adjacent to this harness.
    """
    decoy = types.SimpleNamespace(main=lambda: 0, read_payload=lambda: None)
    decoy.__file__ = "/somewhere/else/protected_paths_guard.py"
    monkeypatch.setitem(sys.modules, "protected_paths_guard", decoy)

    mod = rgc._load_guard_from_this_checkout("protected_paths_guard")

    assert mod is not decoy, "the decoy in sys.modules was returned"
    assert Path(mod.__file__).resolve() == (rgc._HOOKS / "protected_paths_guard.py").resolve()


def test_a_dependency_from_another_checkout_is_refused_not_worked_around(rgc, monkeypatch):
    """Loading the TARGET by path is not sufficient on its own.

    The guards import their dependencies by bare name (`from hook_input import
    read_payload`), which resolves through sys.path — so a foreign `hook_input`
    would still be bound even with the target loaded correctly. Refusing is the
    only honest option: the resulting number would be of the wrong code, and
    unlike a crash or a timeout it cannot be disclosed afterwards because nothing
    downstream can detect it.
    """
    foreign = types.ModuleType("hook_input")
    foreign.__file__ = "/another/checkout/scripts/hooks/hook_input.py"
    monkeypatch.setitem(sys.modules, "hook_input", foreign)

    with pytest.raises(SystemExit) as excinfo:
        rgc._load_guard_from_this_checkout("protected_paths_guard")

    assert "hook_input" in str(excinfo.value)
    assert "another/checkout" in str(excinfo.value), (
        "the refusal did not name where the foreign module came from"
    )


# ── the declarations are verified, not just written ──────────────────────────
#
# Every ReplaySafety record carries prose that a human wrote and nobody checked.
# It has been wrong four times: an early protected_paths entry denied reading the
# environment; bash_safety's side effect arrives through a caller and was missed
# twice; worktree_cwd omitted its sys.argv reads; and all six line-number
# citations went stale within five days, one landing on a comment about an
# unrelated cap.
#
# ONE half now, and the arithmetic of which defects that leaves uncovered is the
# point rather than a footnote. Of the four historical defects above, the
# citation checker catches the stale-citation class — #4, six instances, the only
# one that recurred. It does NOT catch #1 or #3 (a declaration denying an effect
# the guard has) or #2 (an unmentioned shell delegation).
#
# Those three were covered by an AST fact walk and a shell delegation scan, both
# deleted: establishing "this program has no side effects" by reading it is an
# open-set claim, and four review rounds each closed one spelling and surfaced
# the next. What replaces them is not another checker — it is that no guard is
# replayable, so a wrong claim about a guard's effects no longer authorises
# anything. #2036 restores the permission by confinement instead.
#
# So the tests below pin the citation class only, and the declarations' effect
# claims are unverified prose that `--list` labels as such:
# there is one set, not two.


def _hooks(rgc) -> Path:
    """The guards directory the tool itself resolves. The checkers now live in
    the SCRIPT — these tests call the same functions `main()` does, which is the
    property that made moving them worth the churn."""
    return Path(rgc._HOOKS)


@contextlib.contextmanager
def _swap_guard(rgc, name, replacement):
    """Swap one GUARDS entry for a test and put the real one back.

    GUARDS is a process-global the module-scoped `rgc` fixture shares, so a
    leaked entry is visible to every later test — the same hazard `fake_loaded`
    exists for, and worse here because these tests deliberately install
    DANGEROUS declarations.
    """
    real = rgc.GUARDS[name]
    rgc.GUARDS[name] = replacement
    try:
        yield
    finally:
        rgc.GUARDS[name] = real


def _write_guard(hooks: Path, name: str, body: str) -> None:
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / f"{name}.py").write_text(body)


# ── the checks themselves ────────────────────────────────────────────────────


def test_every_citation_still_resolves(rgc):
    """The whole of what is checked, and the reason it is what survived.

    All six declarations once cited line numbers, and every one had drifted
    within five days; one landed on a comment about an unrelated cap while the
    prose around it still read as verified. Symbol + verbatim fragment moves the
    claim somewhere a machine can go and look.
    """
    problems: list[str] = []
    for name, guard in sorted(rgc.GUARDS.items()):
        for cite in guard.safety.cites:
            try:
                haystack, where = rgc.cite_source(cite, _hooks(rgc))
            except rgc.DeclarationError as exc:
                problems.append(f"{name}: {exc}")
                continue
            # An empty fragment is a deliberate symbol-existence citation; getting
            # here already proved the symbol resolves, and asserting `"" in x`
            # would be the vacuous assertion this file bans.
            if cite.fragment and cite.fragment not in haystack:
                problems.append(
                    f"{name}: the fragment cited from {where} is no longer there —\n"
                    f"    {cite.fragment!r}\n"
                    "  It was edited, reflowed, or moved to another symbol. Re-read "
                    "the source and restate the evidence; do NOT relax the fragment "
                    "until it matches, which would keep the citation green while the "
                    "claim it supports has quietly changed."
                )
    assert not problems, "\n".join(problems)

    # And again through the PRODUCTION path, which is the assertion that
    # actually pins the shipped checker. The loop above re-implements the
    # fragment test to produce a per-citation message, and a re-implementation
    # grades its own copy: MEASURED — deleting the real `cite.fragment not in
    # haystack` branch from `_verify_one` left this test green, because nothing
    # in it had called `_verify_one`.
    assert rgc.verify_declarations() == [], "the shipped verifier disagrees with the loop above"


# ── acceptance bar: three arms, each verified RED before this shipped ─────────


def test_arm3_editing_or_renaming_a_cited_construct_turns_the_check_red(rgc, tmp_path):
    """Issue #1946 arm 3 — the arm that would have caught the ACTUAL drift.

    Three ways a citation rots, and all three must fail: the fragment is edited,
    the symbol is renamed, and the file is gone. Editing is the quiet one — the
    symbol still resolves, so anything checking only for existence stays green.
    """
    hooks = tmp_path / "hooks"
    _write_guard(hooks, "cited", 'def target():\n    return os.path.expanduser("~")\n')
    good = rgc.Cite("cited", "target", 'os.path.expanduser("~")')
    haystack, where = rgc.cite_source(good, hooks)
    assert good.fragment in haystack and where == "cited.target"

    # (a) the construct is EDITED in place — symbol still resolves
    _write_guard(hooks, "cited", 'def target():\n    return os.path.expanduser("$HOME")\n')
    haystack, _ = rgc.cite_source(good, hooks)
    assert good.fragment not in haystack, "an edited fragment still matched"

    # (b) the symbol is RENAMED
    _write_guard(hooks, "cited", 'def renamed():\n    return os.path.expanduser("~")\n')
    with pytest.raises(rgc.DeclarationError, match="no longer exists"):
        rgc.cite_source(good, hooks)

    # (c) the file is gone entirely
    (hooks / "cited.py").unlink()
    with pytest.raises(rgc.DeclarationError, match="cited file no longer exists"):
        rgc.cite_source(good, hooks)

    # (d) the name is DEFINED TWICE — refuse rather than silently resolve. Not a
    # live case today (14 cited symbols, 0 duplicates across 188 defs), but
    # `ast.walk` is breadth-first, so among a redefinition pair it returns the
    # shallower — for an ImportError fallback, the DEAD one.
    _write_guard(
        hooks,
        "cited",
        'def target():\n    return os.path.expanduser("~")\n\n\n'
        'def target():\n    return "shadowed"\n',
    )
    with pytest.raises(rgc.DeclarationError, match="is defined 2x"):
        rgc.cite_source(good, hooks)

    # (e) a DECORATED symbol: ast.get_source_segment drops the decorator lines,
    # so a fragment living in one would read as "no longer there" — a true
    # failure with a false cause.
    _write_guard(hooks, "cited", "@staticmethod\ndef target():\n    return 1\n")
    segment, _ = rgc.cite_source(rgc.Cite("cited", "target", "@staticmethod"), hooks)
    assert "@staticmethod" in segment


def test_a_rotted_citation_is_reported_by_the_shipped_verifier(rgc):
    """Arm 3 through `verify_declarations`, which is what actually runs.

    Every assertion in the arm above calls `cite_source` directly, so all of
    them pass while the branch in `_verify_one` that turns a non-matching
    fragment into a problem string is DELETED. MEASURED: replacing that branch's
    condition with `False` left the whole suite green — the resolver was pinned
    and the shipped check was not.

    The fragment here cannot occur in any real source, so a green result means
    the check did not run rather than that the citation held.
    """
    real = rgc.GUARDS["protected_paths"]
    rotted = dataclasses.replace(
        real,
        safety=dataclasses.replace(
            real.safety,
            cites=(rgc.Cite("protected_paths_guard", "main", "NO_SUCH_FRAGMENT_EVER_ZZZ"),),
        ),
    )
    with _swap_guard(rgc, "protected_paths", rotted):
        problems = rgc.verify_declarations()
    assert problems, "a fragment that occurs nowhere was reported as fine"
    assert any("NO_SUCH_FRAGMENT_EVER_ZZZ" in p for p in problems), problems


# ── external review round 1: one arm per finding ─────────────────────────────
#
# Six P2s, all verified by execution before anything was changed. Four of them
# were one class — a published claim broader than the implementation — and the
# generator was structural: the checkers lived in THIS file, so "the
# declarations are checked" held only while CI ran this module, while the
# script, --list and the PR body said it unconditionally. The checkers now live
# in the script and `main()` enforces them; these arms pin each fix.


def test_the_tool_verifies_its_own_declarations(rgc):
    """The end-to-end property, and the one worth stating plainly: the shipped
    table passes the shipped verifier. Everything below tests that individual
    checks BITE; this tests that they are satisfied by what we actually ship."""
    assert rgc.verify_declarations() == []


def test_code_sharing_a_line_with_a_docstring_survives_stripping(rgc, tmp_path):
    """Stripping a docstring must remove the STRING, not its physical lines.

    A docstring followed on the SAME LINE by `; subprocess.run(...)` is valid
    Python, and the AST marks only the string expression as the docstring —
    `Expr(Constant(str))`. Blanking `lineno..end_lineno` therefore
    destroyed the executable call sharing that line, and the checker reported a
    still-valid citation as stale. Because `verify_declarations()` gates every
    CLI path, one citation of that shape made the whole tool refuse — a false
    REFUSAL, which is the expensive direction for a measurement tool.

    The multi-line case is checked too, on its CLOSING line. There is no
    "code before the opening quotes" case to check: a docstring is by definition
    `body[0]`, so anything preceding it on the line would make the string the
    SECOND statement and not a docstring at all — which an earlier version of
    this test got wrong and the fix correctly refused to strip.
    """
    hooks = tmp_path / "hooks"
    _write_guard(
        hooks,
        "shared",
        'import subprocess\n\n\ndef f():\n    """documentation"""; subprocess.run(["x"])\n',
    )
    seg, _ = rgc.cite_source(rgc.Cite("shared", "f", "subprocess.run"), hooks)
    assert "subprocess.run" in seg, f"code on the docstring's line was destroyed: {seg!r}"
    assert "documentation" not in seg, "the docstring itself must still be stripped"

    _write_guard(
        hooks,
        "shared",
        'import subprocess\n\n\ndef f():\n    """line one\n    line two"""; subprocess.run(["y"])\n',
    )
    seg, _ = rgc.cite_source(rgc.Cite("shared", "f", "subprocess.run"), hooks)
    assert "subprocess.run" in seg, f"code AFTER a multi-line docstring was destroyed: {seg!r}"
    assert "line one" not in seg, "the multi-line docstring body must still be stripped"
    assert "line two" not in seg, "the docstring's closing line was not stripped"


def test_a_fragment_surviving_only_in_a_comment_does_not_resolve(rgc, tmp_path):
    """Round 1, finding 4 — a citation is supposed to anchor BEHAVIOUR.

    Matching raw text let a fragment live on in a comment after the
    implementation it described was deleted: the citation stayed green while the
    thing it vouched for was gone. That is this record's own failure mode, one
    level in.
    """
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    cite = rgc.Cite("cited", "target", 'os.path.expanduser("~")')

    (hooks / "cited.py").write_text(
        "def target():\n"
        '    # historical: used to call os.path.expanduser("~") before the rewrite\n'
        '    return "no longer does it"\n'
    )
    haystack, _ = rgc.cite_source(cite, hooks)
    assert cite.fragment not in haystack, "a comment-only fragment still resolved"

    # A docstring is the same hazard with different syntax.
    (hooks / "cited.py").write_text(
        'def target():\n    """Once called os.path.expanduser("~")."""\n    return 1\n'
    )
    haystack, _ = rgc.cite_source(cite, hooks)
    assert cite.fragment not in haystack, "a docstring-only fragment still resolved"

    # Guard-the-guard: the real construct must still match, or the strip is just
    # deleting evidence.
    (hooks / "cited.py").write_text('def target():\n    return os.path.expanduser("~")\n')
    haystack, _ = rgc.cite_source(cite, hooks)
    assert cite.fragment in haystack, "comment stripping ate a live construct"


def test_the_cli_refuses_to_replay_on_a_stale_declaration(rgc, monkeypatch, capsys):
    """Round 1, finding 6 — and the reason the checkers moved out of this file.

    A developer who edits a guard and follows the documented --list-then-replay
    workflow used to get no protection at all: `replay()` trusted `safety.safe`,
    and the verifier only ran when CI ran this module. The window between the
    edit and CI is exactly when a declaration is most likely to be stale.
    """
    monkeypatch.setattr(
        rgc, "verify_declarations", lambda *a, **k: ["worktree_cwd: reads_argv is stale"]
    )
    monkeypatch.setattr(
        rgc,
        "load_corpus",
        lambda **kw: pytest.fail("the corpus was built despite a stale declaration"),
    )
    for argv in (["--list"], ["--guard", "protected_paths"], ["--all"]):
        monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", *argv])
        assert rgc.main() == 2, f"{argv} did not refuse"
        err = capsys.readouterr().err
        assert "REFUSED" in err and "reads_argv is stale" in err
        assert "no --force" in err


# ── external review round 2 + the mandated whole-diff audit ──────────────────
#
# Round 2 returned five P2s and three of them lived in code written to answer
# round 1. That is the fix-churn signature, and the mode-switch audit it
# triggered found the reason: TWO roots wore one symptom.
#
#   Root A — proving absence by reading. Open-set, non-convergent, and what both
#            rounds argued about.
#   Root B — the verification was not BOUND to the decision. Closed-set, three
#            instances, and where the actual replay-permission fail-opens were.
#
# Root B was invisible while everyone argued about spellings. These arms pin it.


def test_a_declaration_that_cannot_be_checked_is_not_a_pass(rgc, tmp_path):
    """COULD-NOT-CHECK is a third state, not silence.

    The way to end up unable to look, now that the fact walk is gone: a CITED
    module that no longer resolves. Silence there would be the same defect the
    deleted walk had — a declaration about a deleted artifact sailing through
    because nothing could be found to contradict it.
    """
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    ghost = rgc.Cite("module_that_was_deleted", "some_symbol", "a fragment")
    with pytest.raises(rgc.DeclarationError):
        rgc.cite_source(ghost, hooks)

    # And it surfaces as a problem string rather than an escaping exception.
    real = rgc.GUARDS["protected_paths"]
    broken = dataclasses.replace(real, safety=dataclasses.replace(real.safety, cites=(ghost,)))
    with _swap_guard(rgc, "protected_paths", broken):
        problems = rgc.verify_declarations()
    assert problems, "a citation that cannot be resolved must not pass in silence"


def test_a_broken_module_in_the_closure_does_not_take_list_down(rgc, monkeypatch, capsys):
    """The wedge. A measurement tool that dies because its own checker crashed is
    worse than one that runs — and --list, whose whole job is explaining
    refusals, was the first casualty.

    A syntax error in a module the checker reads used to escape `main()` as an
    uncaught traceback and exit 1. The PR touches a surface many open PRs also
    touch, so a developer mid-edit on `shell_parse.py` could not run the tool
    at all.

    Retargeted from the deleted fact walk onto `cite_source`, which is now the
    only checker that reads another file and therefore the only one that can
    crash this way. The property is unchanged and is the reason it survived the
    deletion: the crash boundary is about the TOOL staying usable, not about
    which checker happened to fail.
    """

    def exploding_cite(cite, hooks=None):
        raise SyntaxError("invalid syntax (shell_parse.py, line 2447)")

    monkeypatch.setattr(rgc, "cite_source", exploding_cite)
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", "--list"])
    assert rgc.main() == 2, "a crashing checker must refuse in the tool's voice, not traceback"
    err = capsys.readouterr().err
    assert "COULD NOT CHECK" in err
    assert "invalid syntax" in err, "the cause must survive into the message"


def test_the_bare_dep_set_is_derived_not_hardcoded(rgc):
    """The literal claimed to be the same boundary the closure walk derives, and
    was not. MEASURED: it held {hook_input, shell_parse} while the real closures
    also contain discarded_write, audit_jsonl, hook_output and push_allowlist —
    and protected_paths_guard imports discarded_write BY BARE NAME, the exact
    spelling `_load_guard_from_this_checkout`'s refusal keys on."""
    deps = set(rgc._guard_bare_deps())
    assert "discarded_write" in deps, "the module the hardcoded list missed"
    # Reachable ONLY through a py_module closure (git_push_guard), never through
    # bash_safety's `invokes` — so this cannot be satisfied by the delegate loop
    # alone. A first cut of this test could be, and a mutation survived it.
    assert "push_allowlist" in deps, "the py_module closure contributed nothing"
    # The mirror of the line above, and it was missing: reachable ONLY through
    # bash_safety's `invokes`, never through any py_module closure. Without it a
    # mutation that emptied the delegate loop left this test GREEN — MEASURED —
    # so the test proved the closure walk ran and said nothing about the half
    # that exists because no Python walk can reach a shell guard's delegates.
    assert "destructive_command_guard" in deps, "the delegate loop contributed nothing"
    assert {"hook_input", "shell_parse"} <= deps
    # Entry modules are loaded BY PATH, so they are not what the refusal is about.
    assert not deps & {g.py_module for g in rgc.GUARDS.values() if g.py_module}


def test_a_decorated_method_in_a_class_still_strips_comments(rgc, tmp_path):
    """Round 2, finding 2. `get_source_segment` starts the `def` at column 0
    while the decorator lines keep their class indentation, so the segment had
    mixed indentation, `_executable_source` raised IndentationError, and the
    except handler returned RAW source — a comment-only fragment then resolved.
    The shipped test used a module-level function, the one shape that works.
    """
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "cited.py").write_text(
        "import functools\n\n\n"
        "class C:\n"
        "    @functools.cache\n"
        "    def target(self):\n"
        '        # historical: used to call os.path.expanduser("~")\n'
        '        return "gone"\n'
    )
    cite = rgc.Cite("cited", "target", 'os.path.expanduser("~")')
    haystack, _ = rgc.cite_source(cite, hooks)
    assert cite.fragment not in haystack, "a comment-only fragment resolved in a class method"
    assert "@functools.cache" in haystack, "the decorator was lost from the span"


def test_an_unparsable_cited_file_raises_instead_of_returning_raw_text(rgc, tmp_path):
    """The other half of the same defect: both fallbacks in `_executable_source`
    silently returned comment-INCLUSIVE text from a function contracted to strip
    comments. A soundness bug that reports nothing is worse than one that
    raises."""
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "cited.py").write_text("def target():\n    return 1\n")
    ok = rgc.cite_source(rgc.Cite("cited", "target", "return 1"), hooks)
    assert "return 1" in ok[0]

    (hooks / "cited.py").write_text("def target(:\n    this is not python\n")
    with pytest.raises(rgc.DeclarationError):
        rgc.cite_source(rgc.Cite("cited", None, "anything"), hooks)


# ── corpus provenance, and the limit denominator ─────────────────────────────


def _stamped_tree(rgc, monkeypatch, tmp_path, count, *, mtime):
    """A throwaway transcript tree of `count` files, every one stamped `mtime`."""
    tree = tmp_path / "projects"
    tree.mkdir(exist_ok=True)
    for n in range(count):
        leaf = tree / f"session-{n}.jsonl"
        leaf.write_text("{}\n")
        os.utime(leaf, (mtime, mtime))
    monkeypatch.setattr(rgc, "_TRANSCRIPTS", tree)
    return tree


def test_a_stale_cache_reports_both_its_age_and_how_far_the_tree_moved(
    cache, rgc, monkeypatch, tmp_path
):
    """The defect is SILENCE, not staleness.

    A six-day-old cache was returned with nothing said about it, so a rate
    measured over a short corpus read exactly like one measured over the whole
    tree. BOTH numbers are load-bearing: a date alone does not say whether
    anything actually moved, and a drift count alone does not say whether the
    gap is an hour or a month.
    """
    cache.write_text('["echo one", "/tmp"]\n')
    built = time.time() - 6 * 86400
    os.utime(cache, (built, built))
    _stamped_tree(rgc, monkeypatch, tmp_path, 10, mtime=time.time())

    line = rgc._cache_provenance()

    assert "6.0 days ago" in line
    assert "10 of 10 transcript files have changed since" in line


def test_a_current_cache_says_zero_drift_rather_than_going_quiet(cache, rgc, monkeypatch, tmp_path):
    """Silence is ambiguous with 'nobody looked'. `0 of N` is a measurement."""
    _stamped_tree(rgc, monkeypatch, tmp_path, 4, mtime=time.time() - 600)
    cache.write_text('["echo one", "/tmp"]\n')  # written now: newer than all four

    line = rgc._cache_provenance()

    assert "0 of 4 transcript files have changed since" in line


def test_no_cache_yields_no_provenance_line(cache, rgc, monkeypatch, tmp_path):
    """Nothing to describe, so describe nothing — rather than invent a date."""
    _stamped_tree(rgc, monkeypatch, tmp_path, 3, mtime=time.time())
    assert not cache.exists()
    assert rgc._cache_provenance() is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits this test sets")
def test_an_unreadable_SUBDIRECTORY_makes_the_count_a_declared_floor(
    cache, rgc, monkeypatch, tmp_path
):
    """A real `chmod 000` directory, because the stub version tested nothing.

    The first version of this test substituted an object whose `rglob` raised,
    and passed — against behaviour `pathlib` does not have. MEASURED on CPython
    3.12: `Path.walk` swallows a scandir failure when `on_error` is None and
    `rglob` exposes no hook, so a real unreadable subtree yields a SHORT list
    and raises NOTHING. The `except OSError` the old test exercised could never
    fire in production, and the true failure was the opposite shape — not a
    crash, but a confident count over a silently shrunken denominator.

    So this asserts the honest form: the visible files are still counted, and
    the line SAYS it could not see everything.
    """
    cache.write_text('["echo one", "/tmp"]\n')
    built = time.time() - 86400
    os.utime(cache, (built, built))
    tree = tmp_path / "projects"
    (tree / "sub").mkdir(parents=True)
    (tree / "visible.jsonl").write_text("{}\n")
    (tree / "sub" / "hidden.jsonl").write_text("{}\n")
    (tree / "sub").chmod(0o000)
    monkeypatch.setattr(rgc, "_TRANSCRIPTS", tree)
    try:
        line = rgc._cache_provenance()
    finally:
        (tree / "sub").chmod(0o755)

    assert "1 of 1 transcript files have changed since" in line
    assert "1 unreadable directory" in line
    assert "floors" in line


def test_a_cache_that_exists_but_cannot_be_read_says_so(cache, rgc, monkeypatch):
    """Absent and unreadable are different, and only one of them is silence.

    `FileNotFoundError` returns None, which suppresses the line entirely — right,
    because there is nothing to describe. Any OTHER OSError means the cache is
    there and we failed to read it, and returning None for that would delete the
    qualifier exactly when something is wrong.
    """
    cache.write_text('["echo one", "/tmp"]\n')
    real_stat = Path.stat

    def refuse(self, *a, **kw):
        if self == cache:
            raise PermissionError(13, "Permission denied")
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", refuse)

    line = rgc._cache_provenance()

    assert line is not None, "an unreadable cache must not render as no-cache-at-all"
    assert "unreadable" in line
    assert "age and drift are unknown" in line


def test_a_future_cache_mtime_is_reported_not_clamped(cache, rgc, monkeypatch, tmp_path):
    """The worst output this function can produce, so it gets its own test.

    MEASURED before the fix: a cache stamped 30 days ahead printed
    `(0 minutes ago); 0 of 1 transcript files have changed since` — the age
    clamped by `max(0.0, …)` and every `st_mtime > built` false against a future
    instant. Two independent paths both landing on "perfectly current", from an
    input that is evidence the clock is broken.
    """
    _stamped_tree(rgc, monkeypatch, tmp_path, 3, mtime=time.time())
    cache.write_text('["echo one", "/tmp"]\n')
    future = time.time() + 30 * 86400
    os.utime(cache, (future, future))

    line = rgc._cache_provenance()

    assert "FUTURE" in line
    assert "the clock moved" in line
    assert "minutes ago" not in line
    assert "have changed since" not in line


def test_an_empty_tree_reports_unknown_drift_not_zero_drift(cache, rgc, monkeypatch, tmp_path):
    """The same claim reached by a different road: no transcripts at all is not
    evidence that the cache is current."""
    cache.write_text('["echo one", "/tmp"]\n')
    _stamped_tree(rgc, monkeypatch, tmp_path, 0, mtime=time.time())

    line = rgc._cache_provenance()

    assert "no transcripts found" in line
    assert "0 of 0" not in line


def test_a_transcript_vanishing_mid_sweep_costs_one_row_not_the_line(
    cache, rgc, monkeypatch, tmp_path
):
    """Transcripts rotate under the walk.

    One failing stat loses a drift signal; it must not lose the whole
    measurement — and the loss has to be DECLARED, because a skip that is merely
    "slightly low" reads as "nothing changed" once a whole rotated subtree hits
    it.

    A DANGLING SYMLINK, not a stub: `os.walk` lists it among `files` (it cannot
    be a directory) and `os.stat` on it raises FileNotFoundError, which is the
    real shape of a transcript rotated between the walk and the stat. The
    previous version hand-built objects with a `rglob` method, and hand-built
    intermediates are where this exact bug hides — the production walk does not
    call `rglob` at all any more, so that test would have kept passing against
    code it no longer described.
    """
    cache.write_text('["echo one", "/tmp"]\n')
    built = time.time() - 86400
    os.utime(cache, (built, built))
    tree = tmp_path / "projects"
    tree.mkdir()
    for n in range(2):
        (tree / f"real-{n}.jsonl").write_text("{}\n")
    (tree / "rotated.jsonl").symlink_to(tree / "gone-already.jsonl")
    monkeypatch.setattr(rgc, "_TRANSCRIPTS", tree)

    line = rgc._cache_provenance()

    assert "2 of 3 transcript files have changed since" in line
    assert "1 unreadable file" in line
    assert "floors" in line, (
        "a skipped file makes the drift a lower bound; reporting the bare count "
        "would let a whole rotated subtree read as 'nothing has changed'"
    )


def test_the_corpus_line_carries_its_provenance_on_stdout(rgc, monkeypatch, capsys):
    """Beside the number, not in the log.

    The rate printed just below is what gets pasted into a PR body, and the
    caveat print already carries this reasoning: a qualifier that does not travel
    with the number is a qualifier nobody reads.
    """
    monkeypatch.setattr(
        rgc, "GUARDS", {"double": rgc.Guard(run=lambda c, w: False, safety=_double_safe(rgc))}
    )
    monkeypatch.setattr(rgc, "load_corpus", lambda **kw: [("echo hi", "/tmp")])
    monkeypatch.setattr(rgc, "_cache_provenance", lambda: "cache built AT SOME POINT")
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", "--all"])

    assert rgc.main() == 0

    out = capsys.readouterr().out
    assert "corpus: 1 unique real commands" in out
    assert "    cache built AT SOME POINT" in out


def test_a_limited_run_names_the_denominator_it_was_limited_from(rgc, monkeypatch, capsys):
    """`--limit 1` used to print `corpus: 1 unique real commands` against three —
    a subset wearing the grammar of a full sweep, one line above a rate. This
    file already refuses an EMPTY corpus on exactly that reasoning; a TRUNCATED
    one is the same failure carrying a plausible number, which is the harder one
    to notice because it looks like a measurement."""
    monkeypatch.setattr(
        rgc, "GUARDS", {"double": rgc.Guard(run=lambda c, w: False, safety=_double_safe(rgc))}
    )
    monkeypatch.setattr(
        rgc,
        "load_corpus",
        lambda **kw: [("echo one", "/tmp"), ("echo two", "/tmp"), ("echo three", "/tmp")],
    )
    monkeypatch.setattr(rgc, "_cache_provenance", lambda: None)
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", "--all", "--limit", "1"])

    assert rgc.main() == 0

    out = capsys.readouterr().out
    assert "corpus: 1 of 3 unique real commands" in out
    assert "--limit 1" in out
    assert "SUBSET" in out


def test_an_unlimited_run_never_claims_to_be_a_subset(rgc, monkeypatch, capsys):
    """The other direction, without which the notice could become unconditional
    and stop discriminating anything."""
    monkeypatch.setattr(
        rgc, "GUARDS", {"double": rgc.Guard(run=lambda c, w: False, safety=_double_safe(rgc))}
    )
    monkeypatch.setattr(
        rgc, "load_corpus", lambda **kw: [("echo one", "/tmp"), ("echo two", "/tmp")]
    )
    monkeypatch.setattr(rgc, "_cache_provenance", lambda: None)
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", "--all"])

    assert rgc.main() == 0

    out = capsys.readouterr().out
    assert "corpus: 2 unique real commands" in out
    assert "SUBSET" not in out
    assert "--limit" not in out


def test_the_bash_safety_declaration_is_bound_to_the_tracked_settings_file():
    """The declaration states that this repo does not wire the hook, and derives
    two consequences from it. Bind the claim to the settings file rather than to
    its own prose: asserting the SENTENCE is still present would pass forever,
    while this fails the moment the world it describes changes.
    """
    settings = json.loads((_REPO_ROOT / ".claude" / "settings.json").read_text())
    all_commands = [
        hook.get("command", "")
        for entries in settings.get("hooks", {}).values()
        for entry in entries
        for hook in entry.get("hooks", [])
    ]
    assert len(all_commands) > 10, (
        "the settings walk found almost no hook commands — the schema this "
        "comprehension hard-codes has moved, so the assertion below would pass "
        "against an EMPTY set and the declaration's claim would be unbound. "
        f"Found: {all_commands}"
    )
    wired = [c for c in all_commands if "bash_safety_hook" in str(c)]
    assert not wired, (
        "bash_safety_hook.sh is now wired in this repo's settings.json, so the "
        "bash_safety declaration in replay_guard_corpus.py is stale: it states "
        "that the repo does not wire it, and derives from that both the "
        "fresh-clone caveat and the claim that its registration cannot be read "
        f"from inside this repo. Wired as: {wired}"
    )


# ── binding the parts the first sweep left unbound ───────────────────────────


@pytest.mark.parametrize(
    ("age_s", "expected"),
    [
        (30 * 60, "30 minutes ago"),
        (5 * 3600, "5.0 hours ago"),
        (6 * 86400, "6.0 days ago"),
    ],
)
def test_each_age_unit_renders_in_its_own_band(cache, rgc, monkeypatch, tmp_path, age_s, expected):
    """Only the DAYS branch was asserted anywhere, so the other two were free.

    Surviving mutations the first sweep could not see: swap the two divisors,
    swap the 3600/86400 thresholds, or flip a `<` to `>`. Each ships a
    confidently wrong age on the line most likely to be pasted into a PR body —
    "5.0 minutes ago" for a five-hour-old cache reads as fresh.
    """
    _stamped_tree(rgc, monkeypatch, tmp_path, 1, mtime=time.time())
    cache.write_text('["echo one", "/tmp"]\n')
    built = time.time() - age_s
    os.utime(cache, (built, built))

    assert expected in rgc._cache_provenance()


def test_the_stamp_is_the_cache_mtime_not_the_current_time(cache, rgc, monkeypatch, tmp_path):
    """`time.localtime(built)` → `time.localtime()` survived every assertion.

    It renders `cache built <today> (6.0 days ago)` — a self-contradicting line
    whose two halves disagree, and whose date half is the one a reader trusts.
    Nothing pinned the stamp at all, so this asserts the exact rendered date of
    a known mtime.
    """
    _stamped_tree(rgc, monkeypatch, tmp_path, 1, mtime=time.time())
    cache.write_text('["echo one", "/tmp"]\n')
    built = time.time() - 6 * 86400
    os.utime(cache, (built, built))

    expected = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(built))
    line = rgc._cache_provenance()

    assert f"cache built {expected}" in line
    assert time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()) not in line


def test_a_limit_at_or_above_the_corpus_size_is_not_a_subset(rgc, monkeypatch, capsys):
    """The boundary the two limit tests left open.

    They cover `--limit 1` over 3 rows and no limit at all, so mutating the
    branch condition to `if args.limit is None:` keeps both green — while
    printing `corpus: 3 of 3 unique real commands (--limit 5) — a SUBSET` for a
    run that measured the WHOLE corpus. A false subset claim is the mirror of
    the defect this PR fixes, and costs the same credibility.
    """
    monkeypatch.setattr(
        rgc, "GUARDS", {"double": rgc.Guard(run=lambda c, w: False, safety=_double_safe(rgc))}
    )
    monkeypatch.setattr(
        rgc,
        "load_corpus",
        lambda **kw: [("echo one", "/tmp"), ("echo two", "/tmp"), ("echo three", "/tmp")],
    )
    monkeypatch.setattr(rgc, "_cache_provenance", lambda: None)
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", "--all", "--limit", "5"])

    assert rgc.main() == 0

    out = capsys.readouterr().out
    assert "corpus: 3 unique real commands" in out
    assert "SUBSET" not in out
    assert "--limit" not in out


def test_a_missing_provenance_prints_no_line_at_all(rgc, monkeypatch, capsys):
    """Deleting the `if provenance:` guard prints a bare indented `None`.

    Both limit tests stub the provenance to None and assert nothing that a
    literal `None` line would violate, so the guard was unbound. A stray `None`
    under the corpus count is the kind of output a reader rounds off as noise
    and then quotes anyway.
    """
    monkeypatch.setattr(
        rgc, "GUARDS", {"double": rgc.Guard(run=lambda c, w: False, safety=_double_safe(rgc))}
    )
    monkeypatch.setattr(rgc, "load_corpus", lambda **kw: [("echo hi", "/tmp")])
    monkeypatch.setattr(rgc, "_cache_provenance", lambda: None)
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", "--all"])

    assert rgc.main() == 0

    out = capsys.readouterr().out
    assert "corpus: 1 unique real commands" in out
    assert "None" not in out


def test_the_provenance_is_read_AFTER_the_corpus_is_loaded(rgc, monkeypatch, capsys):
    """Order is load-bearing and nothing pinned it.

    `_cache_provenance()` reads the cache file, and `load_corpus(rebuild=True)`
    REWRITES that file. Hoisting the provenance call above the load — a
    plausible tidying, since it reads like a preamble — would make a
    `--rebuild --all` run describe the PRE-rebuild cache: `cache built <6 days
    ago>; 10 of 10 changed`, printed directly above a corpus that was rebuilt
    from that same tree seconds earlier. That is precisely the confident-wrong
    line this feature exists to prevent, reachable by a refactor no other test
    would stop.
    """
    calls: list[str] = []

    def loud_load(**_kw):
        calls.append("load_corpus")
        return [("echo hi", "/tmp")]

    def loud_provenance():
        calls.append("_cache_provenance")
        return "cache built AT SOME POINT"

    monkeypatch.setattr(
        rgc, "GUARDS", {"double": rgc.Guard(run=lambda c, w: False, safety=_double_safe(rgc))}
    )
    monkeypatch.setattr(rgc, "load_corpus", loud_load)
    monkeypatch.setattr(rgc, "_cache_provenance", loud_provenance)
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", "--all"])

    assert rgc.main() == 0

    assert calls == ["load_corpus", "_cache_provenance"], (
        "provenance must describe the cache the corpus was actually loaded from"
    )


def test_a_limited_run_says_the_subset_is_sort_ordered_not_sampled(rgc, monkeypatch, capsys):
    """Truncation and BIAS are two claims, and the notice owes both.

    `_extract_commands` returns `sorted(seen)` and the cache preserves that
    order, so `corpus[:limit]` is a lexicographic PREFIX. Saying only "a SUBSET"
    leaves a reader free to assume a representative sample of N, which is the
    same overstatement one step in.
    """
    monkeypatch.setattr(
        rgc, "GUARDS", {"double": rgc.Guard(run=lambda c, w: False, safety=_double_safe(rgc))}
    )
    monkeypatch.setattr(
        rgc, "load_corpus", lambda **kw: [("a", "/tmp"), ("b", "/tmp"), ("c", "/tmp")]
    )
    monkeypatch.setattr(rgc, "_cache_provenance", lambda: None)
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", "--all", "--limit", "1"])

    assert rgc.main() == 0

    out = capsys.readouterr().out
    assert "SORT ORDER" in out
    assert "rather than a sample" in out


def test_no_test_in_this_module_can_reach_the_real_home(rgc):
    """Guard-the-guard for `_isolate_corpus_paths`.

    The isolation fixture has no failing test of its own: remove it and nothing
    goes red, the suite just quietly starts walking the operator's real
    transcript tree again. An autouse fixture whose absence is invisible is a
    convention, and conventions decay — so this asserts the PROPERTY directly,
    and fails the moment the fixture stops holding it.

    Note the property is "not the PRODUCTION path", not "outside $HOME": on an
    install whose TMPDIR sits under the home directory — which is the shipped
    Claude Code arrangement here — `tmp_path` is itself inside $HOME, so the
    stricter-looking assertion fails on a correctly isolated run. That is the
    first version of this test, caught by running it.
    """
    production_cache = Path.home() / ".genesis" / "output" / "guard-corpus.jsonl"
    production_transcripts = Path.home() / ".claude" / "projects"
    assert production_cache != rgc._CACHE, (
        "_CACHE is the production cache — the isolation fixture is not in "
        "effect, and tests are statting the operator's real 75 MB corpus"
    )
    assert production_transcripts != rgc._TRANSCRIPTS, (
        "_TRANSCRIPTS is the production transcript tree — tests are walking the "
        "operator's real sessions, so their runtime depends on whose box it is"
    )


# ── round-2 review: the branches the 17-mutation sweep still left unbound ─────


def test_the_cache_is_stamped_with_the_walks_START_not_its_end(cache, rgc, monkeypatch, tmp_path):
    """The confident-wrong line this whole feature exists to prevent, reached by
    a road the first round missed.

    `os.replace` sets the cache mtime to the WRITE, which is the walk's END. A
    transcript appended at minute 3 of a twenty-minute walk therefore lands with
    an mtime EARLIER than the cache and reads as "unchanged" — over a corpus
    that provably does not contain it. MEASURED before the fix: a file appended
    2s into a walk produced `0 of 1 transcript files have changed since`.

    The fix stamps the walk's START, so anything touched during the build is
    reported as drift. That is deliberately the over-reporting direction: a
    false "something changed" costs a rebuild, a false "nothing changed" costs
    the measurement its meaning.
    """
    tree = tmp_path / "projects"
    tree.mkdir()
    appended = tree / "a.jsonl"
    appended.write_text("{}\n")
    monkeypatch.setattr(rgc, "_TRANSCRIPTS", tree)

    def walk_that_takes_time():
        # The sleep comes FIRST so the append lands measurably after the walk
        # began. Linux stamps inode mtimes from the coarse clock (~1-4ms), so an
        # append issued microseconds after the walk starts can share a timestamp
        # bucket with it and the test proves nothing — that is how the first
        # version of this test failed against a CORRECT fix.
        time.sleep(1.1)
        appended.write_text("{}\n{}\n")  # a live session appends during the walk
        return list(_ROWS)

    monkeypatch.setattr(rgc, "_extract_commands", walk_that_takes_time)
    rgc.load_corpus(rebuild=True)

    assert appended.read_text().count("\n") == 2, (
        "guard-the-guard: the stub must actually have appended, or the drift "
        "this test looks for was never created"
    )
    assert appended.stat().st_mtime > cache.stat().st_mtime, (
        "the cache must be stamped with the walk's START, so a file touched "
        "during the build is newer than the cache and reads as drift"
    )
    assert "1 of 1 transcript files have changed since" in rgc._cache_provenance()


def test_the_provenance_population_matches_the_corpus_BUILD_population(
    cache, rgc, monkeypatch, tmp_path
):
    """One parity test for two independent walks over the same tree.

    `_extract_commands` uses `sorted(_TRANSCRIPTS.rglob("*.jsonl"))`;
    `_cache_provenance` uses `os.walk` with an `endswith(".jsonl")` filter. Two
    APIs, two filter spellings, two symlink policies — and a denominator that
    means nothing unless they agree. Without this, dropping the `.jsonl` filter
    or flipping `followlinks` both keep the suite green while the reported total
    diverges from the corpus source (MEASURED on the real tree: 12,055 `.jsonl`
    against 21,476 files, so an unfiltered walk overstates by 78%).

    The mixed tree is the point: a dotfile, a non-`.jsonl` sibling, a nested
    directory, and a SYMLINKED directory, which is the case where the two APIs
    could legitimately disagree and where `followlinks=True` would double-count.
    """
    tree = tmp_path / "projects"
    (tree / "nested").mkdir(parents=True)
    (tree / "plain.jsonl").write_text("{}\n")
    (tree / ".hidden.jsonl").write_text("{}\n")
    (tree / "notes.md").write_text("not a transcript\n")
    (tree / "data.json").write_text("{}\n")
    (tree / "nested" / "deep.jsonl").write_text("{}\n")
    (tree / "linked").symlink_to(tree / "nested", target_is_directory=True)
    monkeypatch.setattr(rgc, "_TRANSCRIPTS", tree)
    cache.write_text('["echo one", "/tmp"]\n')
    built = time.time() - 86400
    os.utime(cache, (built, built))

    expected = len(sorted(tree.rglob("*.jsonl")))
    line = rgc._cache_provenance()
    reported = int(re.search(r"of (\d+) transcript files", line).group(1))

    assert reported == expected, (
        f"provenance counted {reported} files, the corpus build walk counts "
        f"{expected} — the denominator does not describe the corpus"
    )
    assert expected == 3, (
        "guard-the-guard: the fixture must contain exactly 3 .jsonl files among "
        "6 entries plus a symlinked directory, or this parity check is trivial"
    )


def test_an_unrenderable_cache_mtime_degrades_instead_of_aborting(cache, rgc, monkeypatch):
    """The branch whose own comment says an escape would discard a 20-minute run.

    Nothing exercised it, so narrowing its `except` to an unrelated exception
    kept the suite green. It is genuinely reachable: an mtime outside the
    platform's range raises out of `time.localtime`.
    """
    cache.write_text('["echo one", "/tmp"]\n')
    # Inject the out-of-range mtime at the stat boundary, not via os.utime:
    # ext4 silently clamps 2**62 to its ~2446 max, which RENDERS and lands in
    # the future-mtime branch instead of this one — the utime version was
    # green only on filesystems that store the value unclamped.
    class _OutOfRangeStat:
        st_mtime = 2**62

    monkeypatch.setattr(
        type(cache), "stat", lambda self, *a, **k: _OutOfRangeStat()
    )

    line = rgc._cache_provenance()

    assert "cannot be rendered" in line
    assert "age is unknown" in line


def test_a_transcript_tree_that_is_absent_says_so_rather_than_empty(
    cache, rgc, monkeypatch, tmp_path
):
    """ "Nothing was found" and "nothing could be READ" are different diagnoses.

    Deleting the `unreadable_dirs` arm of the zero-total branch left the suite
    green while a completely unreachable tree reported "no transcripts found" —
    which sends the reader after a missing corpus instead of a broken path.

    Uses an ABSENT directory rather than `chmod 000`, so it needs no mode bits
    and therefore runs as root too: the sibling permission test is skipped under
    uid 0, which would otherwise leave `onerror` unbound wherever CI runs as root.
    """
    cache.write_text('["echo one", "/tmp"]\n')
    monkeypatch.setattr(rgc, "_TRANSCRIPTS", tmp_path / "definitely-not-here")

    line = rgc._cache_provenance()

    assert "could not be read" in line
    assert "no transcripts found" not in line


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits this test sets")
def test_BOTH_blind_spots_are_named_when_both_occur(cache, rgc, monkeypatch, tmp_path):
    """Half a disclosure is the failure mode here.

    `' and '.join(missed)` → `missed[0]` kept the suite green: with an unreadable
    directory AND an unreadable file, one of the two silently stopped being
    mentioned. No test produced both at once.
    """
    cache.write_text('["echo one", "/tmp"]\n')
    built = time.time() - 86400
    os.utime(cache, (built, built))
    tree = tmp_path / "projects"
    (tree / "locked").mkdir(parents=True)
    (tree / "real.jsonl").write_text("{}\n")
    (tree / "rotated.jsonl").symlink_to(tree / "gone.jsonl")
    (tree / "locked").chmod(0o000)
    monkeypatch.setattr(rgc, "_TRANSCRIPTS", tree)
    try:
        line = rgc._cache_provenance()
    finally:
        (tree / "locked").chmod(0o755)

    assert "1 unreadable directory" in line
    assert "1 unreadable file" in line
    assert " and " in line, "both blind spots must be named, not just the first"
    assert "floors" in line


def test_no_provenance_line_can_leak_a_filesystem_path(cache, rgc, monkeypatch, tmp_path):
    """The provenance line is printed to STDOUT precisely so it travels with the
    number into a PR body — which makes it a PUBLIC surface, and the repo's hard
    rule is that no /home/<user> path reaches one.

    An `OSError`'s `str()` embeds the absolute filename, so the obvious
    `f"… ({exc})"` spelling publishes the operator's home path on the one line
    most likely to be copied. Every failure branch must render `strerror` alone.
    The stderr warnings in this module deliberately DO print the path: stderr is
    the operator's channel, not the published one, and this test is scoped to
    the returned string.
    """
    cache.write_text('["echo one", "/tmp"]\n')
    real_stat = Path.stat

    def refuse(self, *a, **kw):
        if self == cache:
            raise PermissionError(errno.EACCES, "Permission denied", str(cache))
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", refuse)
    line = rgc._cache_provenance()

    assert "Permission denied" in line, "the cause must still be named"
    assert str(cache) not in line, f"the cache path leaked into stdout: {line}"
    assert str(Path.home()) not in line, f"a home path leaked into stdout: {line}"
    assert "/" not in line.split("(")[-1], (
        f"a filesystem path appears in the rendered cause: {line}"
    )


def _every_provenance_branch(rgc, cache, tmp_path, monkeypatch, built):
    """Drive every branch of `_cache_provenance` that returns a string.

    Returns `[(label, line)]`. Used by the allowlist test below, which has to
    assert its property over the whole population rather than one member — the
    defect it exists to prevent was found on the branch nobody had driven.
    """
    tree = tmp_path / "prov-tree"
    tree.mkdir()
    (tree / "one.jsonl").write_text("{}\n")
    monkeypatch.setattr(rgc, "_TRANSCRIPTS", tree)
    cache.write_text('["echo one", "/tmp"]\n')
    os.utime(cache, (built, built))
    out = [("normal", rgc._cache_provenance())]

    empty = tmp_path / "empty-tree"
    empty.mkdir()
    monkeypatch.setattr(rgc, "_TRANSCRIPTS", empty)
    out.append(("empty tree", rgc._cache_provenance()))

    monkeypatch.setattr(rgc, "_TRANSCRIPTS", tmp_path / "absent")
    out.append(("unreadable tree", rgc._cache_provenance()))

    monkeypatch.setattr(rgc, "_TRANSCRIPTS", tree)
    future = time.time() + 86400
    os.utime(cache, (future, future))
    out.append(("future mtime", rgc._cache_provenance()))

    os.utime(cache, (built, built))
    return out


def test_no_provenance_branch_leaks_location_or_schedule(cache, rgc, monkeypatch, tmp_path):
    """Allowlist polarity over EVERY stamped branch, because the round before
    this one was an INSTANCE fix and the class bit back.

    That round fixed one error branch's filesystem-path leak and left the HOT
    path rendering `%Z` — the operator's timezone abbreviation — and a localtime
    hour, i.e. their region and when they were at the machine. Those are the
    INDIRECT personal context the privacy rule names explicitly, on the one
    string in this module whose stated purpose is to be pasted into a PR body.

    So this asserts the property across the whole population rather than the
    member someone happened to report: a branch added later inherits the
    guarantee instead of quietly reintroducing the defect.
    """
    built = time.time() - 6 * 86400
    utc_stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(built))
    local_render = time.strftime("%Y-%m-%d %H:%M", time.localtime(built))

    branches = _every_provenance_branch(rgc, cache, tmp_path, monkeypatch, built)
    assert len(branches) == 4, "guard-the-guard: all four stamped branches must be driven"

    for label, line in branches:
        # The FORMAT, not one fixed instant: the future branch legitimately
        # renders a different timestamp, and pinning the exact stamp here would
        # duplicate the dedicated stamp test while failing on that branch.
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", line), (
            f"{label}: no UTC-rendered timestamp — {line}"
        )
        if label != "future mtime":
            assert utc_stamp in line, f"{label}: stamp is not the UTC rendering — {line}"
        if local_render != utc_stamp.removesuffix(" UTC"):
            assert local_render not in line, (
                f"{label}: the line carries the operator's LOCAL clock — {line}"
            )
        if "UTC" not in time.tzname:
            for abbrev in time.tzname:
                assert abbrev not in line, (
                    f"{label}: the line names the operator's timezone {abbrev!r} — {line}"
                )
        assert str(Path.home()) not in line, f"{label}: home path leaked — {line}"
        assert str(tmp_path) not in line, f"{label}: a filesystem path leaked — {line}"


def test_the_rebuild_notice_keeps_the_cache_path_off_stdout(rgc, monkeypatch, capsys, tmp_path):
    """The sibling of the provenance leak, in the same file and on the same
    surface: `--rebuild` alone printed the absolute cache path to STDOUT.

    Pre-existing rather than introduced here, but it contradicts the invariant
    this change's own tests now assert, so it is fixed as a member of the class
    rather than left as a counterexample sitting six hundred lines away.
    """
    monkeypatch.setattr(rgc, "_CACHE", tmp_path / "corpus.jsonl")
    monkeypatch.setattr(rgc, "_extract_commands", lambda: [("echo hi", "/tmp")])
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", "--rebuild"])

    assert rgc.main() == 0

    captured = capsys.readouterr()
    assert "corpus rebuilt" in captured.err, "the notice must still be emitted"
    assert "corpus rebuilt" not in captured.out, "it must not be on the pasted surface"
    assert str(tmp_path) not in captured.out, f"a path reached stdout: {captured.out!r}"
