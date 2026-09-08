"""Tests for scripts/hooks/ask_question_gate.py — the ask-time remedy gate.

The layer that catches a corrupted relay AT THE MOMENT OF CORRUPTION rather than
afterwards. When a gate has declared a remedy set and it is still unacknowledged,
an ``AskUserQuestion`` must carry those remedies among its options.

The guard is driven as a real subprocess with the CC PreToolUse payload on stdin,
per this directory's convention — that is what proves the wiring, not just the
logic. HOME is redirected so the live ``~/.genesis`` state is never touched.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOK = _REPO_ROOT / "scripts" / "hooks" / "ask_question_gate.py"

sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "hooks"))

REMEDIES = [
    {"key": "redesign", "label": "robust-by-construction redesign"},
    {"key": "narrow", "label": "narrow the scope"},
    {"key": "shelve", "label": "shelve the change"},
]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "-c", "init.defaultBranch=main", "init", "-q")
    _git(r, "config", "user.email", "t@e.st")
    _git(r, "config", "user.name", "tester")
    (r / "f.py").write_text("x = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "checkout", "-q", "-b", "feature/x")
    return r


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / ".genesis").mkdir(parents=True)
    return h


def _declare(repo: Path, home: Path) -> None:
    """Write a live demand into the redirected HOME's round store."""
    env = {**os.environ, "HOME": str(home)}
    code = (
        f"import sys; sys.path.insert(0, {str(_REPO_ROOT / 'scripts')!r});"
        "import review_state as rs;"
        f"rs.write_gate_demand(gate='escalation-cap', remedies={REMEDIES!r},"
        f" required_action='relay them', cwd={str(repo)!r})"
    )
    subprocess.run(
        [sys.executable, "-c", code], env=env, cwd=str(repo), check=True, capture_output=True
    )


def _ask(
    repo: Path, home: Path, questions: list[dict], *, env_extra: dict | None = None, **extra
) -> subprocess.CompletedProcess:
    # env_extra is a KEYWORD of its own rather than something popped back out of
    # **extra: the earlier version spread **extra into the payload first and
    # popped afterwards, so an env override would have been sent as a payload
    # field instead.
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": questions},
            "session_id": "test",
            "cwd": str(repo),
            **extra,
        }
    )
    env = {**os.environ, "HOME": str(home)}
    env.pop("GENESIS_GATE_ACK_DISABLED", None)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload,
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _q(*labels: str, question: str = "How should we proceed?") -> dict:
    return {
        "question": question,
        "header": "Cap",
        "multiSelect": False,
        "options": [{"label": lb, "description": ""} for lb in labels],
    }


def test_no_demand_means_no_interference(repo, home):
    """The overwhelming majority of asks. The gate must be invisible for them."""
    res = _ask(repo, home, [_q("Yes", "No")])
    assert res.returncode == 0, res.stderr
    assert res.stderr.strip() == "", res.stderr


def test_an_ask_carrying_every_remedy_passes(repo, home):
    _declare(repo, home)
    res = _ask(repo, home, [_q("Redesign it", "Narrow the scope", "Shelve it")])
    assert res.returncode == 0, res.stderr


def test_the_measured_corruption_is_blocked(repo, home):
    """Replays 2026-08-31 exactly: 'redesign' dropped, 'split the PR' invented,
    'ship as-is' added. This is the acceptance bar — if it does not catch this
    case, the guard does not earn its place."""
    _declare(repo, home)
    res = _ask(repo, home, [_q("Split the PR", "Ship as-is", "Narrow the scope", "Shelve it")])
    assert res.returncode == 2, res.stdout + res.stderr
    assert "redesign" in res.stderr
    assert "robust-by-construction redesign" in res.stderr


def test_an_unrelated_question_may_ride_alongside(repo, home):
    """The standing convention mandates >=2 questions per call. Blocking a call
    that carries the remedies plus a clarifier would make the gate refuse exactly
    the shape it is asking for."""
    _declare(repo, home)
    res = _ask(
        repo,
        home,
        [_q("A", "B", question="Unrelated?"), _q("Redesign it", "Narrow it", "Shelve it")],
    )
    assert res.returncode == 0, res.stderr


def test_a_satisfied_demand_stops_gating(repo, home):
    _declare(repo, home)
    env = {**os.environ, "HOME": str(home)}
    subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {str(_REPO_ROOT / 'scripts')!r});"
            f" import review_state as rs;"
            f" rs.satisfy_gate_demand('redesign', cwd={str(repo)!r},"
            f" gate='escalation-cap')",
        ],
        env=env,
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    res = _ask(repo, home, [_q("Yes", "No")])
    assert res.returncode == 0, res.stderr


def test_the_env_kill_switch_disarms_it(repo, home):
    _declare(repo, home)
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [_q("Ship as-is")]},
            "cwd": str(repo),
        }
    )
    env = {**os.environ, "HOME": str(home), "GENESIS_GATE_ACK_DISABLED": "1"}
    res = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload,
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stderr


def test_the_config_kill_switch_disarms_it(repo, home):
    _declare(repo, home)
    cfg = home / ".genesis" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "gate_ack.yaml").write_text("enabled: false\n")
    res = _ask(repo, home, [_q("Ship as-is")])
    assert res.returncode == 0, res.stderr


def test_a_malformed_config_leaves_the_gate_ARMED(repo, home):
    """Fail direction, and the opposite of this guard's internal fail-open.

    An internal error can wedge the session, so the guard's own faults fail open.
    A config typo is a human's mistake, and silently disarming a gate on one is
    the failure this repo keeps re-learning. The env switch stays as the
    unconditional escape, so nobody is stuck.
    """
    _declare(repo, home)
    cfg = home / ".genesis" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "gate_ack.yaml").write_text("enabled: [this is not a bool\n")
    res = _ask(repo, home, [_q("Ship as-is")])
    assert res.returncode == 2, res.stdout + res.stderr


def test_a_drifted_payload_shape_stands_the_guard_DOWN(repo, home):
    """A schema change must not wall off every question in the session.

    `hook_input.tool_input` falls back to returning the WHOLE payload when the
    `tool_input` key is absent, so a drift that moved or dropped that key would
    yield questions=None -> every remedy "missing" -> block. That is a shape
    surprise failing CLOSED inside a guard whose entire fail direction is open,
    arriving through the door marked "degrades toward not-covered" — and with a
    live demand it would refuse every ask until someone found the kill switch.

    MEASURED as unpinned: the isinstance guard could be deleted outright and the
    suite stayed green, because every other test either has no live demand or
    supplies a well-formed payload.
    """
    _declare(repo, home)
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "AskUserQuestion",
            "cwd": str(repo),
            # `tool_input` present but NOT an object — the drift shape.
            "tool_input": [{"questions": []}],
        }
    )
    res = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload,
        cwd=str(repo),
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_a_crash_fails_OPEN(repo, home, tmp_path):
    """Inverted from the house default, deliberately.

    A guard that can refuse AskUserQuestion can, when buggy, leave a session
    unable to ask the user anything at all — including how to unwedge it. Its
    failure costs more than its miss, so it must never be wired through
    run_guard.
    """
    # Force a REAL exception. Malformed stdin does NOT: `read_payload` returns {}
    # rather than raising, so the earlier version exited at the tool_name check and
    # never entered the wrapper — MEASURED, deleting the whole `__main__` block
    # left it green. PYTHONPATH does not work either, because the guard inserts its
    # own directory at sys.path[0]. So copy the guard into a throwaway tree beside
    # a poisoned sibling, the recipe test_untokenizable_probe.py already uses.
    tree = tmp_path / "poisoned"
    tree.mkdir()
    shutil.copy(_HOOK, tree / _HOOK.name)
    shutil.copy(_HOOK.parent / "hook_input.py", tree / "hook_input.py")
    (tree / "gate_demand.py").write_text("raise RuntimeError('induced failure')\n")
    res = subprocess.run(
        [sys.executable, str(tree / _HOOK.name)],
        input=json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "AskUserQuestion",
                "cwd": str(repo),
                "tool_input": {"questions": [_q("x")]},
            }
        ),
        cwd=str(repo),
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    # The "loudly" half of the contract, which nothing checked before.
    assert "GUARD ERROR (ask_question_gate)" in res.stderr


def test_it_is_not_wired_through_run_guard(repo, home):
    """Structural lock on the inverted fail direction.

    run_guard converts an unexpected crash into a BLOCK. Wiring this guard
    through it would silently turn the documented fail-open into a fail-closed
    that can wall off every question in the session — the exact outcome the
    docstring above rules out. A prose warning would not survive a future edit;
    this does.
    """
    tree = ast.parse(_HOOK.read_text())
    called = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "run_guard" not in called
    imported = {
        alias.name
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom | ast.Import)
        for alias in n.names
    }
    assert "run_guard" not in imported
    # `import hook_input; hook_input.run_guard(main)` is an Attribute call, which
    # the Name-only check above walks straight past — and is exactly the shape a
    # tidy-up refactor would reach for.
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "run_guard" not in attrs
    # Naming it in prose is fine and in fact required — the docstring has to say
    # WHY it is absent, or the next reader adds it back as an obvious oversight.


def test_a_non_askuserquestion_payload_is_ignored(repo, home):
    """The matcher should scope this, but a guard must not depend on its own
    wiring being right — this repo has shipped silently mis-wired hooks before."""
    _declare(repo, home)
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "cwd": str(repo),
        }
    )
    res = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload,
        cwd=str(repo),
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stderr


def test_a_background_session_is_not_left_unable_to_ask(repo, home):
    """Standing axiom: a background session must stay as capable as a foreground
    one. This gate blocks BOTH equally — which is fine, because a block is not an
    'ask' — but the refusal has to be self-service: it must state exactly what to
    add so a session with no human present can comply and move on.
    """
    _declare(repo, home)
    res = _ask(repo, home, [_q("Ship as-is")])
    assert res.returncode == 2
    for key in ("redesign", "narrow", "shelve"):
        assert key in res.stderr
    assert "option" in res.stderr.lower()


def test_the_refusal_never_suggests_dropping_the_question(repo, home):
    """The remedy for a refusal is to ADD the missing options, never to skip the
    ask. A message that offers the second reading would teach the corruption."""
    _declare(repo, home)
    res = _ask(repo, home, [_q("Ship as-is")])
    assert res.returncode == 2
    lowered = res.stderr.lower()
    for bad in ("skip the question", "without asking", "proceed without"):
        assert bad not in lowered


def test_the_hook_only_sees_ITS_OWN_sessions_demand(repo, home):
    """Pinned through the hook, not the state function.

    The resolution layer was previously exercised only by calling review_state
    directly — deleting the whole lookup left the suite green. This drives the
    real guard with a payload carrying a DIFFERENT session id.
    """
    env = {**os.environ, "HOME": str(home)}
    code = (
        f"import sys; sys.path.insert(0, {str(_REPO_ROOT / 'scripts')!r});"
        "import review_state as rs;"
        f"rs.write_gate_demand(gate='escalation-cap', remedies={REMEDIES!r},"
        f" required_action='relay them', cwd={str(repo)!r}, session_id='owner-session')"
    )
    subprocess.run(
        [sys.executable, "-c", code], env=env, cwd=str(repo), check=True, capture_output=True
    )
    bad = [_q("Ship as-is")]
    owner = _ask(repo, home, bad, session_id="owner-session")
    assert owner.returncode == 2, owner.stdout + owner.stderr
    other = _ask(repo, home, bad, session_id="another-session")
    assert other.returncode == 0, (
        "a different session must not inherit this obligation: " + other.stderr
    )


def test_the_gate_is_actually_wired(settings):
    """An unwired guard is a decoration.

    This repo has shipped 15 hooks and 2 inline guards that were SILENTLY INERT
    (commit e960f547): `git push`, `rm -rf` and protected-path writes all sailed
    through unblocked while the logic sat there passing its own tests. Logic tests
    prove the guard works when called; only this proves it is called.
    """
    entries = settings["hooks"]["PreToolUse"]
    matched = [e for e in entries if e.get("matcher") == "AskUserQuestion"]
    assert matched, "no PreToolUse matcher for AskUserQuestion"
    commands = [h.get("command", "") for e in matched for h in e.get("hooks", [])]
    assert any("ask_question_gate.py" in c for c in commands), commands


def test_the_wired_path_resolves_to_a_real_script(settings):
    """A matcher pointing at a path that does not exist is inert in a way that
    looks wired — the failure mode the previous test alone would not catch."""
    entries = settings["hooks"]["PreToolUse"]
    commands = [
        h.get("command", "")
        for e in entries
        if e.get("matcher") == "AskUserQuestion"
        for h in e.get("hooks", [])
    ]
    target = next(c for c in commands if "ask_question_gate.py" in c)
    rel = target.split("genesis-hook", 1)[1].strip()
    assert (_REPO_ROOT / "scripts" / rel).is_file(), rel
