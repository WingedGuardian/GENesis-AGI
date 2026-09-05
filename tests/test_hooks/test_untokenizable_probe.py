"""Shared tokenizability probe, and the commit gate's fail-closed wrapper.

`shell_parse.analyze()` degrades to a naive split on a shlex error SILENTLY, so
it cannot report that its own answer is untrustworthy: "no gated segment found"
and "no gated command present" are the same return value. `untokenizable()` is
the signal that separates them, so a security-critical caller can pick its own
fail direction while the parser keeps degrading gracefully.

This file covers the probe itself, the removal of the duplicate copy of it in
`protected_paths_guard` (including that the guard actually CALLS the shared
one), and the commit gate's `run_guard` wrapper plus its degrade when
`review_state` will not load.

Trigger literals are assembled from fragments so this file's own text does not
carry them (matching the convention in test_shell_parse.py).
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_HOOKS_DIR = _WORKTREE / "scripts" / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import shell_parse as sp  # noqa: E402

_COMMIT_GUARD = _WORKTREE / "scripts" / "review_enforcement_commit.py"
_PROTECTED_GUARD = _HOOKS_DIR / "protected_paths_guard.py"
_PY = sys.executable

COMMIT = "com" + "mit"

# ALLOWLIST, not a subtract-list. A guard child that inherits the caller's
# environment reports on the caller, not on the code: `GIT_DIR` re-points
# repo-state lookups at whatever repo the developer happens to be in, and
# `GENESIS_CC_SESSION` makes a suite test its caller's session mode. Naming what
# may pass means the NEXT ambient input cannot leak in by default.
_NEUTRAL_ENV = frozenset({"PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "PYTHONHASHSEED"})


def _child_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _NEUTRAL_ENV}
    # HOME is set explicitly rather than allow-listed: without it the child
    # falls back to the passwd DB while the test computes Path.home() from the
    # env var, so the suite breaks wherever those disagree (measured: 3 failures
    # under a non-passwd HOME, including a safety control).
    env["HOME"] = os.path.expanduser("~")
    env["GIT_CONFIG_GLOBAL"] = os.devnull  # no developer gitconfig (gpgsign etc.)
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env.update(extra)
    return env


def _run(script: Path, cmd: str, cwd: str, **env_extra: str) -> subprocess.CompletedProcess:
    """Run a guard with the payload cwd and the child cwd AGREEING.

    Passing cwd only in the payload leaves the child in whatever directory
    pytest ran from, so repo-state gates silently evaluate the wrong repository.
    """
    return subprocess.run(
        [_PY, str(script)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": cwd}),
        capture_output=True,
        text=True,
        timeout=30,
        env=_child_env(**env_extra),
        cwd=cwd,
    )


class TestUntokenizable:
    """The probe's contract: True exactly when shlex cannot tokenize the RAW text."""

    def test_ansic_escaped_quote_is_untokenizable(self):
        # shell_parse reads `$'…'` as a plain single-quote span, so the `\'`
        # inside closes it early — the exact shape that shifts segmentation.
        assert sp.untokenizable("echo $'a\\'b)c'") is True

    def test_heredoc_body_apostrophe_is_untokenizable(self):
        # An ORDINARY shape, not an evasion: a quoted here-doc whose body
        # contains a contraction. It genuinely shifts analyze()'s segmentation,
        # so the probe must report it rather than normalising it away.
        cmd = f"{COMMIT} -F - <<'EOF'\nit's a message with an apostrophe\nEOF"
        assert sp.untokenizable(cmd) is True

    @pytest.mark.parametrize(
        "cmd",
        [
            "echo hello",
            "git status --short",
            "grep -n 'pattern' file.txt",
            "python3 -c \"print('ok')\"",
            # $'…' with no escaped quote tokenizes fine and must NOT be flagged
            "git commit -m $'line1\\nline2'",
        ],
    )
    def test_ordinary_commands_are_tokenizable(self, cmd):
        assert sp.untokenizable(cmd) is False

    def test_probe_reads_the_raw_command(self, monkeypatch):
        """No normalisation, and specifically no line-continuation folding.

        An earlier revision folded `\\<newline>` to a SPACE first. That is wrong
        about bash, which REMOVES a continuation and joins the halves into one
        word, and it also contradicted the probe's own contract. Measured over
        12,099 real commands the two classify identically, so the fold bought
        nothing; this pins the raw reading so it cannot creep back.

        The verdict cannot pin it. Folded and unfolded classify this command
        IDENTICALLY — that is exactly what the 12,099-command measurement found
        — and a str is immutable, so a probe that rewrote its input internally
        would still leave the caller's copy untouched. Both of those assertions
        stay green with the fold restored, which makes them a statement about
        Python, not about the contract. So spy on the tokenizer and assert what
        it RECEIVED: the fold changes that argument, and only that argument.
        """
        seen: list[str] = []
        real_split = sp.shlex.split

        def spy(s, *args, **kwargs):
            seen.append(s)
            return real_split(s, *args, **kwargs)

        monkeypatch.setattr(sp.shlex, "split", spy)
        cmd = "git pu\\\nsh origin main"
        assert sp.untokenizable(cmd) is False
        assert seen == [cmd], (
            "the probe rewrote its input before tokenizing; it must read the "
            f"command RAW. shlex.split received: {seen!r}"
        )


class TestProtectedPathsUsesTheSharedProbe:
    """The duplicate probe in this guard is gone; behaviour is preserved."""

    def test_ansic_obfuscated_rm_of_protected_dir_still_blocks(self, tmp_path):
        """The reason the inline probe existed — it must still work.

        This is a BEHAVIOUR check, not a wiring one, and it cannot be made into
        a wiring one: MEASURED, this command still blocks even when the probe is
        forced to answer "tokenizable", because an operand keeping an unresolved
        `$` reaches the same legacy fallback by a second route
        (the unresolved-`$` operand check further down `main`). Deleting the
        probe would not move this assertion. An earlier version of this docstring claimed the opposite —
        that without it the refactor could delete the probe and everything here
        would stay green — which was false and contradicted the wiring test
        below. The wiring claim lives there, on the CALL; this one only pins
        that the dangerous input is still refused.
        """
        protected = Path.home() / "backups"
        cmd = "rm -rf $'a\\'b)c' " + str(protected)
        r = _run(_PROTECTED_GUARD, cmd, cwd=str(tmp_path))
        assert r.returncode == 2, r.stdout + r.stderr

    def test_ordinary_rm_is_untouched(self, tmp_path):
        # `== 0`, not `!= 2`: an import-time crash exits 1 and would satisfy the
        # looser form, so it would pass against a guard that is entirely broken.
        r = _run(_PROTECTED_GUARD, f"rm -rf {tmp_path}/scratch", cwd=str(tmp_path))
        assert r.returncode == 0, r.stdout + r.stderr

    def _guard_in_tree_whose_probe(self, tmp_path, label: str, body: str, command: str):
        """Run the guard from a temp tree whose `shell_parse` is ours.

        The guard does `sys.path.insert(0, dirname(__file__))` before importing,
        so the sibling written here is the module it actually gets — PYTHONPATH
        shadowing loses to that (measured in the crash test below). Every name
        except `analyze_checked` is delegated to the genuine parser, so exactly
        one variable changes between the two calls.

        `analyze_checked` is the poison target because it is the CHOKEPOINT the
        guards now ask — one call returning both the segments and whether the parse
        could read all of the command. Poisoning `untokenizable` instead would prove
        nothing about wiring: the guard reaches it only THROUGH the chokepoint, so a
        guard that had dropped the chokepoint entirely would sail past a poisoned
        `untokenizable` and this test would go green on a guard that asks nothing.
        """
        hooks = tmp_path / label / "hooks"
        hooks.mkdir(parents=True)
        (hooks / _PROTECTED_GUARD.name).write_text(_PROTECTED_GUARD.read_text())
        (hooks / "hook_input.py").write_text((_HOOKS_DIR / "hook_input.py").read_text())
        (hooks / "shell_parse.py").write_text(
            "import importlib.util, sys\n"
            "_s = importlib.util.spec_from_file_location(\n"
            f"    '_real_sp', {str(_HOOKS_DIR / 'shell_parse.py')!r}\n"
            ")\n"
            "_real = importlib.util.module_from_spec(_s)\n"
            # Register BEFORE exec_module: @dataclass resolves
            # sys.modules[cls.__module__] while creating the class, and a module
            # missing from it raises AttributeError at IMPORT time. That crash
            # exits 1 (the documented import-time fail-open), which silently
            # satisfies a "did not block" assertion — this test was vacuous for
            # exactly that reason until the control below caught it.
            "sys.modules['_real_sp'] = _real\n"
            "_s.loader.exec_module(_real)\n"
            # Delegate EVERY other name via PEP 562 rather than listing them.
            # A hand-written list goes stale the moment the guard imports one
            # more symbol, and the failure is not obvious: the guard's import
            # raises, the child exits 1, and that is an import crash wearing the
            # costume of a verdict. This way the ONLY difference from a real run
            # is the probe's answer, by construction.
            "def __getattr__(name):\n    return getattr(_real, name)\n"
            f"def analyze_checked(*a, **k):\n    {body}\n"
        )
        return subprocess.run(
            [_PY, str(hooks / _PROTECTED_GUARD.name)],
            input=json.dumps(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                    "cwd": str(tmp_path),
                }
            ),
            capture_output=True,
            text=True,
            timeout=30,
            env=_child_env(),
            cwd=str(tmp_path),
        )

    def test_the_guard_actually_CALLS_the_shared_probe(self, tmp_path):
        """Wiring, asserted directly: make the shared probe raise.

        The behavioural test above passes against the PARENT implementation too
        — its inline copy calls the same command untokenizable and blocks
        through the same legacy fallback — so alone it cannot tell a wired guard
        from a duplicated one, and reverting only the refactor leaves that whole
        class green.

        Nor can a VERDICT on the ANSI-C command carry the claim, which is what
        this test tried first: with the probe forced to answer "tokenizable"
        that command STILL blocks, because the unresolved-`$` operand check further down `main` reaches
        the same legacy fallback by a second route when an operand keeps an
        unresolved `$`. The guard is defence-in-depth there, so its verdict is
        the wrong observable — the probe could be deleted entirely and the
        verdict would not move.

        So observe the CALL instead, on a BENIGN command whose honest verdict is
        allow: a probe that raises turns into a fail-closed 2 if and only if the
        guard consults it. An inline copy never touches ours and still exits 0.
        """
        benign = f"rm -rf {tmp_path}/a/b/c/scratch"
        honest = self._guard_in_tree_whose_probe(
            tmp_path, "honest", "return _real.analyze_checked(*a, **k)", benign
        )
        assert honest.returncode == 0, (
            "POSITIVE CONTROL FAILED — this command is not an allow in the temp "
            "tree, so the poisoned run below cannot mean what it claims.\n"
            f"{honest.stdout}{honest.stderr}"
        )
        poisoned = self._guard_in_tree_whose_probe(
            tmp_path, "raising", "raise RuntimeError('probe consulted')", benign
        )
        assert poisoned.returncode == 2, (
            "a raising shared probe did not reach the guard, so the guard is "
            "not calling it — the private copy is back.\n"
            f"{poisoned.stdout}{poisoned.stderr}"
        )


class TestCommitGuardFailsClosedOnCrash:
    """A crash in the commit gate must BLOCK, not silently allow.

    CC's PreToolUse contract is "exit 2 = block; ANY other code = non-blocking,
    the tool runs". The module called `main()` bare, so every uncaught exception
    exited 1 — a silent fail-open on a commit.
    """

    def test_real_module_crash_exits_2(self, tmp_path):
        """Drives the REAL module and makes IT crash.

        An earlier version of this test built a local function that raised and
        handed it to `run_guard`, naming the module only in a string. That
        passes against the UNWRAPPED module too — it proves `run_guard` works,
        not that this gate uses it, so it was vacuous with respect to the change
        it claimed to cover.

        Shadowing via PYTHONPATH does not work either: the guard does
        `sys.path.insert(0, dirname(__file__)/"hooks")` before importing, which
        beats PYTHONPATH, so the real dependency wins and nothing crashes
        (measured: exit 0). Since that path is resolved relative to the guard's
        OWN location, the module is copied into a temp tree whose sibling
        `hooks/` holds a poisoned `shell_parse` — so the import the real module
        performs is the one that fails.
        """
        scripts = tmp_path / "scripts"
        hooks = scripts / "hooks"
        hooks.mkdir(parents=True)
        (scripts / _COMMIT_GUARD.name).write_text(_COMMIT_GUARD.read_text())
        # Real helper (run_guard must be the genuine one), and a parser that
        # IMPORTS cleanly but raises when called. The distinction is the whole
        # point: raising at import time crashes before `run_guard(main, …)` is
        # ever reached, so the wrapper cannot catch it — see
        # test_import_time_failure_is_a_documented_gap below. To exercise the
        # wrapper the failure has to originate inside main().
        (hooks / "hook_input.py").write_text((_HOOKS_DIR / "hook_input.py").read_text())
        # Delegate every name to the real parser and poison exactly one, rather than
        # hand-listing the guard's imports. A hand-written list goes stale the moment
        # the guard imports one more symbol, and it fails in disguise: the import
        # raises, the child exits 1, and that is the DOCUMENTED import-time gap
        # wearing the costume of a runtime crash — so this test would pass or fail
        # for a reason unrelated to what it asserts. (Measured: it did exactly that
        # when the guard moved to `analyze_checked`.) Same technique as
        # `_guard_in_tree_whose_probe` above, for the same reason.
        (hooks / "shell_parse.py").write_text(
            "import importlib.util, sys\n"
            "_s = importlib.util.spec_from_file_location(\n"
            f"    '_real_sp', {str(_HOOKS_DIR / 'shell_parse.py')!r}\n"
            ")\n"
            "_real = importlib.util.module_from_spec(_s)\n"
            "sys.modules['_real_sp'] = _real\n"
            "_s.loader.exec_module(_real)\n"
            "def __getattr__(name):\n    return getattr(_real, name)\n"
            "def analyze_checked(*a, **k):\n"
            "    raise RuntimeError('induced failure inside the real guard')\n"
        )
        r = subprocess.run(
            [_PY, str(scripts / _COMMIT_GUARD.name)],
            input=json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": f"git {COMMIT} -m x"}}
            ),
            capture_output=True,
            text=True,
            timeout=30,
            env=_child_env(),
            cwd=str(tmp_path),
        )
        assert r.returncode == 2, (
            "a crash in the commit gate must fail CLOSED (exit 2); "
            f"got {r.returncode}\n{r.stdout}{r.stderr}"
        )

    def test_import_time_failure_is_a_documented_gap(self, tmp_path):
        """The wrap covers main(), NOT module import. Measured, and locked.

        `run_guard` is called at the bottom of the module, so an exception
        raised while the module is still importing — a broken dependency, a
        syntax error in a helper — never reaches it. MEASURED: the guard exits 1
        in that case, which CC treats as non-blocking, i.e. it still fails OPEN.

        This is pinned rather than hidden so the wrap is not read as a stronger
        guarantee than it is. Closing it needs the import itself guarded, which
        is a different change; what this PR fixes is every crash from main()
        onward, which is where the gate's own logic lives.
        """
        scripts = tmp_path / "scripts"
        hooks = scripts / "hooks"
        hooks.mkdir(parents=True)
        (scripts / _COMMIT_GUARD.name).write_text(_COMMIT_GUARD.read_text())
        (hooks / "hook_input.py").write_text((_HOOKS_DIR / "hook_input.py").read_text())
        (hooks / "shell_parse.py").write_text("raise RuntimeError('broken at import')\n")
        r = subprocess.run(
            [_PY, str(scripts / _COMMIT_GUARD.name)],
            input=json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": f"git {COMMIT} -m x"}}
            ),
            capture_output=True,
            text=True,
            timeout=30,
            env=_child_env(),
            cwd=str(tmp_path),
        )
        assert r.returncode == 1, (
            "documented gap changed — an import-time failure now exits "
            f"{r.returncode}. If this is now 2 the gap is CLOSED: delete this "
            "test and say so, rather than loosening it."
        )

    def test_ordinary_commit_still_reaches_a_verdict(self, tmp_path):
        """CONTROL — the wrap must not turn every command into a block.

        Without this, a guard that exits 2 unconditionally would satisfy the
        test above — but only if the command REACHES the guarded code, and the
        obvious phrasing does not. `_COMMIT_PATTERN` is `\\bcommit\\b`, and
        "echo not a git command" does not match it ("command" is not "commit"),
        so the guard returned at its early-out before `analyze()`, before the
        `review_state` import, before any git call. Everything this control
        exists to cover was downstream of the return. Use a real commit in a
        non-repo tmp dir, which does traverse all of it and still allows.
        """
        r = _run(_COMMIT_GUARD, f"git {COMMIT} -m x", cwd=str(tmp_path))
        assert r.returncode == 0, r.stdout + r.stderr

    def test_a_broken_review_state_does_not_wall_off_every_commit(self, tmp_path):
        """The fail-closed wrap must not make the gate block its own repair.

        `main()` imports `review_state` inside a try/except. While that caught
        only ImportError, a SyntaxError in that module escaped it and exited 1 —
        non-blocking, so commits proceeded. Wrapping main() in `run_guard`
        silently converted that escape into a hard BLOCK, and MEASURED it blocks
        EVERY commit on the box: `# review-override` cannot rescue it either,
        because the override is parsed after the import. That includes the
        commit repairing review_state, so the gate walls off its own fix.

        A broken review-round ledger costs the round advisory, never the ability
        to commit — which is the degrade the sibling guard already applies to
        the SAME module.
        """
        scripts = tmp_path / "scripts"
        hooks = scripts / "hooks"
        hooks.mkdir(parents=True)
        (scripts / _COMMIT_GUARD.name).write_text(_COMMIT_GUARD.read_text())
        for dep in ("hook_input.py", "shell_parse.py"):
            (hooks / dep).write_text((_HOOKS_DIR / dep).read_text())
        # Not an ImportError: the module is present and imports cleanly as a
        # FILE, but fails to compile. That is the case the narrow handler missed.
        (scripts / "review_state.py").write_text("def broken(:\n")
        r = subprocess.run(
            [_PY, str(scripts / _COMMIT_GUARD.name)],
            input=json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": f"git {COMMIT} -m x"}}
            ),
            capture_output=True,
            text=True,
            timeout=30,
            env=_child_env(),
            cwd=str(tmp_path),
        )
        assert r.returncode == 0, (
            "a broken review_state hard-blocks every commit, including the one "
            f"that would fix it.\n{r.stdout}{r.stderr}"
        )


class TestEveryGuardConsultsTheChokepoint:
    """Each guard that fails closed on an unreadable command must ASK, not just import.

    `analyze_checked` is one call answering two questions — what does this run, and
    could I read all of it. That collapses what used to be a per-call-site convention
    (remember `untokenizable`, and now remember `over_nested` too) into a single
    question. But a chokepoint only helps if each consumer actually reaches it, and an
    import proves nothing: this repo has shipped a guard that imported a helper and
    never called it, with a green suite, more than once.

    So the observable is the CALL. Each guard runs twice from a temp tree that differs
    in one variable — the chokepoint answers honestly, or it raises — on a command
    whose honest verdict is "no gate". A guard that consults it turns the raise into a
    fail-closed refusal; a guard that does not is untouched and allows. The positive
    control is what makes the second run mean anything: without it, a guard that
    refuses for some unrelated reason would look wired.
    """

    def _tree(self, tmp_path, label: str, guard: Path, body: str) -> Path:
        scripts = tmp_path / label / "scripts"
        hooks = scripts / "hooks"
        hooks.mkdir(parents=True)
        # Mirror the guard's REAL position in the tree. Each guard resolves its
        # dependencies from its own location — a hooks/ guard inserts its own dir, a
        # scripts/ guard inserts its sibling hooks/ — so a copy placed in the wrong
        # one cannot import hook_input and dies before reaching any parser at all.
        # (The positive control caught exactly that; without it the run would have
        # been a non-zero exit that looked like a wired guard.)
        target = (hooks if guard.parent.name == "hooks" else scripts) / guard.name
        target.write_text(guard.read_text())
        (hooks / "hook_input.py").write_text((_HOOKS_DIR / "hook_input.py").read_text())
        (hooks / "shell_parse.py").write_text(
            "import importlib.util, sys\n"
            "_s = importlib.util.spec_from_file_location(\n"
            f"    '_real_sp', {str(_HOOKS_DIR / 'shell_parse.py')!r}\n"
            ")\n"
            "_real = importlib.util.module_from_spec(_s)\n"
            "sys.modules['_real_sp'] = _real\n"
            "_s.loader.exec_module(_real)\n"
            "def __getattr__(name):\n    return getattr(_real, name)\n"
            f"def analyze_checked(*a, **k):\n    {body}\n"
        )
        return target

    #: A poison that RETURNS a blind spot instead of raising, for the guards that
    #: catch exceptions and fail open. For those, a raising probe is indistinguishable
    #: from an honest one — both end in "allow" — so the raise proves nothing and the
    #: test would pass against a guard that never asked. What distinguishes them is
    #: the guard ACTING on a reported blind spot.
    #:
    #: Returns the module's REAL over-length blind spot rather than a hand-built one.
    #: A hand-built BlindSpot coupled this test to the poison rather than to the
    #: shipped policy, so a change to a real blind spot's fields left it green.
    #: Returning a shipped singleton means the poison carries whatever the module
    #: actually declares.
    #:
    #: `_BLIND_OVER_LONG` specifically: while a per-axis severity flag briefly
    #: existed, the length bound was the SOFT one, and the guards wired to it
    #: fail-opened on a real unrecoverable discard. That flag is deleted and every
    #: bounds-induced blind spot now refuses identically, so the choice no longer
    #: discriminates — it is kept deliberately, because any future attempt to
    #: re-introduce a soft axis would have to soften THIS one first, and this test
    #: would go red the moment it did.
    _POISON_RETURNS_BLIND = "return ([], _real._BLIND_OVER_LONG)"

    #: The other poison: raise. For guards whose wrapper fails CLOSED on an exception,
    #: where the raise turning an allow into a refusal IS the observation.
    _RAISE = "raise RuntimeError('chokepoint consulted')"

    @pytest.mark.parametrize(
        "guard_path,benign,poison,expect_rc",
        [
            (_COMMIT_GUARD, f"echo about to {COMMIT} nothing", _RAISE, 2),
            (_HOOKS_DIR / "git_push_guard.py", "echo about to push nothing", _RAISE, 2),
            # These two catch exceptions and fail open, so they need the returning
            # poison. Both were REGRESSIONS: a bound silently removed the buried
            # command they block on, and each went from refused to allowed.
            # Must contain "git" AND a trigger substring, or main() early-outs before
            # the parse and the run proves nothing. (It did exactly that on the first
            # attempt; the positive control is what surfaced it.)
            (
                _HOOKS_DIR / "git_discard_guard.py",
                "echo git cleanup notes",
                _POISON_RETURNS_BLIND,
                2,
            ),
            (
                _HOOKS_DIR / "full_suite_guard.py",
                "echo pytest is a tool",
                _POISON_RETURNS_BLIND,
                2,
            ),
        ],
        ids=[
            "review_enforcement_commit",
            "git_push_guard",
            "git_discard_guard",
            "full_suite_guard",
        ],
    )
    def test_the_guard_consults_analyze_checked(
        self, tmp_path, guard_path, benign, poison, expect_rc
    ):
        honest = self._tree(tmp_path, "honest", guard_path, "return _real.analyze_checked(*a, **k)")
        r_honest = subprocess.run(
            [_PY, str(honest)],
            input=json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": benign}, "cwd": str(tmp_path)}
            ),
            capture_output=True,
            text=True,
            timeout=60,
            env=_child_env(),
            cwd=str(tmp_path),
        )
        assert r_honest.returncode == 0, (
            "POSITIVE CONTROL FAILED — this command is not an allow in the temp "
            "tree, so the poisoned run below cannot mean what it claims.\n"
            f"{r_honest.stdout}{r_honest.stderr}"
        )

        poisoned = self._tree(tmp_path, "poisoned", guard_path, poison)
        r_poisoned = subprocess.run(
            [_PY, str(poisoned)],
            input=json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": benign}, "cwd": str(tmp_path)}
            ),
            capture_output=True,
            text=True,
            timeout=60,
            env=_child_env(),
            cwd=str(tmp_path),
        )
        assert r_poisoned.returncode == expect_rc, (
            "a poisoned chokepoint did not change this guard's verdict, so the guard "
            "is not asking whether its parse could read the whole command.\n"
            f"{r_poisoned.stdout}{r_poisoned.stderr}"
        )


class TestNoConsumerCanSkipTheChokepoint:
    """The lock. Wiring guards by hand is the convention that already failed twice.

    ``shell_parse.analyze()`` is BOUNDED — it stops at a depth limit, and refuses a
    command over a length cap — and it reports neither. A consumer holding bare
    ``analyze`` therefore cannot tell "found nothing" from "stopped looking", and
    every such consumer got strictly WORSE when the bounds landed: the unbounded
    parser used to reach a buried command by accident, and a bound stops at an exact
    index an attacker picks. MEASURED at the time, both going from refused to allowed
    when nested 9 deep: the discard guard's unrecoverable-delete block, and the
    full-suite guard's untargeted-run block.

    Two review rounds found that class, twice, because the fix each time was "wire
    the consumers I can think of". So the consumer set is DERIVED FROM THE CODE here
    instead. Importing bare ``analyze`` now requires being on the allowlist below
    with a written reason, which makes forgetting impossible rather than unlikely.
    """

    #: Modules permitted to import bare ``analyze``, each with the reason it is safe.
    #: Adding a name is a deliberate act that must survive review; the reason is the
    #: point, not the entry.
    _BARE_ANALYZE_ALLOWED = {
        "git_push_guard.py": (
            "One use, for cwd matching, which filters to `depth == 0` — a depth bound "
            "only removes segments BELOW that, so the filtered result is identical "
            "either way. The length cap can only fire on a command longer than the "
            "cap, and such a command is caught upstream by this guard's own "
            "blind-spot net before this code runs (verified end to end)."
        ),
    }

    def _shell_parse_imports(self, path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "shell_parse":
                names.update(alias.name for alias in node.names)
        return names

    def test_bare_analyze_requires_being_on_the_allowlist(self):
        scanned = 0
        offenders = []
        for path in sorted((_WORKTREE / "scripts").rglob("*.py")):
            if path.name == "shell_parse.py":
                continue  # the parser's own internals are bounded by construction
            imports = self._shell_parse_imports(path)
            if not imports:
                continue
            scanned += 1
            if "analyze" in imports and path.name not in self._BARE_ANALYZE_ALLOWED:
                offenders.append(str(path.relative_to(_WORKTREE)))

        # A floor, because a walk that silently matches NOTHING passes every assertion
        # below and reports a clean lock over an empty set.
        assert scanned >= 6, (
            f"only {scanned} shell_parse consumers found — the walk is broken, so a "
            "green result here means nothing"
        )
        assert not offenders, (
            "these modules import bare `analyze`, which cannot report that the parse "
            "was cut short by a bound — so an empty result reads as 'nothing found' "
            "when it may mean 'stopped looking'. Use `analyze_checked` and decide what "
            "a blind parse means for this guard, or add the module to "
            f"_BARE_ANALYZE_ALLOWED with the reason it is safe: {offenders}"
        )

    def test_the_allowlist_itself_is_still_accurate(self):
        """An allowlist entry for a module that no longer imports bare `analyze` is
        rot — it silently pre-approves a future re-import nobody reviewed."""
        actually_importing = {
            path.name
            for path in (_WORKTREE / "scripts").rglob("*.py")
            if path.name != "shell_parse.py" and "analyze" in self._shell_parse_imports(path)
        }
        stale = set(self._BARE_ANALYZE_ALLOWED) - actually_importing
        assert not stale, (
            f"allowlist entries for modules that no longer import bare `analyze`: "
            f"{stale}. Remove them so the exemption cannot be inherited silently."
        )
