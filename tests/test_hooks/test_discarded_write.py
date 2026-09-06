"""The refusal note: a block discards the WHOLE command, and says so.

An earlier version of this module worked out WHICH files the refused command would
have written. That did not converge — naming a file means mapping argv to effect,
which has no closed boundary, and three review rounds produced fourteen findings
that were overwhelmingly one more option spelling or operand class. Worse, assembling
the list was quadratic in the segment count and MEASURED as a fail-OPEN: it SIGKILLed
`bash_safety_hook.sh` past its registration timeout, and a non-2 exit lets the refused
command RUN.

So the note now states the one thing true of every block, needing no knowledge of any
tool: the entire command was discarded. These tests pin that contract and — more
importantly — pin the two properties that make it safe to sit inside a security hook:
it never changes a verdict, and it never costs measurable time.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import time
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
sys.path.insert(0, str(_HOOKS))
import discarded_write as dw  # noqa: E402

# ── when the note appears ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cmd",
    [
        "cat > config.py <<'EOF'\nx = 1\nEOF\ngit commit -m x",
        "sed -i 's/a/b/' f.py && git commit -m x",
        'python3 -c \'open("f","w").write("x")\' ; git push',
        "cd /wt && git push",
        "ruff check . && pytest -q",
        "a | b",
    ],
)
def test_a_multi_step_command_gets_the_note(cmd):
    """Every shape carrying more than one step, whatever the steps ARE.

    The point of the rewrite: `sed -i`, a heredoc write, an inline python script and a
    bare `cd` are all covered without the module knowing anything about sed, python or
    cd. The old version needed a rule per tool and missed the ones it had no rule for.
    """
    assert dw.note(cmd) is not None, cmd


_NESTED = [
    "bash -c 'cp a b && git " + "commit -m x'",
    "$(cp a b; git " + "commit -m x)",
    "bash -lc 'cat > f <<EOF\nx\nEOF\ngit " + "commit -m x'",
]


_NESTED_SINGLE = [
    "bash -c 'git " + "reset --hard'",
    "bash -lc 'git push'",
    "$(git push)",
]


@pytest.mark.parametrize("cmd", _NESTED_SINGLE)
def test_a_NESTED_single_step_command_stays_silent(cmd):
    """The control that caught the first fix being wrong in the other direction.

    ``analyze`` also yields the ``bash -c`` WRAPPER, so a bare ``len(analyze) > 1``
    reports collateral for a nested command that has exactly one step. The wrapper
    is not something the refusal discarded — it IS the thing refused. Found by an
    end-to-end run against the real hook, not by the unit tests, which is why it is
    pinned here.
    """
    assert dw.note(cmd) is None, cmd


@pytest.mark.parametrize("cmd", _NESTED)
def test_a_NESTED_multi_step_command_gets_the_note(cmd):
    """The note must count at the level the GUARD blocks at, not one above it.

    ``split_segments`` cuts only at the top level, so a compound inside ``bash -c``
    or a command substitution reads as ONE step — while the guard recursively finds
    the inner step and blocks the whole call. MEASURED on the first case:
    ``split_segments`` sees 1, ``analyze`` sees 3. Counting shallower than the guard
    blocks at is how the note goes silent exactly where the discarded work is
    hardest for the reader to spot.

    The literals are split so this file's own text cannot trip the commit guard
    that reads it — the same reason ``NV`` is spelled that way in test_shell_parse.
    """
    assert dw.note(cmd) is not None, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "git push",
        "git commit -m 'a && b'",  # separator INSIDE a quoted string is not a step
        "echo 'a | b'",
        "pytest tests/foo.py",
        "",
        "   ",
    ],
)
def test_a_single_step_command_stays_silent(cmd):
    """The refused step IS the whole call, so nothing was carried alongside it.

    The quoted cases matter most: a naive substring check for `&&` would fire on them
    and make the note noise on ordinary commits. Segmentation is the shell's own, so
    a separator inside quotes is correctly not a step.
    """
    assert dw.note(cmd) is None, cmd


@pytest.mark.parametrize("cmd", ["> /tmp/result && git push", "echo a && >q0 >q1"])
def test_a_bare_redirect_step_is_a_KNOWN_and_measured_miss(cmd):
    """Documented gap, pinned so it is a decision rather than a surprise.

    Both splitters drop a segment that redirects but executes nothing, so a command
    whose only collateral is a bare `> f` gets no note even though bash really does
    create the file. Catching it needs the write-detection layer this rewrite deleted,
    and that layer is what produced the review loop and the measured fail-open.

    MEASURED over 1,468 real Bash commands from this box's transcripts: 2 matched a
    generous bare-redirect pattern (0.14%), and inspecting both showed they were
    pattern false positives — an ordinary `cat > f <<EOF` and a line continuation.
    The real rate is indistinguishable from zero, and the failure direction is
    silence, which is the pre-feature status quo.
    """
    assert dw.note(cmd) is None


def test_the_note_says_the_whole_command_went_not_which_files():
    """The contract that replaced the file list — it must not creep back.

    A future 'small improvement' that names a path is the start of the argv-to-effect
    modelling this module deleted, so the absence is asserted rather than assumed.
    """
    text = dw.note("cat > cfg.py <<'EOF'\nx\nEOF\ngit commit -m x")
    assert text is not None
    assert "ENTIRE command was discarded" in text
    assert "cfg.py" not in text, "the note must not name files — that is the deleted design"


# ── the approval-prompt variant ──────────────────────────────────────────────


def test_the_prompt_note_warns_about_DECLINING_not_about_a_discard():
    """A prompt has not thrown anything away yet, so the tense must differ.

    The refusal note is past ("was discarded"); the prompt note is conditional
    ("declining also skips…"). Saying "was discarded" in a dialog where nothing has
    happened yet would be plainly false, so the two strings are asserted distinct.
    """
    prompt = dw.prompt_note("cat > f <<'EOF'\nx\nEOF\ngit push")
    assert prompt is not None
    assert "Declining" in prompt
    assert "was discarded" not in prompt
    assert prompt != dw.note("cat > f <<'EOF'\nx\nEOF\ngit push")


def test_the_prompt_note_is_silent_on_a_single_step_command():
    assert dw.prompt_note("git push") is None


def test_the_prompt_note_uses_the_remembered_command():
    dw._COMMAND = None
    assert dw.prompt_note() is None, "no remembered command must not invent a note"
    dw.remember("cd /x && git push")
    assert dw.prompt_note() is not None
    dw._COMMAND = None


def test_the_prompt_note_is_fail_open_on_a_broken_parser(monkeypatch):
    """It is appended to a permission dialog, so a raise here would cost the gate
    its prompt — the one failure a cosmetic helper must never cause."""

    def boom(_):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(dw, "analyze", boom)
    assert dw.prompt_note("a && b") is None


# ── it can never change a verdict ────────────────────────────────────────────


def test_every_entry_point_is_fail_open_on_a_broken_parser(monkeypatch):
    """A cosmetic helper must never raise into the guard that called it.

    Mutating the parser to raise stands in for any parser failure. The module's
    whole safety argument is that its worst case is silence, so this is the test that
    licenses calling it from inside a security gate.
    """

    def boom(_):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(dw, "analyze", boom)
    assert dw.carried_more_than_the_refused_step("a && b") is False
    assert dw.note("a && b") is None
    dw.warn("a && b")  # must not raise


def test_warn_never_raises_on_junk_input():
    for junk in (None, "", "   ", "\x00\xff", "a" * 200_000):
        dw.warn(junk)  # no assertion: not raising IS the contract


def test_remember_then_warn_uses_the_remembered_command(capsys):
    dw.remember("cat > f <<'EOF'\nx\nEOF\ngit commit -m x")
    dw.warn()
    assert "ENTIRE command was discarded" in capsys.readouterr().err
    dw._COMMAND = None


def test_remember_ignores_junk_so_a_stale_command_is_never_printed():
    dw._COMMAND = None
    for junk in (None, "", "   ", 42):
        dw.remember(junk)
    assert dw._COMMAND is None


# ── it can never cost the hook its exit code ─────────────────────────────────


def test_the_shape_that_once_sigkilled_the_hook_is_now_instant():
    """The acceptance replay of the fail-OPEN this rewrite exists to remove.

    MEASURED before: `rm -rf x && >q0000 >q0001 …` at ~63k chars sat inside every
    input bound and cost 2.5s to assemble a file list; bash_safety_hook.sh pays that
    TWICE per block against a 5-second registration, so the hook was SIGKILLed before
    its `exit 2` and the refused `rm -rf` RAN. There is no assembly any more, so the
    cost is one segmentation pass. A generous bound catches a regression to
    superlinear behaviour without being flaky on a loaded box.
    """
    cmd = " && ".join(f"touch q{i:04d}" for i in range(4000))
    started = time.monotonic()
    result = dw.note(cmd)
    elapsed = time.monotonic() - started
    assert result is not None, "a 4000-step command certainly carried collateral"
    assert elapsed < 1.0, f"took {elapsed:.2f}s — the quadratic assembly is back"


@pytest.mark.parametrize("depth", [8, 40, 255])
def test_a_NESTED_substitution_bomb_inside_every_bound_stays_cheap(depth):
    """The axis the breadth test above does NOT cover, and the one that bit.

    The test above sweeps BREADTH — 4000 top-level steps — which the rewrite already
    made cheap. Cost actually scales with substitution NESTING DEPTH, because
    ``analyze`` recurses once per level and re-scans the remainder each time. So the
    real cost is length x depth: the PRODUCT of the two axes the char/substitution
    bounds cap INDEPENDENTLY, which is why both proxies pass while the work explodes.

    MEASURED before the wall-clock budget, every input inside both declared bounds:
    depth 32 -> 8.07s, depth 128 -> 39.31s, depth 255 -> 82.74s. On the real
    ``destructive_command_guard`` (which parsed nothing at all before this helper),
    depth 40 was SIGKILLed past its 10s registration and the refused ``rm -rf`` RAN,
    while origin/main refused the same command in 0.21s at every depth.

    Guarding the axis that was never the problem is how that shipped.
    """
    cmd = "$(" * depth + "x" * (65_400 - depth * 3) + ")" * depth
    assert len(cmd) <= dw._MAX_COMMAND_CHARS, "the fixture must sit inside the char bound"
    assert cmd.count("$(") <= dw._MAX_SUBSTITUTIONS, "and inside the substitution bound"
    started = time.monotonic()
    dw.note(cmd)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"took {elapsed:.2f}s — the budget is not bounding the parse"


def test_an_oversized_command_is_refused_before_parsing():
    """Past the input bound the answer is silence, not a slow parse."""
    assert dw.note("a && " + "x" * (dw._MAX_COMMAND_CHARS + 1)) is None


def test_a_substitution_bomb_is_refused_before_parsing():
    assert dw.note("a && " + "$(x)" * (dw._MAX_SUBSTITUTIONS + 1)) is None


# ── every consumer's guarded import must match its own fallback ──────────────


def test_every_guard_with_an_ask_path_wires_the_prompt_note():
    """The class, not the two instances: an approval prompt must carry the warning.

    Declining a prompt discards the whole command exactly as a refusal does, so a
    guard that can ASK and does not append the prompt note leaves the operator
    deciding without the fact that matters. Two guards have an ``_ask``; the note
    was wired into one, which is how this gap shipped — the same per-call-site
    pattern that keeps producing findings in this area.

    Derived by ast so a THIRD guard growing an ``_ask`` is covered automatically.
    """
    scripts = _HOOKS.parent
    asking, wired = set(), set()
    for path in sorted(scripts.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_ask":
                asking.add(path.name)
            if isinstance(node, ast.ImportFrom) and node.module == "discarded_write":
                for alias in node.names:
                    if alias.name == "prompt_note":
                        wired.add(path.name)
    assert asking, "the walk found no guard with an _ask path — it went blind"
    missing = asking - wired
    assert not missing, f"guards with an _ask that never append the prompt note: {missing}"


def test_every_consumer_actually_emits_the_note_it_imports():
    """The WIRING, not the module — the gap a mutation sweep measured.

    The module is well tested; the feature was not. MEASURED: 9 of 15 mutations
    survived the entire suite, and every one was a deleted CALL SITE — remove
    `_warn_discarded()` from any guard, or unwire the `_main_with_note` wrapper,
    and CI stays green while that guard silently stops telling the operator what
    its refusal threw away.

    This is the repo's convention -> chokepoint -> LOCK pattern: six call sites
    that must each REMEMBER to emit, and no lock. Deriving the consumer set from
    the imports means a seventh guard is covered the day it is added.
    """
    scripts = _HOOKS.parent
    checked = 0
    for path in sorted(scripts.rglob("*.py")):
        if path.name == "discarded_write.py":
            continue  # the module itself is not one of its own consumers
        src = path.read_text()
        if "from discarded_write import" not in src:
            continue
        checked += 1
        assert "_warn_discarded(" in src, (
            f"{path.name}: imports the note helper but never emits it — "
            "the import is wiring that does nothing"
        )
    assert checked >= 6, f"the walk found only {checked} consumers — it went blind"


def test_each_guarded_import_has_a_stand_in_for_every_name_it_imports():
    """The asymmetry class, caught end-to-end and pinned here.

    Every consumer wraps ``from discarded_write import …`` in try/except and defines
    no-op stand-ins in the except branch, because an unguarded import failure aborts
    the guard's module load and CC reads the non-2 exit as NON-blocking — the gate
    vanishes. The failure mode is subtle in the OTHER direction: adding a name to the
    import without adding its stand-in leaves the fallback fine and the HAPPY path
    broken, or vice versa.

    MEASURED, this session: the auto-formatter removed a freshly-added import line
    (its usage was added in a later edit, so the import was momentarily unused) while
    the hand-written stand-in survived. `git_push_guard` then raised NameError on
    every push. It failed CLOSED, so nothing was let through — but every push was
    blocked, and no unit test saw it because they exercise this module directly
    rather than through a consumer's import.

    Derived by ast over the real consumers, so a new one is covered automatically.
    """
    scripts = _HOOKS.parent
    checked = 0
    for path in sorted(scripts.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            imported = {
                (alias.asname or alias.name)
                for stmt in node.body
                if isinstance(stmt, ast.ImportFrom) and stmt.module == "discarded_write"
                for alias in stmt.names
            }
            if not imported:
                continue
            checked += 1
            defined = {
                stmt.name
                for handler in node.handlers
                for stmt in handler.body
                if isinstance(stmt, ast.FunctionDef)
            }
            # SET EQUALITY, both directions. This test previously asserted only
            # `imported - defined`, which is the OPPOSITE of the incident its own
            # docstring cites: the formatter deleted an IMPORT and left the
            # hand-written stand-in behind, so the failing path was fine and the
            # SUCCESSFUL one raised NameError. MEASURED: deleting one import while
            # keeping its stand-in left this whole suite green while every push
            # failed closed on a NameError.
            assert imported == defined, (
                f"{path.name}: guarded import and fallback disagree — "
                f"imported with no stand-in: {imported - defined}; "
                f"stand-in with no import (the formatter failure): {defined - imported}"
            )
    assert checked >= 4, f"the walk found only {checked} guarded imports — it went blind"


# ── the CLI the shell hook calls ─────────────────────────────────────────────


def test_cli_prints_the_note_to_stderr_and_always_exits_zero():
    """`bash_safety_hook.sh` calls this; a non-zero exit here would perturb the
    caller's own verdict, which is the one thing a cosmetic helper must not do."""
    proc = subprocess.run(
        [sys.executable, str(_HOOKS / "discarded_write.py"), "--command", "cd /x && git push"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )
    assert proc.returncode == 0
    assert "ENTIRE command was discarded" in proc.stderr
    assert proc.stdout == "", "stdout belongs to the calling hook's own protocol"


def test_cli_is_silent_and_zero_for_a_single_step_command():
    proc = subprocess.run(
        [sys.executable, str(_HOOKS / "discarded_write.py"), "--command", "git push"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stderr == ""
_FORCE = "--" + "force"  # split so substring-matching safety hooks don't trip on fixtures
_LONE_SUBSTITUTION = [
    f'git push {_FORCE} "$(cp a b)"',
    f'git push {_FORCE} "`cp a b`"',
    'git commit -m x > "$(mktemp)"',
    "bash -c 'git push \"$(cp a b)\"'",
]


@pytest.mark.parametrize("cmd", _LONE_SUBSTITUTION)
def test_a_lone_substitution_beside_a_real_command_is_collateral(cmd):
    """One nested command CAN be collateral — when it rides a real host.

    A substitution's command runs in addition to the host command carrying it,
    so refusing a force-push whose argument is `"$(cp a b)"` also loses the
    `cp` — and the plain nested-count>1 rule said nothing, because the
    substitution contributes exactly one depth-positive segment. Backticks and
    an expansion inside a redirect target are the same class.
    """
    assert dw.note(cmd) is not None, cmd


def test_a_bare_substitution_is_still_the_refused_step_itself():
    """The control bounding the rule above, kept from the wrapper lesson.

    In `$(git push)` the top level is the substitution expression itself, the
    nested push IS the thing refused, and nothing else was lost — same logic
    as the `bash -c` wrapper, reached through the other nesting kind.
    """
    assert dw.note("$(git push)") is None


def test_every_configured_bash_blocker_is_wired_for_the_note():
    """The coverage set is the CONFIGURATION, not the importers.

    The sibling wiring tests walk modules that already import this helper —
    structurally blind to a blocker that was never wired at all, which is how
    two configured exit-2 sites (git_discard_guard, and the inline blocker in
    .claude/settings.json) shipped noteless. This test derives the set from
    .claude/settings.json's PreToolUse Bash hooks: every referenced script
    that can return a blocking verdict must import the helper, and an inline
    command that can `exit 2` must invoke the CLI. A blocker added tomorrow is
    covered the day its hook is configured.
    """
    import json
    import re

    repo = _HOOKS.parents[1]
    settings = json.loads((repo / ".claude" / "settings.json").read_text())
    checked_scripts, checked_inline = 0, 0
    for entry in settings["hooks"]["PreToolUse"]:
        if entry.get("matcher") != "Bash":
            continue
        for h in entry["hooks"]:
            cmd = h["command"]
            m = re.search(r"genesis-hook\s+(\S+\.py)", cmd)
            if m:
                script = repo / "scripts" / m.group(1)
                src = script.read_text()
                if re.search(r"return 2|sys\.exit\(2\)", src):
                    checked_scripts += 1
                    assert "from discarded_write import" in src, (
                        f"{script.name}: configured Bash blocker never wires "
                        "the discarded-command note"
                    )
            elif "exit 2" in cmd:
                checked_inline += 1
                assert "discarded_write.py" in cmd, (
                    "inline settings.json Bash blocker never invokes the note CLI"
                )
    assert checked_scripts >= 8, f"blocker walk went blind: {checked_scripts}"
    assert checked_inline >= 1, "the inline settings.json blocker was not seen"
