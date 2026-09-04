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
    """Point the module at a throwaway cache and stub the 1.4 GB transcript walk."""
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
    rgc._run_python_guard._loaded["fake_guard"] = mod

    before = os.getcwd()
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

    The build walks ~1.4 GB; a kill part-way leaves a truncated final line, and
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
    delegation and wrote rows to the live recovery log, which self-trims at 1 MB
    and would have evicted real recovery history. That redirect is gone, because
    it was never sufficient — it moved the LOG but not the objects
    `git stash create` writes into whatever repository each row was recorded in.
    `bash_safety` is refused outright now, and the half-measure went with it.

    What still matters on this path is the one variable measured to change a
    VERDICT rather than a message. bash_safety_hook.sh:319 exits 0 when
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


# ── replay-safety declaration ────────────────────────────────────────────────
#
# The exclusions used to live in prose comments that reasoned about WHICH GUARD
# was in the table. A delegation defeated exactly that: `bash_safety` is in the
# table and reaches `git_discard_guard`, which is not, through a CALLER. A
# comment cannot be defeated that way once what it governs is permission to run
# at all, declared per entry and enforced by the runner.


_UNSET = object()


@contextlib.contextmanager
def fake_guard(rgc, name, fn, *, spawns_process=False, safety=_UNSET):
    """Register a guard for one test and remove it afterwards.

    Replaces the old pattern of poking two globals (``GUARDS`` and
    ``_SHELL_GUARDS``), which could disagree. Passing no ``safety`` builds the
    record the way a careless newcomer would — that is the point of the first
    test below, so the default here must stay "argument omitted" rather than
    "declared unsafe".
    """
    kwargs = {"run": fn, "spawns_process": spawns_process}
    if safety is not _UNSET:
        kwargs["safety"] = safety
    rgc.GUARDS[name] = rgc.Guard(**kwargs)
    try:
        yield
    finally:
        rgc.GUARDS.pop(name, None)


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
    """The corpus build walks ~1.4 GB. A refusal that arrives after it has spent
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

    rgc._run_python_guard._loaded["crashy"] = types.SimpleNamespace(
        main=boom_main, read_payload=lambda: None
    )
    corpus = [("echo hi", "/tmp")] * 4
    with fake_guard(
        rgc,
        "crashy",
        lambda c, w: rgc._run_python_guard("crashy", c, w),
        safety=rgc.replay_safe("a test double"),
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
    rgc._run_python_guard._loaded["restorer"] = mod
    before = os.getcwd()

    with pytest.raises(RuntimeError):
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
