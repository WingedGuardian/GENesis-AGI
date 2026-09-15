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
import re
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


def _double_safe(rgc, why: str = "a test double"):
    """A replay-safe declaration for a guard that exists only inside one test.

    `replay_safe` requires `evidence` and has no default, which is the point: a
    declaration with nothing behind it is exactly what the record replaced. So a
    double has to supply some, and the honest form is ShEvidence over its own
    empty source — there is no module to walk, the guard is a lambda defined
    three lines up, and it delegates to nothing. Were the verifier to see one it
    would pass on the merits rather than on an exemption, which is why there is
    no test-only branch in the production type.
    """
    return rgc.replay_safe(why, evidence=_double_evidence(rgc))


def _double_unsafe(rgc, why: str = "a test double"):
    """The refusing sibling of `_double_safe`, same reasoning."""
    return rgc.not_replay_safe(why, evidence=_double_evidence(rgc))


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


def _double_evidence(rgc):
    # NON-EMPTY on purpose. An empty source made the delegation scan read nothing
    # and assert nothing, which is how a vacuous declaration used to pass — the
    # template for that hole shipped from this very helper. This text is real
    # shell that genuinely delegates to nothing, so a double passes on the merits.
    return rgc.ShEvidence(
        source=lambda: "# a test double: no delegates, no programs\n:\n",
        label="a test double",
        references_scripts=(),
        references_programs=(),
    )


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
        safety=_double_safe(rgc,"a test double; it touches nothing"),
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
        safety=_double_safe(rgc,"a test double; it touches nothing"),
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
        safety=_double_safe(rgc,"a test double; it only raises"),
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
            rgc, "nope", lambda c, w: True, safety=_double_unsafe(rgc,"writes to a live repo")
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
            safety=_double_safe(rgc,"a test double"),
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
        rgc, "hang", hang, spawns_process=jobs > 1, safety=_double_safe(rgc,"a test double")
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
                safety=_double_safe(rgc,"a test double"),
            ),
            "refused_one": rgc.Guard(
                run=lambda c, w: bool(ran.append("refused_one")) or True,
                safety=_double_unsafe(rgc,"writes into a live repository"),
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
        {"refused_one": rgc.Guard(run=lambda c, w: True, safety=_double_unsafe(rgc,"writes"))},
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
                safety=_double_safe(rgc,"a test double"),
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
    protected = _list_sections(out)["protected_paths"]
    assert "no environment reads" not in protected
    assert "expandvars" in protected or "environment" in protected
    # The prose claim now has a machine-checked twin printed beside it. Both are
    # asserted: the sentence a human reads, and the fact a walk produced.
    assert "reads_env=True" in protected


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
        {"boom": rgc.Guard(run=boom, safety=_double_safe(rgc,"a test double"))},
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
        safety=_double_safe(rgc,"a test double"),
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
        safety=_double_safe(rgc,"a test double"),
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
# Two halves, because neither catches the other's class. An AST fact-checker
# would have caught NONE of the six stale citations. A citation checker would not
# notice a guard that quietly gains a subprocess.run.
#
# And a third, because the first two together still miss the case that motivated
# the issue: `bash_safety` has no Python module at all, and its delegation to
# git_discard_guard is a SHELL PIPE. A Python import-closure walk cannot reach it
# by construction. Replaying the four historical defects: the facts catch #1 and
# #3, the citations catch #4, and only the shell scan catches #2 — the one that
# got past prose review twice.
#
# All matchers come from the module's own published constants, so the coverage
# `--list` advertises and the coverage this checker applies cannot drift apart:
# there is one set, not two.


def _hooks(rgc) -> Path:
    """The guards directory the tool itself resolves. The checkers now live in
    the SCRIPT — these tests call the same functions `main()` does, which is the
    property that made moving them worth the churn."""
    return Path(rgc._HOOKS)


@contextlib.contextmanager
def _fake_repo_scripts(rgc, names: set[str]):
    """Swap the tracked-script inventory for one test, and put it back.

    `repo_script_names()` is `functools.cache`d and shells out to git, so a test
    that needs a synthetic delegate has to replace it AND clear the cache on both
    sides — otherwise the fake leaks into every later test in the module, which
    is the same process-global-memo hazard `fake_loaded` exists for.
    """
    real = rgc.repo_script_names
    rgc.repo_script_names = lambda: frozenset(names)
    try:
        yield
    finally:
        rgc.repo_script_names = real
        real.cache_clear()


def _module_using(spelling: str) -> str | None:
    """A throwaway module that really uses `spelling`, or None if it is a prose row.

    Turning the published list into compilable code is what makes it a CLAIM the
    matcher has to honour rather than a paragraph beside it. The two non-code
    rows — the `open(..., mode with …)` descriptions — are rendered by
    `fact_coverage()` for humans and are exercised by their own dedicated cases
    below rather than skipped silently.
    """
    if "mode with any of" in spelling:
        name = spelling.split("(")[0]
        if name == ".open":
            return "import pathlib\n\n\ndef f():\n    return pathlib.Path('/x').open('w')\n"
        if name == "open":
            return "def f():\n    return open('/x', 'w')\n"
        root = name.split(".")[0]
        first = "3" if name == "os.fdopen" else "'/x'"
        return f"import {root}\n\n\ndef f():\n    return {name}({first}, 'w')\n"
    if spelling.startswith("."):
        method = spelling[1:-2]
        return f"import pathlib\n\n\ndef f():\n    return pathlib.Path('/x').{method}()\n"
    root = spelling.split(".")[0]
    return f"import {root}\n\n\ndef f():\n    return {spelling}\n"


def _write_guard(hooks: Path, name: str, body: str) -> None:
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / f"{name}.py").write_text(body)


# ── the checks themselves ────────────────────────────────────────────────────


def test_every_guard_declares_evidence_of_the_kind_its_mechanism_needs(rgc):
    """ALLOWLIST polarity — and the earlier version of this test did NOT have it.

    It asserted only that an evidence OBJECT was present. That is satisfied by
    whichever type is cheapest to fake, and the two verifiers are disjoint: the
    fact walk iterates PyEvidence, the shell scan iterates ShEvidence. So a
    PYTHON guard carrying an ShEvidence over an empty string is checked by
    neither.

    MEASURED, by execution: `git_discard` — refused because `git stash create`
    writes objects into whatever live repository each row was recorded in — was
    re-declared `replay_safe(...)` with `ShEvidence(source=lambda: "")` and
    passed all four checkers, arriving at `safe=True`. Four green, zero
    verification, on the most dangerous guard in the table. Requiring `evidence`
    at the constructor is not polarity; binding its KIND to the mechanism is.
    """
    problems = []
    for name, guard in sorted(rgc.GUARDS.items()):
        evidence = guard.safety.evidence
        if not isinstance(evidence, rgc.PyEvidence | rgc.ShEvidence):
            problems.append(f"{name}: no machine-checkable evidence at all")
        elif guard.py_module is not None:
            if not isinstance(evidence, rgc.PyEvidence):
                problems.append(
                    f"{name} runs the Python guard {guard.py_module!r} but declares "
                    f"{type(evidence).__name__}, which the fact walk never looks at. "
                    "Shell evidence on a Python guard is an unchecked declaration "
                    "wearing a checked one's clothes."
                )
            elif evidence.module != guard.py_module:
                problems.append(
                    f"{name} runs {guard.py_module!r} but its evidence describes "
                    f"{evidence.module!r} — the declaration is about a different "
                    "artifact from the one that will be replayed."
                )
        elif not isinstance(evidence, rgc.ShEvidence):
            # The mirror of the case above, and it used to CRASH here with
            # `AttributeError: 'PyEvidence' object has no attribute 'source'`
            # instead of reporting — a red for the wrong reason, which is a red
            # you have not earned. Found by the mutation sweep, not by reading.
            problems.append(
                f"{name} is a shell guard (no py_module) but declares "
                f"{type(evidence).__name__}, whose module the fact walk would "
                "look for under scripts/hooks/ and never find."
            )
        elif not evidence.source().strip():
            problems.append(
                f"{name}: ShEvidence.source() is empty, so the delegation scan "
                "reads nothing and asserts nothing. Point it at the real text."
            )
    assert not problems, "\n".join(problems)


def test_every_published_spelling_is_actually_detected(rgc, tmp_path):
    """Bind the published coverage to the matcher, so the list cannot over-claim.

    This is the fix for the CLASS the review found, not for one instance of it.
    Every finding was the same generator: a claim written to describe intended
    semantics, an implementation that was narrower, and nothing holding them
    together. `--list` published `subprocess.run` while the matcher compared
    literal text, so `import subprocess as sp` walked straight through — MEASURED
    at ten realistic spellings missed out of ten, against two controls detected.

    Here the published list is EXECUTABLE: each spelling is compiled into a
    throwaway module and the walk must find it. A spelling that goes in the list
    without the matcher seeing it now fails immediately, in both directions.
    """
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    undetected: list[str] = []
    for fact, spellings in rgc.fact_coverage().items():
        for spelling in spellings:
            body = _module_using(spelling)
            if body is None:
                continue
            for stale in hooks.glob("*.py"):
                stale.unlink()
            (hooks / "probe.py").write_text(body)
            if not rgc.walk_facts("probe", hooks)[fact]:
                undetected.append(f"{fact}: {spelling!r} is published but not matched")
    assert not undetected, "\n".join(undetected)


def test_a_bare_method_name_is_unambiguous_by_construction(rgc):
    """The eligibility rule for `_FACT_METHODS`, derived from the stdlib.

    Naming `.replace`, `.write` and `.truncate` as forbidden is a three-name
    DENYLIST — it cannot see `.read`, `.close`, `.seek`, `.flush` or `.pop`, and
    the next ambiguous name added would pass. A bare name is matched on ANY
    receiver, so the property that makes one safe is that pathlib carries it and
    the common non-filesystem receivers do not. Asserting THAT is allowlist
    polarity; listing three exclusions is not.
    """
    import io

    # io.IOBase alone is not enough: it declares `truncate` but NOT `write`,
    # which lives on the Raw/Buffered/Text subclasses. Checking only the base
    # would have let `.write` through the very invariant added to exclude it.
    others = (str, bytes, list, dict, io.IOBase, io.RawIOBase, io.BufferedIOBase, io.TextIOBase)

    ambiguous = []
    for name in sorted(set().union(*rgc._FACT_METHODS.values())):
        carriers = [t.__name__ for t in others if hasattr(t, name)]
        if not hasattr(Path, name):
            ambiguous.append(f"{name!r} is not a pathlib.Path method at all")
        elif carriers:
            ambiguous.append(f"{name!r} is also carried by {', '.join(carriers)}")
    assert not ambiguous, "\n".join(ambiguous)
    # Guard-the-guard: the invariant must actually REJECT the three names that
    # motivated it, or it is passing for the wrong reason.
    for known_bad in ("replace", "write", "truncate"):
        carriers = [t.__name__ for t in others if hasattr(t, known_bad)]
        assert carriers, f"{known_bad!r} was expected to be ambiguous and is not"


def test_the_declared_facts_match_a_transitive_walk_of_the_guards_imports(rgc):
    """The fact half, bidirectional, over every Python guard.

    This would have caught two of the four historical defects: protected_paths
    declaring "no environment reads" while `_expand` runs `os.path.expandvars`,
    and worktree_cwd omitting the `sys.argv` branch that decides which classifier
    a row goes through.

    It is bidirectional deliberately. A declared-but-untrue fact is the rot
    direction nobody inspects, because an over-cautious declaration still reads
    as careful.
    """
    problems: list[str] = []
    for name, guard in sorted(rgc.GUARDS.items()):
        evidence = guard.safety.evidence
        if isinstance(evidence, rgc.PyEvidence):
            problems += rgc._check_facts(name, evidence, _hooks(rgc))
    assert not problems, "\n".join(problems)


def test_every_citation_still_resolves(rgc):
    """The citation half — the one an AST fact-checker cannot do.

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


def test_a_shell_guard_acknowledges_every_program_and_script_it_names(rgc):
    """The half that reaches the case the other two miss by construction.

    `bash_safety` has no Python module: its delegation to git_discard_guard is a
    shell pipe, so no import walk can see it. MEASURED when this check was first
    run against the tree — the file references SIX guards and its declaration
    named ONE. Three are invoked (destructive_command_guard and
    protected_paths_guard through one loop, git_discard_guard through the pipe);
    the other three are an existence test and two comments.

    The scan proves OCCURRENCE, never invocation. That is the honest claim a
    closed-set cross-reference can make, and it is enough: the obligation it
    creates is to say in `why` what each name is doing there.
    """
    problems: list[str] = []
    for name, guard in sorted(rgc.GUARDS.items()):
        evidence = guard.safety.evidence
        if not isinstance(evidence, rgc.ShEvidence):
            continue
        self_name = Path(evidence.label).name
        try:
            text = evidence.source()
        except BaseException as exc:  # noqa: BLE001 - SystemExit is the live case
            # `_inline_blob` locates the blob by CONTENT, so a reword in
            # .claude/settings.json raises SystemExit out of source(). Seven open
            # PRs touch that file. Unwrapped it surfaces as an unhandled
            # BaseException with no hint of the cause.
            problems.append(
                f"{name}: could not read {evidence.label} ({exc!r}). If this is the "
                "inline blob, it was probably reworded — `_inline_blob` finds it by "
                "matching its text, so the locator needs updating, not the scan."
            )
            continue
        scripts, programs = rgc.scan_shell(text, self_name)
        for kind, actual, declared in (
            ("script", scripts, set(evidence.references_scripts)),
            ("program", programs, set(evidence.references_programs)),
        ):
            for extra in sorted(actual - declared):
                problems.append(
                    f"{name}: {evidence.label} references {kind} {extra!r}, which "
                    "the declaration does not acknowledge. Say what it is doing "
                    "there — a delegation whose side effects belong in `why`, or "
                    "message text that only looks like one."
                )
            for stale in sorted(declared - actual):
                problems.append(
                    f"{name}: declares {kind} {stale!r}, which no longer occurs in "
                    f"{evidence.label}. The prose about it is now describing "
                    "something that is not there."
                )
    assert not problems, "\n".join(problems)


def test_the_published_coverage_excludes_the_names_that_over_report(rgc):
    """`--list` prints the coverage set, so the set is a published claim.

    `.replace` and `.write` are the load-bearing absences and this pins them. A
    first cut of the walk matched bare attribute names on any receiver and
    reported writes_fs=True for protected_paths from
    `prot.replace(home, "~", 1)` — a STRING replace in
    `_legacy_substring_block`. An over-reporting checker is not the safe kind of
    wrong: it forces a pure argv classifier to declare a filesystem write, which
    is the declaration lying in the other direction, and a reader who catches it
    once stops believing any of the facts.
    """
    every_method = set().union(*rgc._FACT_METHODS.values())
    assert "replace" not in every_method
    assert "write" not in every_method
    assert "truncate" not in every_method
    # Still bounded, and the honesty is in saying so rather than in the size.
    for fact, spellings in rgc.fact_coverage().items():
        assert spellings, f"{fact} is checked against nothing, so False means nothing"


# ── acceptance bar: three arms, each verified RED before this shipped ─────────


def test_arm1_deleting_a_declared_fact_turns_the_check_red(rgc):
    """Issue #1946 arm 1, run against the real declaration rather than a stand-in.

    worktree_cwd's `sys.argv` read is a real one: main() branches on
    `"--enter-worktree" in sys.argv`, and an inherited flag put every row through
    the Enter/ExitWorktree classifier. Deleting the fact must fail.
    """
    real = rgc.GUARDS["worktree_cwd"].safety.evidence
    understated = real._replace(reads_argv=False)
    problems = rgc._check_facts("worktree_cwd", understated, _hooks(rgc))
    assert problems, "removing a TRUE fact from the declaration was not noticed"
    assert any("reads_argv" in p and "sys.argv" in p for p in problems), problems


def test_arm2_a_guard_that_gains_a_subprocess_turns_the_check_red(rgc, tmp_path):
    """Issue #1946 arm 2 — the drift a citation checker cannot see.

    Built as a throwaway hooks tree rather than by mutating a real guard: the
    check must fail because of what the code DOES, and pointing it at a directory
    of our own is the only way to be sure the fixture created the shape it claims.
    """
    hooks = tmp_path / "hooks"
    _write_guard(
        hooks, "pure_guard", "import helper\n\n\ndef main() -> int:\n    return helper.n\n"
    )
    _write_guard(hooks, "helper", "n = 0\n")
    declared_pure = rgc.PyEvidence(
        module="pure_guard",
        reads_env=False,
        reads_argv=False,
        network=False,
        spawns=False,
        writes_fs=False,
    )
    # Guard-the-guard: the fixture must be clean BEFORE the mutation, or a red
    # below proves nothing about the subprocess we are about to add.
    assert not rgc._check_facts("pure_guard", declared_pure, hooks)

    # The spawn arrives TRANSITIVELY, through the imported helper — which is the
    # shape that matters. A subprocess in the guard's own file would be caught by
    # reading it; this one would not.
    _write_guard(
        hooks, "helper", "import subprocess\n\nn = 0\n\n\ndef go():\n    subprocess.run(['true'])\n"
    )
    problems = rgc._check_facts("pure_guard", declared_pure, hooks)
    assert problems, "a guard that gained a transitive subprocess.run was not noticed"
    assert any("spawns" in p and "subprocess.run" in p for p in problems), problems


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


def test_a_shell_guard_that_gains_a_delegation_turns_the_check_red(rgc):
    """The fourth arm, for the third half — the historical defect #2 replay.

    Not in the issue's list because the issue believed the AST walk covered this
    case. It does not, so the arm belongs with the others: dropping
    git_discard_guard.py from bash_safety's acknowledged set must fail, because
    that omission IS the defect that shipped twice.
    """
    evidence = rgc.GUARDS["bash_safety"].safety.evidence
    understated = evidence._replace(
        references_scripts=tuple(
            s for s in evidence.references_scripts if s != "git_discard_guard.py"
        )
    )
    scripts, _ = rgc.scan_shell(understated.source(), Path(understated.label).name)
    assert "git_discard_guard.py" in scripts - set(understated.references_scripts), (
        "dropping the delegate from the declaration went unnoticed — this is "
        "exactly the omission that got past prose review twice"
    )


def test_list_prints_the_facts_and_says_what_a_false_means(rgc, monkeypatch, capsys):
    """The coverage line is not decoration: it is what stops `spawns=False` being
    read as proof of purity. --list is the surface every rate-reader passes
    through, so the qualifier travels with the claim."""
    monkeypatch.setattr(sys, "argv", ["replay_guard_corpus.py", "--list"])
    assert rgc.main() == 0
    out = capsys.readouterr().out
    assert "facts (protected_paths_guard, transitive): reads_env=True" in out
    assert "shell (scripts/bash_safety_hook.sh)" in out
    assert "a False fact means none of these spellings were found" in out
    # The published set must be the REAL one. `--list` used to say "every tracked
    # repo script name" while the scan used a non-recursive, suffix-filtered
    # filesystem glob holding 180 of 236 names.
    assert f"plus {len(rgc.repo_script_names())} tracked script basenames" in out
    # Every uncovered case is NAMED rather than silently omitted — the
    # UNSCANNED_ROOTS convention, applied to claims instead of directories. All
    # three, because listing only the comfortable one is the same overclaim in
    # miniature.
    assert "Not covered, and named rather than left silent" in out
    uncovered = out.split("Not covered, and named rather than left silent")[1]
    assert "getattr" in uncovered, "the runtime-rebinding blind spot is unstated"
    assert "$_guard" in uncovered, "the variable-built delegation path is unstated"
    assert "inline_blob" in uncovered, "the uncheckable blob measurement is unstated"
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


def test_an_unaliased_dotted_import_binds_its_first_component(rgc, tmp_path):
    """Round 1, finding 2 — a fail-open introduced BY the fix for a fail-open.

    `import os.path` binds the name `os`, not `os.path`. Recording `os ->
    os.path` turned `os.path.expandvars` into `os.path.path.expandvars`, so a
    guard written in that style kept `reads_env=False` while reading the
    environment on every operand — and the bidirectional check stayed green.
    Caught by the external reviewer, not by this file's own tests, which is the
    reason it gets an arm rather than a comment.
    """
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for label, body in (
        ("import os.path", "import os.path\n\n\ndef f(t):\n    return os.path.expandvars(t)\n"),
        ("import os", "import os\n\n\ndef f(t):\n    return os.path.expandvars(t)\n"),
        ("aliased", "import os.path as p\n\n\ndef f(t):\n    return p.expandvars(t)\n"),
    ):
        (hooks / "probe.py").write_text(body)
        found = rgc.walk_facts("probe", hooks)
        assert found["reads_env"], f"{label}: the environment read went undetected"


def test_a_network_call_is_a_declared_fact(rgc, tmp_path):
    """Round 1, finding 3 — the record's own docstring defines replay safety as
    whether repeated invocation "writes, spawns, or calls out", and the
    undeclared-guard message demands network effects be stated, but nothing
    checked one. A guard could add an HTTP call per corpus row and stay green.

    `urllib.parse` is the negative control and it is not incidental: push_allowlist
    imports it for URL string parsing, so counting it would force git_push to
    declare a network call it does not make.
    """
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "probe.py").write_text(
        "import urllib.request\n\n\ndef f(u):\n    return urllib.request.urlopen(u)\n"
    )
    assert rgc.walk_facts("probe", hooks)["network"]

    (hooks / "probe.py").write_text(
        "import urllib.parse\n\n\ndef f(u):\n    return urllib.parse.urlsplit(u)\n"
    )
    assert not rgc.walk_facts("probe", hooks)["network"], (
        "URL parsing is pure string work and must not read as a network call"
    )


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


def test_a_shell_guards_delegates_have_their_facts_checked(rgc, tmp_path):
    """Round 1, finding 5 — the sharpest of the six.

    `bash_safety`'s declaration asserts its two extra delegates are "pure
    argv/string classifiers ... neither spawns nor writes". That was verified by
    hand with a throwaway probe and by NOTHING shipped: MEASURED, both
    destructive_command_guard and shell_parse were referenced by the declaration
    and fact-walked by no check at all. A prose claim nobody checks, committed
    inside the mechanism built to end prose claims nobody checks.
    """
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    _write_guard(hooks, "delegate", "def main() -> int:\n    return 0\n")
    pure = rgc.PyEvidence(
        module="delegate",
        reads_env=False,
        reads_argv=False,
        network=False,
        spawns=False,
        writes_fs=False,
    )
    # Drive the REAL seam. An earlier version of this test called _check_facts
    # directly, and a mutation sweep proved that vacuous: deleting the delegate
    # walk from _check_shell entirely left it GREEN, because the integration
    # point — the thing the finding was actually about — was never exercised.
    evidence = rgc.ShEvidence(
        source=lambda: '"$_py" "$SCRIPT_DIR/hooks/delegate.py"\n',
        label="fake_hook.sh",
        references_scripts=("delegate.py",),
        # Nothing declared here that the fake text does not contain — the
        # guard-the-guard below is what caught an earlier version declaring
        # "python3" against source that never mentions it.
        references_programs=(),
        invokes=(pure,),
    )
    with _fake_repo_scripts(rgc, {"delegate.py"}):
        # Guard-the-guard: clean before the mutation, or the RED proves nothing.
        assert not rgc._check_shell("fake", evidence, hooks)

        _write_guard(
            hooks,
            "delegate",
            "import subprocess\n\n\ndef main() -> int:\n"
            "    subprocess.run(['true'])\n    return 0\n",
        )
        problems = rgc._check_shell("fake", evidence, hooks)
    assert problems, "a delegate that gained a subprocess was not noticed"
    assert any("spawns" in p for p in problems), problems

    # And a delegate the shell text does not even mention is refused, because
    # "invokes" is the author's claim and it has to be a claim about this file.
    stray = rgc.ShEvidence(
        source=lambda: "echo hi\n",
        label="fake_hook.sh",
        references_scripts=(),
        references_programs=(),
        invokes=(pure,),
    )
    assert any("does not occur" in p for p in rgc._check_shell("fake", stray, hooks))


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


def test_a_quoted_path_cannot_fall_out_of_the_script_name_set(rgc, monkeypatch):
    """Round 1, finding 1 — git QUOTES a path containing whitespace, so a plain
    split() mangles it and the real basename silently leaves the set. An
    under-reporting delegation scan reads exactly like a clean one."""
    import subprocess as sp

    captured: dict[str, list[str]] = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return sp.CompletedProcess(argv, 0, "scripts/a b.sh\0scripts/hooks/pre-push\0", "")

    monkeypatch.setattr(rgc.subprocess, "run", fake_run)
    rgc.repo_script_names.cache_clear()
    try:
        names = rgc.repo_script_names()
    finally:
        rgc.repo_script_names.cache_clear()
    assert "-z" in captured["argv"], "NUL-delimited output was not requested"
    assert "a b.sh" in names, "a path with a space did not survive as a basename"
    assert "pre-push" in names
