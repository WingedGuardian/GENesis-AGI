"""Shared tokenizability probe, and the commit gate's fail-closed wrapper.

`shell_parse.analyze()` degrades to a naive split on a shlex error SILENTLY, so
it cannot report that its own answer is untrustworthy: "no gated segment found"
and "no gated command present" are the same return value. `untokenizable()` is
the signal that separates them, so a security-critical caller can pick its own
fail direction while the parser keeps degrading gracefully.

This file covers the probe itself, the removal of the third hand-rolled copy of
it, and the commit gate's `run_guard` wrapper. It does NOT cover any gate that
consumes the probe to make a verdict — that lives with the change that adds one.

Trigger literals are assembled from fragments so this file's own text does not
carry them (matching the convention in test_shell_parse.py).
"""

from __future__ import annotations

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

    def test_probe_reads_the_raw_command(self):
        """No normalisation, and specifically no line-continuation folding.

        An earlier revision folded `\\<newline>` to a SPACE first. That is wrong
        about bash, which REMOVES a continuation and joins the halves into one
        word, and it also contradicted the probe's own contract. Measured over
        12,099 real commands the two classify identically, so the fold bought
        nothing; this pins the raw reading so it cannot creep back.
        """
        cmd = "git pu\\\nsh origin main"
        assert sp.untokenizable(cmd) is False
        # The raw text still contains the continuation — nothing rewrote it.
        assert "\\\n" in cmd


class TestProtectedPathsUsesTheSharedProbe:
    """The third hand-rolled copy of the probe is gone; behaviour is preserved."""

    def test_ansic_obfuscated_rm_of_protected_dir_still_blocks(self, tmp_path):
        """The reason the inline probe existed — it must still work.

        Without this control the refactor could delete the probe entirely and
        every other assertion here would stay green.
        """
        protected = Path.home() / "backups"
        cmd = "rm -rf $'a\\'b)c' " + str(protected)
        r = _run(_PROTECTED_GUARD, cmd, cwd=str(tmp_path))
        assert r.returncode == 2, r.stdout + r.stderr

    def test_ordinary_rm_is_untouched(self, tmp_path):
        r = _run(_PROTECTED_GUARD, f"rm -rf {tmp_path}/scratch", cwd=str(tmp_path))
        assert r.returncode != 2, r.stdout + r.stderr


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
        (hooks / "shell_parse.py").write_text(
            "def analyze(command):\n"
            "    raise RuntimeError('induced failure inside the real guard')\n"
            "def commit_skips_hooks(*a, **k):\n    return False\n"
            "def git_subcommand(*a, **k):\n    return None\n"
            "def has_trailing_override(*a, **k):\n    return False\n"
            "def split_segments(*a, **k):\n    return []\n"
            "def untokenizable(*a, **k):\n    return False\n"
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
        test above.
        """
        r = _run(_COMMIT_GUARD, "echo not a git command", cwd=str(tmp_path))
        assert r.returncode != 2, r.stdout + r.stderr
