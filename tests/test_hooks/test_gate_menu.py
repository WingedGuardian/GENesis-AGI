"""The escalation cap's menu, put to the user in the gate's own words.

WHY THIS EXISTS. On 2026-08-31 the cap printed three remedies and the session's relay
to the user dropped the first, invented a fourth, and added "ship as-is" -- the one
outcome the cap exists to prevent.

WHAT IS AND IS NOT COVERED, stated here because the scope IS the design. The hook reads
the existing branch-scoped review-round counter and appends the gate's own question when
the CAP tier is live. There is no persisted marker, so there is no lifecycle to test --
no write, no read-back, no retirement, no scoping, no validation of stored data. Two
earlier designs carried all of that and it drew ~25 findings across four reviewers
against ~3 for the substitution itself; the last was a P1 that made the marker's headline
mechanism inoperative in production while its own tests passed. What replaced it answers
the marker's one real question -- which tier is live -- from state that already exists.

The round-2 mode-switch tier is deliberately NOT covered, and one test below pins that
exclusion WITH its reason, because it is the load-bearing scope decision rather than an
omission.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOK = _REPO_ROOT / "scripts" / "hooks" / "ask_gate_menu.py"

sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
import review_state  # noqa: E402
from gate_menu import CAP_REMEDIES  # noqa: E402

# Reuse the cap suite's known-good machinery rather than re-deriving it. Four ad-hoc
# harnesses built during this work failed to arm the cap at all -- their `mark` calls
# were silently refused, so they measured a different rule's refusal and reported it as
# a clean result. The helper that the shipped cap tests already depend on cannot drift
# out from under this file without those tests failing too.
from tests.test_hooks.test_escalation_cap import (  # noqa: E402
    _git,
    _reach_rounds,
    _run_hook,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Same shape the imported helpers expect: a git repo on a feature branch.

    Defined here rather than imported because importing a pytest fixture rebinds a name
    that every test then shadows as a parameter, which ruff flags on each one. The
    repo's house pattern is per-module fixtures plus SHARED HELPERS (see
    tests/test_hooks/conftest.py); the helpers above take repo/home as arguments, so
    they work unchanged against these.
    """
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "-c", "init.defaultBranch=main", "init", "-q")
    _git(r, "config", "user.email", "t@e.st")
    _git(r, "config", "user.name", "tester")
    (r / "f.py").write_text("base = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "checkout", "-q", "-b", "feature/x")
    return r


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / ".genesis").mkdir(parents=True)
    return h


def _ask_payload(repo_path: Path, questions: list[dict] | None = None) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "session_id": "test-session",
        "cwd": str(repo_path),
        "tool_input": {
            "questions": questions
            if questions is not None
            else [
                {
                    "question": "an ordinary question the agent asked",
                    "header": "Next",
                    "multiSelect": False,
                    "options": [
                        {"label": "one", "description": ""},
                        {"label": "two", "description": ""},
                    ],
                }
            ]
        },
    }


def _hook(payload: dict, repo_path: Path, home_path: Path, **extra_env):
    env = {**os.environ, "HOME": str(home_path), **extra_env}
    env.pop("GENESIS_GATE_MENU_DISABLED", None)
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(payload),
        cwd=str(repo_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _questions_from(res) -> list[dict] | None:
    """The questions the USER would see, or None when the hook stayed silent."""
    if not res.stdout:
        return None
    out = json.loads(res.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in out, (
        "MEASURED: a permissionDecision alongside updatedInput breaks the call into "
        "'user did not answer' WITHOUT the user acting; the docs separately note a "
        "deny discards the rewrite. Never emit it."
    )
    return out["updatedInput"]["questions"]


# ─── Dormant: the cap tier this menu answered is retired ─────────────────────


def test_below_the_cap_the_hook_is_silent(repo, home):
    """No tier is live, so there is no decision to put to anyone."""
    assert _hook(_ask_payload(repo), repo, home).stdout == ""


def test_at_the_old_cap_the_hook_is_now_silent_too(repo, home):
    """The commit gate no longer stops on the local streak (a round reflection
    replaced it), so a counter sitting at the old cap must not bring back a menu
    for a stop that no longer exists. Control: the counter really is at the cap."""
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    stored = [
        json.loads(p.read_text()).get("round", 0)
        for p in (home / ".genesis" / "review_rounds").glob("*.json")
    ]
    assert max(stored, default=0) >= review_state.ESCALATION_ROUND_CAP, stored
    assert _hook(_ask_payload(repo), repo, home).stdout == ""
    err = _run_hook('git commit -m "wip"', repo, home).stderr
    assert "escalation cap reached" not in err and "MODE-SWITCH" not in err


# ─── Substitution behaviour ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "remedies,why",
    [
        ([{"key": "a", "label": "ONE", "description": "x"}], "1 option is below the min"),
        (
            [{"key": f"k{i}", "label": f"L{i}", "description": "x"} for i in range(5)],
            "5 options is above the max",
        ),
        (
            [
                {"key": "a", "label": "SAME", "description": "x"},
                {"key": "b", "label": "SAME", "description": "x"},
            ],
            "duplicate labels",
        ),
    ],
)
def test_an_UNRENDERABLE_menu_produces_no_ask(monkeypatch, remedies, why):
    """The two guards standing between a drifted declaration and CC rejecting the WHOLE
    call with "do not retry" — the wedge this hook vows never to cause.

    MEASURED: deleting either guard left all 21 tests green, because the only coverage
    was a STATIC assertion about the shipped constant. A static check cannot exercise a
    guard; this drives `_canonical_question` with the bad input directly.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_agm_probe", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_agm_probe"] = mod
    try:
        spec.loader.exec_module(mod)
        import gate_menu as gm

        monkeypatch.setattr(gm, "CAP_REMEDIES", remedies)
        assert mod._canonical_question() is None, f"unrenderable ({why}) must yield None"
    finally:
        sys.modules.pop("_agm_probe", None)


def test_the_canonical_question_carries_multiSelect_false():
    """Pinned because removing the field entirely left every test green, and a missing
    `multiSelect` changes how the tool renders the choice."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_agm_probe2", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_agm_probe2"] = mod
    try:
        spec.loader.exec_module(mod)
        q = mod._canonical_question()
        assert q is not None and q["multiSelect"] is False
        assert len(q["header"]) <= 12, (
            f"header is a chip with a documented 12-char budget; {q['header']!r} is "
            f"{len(q['header'])}"
        )
    finally:
        sys.modules.pop("_agm_probe2", None)


def test_the_shipped_menu_is_RENDERABLE_by_the_tool(repo, home):
    """MEASURED from the CC 2.1.246 binary: `options:Me(J7o()).min(2).max(4)`, with a
    hard MINIMUM as well as a maximum. Out of range is not a shorter menu -- CC rejects
    the whole call and the person never sees it. A static check, because the menu is
    source code rather than data on disk."""
    assert 2 <= len(CAP_REMEDIES) <= 4, f"{len(CAP_REMEDIES)} options is unrenderable"
    labels = [r["label"] for r in CAP_REMEDIES]
    assert len(set(labels)) == len(labels), "labels must be unique within a question"
    assert all(r.get("key") and r.get("label") for r in CAP_REMEDIES)


# ─── Fail direction: every degradation costs a menu, never a gate ───────────


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Bash", "tool_input": {"command": "ls"}},
        {"tool_name": "AskUserQuestion", "tool_input": {"questions": "not a list"}},
        {"tool_name": "AskUserQuestion"},
        {},
    ],
)
def test_malformed_or_irrelevant_payloads_emit_nothing(repo, home, payload):
    res = _hook(payload, repo, home)
    assert res.returncode == 0 and res.stdout == ""


def test_garbage_stdin_does_not_crash_the_hook(repo, home):
    res = subprocess.run(
        [sys.executable, str(_HOOK)],
        input="not json",
        capture_output=True,
        text=True,
        cwd=str(repo),
        env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
        timeout=30,
    )
    assert res.returncode == 0 and res.stdout == ""


def test_an_unimportable_review_state_FAILS_OPEN(tmp_path, repo, home):
    """Against the house default, on purpose: a hook that can refuse a question could
    leave a session unable to ask ANYTHING, including how to unwedge itself. Its failure
    must cost more than its miss -- so it exits 0 and emits nothing."""
    broken = tmp_path / "broken"
    (broken / "scripts" / "hooks").mkdir(parents=True)
    (broken / "scripts" / "lib").mkdir(parents=True)
    (broken / "scripts" / "review_state.py").write_text("raise RuntimeError('poisoned')\n")
    (broken / "scripts" / "lib" / "gate_menu.py").write_text(
        (_REPO_ROOT / "scripts" / "lib" / "gate_menu.py").read_text()
    )
    hook_copy = broken / "scripts" / "hooks" / "ask_gate_menu.py"
    hook_copy.write_text(_HOOK.read_text())
    res = subprocess.run(
        [sys.executable, str(hook_copy)],
        input=json.dumps(_ask_payload(repo)),
        capture_output=True,
        text=True,
        cwd=str(repo),
        env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
        timeout=30,
    )
    assert res.returncode == 0 and res.stdout == ""


def test_an_unimportable_gate_menu_FAILS_OPEN(tmp_path, repo, home):
    """Version skew is a real configuration here, not a hypothetical: `genesis-hook`
    resolves scripts from the MAIN worktree while GENESIS_HOOK_DEV_LOCAL=1 splits the
    trees, so this hook can run against a tree with no `gate_menu` module at all."""
    # ARM THE COUNTER FIRST. Without this the hook returns at the `_cap_is_live` check
    # before `_canonical_question` -- the function whose fail direction this test names
    # -- is ever called, and the assertion holds for the wrong reason. MEASURED: with
    # the arming absent, inverting the unimportable-module handler to return a
    # renderable menu left all 29 tests green.
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    broken = tmp_path / "nomenu"
    (broken / "scripts" / "hooks").mkdir(parents=True)
    (broken / "scripts" / "lib").mkdir(parents=True)
    (broken / "scripts" / "review_state.py").write_text(
        (_REPO_ROOT / "scripts" / "review_state.py").read_text()
    )
    hook_copy = broken / "scripts" / "hooks" / "ask_gate_menu.py"
    hook_copy.write_text(_HOOK.read_text())
    res = subprocess.run(
        [sys.executable, str(hook_copy)],
        input=json.dumps(_ask_payload(repo)),
        capture_output=True,
        text=True,
        cwd=str(repo),
        env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
        timeout=30,
    )
    assert res.returncode == 0 and res.stdout == ""


# ─── The wiring is the feature ──────────────────────────────────────────────


def test_the_hook_is_wired_as_a_PreToolUse_matcher_ONLY():
    """A hook nobody calls is not a mechanism.

    ONE event, and the singular is load-bearing: an earlier design also wired a
    PostToolUse recorder that read the user's answer back and made a commit gate honour
    it. That half drew ~20 findings against ~0 for this one and was deleted. A
    PostToolUse entry reappearing would mean the authorisation path had come back by the
    back door, which is why this asserts its ABSENCE rather than simply not mentioning it.
    """
    settings = json.loads((_REPO_ROOT / ".claude" / "settings.json").read_text())
    pre = [
        h["command"]
        for entry in settings["hooks"]["PreToolUse"]
        if entry.get("matcher") == "AskUserQuestion"
        for h in entry["hooks"]
    ]
    assert any("ask_gate_menu.py" in c for c in pre), pre
    post = [
        h["command"] for entry in settings["hooks"].get("PostToolUse", []) for h in entry["hooks"]
    ]
    assert not any("gate_menu" in c for c in post), post


def test_nothing_in_the_gate_READS_the_menu_module_back():
    """The scoping claim, as a lock instead of a docstring.

    The gate PRINTS these labels; it must never consult the hook's view of them or grow
    a store to read. Keyed on the symbols that would indicate either.
    """
    import ast

    src = (_REPO_ROOT / "scripts" / "review_enforcement_commit.py").read_text()
    seen: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Name):
            seen.add(node.id)
        elif isinstance(node, ast.Attribute):
            seen.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            seen.update(a.name for a in node.names)
    banned = {
        "write_gate_demand",
        "read_gate_demand",
        "find_session_gate_demand",
        "retire_gate_demand",
        "gate_demand_present",
    }
    assert not (banned & seen), f"the gate grew marker machinery again: {sorted(banned & seen)}"
    # Control: this must not pass on a file that stopped parsing or importing anything.
    # Control: this must not pass on a file that stopped parsing. The gate now reads
    # the round reflection, never the streak counter this menu keyed on.
    assert "_reflection_rule" in seen, "control: the gate must still define Rule R"
    assert "get_review_counters" not in seen, "the retired streak tiers came back"
