"""The discarded-command note: behaviour, fail-open contract, and wiring locks.

The wiring locks are the load-bearing half. A note helper that is imported but
never called, or a guard that was never wired at all, is indistinguishable from a
working one until the day it matters — so the coverage set is derived from
`.claude/settings.json` (the CONFIGURATION) rather than from the modules that
happen to import the helper, which is structurally blind to a blocker nobody
wired.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_HOOKS = _REPO / "scripts" / "hooks"
_MODULE = _HOOKS / "discarded_write.py"


def _load():
    spec = importlib.util.spec_from_file_location("discarded_write_uut", _MODULE)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


dw = _load()

# Assembled from fragments so this file cannot trip the repo's own guards.
_PUSH = "git " + "push --" + "force"


# ── behaviour ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cmd",
    [
        "cat > config.py <<'EOF'\nPORT=8080\nEOF\ngit commit -m x",  # THE acceptance case
        f"echo hi > f && {_PUSH}",
        "cd /x && git commit -m y",
        "echo one; echo two",
        "ls | wc -l",  # a pipeline IS multi-segment — see the note's wording
    ],
)
def test_a_multi_step_command_gets_the_note(cmd):
    assert dw.note(cmd) is not None


@pytest.mark.parametrize("cmd", [_PUSH, "git status", "ls -la", "", "   "])
def test_a_single_step_command_stays_silent(cmd):
    assert dw.note(cmd) is None


def test_the_note_says_the_whole_command_went_and_names_no_file():
    """It states the one fact true of every block and classifies nothing.

    Naming files means mapping argv to effect (14 findings); listing the parsed
    steps renders the acceptance case as `cat / PORT=8080 / EOF`, because
    parse_segments drops a plain redirect target and splits on `|`. Both were
    tried and refuted, so the note names nothing.
    """
    text = dw.note("cat > config.py <<'EOF'\nx\nEOF\ngit commit -m x")
    assert "config.py" not in text
    assert "ENTIRE command was discarded" in text


def test_the_note_is_conditional_so_a_pure_pipeline_is_not_overclaimed():
    """MEASURED >=9.5% of firings are pure pipelines, where no separable earlier
    step exists. The wording must not assert one did not run."""
    text = dw.note("ls | wc -l")
    assert "If any earlier step" in text
    assert "did NOT run" not in text  # the flat, overclaiming phrasing


def test_the_prompt_note_warns_about_DECLINING_not_about_a_discard():
    text = dw.prompt_note(f"cd /x && {_PUSH}")
    assert text is not None and "Declining" in text
    assert "discarded" not in text  # nothing is discarded yet at a prompt


def test_the_prompt_note_is_silent_on_a_single_step_command():
    assert dw.prompt_note(_PUSH) is None


def test_remember_then_warn_uses_the_remembered_command(capsys):
    mod = _load()
    mod.remember(f"cd /x && {_PUSH}")
    mod.warn()
    assert "ENTIRE command was discarded" in capsys.readouterr().err


def test_remember_ignores_junk_so_a_stale_command_is_never_printed(capsys):
    mod = _load()
    for junk in (None, "", "   ", 17):
        mod.remember(junk)
    mod.warn()
    assert capsys.readouterr().err == ""


# ── the fail-open contract ───────────────────────────────────────────────────


def test_every_entry_point_is_fail_open_on_a_broken_parser(monkeypatch):
    """A cosmetic helper must never raise into a guard that is mid-refusal."""
    mod = _load()

    def boom(_):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(mod, "split_segments", boom)
    assert mod.carried_more_than_the_refused_step("a && b") is False
    assert mod.note("a && b") is None
    assert mod.prompt_note("a && b") is None
    mod.warn("a && b")  # must not raise


def test_an_absent_parser_degrades_to_silence(monkeypatch):
    """A partially-synced hooks/ dir (this file present, shell_parse absent) is
    the sentinel case the guarded import exists for."""
    mod = _load()
    monkeypatch.setattr(mod, "split_segments", None)
    assert mod.note("a && b") is None
    assert mod.prompt_note("a && b") is None


def test_warn_never_raises_on_junk_input():
    mod = _load()
    for junk in (None, "", 17, object()):
        mod.warn(junk)


def test_the_module_never_imports_analyze():
    """`analyze` recurses and is bounded for that reason; a bare import of it
    requires an entry in test_untokenizable_probe's allowlist. `split_segments`
    is a single-level split, so this module stays outside that surface — and
    this test is what keeps it there."""
    tree = ast.parse(_MODULE.read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "shell_parse"
        for alias in node.names
    }
    assert imported == {"split_segments"}, imported


# ── wiring locks ─────────────────────────────────────────────────────────────

#: SHELL blockers that ship in this repo and do NOT carry the note, keyed on the
#: thing that is unwired so that WIRING one means DELETING its entry. An earlier
#: shape asserted `count(configured) == len(this dict)`, which held only while the
#: gap remained — it actively penalised closing it.
_UNWIRED_SHELL_BLOCKERS = {
    "<inline bash -c in .claude/settings.json>": (
        "four `exit 2` arms, each already echoing to stderr immediately before it "
        "exits, so a note is a one-line addition — but it needs the discarded_write "
        "CLI, which ships with the shell PR below."
    ),
    "bash_safety_hook.sh": (
        "471 lines, 18 `exit 2` sites, no shell functions, no `trap` and no "
        "`set -e`. Wiring it means introducing an EXIT trap into a safety script "
        "that has none — the riskiest edit in this change, deliberately given its "
        "own focused PR. It is wired per-install at the USER level, so the "
        "repo-config walk below cannot see it and it must be named here by hand."
    ),
}


def _settings_bash_hooks() -> tuple[list[Path], list[str], list[str]]:
    """(python blockers, shell-script blockers, inline shell blockers).

    Reads ONLY the repo's own `.claude/settings.json` — deliberately. A test that
    consulted `~/.claude/settings.json` would pass or fail depending on the
    machine, and everything here must hold on a fresh clone. The cost is that a
    repo-shipped script wired only at user level (`bash_safety_hook.sh`) is
    invisible from here, which is exactly why `_UNWIRED_SHELL_BLOCKERS` names it
    by hand instead of deriving it.
    """
    settings = json.loads((_REPO / ".claude" / "settings.json").read_text())
    scripts: list[Path] = []
    shell: list[str] = []
    inline: list[str] = []
    for entry in settings["hooks"]["PreToolUse"]:
        if entry.get("matcher") != "Bash":
            continue
        for hook in entry["hooks"]:
            cmd = hook["command"]
            if m := re.search(r"genesis-hook\s+(\S+\.py)", cmd):
                path = _REPO / "scripts" / m.group(1)
                if path.exists() and re.search(r"return 2|sys\.exit\(2\)", path.read_text()):
                    scripts.append(path)
            elif m := re.search(r"\bbash\s+(\S+)\s*$", cmd):
                # A `bash <script>` hook. The previous extractor matched neither
                # branch for this shape and dropped it silently; resolve it in the
                # repo and keep it only if it can actually block.
                name = Path(m.group(1)).name
                for cand in (_REPO / "scripts" / name, _REPO / ".claude" / "hooks" / name):
                    if cand.exists() and "exit 2" in cand.read_text():
                        shell.append(name)
                        break
            elif "exit 2" in cmd:
                inline.append("<inline bash -c in .claude/settings.json>")
    return scripts, shell, inline


def _imports_helper(tree: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Import) and any(a.name == "discarded_write" for a in n.names)
        for n in ast.walk(tree)
    )


def _calls(tree: ast.AST, attr: str) -> bool:
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == attr
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "discarded_write"
        for n in ast.walk(tree)
    )


def test_every_configured_python_bash_blocker_emits_the_note():
    """The coverage set is the CONFIGURATION, not the importers.

    Walking modules that already import the helper is structurally blind to a
    blocker that was never wired at all — which is exactly how worktree_cwd_guard
    was nearly shipped noteless while eight siblings were done.

    Both halves are required, and an `or` here would be VACUOUS — MEASURED: a
    mutation deleting `warn()` from a guard survived an `or` version of this
    assertion, because `remember()` was still present. A guard that remembers and
    never warns is exactly the silently-inert shape this lock exists to prevent;
    one that warns without remembering prints nothing, since stdin is consumed
    once and the command can only be handed over where it is first read.
    """
    scripts, _, _ = _settings_bash_hooks()
    assert len(scripts) >= 9, (
        f"blocker walk went blind: found only {len(scripts)} configured python "
        "Bash blockers, so a green result here means nothing"
    )
    missing = []
    for path in scripts:
        tree = ast.parse(path.read_text())
        if not _imports_helper(tree):
            missing.append(f"{path.name}: never imports the note helper")
            continue
        if not _calls(tree, "remember"):
            missing.append(f"{path.name}: never hands the command over (remember)")
        if not _calls(tree, "warn"):
            missing.append(f"{path.name}: imports the helper but never emits (warn)")
    assert not missing, missing


def test_every_unwired_shell_blocker_is_named_and_still_unwired():
    """A blocker with no note is acceptable only as a STATED decision.

    Two directions, because either alone rots. Every shell blocker the repo config
    exposes must appear in the record — otherwise a new one is silently
    uncovered. And every NAMED entry must still lack the wiring — otherwise the
    record outlives the gap and pre-approves a regression nobody reviewed.
    """
    _, shell, inline = _settings_bash_hooks()
    exposed = set(shell) | set(inline)
    unnamed = exposed - set(_UNWIRED_SHELL_BLOCKERS)
    assert not unnamed, (
        f"shell blocker(s) the repo config exposes but nothing records: {unnamed}. "
        "Wire them, or name them in _UNWIRED_SHELL_BLOCKERS with the reason."
    )
    stale = []
    for name in _UNWIRED_SHELL_BLOCKERS:
        if name.startswith("<"):
            continue  # the inline blob lives in settings.json, checked above
        path = _REPO / "scripts" / name
        if path.exists() and "discarded_write" in path.read_text():
            stale.append(name)
    assert not stale, (
        f"{stale} now carries the note but is still recorded as unwired — delete "
        "its entry so the record cannot pre-approve a silent regression."
    )


def test_every_guard_with_an_ask_path_wires_the_prompt_note():
    """A prompt has not discarded anything yet, so it needs the other tense."""
    ask_guards = [
        _HOOKS / "git_push_guard.py",
        _REPO / "scripts" / "review_enforcement_commit.py",
    ]
    missing = []
    for path in ask_guards:
        src = path.read_text()
        assert '"permissionDecision": "ask"' in src, f"{path.name}: no ask path left"
        if not _calls(ast.parse(src), "prompt_note"):
            missing.append(path.name)
    assert not missing, missing


def test_git_push_guards_wrapper_is_actually_reached():
    """`_main_with_note` covers 28 scattered return-2 sites. Defined but not
    referenced, it is decoration — and the file still reads as wired."""
    src = (_HOOKS / "git_push_guard.py").read_text()
    assert "def _main_with_note" in src
    assert "run_guard(_main_with_note" in src
    assert "run_guard(main," not in src


def test_each_guarded_import_has_a_stand_in(monkeypatch):
    """An unguarded import that fails aborts module load -> exit 1 -> CC reads a
    non-2 exit as NON-blocking -> the guarded command RUNS. Every consumer must
    therefore define the name in its except branch."""
    scripts, _, _ = _settings_bash_hooks()
    bad = []
    for path in scripts:
        src = path.read_text()
        if "import discarded_write" not in src:
            continue
        # COUNT, not just shape. The regex below is DOTALL and unanchored, so it
        # only requires the four tokens in order ANYWHERE in the file — a second,
        # BARE import added later would satisfy it while being the very fail-open
        # this test is named for.
        if src.count("import discarded_write") != 1:
            bad.append(f"{path.name}: {src.count('import discarded_write')} imports, expected 1")
            continue
        if not re.search(
            r"try:.*?import discarded_write.*?except Exception:.*?"
            r"discarded_write = None",
            src,
            re.S,
        ):
            bad.append(path.name)
    assert not bad, bad


def test_a_guard_still_blocks_when_the_helper_is_absent(tmp_path):
    """The whole contract in one live check: delete the helper, and the guard
    must still refuse with its own message and exit 2."""
    guard = _HOOKS / "protected_paths_guard.py"
    sandbox = tmp_path / "hooks"
    sandbox.mkdir()
    for name in ("hook_input.py", "shell_parse.py", guard.name):
        (sandbox / name).write_text((_HOOKS / name).read_text())
    # discarded_write.py deliberately NOT copied.
    # Derived from the guard's own _PROTECTED_RELATIVE joined to THIS install's
    # home — never a hardcoded /home/<user> path, which would be install-specific
    # and would fail on every other clone.
    target = Path.home() / "genesis" / "data"
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": f"rm -rf {target}"}})
    proc = subprocess.run(
        [sys.executable, str(sandbox / guard.name)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "BLOCKED" in proc.stderr
    assert "ENTIRE command was discarded" not in proc.stderr


# ── live behaviour ───────────────────────────────────────────────────────────
# The locks above are AST-shaped, and AST shape is satisfiable by INERT code.
# MEASURED: deleting one of two `warn()` sites, passing `remember(None)`, and
# dropping `prompt_note()`'s return value ALL left every AST lock green. These
# tests drive the real guards and assert the real output, which is the only
# thing those three mutations cannot survive.


def _drive(script: str, command: str, background: bool = False):
    tool_input: dict = {"command": command}
    if background:
        tool_input["run_in_background"] = True
    payload = json.dumps({"tool_name": "Bash", "tool_input": tool_input, "cwd": str(_REPO)})
    return subprocess.run(
        [sys.executable, str(_REPO / script)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(_REPO),
        timeout=120,
    )


# Split so this file's own text cannot trip the repo's destructive-command guard.
_RM = "rm -" + "rf "
_PROTECTED = str(Path.home() / "genesis" / "data")

#: (guard script, a command it refuses, whether the payload must be backgrounded)
_LIVE_GUARDS = [
    ("scripts/hooks/protected_paths_guard.py", _RM + _PROTECTED, False),
    ("scripts/hooks/destructive_command_guard.py", _RM + "/tmp/a", False),
    ("scripts/hooks/full_suite_guard.py", "pytest tests/", False),
    ("scripts/hooks/git_discard_guard.py", "git clean -fd", False),
    ("scripts/hooks/background_pipe_guard.py", "ls | wc -l", True),
]


@pytest.mark.parametrize(("script", "refusal", "background"), _LIVE_GUARDS)
def test_live_a_multi_step_refusal_carries_the_note(script, refusal, background):
    proc = _drive(script, f"echo hi > /tmp/dw_probe && {refusal}", background)
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "ENTIRE command was discarded" in proc.stderr, proc.stderr


@pytest.mark.parametrize(("script", "refusal", "background"), _LIVE_GUARDS)
def test_live_the_refusal_still_blocks_and_says_why(script, refusal, background):
    """The note is ADDITIVE: the guard's own verdict and message are unchanged."""
    proc = _drive(script, f"echo hi > /tmp/dw_probe && {refusal}", background)
    assert proc.returncode == 2
    assert "blocked" in proc.stderr.lower()


@pytest.mark.parametrize(("script", "refusal", "background"), _LIVE_GUARDS)
def test_live_an_allowed_command_is_untouched(script, refusal, background):
    proc = _drive(script, "cd /tmp && echo hello", background)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "ENTIRE command was discarded" not in proc.stderr


#: A build of the helper whose every entry point raises. Not the absent-module
#: case (that is the guarded import, covered above) — this is the FUTURE-EDIT
#: case: someone changes the helper and it starts throwing.
_POISONED_HELPER = """
def remember(command=None):
    raise RuntimeError("poisoned remember")
def warn(command=None):
    raise RuntimeError("poisoned warn")
def note(command=None):
    raise RuntimeError("poisoned note")
def prompt_note(command=None):
    raise RuntimeError("poisoned prompt_note")
"""


@pytest.mark.parametrize(("script", "refusal", "background"), _LIVE_GUARDS)
def test_live_a_poisoned_helper_can_never_fail_a_guard_open(tmp_path, script, refusal, background):
    """A cosmetic helper must not be able to turn a refusal into a non-block.

    Claude Code reads exit 2 as BLOCK and every other code as a NON-blocking
    error, so an exception escaping the note helper exits 1 and the refused
    command RUNS. Three of these guards are not wrapped by ``run_guard``, so
    nothing upstream converts that back to a block.

    MEASURED before the fix, with this exact poison: `git clean -fd` came back
    rc=1 from `git_discard_guard` — whose own docstring promises a parser bug
    "can never become a silent ALLOW" — and `full_suite_guard` and
    `background_pipe_guard` did the same. This test is what keeps that closed.
    """
    sandbox = tmp_path / "hooks"
    sandbox.mkdir()
    for f in _HOOKS.glob("*.py"):
        shutil.copy2(f, sandbox / f.name)
    (sandbox / "discarded_write.py").write_text(_POISONED_HELPER)

    tool_input: dict = {"command": f"echo hi > /tmp/dw_probe && {refusal}"}
    if background:
        tool_input["run_in_background"] = True
    proc = subprocess.run(
        [sys.executable, str(sandbox / Path(script).name)],
        input=json.dumps({"tool_name": "Bash", "tool_input": tool_input, "cwd": str(_REPO)}),
        capture_output=True,
        text=True,
        cwd=str(_REPO),
        timeout=120,
    )
    assert proc.returncode == 2, (
        f"{Path(script).name} exited {proc.returncode} with a poisoned note helper — "
        "CC treats any non-2 exit as non-blocking, so the refused command would RUN. "
        f"stderr: {proc.stderr[-600:]}"
    )


def test_live_the_ask_path_carries_the_prompt_note():
    """Exercises the `ask` renderer directly, because the blind-spot net that
    normally reaches it is not portably triggerable.

    A bare `prompt_note()` whose return value is DROPPED satisfies every AST lock
    in this file and fails here — which is the whole reason this test exists.
    """
    spec = importlib.util.spec_from_file_location("gpg_uut", _HOOKS / "git_push_guard.py")
    gpg = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(gpg)

    gpg.discarded_write.remember("cd /x && git " + "push --" + "force")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = gpg._ask("needs your approval")
    assert rc == 0
    reason = json.loads(buf.getvalue())["hookSpecificOutput"]["permissionDecisionReason"]
    assert "needs your approval" in reason
    assert "Declining also skips" in reason
