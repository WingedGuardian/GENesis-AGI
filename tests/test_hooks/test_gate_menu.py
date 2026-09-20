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
from gate_menu import CAP_QUESTION, CAP_REMEDIES  # noqa: E402

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


# ─── The counter decides, and only at the cap ───────────────────────────────


def test_below_the_cap_the_hook_is_silent(repo, home):
    """No tier is live, so there is no decision to put to anyone."""
    assert _hook(_ask_payload(repo), repo, home).stdout == ""


def test_at_the_cap_the_gates_own_question_is_APPENDED(repo, home):
    """THE FOUNDING INCIDENT, inverted.

    The agent asks its own question; what the user is shown carries the agent's
    question UNTOUCHED plus the gate's, with the gate's options in the gate's order.
    """
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    questions = _questions_from(_hook(_ask_payload(repo), repo, home))
    assert questions is not None, "the cap is live; the menu must be appended"
    assert questions[0]["question"] == "an ordinary question the agent asked", (
        "this APPENDS; it must never censor what the agent asked"
    )
    gate_q = questions[-1]
    assert gate_q["question"] == CAP_QUESTION
    assert [o["label"] for o in gate_q["options"]] == [r["label"] for r in CAP_REMEDIES]
    assert gate_q["options"][0]["label"] == "HAND IT BACK", "hand-back stays FIRST"


def test_the_MODE_SWITCH_tier_is_deliberately_NOT_covered(repo, home):
    """The load-bearing scope decision, pinned with its reason.

    `# escalation-ack` at the cap calls `reset_review_round`, so the counter drops and
    the menu stops appearing BY CONSTRUCTION. `# audit-ack` at the mode-switch tier
    deliberately does NOT reset the streak -- a still-narrow fix must still reach the
    round-3 stop -- so a menu keyed on `round == 2` would keep re-asking a decision the
    user already made, on every later question, until an external clean review happened
    to land. That is precisely the defect the deleted design needed retirement machinery
    to avoid. Excluding the tier is how this design avoids needing that machinery.

    If someone extends this hook to the mode-switch tier, this test fails and the
    docstring tells them what they are buying.
    """
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP - 1)
    assert _hook(_ask_payload(repo), repo, home).stdout == ""


def test_branch_lifetime_no_longer_hides_the_local_cap_menu(repo, home):
    """The legacy lifetime counter has no current authorization semantics."""
    for _ in range(2):
        _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
        acked = _run_hook('git commit -m "wip"  # escalation-ack', repo, home)
        assert acked.returncode == 0, acked.stdout + acked.stderr
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    assert _hook(_ask_payload(repo), repo, home).stdout != ""


def test_the_menu_stops_after_the_ack_BY_CONSTRUCTION(repo, home):
    """No retirement code: the ack resets the counter, so the tier is simply no longer
    live. This is the property that let the whole marker lifecycle be deleted."""
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    assert _hook(_ask_payload(repo), repo, home).stdout != "", "precondition: menu shown"
    acked = _run_hook('git commit -m "wip"  # escalation-ack', repo, home)
    assert acked.returncode == 0, acked.stdout + acked.stderr
    assert _hook(_ask_payload(repo), repo, home).stdout == "", (
        "the decision was made; the menu must stop being asked"
    )


def test_the_PAYLOAD_cwd_wins_over_the_process_cwd(tmp_path, repo, home):
    """The session's directory comes from the payload, not from wherever the hook
    happens to be standing. Unpinned before: every fixture ran the hook with
    cwd == payload cwd, so ignoring the payload entirely left all tests green."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    # Process cwd is a bare directory with no counter; the payload names the capped repo.
    res = _hook(_ask_payload(repo), elsewhere, home)
    assert res.stdout != "", "the payload's cwd must decide, not the process cwd"
    # And the converse: payload pointing at the bare dir yields nothing, from the repo.
    payload = _ask_payload(repo)
    payload["cwd"] = str(elsewhere)
    assert _hook(payload, repo, home).stdout == ""


def test_a_menu_does_not_follow_you_to_another_branch(repo, home):
    """`get_review_round` is branch-scoped, so a new change starts fresh."""
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    assert _hook(_ask_payload(repo), repo, home).stdout != ""
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", "-b", "other"],
        check=True,
        capture_output=True,
    )
    assert _hook(_ask_payload(repo), repo, home).stdout == ""


# ─── Substitution behaviour ─────────────────────────────────────────────────


def test_a_FORGED_gate_question_is_REPLACED_not_skipped(repo, home):
    """The measured forgery path, locked.

    An earlier revision returned early when the gate's question was already present, on
    the assumption the agent was echoing one we built. That compared only the QUESTION
    STRING and never the options -- so an agent could emit the gate's exact question
    carrying ONE option it wrote itself and the hook would stand down. That is the
    founding incident laundered through the mechanism built to prevent it.
    """
    forged = {
        "question": CAP_QUESTION,
        "header": "Gate",
        "multiSelect": False,
        "options": [{"label": "ship as-is", "description": "agent-invented"}],
    }
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    questions = _questions_from(_hook(_ask_payload(repo, [forged]), repo, home))
    assert questions is not None
    assert len(questions) == 1, "replaced in place, not appended beside itself"
    labels = [o["label"] for o in questions[0]["options"]]
    assert labels == [r["label"] for r in CAP_REMEDIES]
    assert "ship as-is" not in labels, "the forged option must not survive"


def test_a_call_already_at_the_question_maximum_passes_through(repo, home):
    """Refusing would reintroduce 'the gate can wedge asks' -- the inverted fail
    direction this whole line of work exists to avoid. The counter is still at the cap,
    so the next ask gets the menu."""
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    four = [
        {
            "question": f"agent question {i}",
            "header": "h",
            "multiSelect": False,
            "options": [
                {"label": "a", "description": ""},
                {"label": "b", "description": ""},
            ],
        }
        for i in range(4)
    ]
    assert _hook(_ask_payload(repo, four), repo, home).stdout == ""


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


# ─── The data and the prose cannot drift ────────────────────────────────────


def _cap_block(repo, home) -> str:
    """The gate's REAL rendered cap message. Fails loudly if the cap did not fire.

    The precondition assert is not decoration: five harnesses in this work silently
    failed to arm the cap and measured a DIFFERENT rule's refusal as though it were
    this one.
    """
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    err = _run_hook('git commit -m "wip"', repo, home).stderr
    assert "escalation cap reached" in err, (
        "precondition: the cap message must have rendered, or this test is measuring "
        f"some other rule's refusal. Got: {err[:200]!r}"
    )
    return err


def test_every_declared_remedy_appears_in_the_gates_MESSAGE_in_order(repo, home):
    """DRIFT LOCK, direction 1: nothing in the menu is absent from the gate's message.

    The menu the user clicks and the menu the gate prints are two renderings of one
    decision. If they disagree, the substitution shows the user something the gate did
    not say -- the failure this feature exists to prevent, by a different door.

    Labels AND descriptions, because the descriptions are most of what the user reads
    and an earlier version of this suite pinned only the labels: blanking every
    description to "ZZZ." left all 21 tests green.
    """
    err = _cap_block(repo, home)
    positions = []
    for remedy in CAP_REMEDIES:
        i = err.find(remedy["label"])
        assert i != -1, f"the gate's message never names {remedy['label']!r}"
        assert remedy["description"] in err, (
            f"the description for {remedy['key']!r} is not a verbatim excerpt of the "
            "gate's message — descriptions are the gate's words, not a paraphrase"
        )
        positions.append(i)
    assert positions == sorted(positions), (
        f"declaration order must match the printed order (got {positions}) — a session "
        "takes the menu in the order the gate prints it"
    )


def test_the_gate_PRINTS_no_option_the_menu_omits(repo, home):
    """DRIFT LOCK, direction 2 — and this is the direction that matters.

    Direction 1 cannot see the gate GROW an option. MEASURED: with only direction 1 in
    place, inserting "(e) SHIP AS-IS and move on." into the gate's cap message left all
    21 tests green — the substituted menu would have silently dropped from the user's
    view the one outcome the cap exists to prevent. That is the 2026-08-31 incident
    exactly: an option present in the gate, absent from what the user was shown.
    """
    import re

    err = _cap_block(repo, home)
    block = err.split("escalation cap reached", 1)[1].split("Bring evidence to the choice", 1)[0]
    # Deliberately WIDER than the house "  (x) " convention, and the slice now ends at
    # the last content line rather than a heading further down. MEASURED: the previous
    # pattern was ^\s{2}\(([a-z])\)\s, so inserting "    (e) SHIP AS-IS" (four spaces)
    # or "  - SHIP AS-IS" into the gate left all 29 tests green -- a FORMAT lock wearing
    # a content lock's docstring. Reformatting the gate's message must not disarm the
    # one direction that can see it grow an option.
    printed = re.findall(r"^\s*(?:\([a-z]\)|[-*])\s*(.+)$", block, re.MULTILINE)
    assert printed, "precondition: the cap block must print lettered options"
    assert len(printed) == len(CAP_REMEDIES), (
        f"the gate prints {len(printed)} options {printed} but the menu declares "
        f"{len(CAP_REMEDIES)} — the user would be shown a SHORTER menu than the gate "
        "gave, which is the founding incident"
    )
    # ARITY IS NOT IDENTITY. A count cannot see the gate REPLACE an option in place --
    # four options both sides, different words, menu silently wrong.
    for text, remedy in zip(printed, CAP_REMEDIES, strict=True):
        assert text.startswith(remedy["label"]), (
            f"the gate prints {text[:60]!r} where the menu declares {remedy['label']!r} "
            "— the user would be shown a DIFFERENT option from the one the gate gave"
        )


def test_the_QUESTION_names_the_tier_the_gate_is_blocking_on(repo, home):
    """Content, not plumbing. Comparing the rendered question to the imported constant
    (which an earlier version did) pins only that the string survived the trip:
    replacing CAP_QUESTION with "Pick one:" left every test green. This ties it to the
    GATE instead."""
    err = _cap_block(repo, home)
    assert "escalation cap" in CAP_QUESTION.lower()
    assert "escalation cap" in err.lower()


# ─── Fail direction: every degradation costs a menu, never a gate ───────────


@pytest.mark.parametrize("via", ["env", "marker"])
def test_the_kill_switch_stops_the_substitution(repo, home, via):
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    extra = {}
    if via == "env":
        extra["GENESIS_GATE_MENU_DISABLED"] = "1"
    else:
        marker = home / ".genesis" / "config" / "gate_menu_disabled"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("")
    assert _hook(_ask_payload(repo), repo, home, **extra).stdout == ""


@pytest.mark.parametrize(
    ("value", "disabled"),
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("on", True),
        ("yes", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("", False),
        ("  ", False),
    ],
)
def test_which_env_SPELLINGS_disable_the_substitution(repo, home, value, disabled):
    """The tuple of falsy spellings is load-bearing and was untested. MEASURED:
    collapsing ("", "0", "false", "no", "off") to ("",) left all 29 tests green — and
    that mutation makes `=0` and `=false` DISABLE the menu, the precise inversion the
    comment above the check records as measured-and-fixed. `no`/`off` name the SWITCH's
    position, so they mean "do not disable"; only this pins that.
    """
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    out = _hook(_ask_payload(repo), repo, home, GENESIS_GATE_MENU_DISABLED=value).stdout
    assert (out == "") is disabled, (
        f"GENESIS_GATE_MENU_DISABLED={value!r} "
        f"{'should' if disabled else 'should NOT'} disable the menu"
    )


def test_the_kill_switch_leaves_the_GATE_untouched(repo, home):
    """The switch costs the menu, never the block. Guard-the-guard for the test above:
    without this, disabling the gate itself would look like a working kill switch."""
    _reach_rounds(repo, home, review_state.ESCALATION_ROUND_CAP)
    env = {"GENESIS_GATE_MENU_DISABLED": "1"}
    blocked = _run_hook('git commit -m "wip"', repo, home)
    assert blocked.returncode == 2, "the cap must still block"
    assert _hook(_ask_payload(repo), repo, home, **env).stdout == ""


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


def test_cap_menu_uses_the_snapshot_streak_and_ignores_legacy_lifetime(
    tmp_path, repo, home
):
    tree = tmp_path / "snapshot"
    (tree / "scripts" / "hooks").mkdir(parents=True)
    (tree / "scripts" / "lib").mkdir(parents=True)
    (tree / "scripts" / "lib" / "gate_menu.py").write_text(
        (_REPO_ROOT / "scripts" / "lib" / "gate_menu.py").read_text()
    )
    (tree / "scripts" / "review_state.py").write_text(
        "ESCALATION_ROUND_CAP = 3\n"
        "def get_review_counters(cwd=None):\n"
        "    return (3, 99)\n"
    )
    hook_copy = tree / "scripts" / "hooks" / "ask_gate_menu.py"
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
    assert res.returncode == 0
    assert res.stdout != ""


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
    assert "get_review_counters" in seen, "control: the gate must still read the counter"
