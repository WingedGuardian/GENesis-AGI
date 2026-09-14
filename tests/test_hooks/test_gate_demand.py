"""The recorded menu, the ask hook that substitutes it, and the data-to-prose lock.

WHY THIS EXISTS. The escalation cap printed three remedies on 2026-08-31 and the
relay to the user dropped the first, invented a fourth, and added "ship as-is" —
the outcome the cap exists to prevent. PR #1863 tried to VALIDATE the agent's
option text against the declared set and drew 16 findings across four rounds, the
largest class being that matching: a coverage rule over an OPEN SET of words the
agent chooses. This is the inversion — the gate's own question is SUBSTITUTED into
the ask, so the agent never authors those options.

THERE IS NO LIFECYCLE HERE ANY MORE, and the absence is the design. An earlier
revision recorded the user's answer and had the commit gate honour it; that half
carried a LIVE/ANSWERED/CONSUMED state machine, drew roughly twenty findings across
four reviewers against zero for the substitution half, and was deleted. What is left
is a marker that is written when a tier blocks, read by the ask hook, and retired
when a commit finally goes through. The tests below cover WRITE, READ, RETIRE, and
the ways a marker can be unreadable -- not states and operations.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ASK_HOOK = _REPO_ROOT / "scripts" / "hooks" / "ask_gate_demand.py"

sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import review_state  # noqa: E402

# The commit gate is loaded PRIVATELY. It registers no shared name of its own, but
# loading it by path through the chokepoint is the repo's one way to load a script
# in a test — a module that registers a shared name and does not restore it makes
# the last registration win for the whole pytest session, and production code doing
# a call-time `from X import ...` then resolves an object `monkeypatch.setattr`
# never touched (tests/conftest.py::private_module).
from tests.conftest import private_module  # noqa: E402

_gate = private_module(
    "review_enforcement_commit_gatedemand",
    _REPO_ROOT / "scripts" / "review_enforcement_commit.py",
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-b", "feature-x", str(r)], check=True, capture_output=True)
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "seed.txt").write_text("seed\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "seed", "--no-verify")
    return r


@pytest.fixture
def rounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "rounds"
    monkeypatch.setattr(review_state, "_ROUND_DIR", d)
    return d


REMEDIES = [
    {
        "key": "hand_back",
        "label": "HAND IT BACK",
        "description": "terminal",
    },
    {
        "key": "redesign",
        "label": "REDESIGN it",
        "description": "continues",
    },
    {
        "key": "audit",
        "label": "AUDIT the class",
        "description": "continues, no reset",
    },
]
Q = "The gate fired. How should this proceed?"


def _declare(repo: Path, *, question: str = Q, remedies=None) -> None:
    review_state.write_gate_demand(
        gate="escalation_cap",
        tier="cap",
        question=question,
        remedies=REMEDIES if remedies is None else remedies,
        cwd=str(repo),
    )


# ─── Nothing recorded ───────────────────────────────────────────────────────


def test_absent_reads_as_no_demand(repo, rounds):
    assert review_state.read_gate_demand(str(repo)) is None


# ─── A block records the menu ───────────────────────────────────────────────


def test_a_block_declares_the_remedy_set_as_data(repo, rounds):
    _declare(repo)
    d = review_state.read_gate_demand(str(repo))
    assert d["question"] == Q
    assert [r["label"] for r in d["remedies"]] == [
        "HAND IT BACK",
        "REDESIGN it",
        "AUDIT the class",
    ]


def test_declaration_ORDER_is_preserved_exactly(repo, rounds):
    """Order is a CONTRACT, not presentation.

    A session takes the menu in the order the gate prints it, and a menu whose
    first entry preserves the change reads as "try harder" at exactly the moment
    that is the wrong instruction. Nothing may normalise these to a dict or a set.
    """
    _declare(repo)
    d = review_state.read_gate_demand(str(repo))
    assert [r["key"] for r in d["remedies"]] == ["hand_back", "redesign", "audit"]
    assert d["remedies"][0]["key"] == "hand_back", "hand-back must stay FIRST"


# ─── Scope: the menu belongs to one branch ──────────────────────────────────


def test_a_demand_does_not_follow_you_to_another_branch(repo, rounds):
    """Per-branch for the same reason the streak is: a new change starts fresh, and
    an answer about a design that no longer exists must not authorise a commit on
    the next one."""
    _declare(repo)
    _git(repo, "checkout", "-q", "-b", "other")
    assert review_state.read_gate_demand(str(repo)) is None
    _git(repo, "checkout", "-q", "feature-x")
    assert review_state.read_gate_demand(str(repo)) is not None


# ─── Malformed input: skip, never raise ─────────────────────────────────────


def test_a_malformed_remedy_is_refused_ALL_OR_NOTHING(repo, rounds):
    """One bad entry makes the WHOLE menu unreadable. It is never skipped.

    This is the founding incident reproduced by the mechanism built to prevent it,
    and an earlier revision shipped it: a malformed entry was SKIPPED and the
    survivors returned, so a single damaged remedy silently shortened the menu --
    and if the damaged one happened to be HAND IT BACK, the user would be shown
    every option except the one the gate names first. Raised by an external
    reviewer, not by this suite.

    The malformed value is PLANTED IN THE FILE rather than passed to
    `write_gate_demand`. Going through the writer made this vacuous: the writer
    refuses an invalid remedy set and writes NOTHING, so the assertion passed
    because the file was empty, and deleting `_valid_remedy` from `read_gate_demand`
    entirely left it green. The read boundary is what this test names, so the read
    boundary is what it must exercise.

    Costing the menu is the CORRECT direction here and is why all-or-nothing is
    affordable: nothing authorises on this marker, so an unreadable one means the
    user is asked the way they were before this change, and the next block rewrites
    it from the gate's own canonical set.
    """
    _declare(repo)  # a VALID demand first, so there is something to corrupt
    stored = json.loads(review_state._round_file(str(repo)).read_text())
    good = stored["gate_demand"]["remedies"]
    assert len(good) == 3 and good[0]["label"] == "HAND IT BACK"
    # Corrupt the LAST entry only: a skipping reader would return the first two and
    # look healthy, which is exactly the shape that must fail.
    stored["gate_demand"]["remedies"] = [good[0], good[1], {"key": "k"}]
    review_state._round_file(str(repo)).write_text(json.dumps(stored))
    assert review_state.read_gate_demand(str(repo)) is None


def test_a_demand_with_no_intelligible_remedy_reads_as_ABSENT(repo, rounds):
    """It could never be answered, so reporting it live would wedge the branch with
    no route out. Absent means the gate writes a fresh one on its next block."""
    _declare(repo, remedies=[{"key": "k"}])
    assert review_state.read_gate_demand(str(repo)) is None


def test_an_implausible_remedy_count_is_refused_at_BOTH_boundaries(repo, rounds):
    """A menu is a thing a human chooses from; 30 entries is a malformed one.

    BOTH boundaries, and the split is the point. Going through the writer alone made
    this VACUOUS — caught by the verify-RED sweep, not by reading it: the writer
    refuses an oversized set and stores NOTHING, so the read returned None for the
    empty file and disabling the read cap entirely left the test green. The read
    boundary is what a hand-edited round file reaches, so the read boundary needs its
    own planted case.
    """
    # The writer refuses: nothing is stored at all.
    _declare(repo, remedies=REMEDIES * 10)
    assert not review_state._round_file(str(repo)).exists() or "gate_demand" not in json.loads(
        review_state._round_file(str(repo)).read_text()
    )
    # The reader refuses independently, against a file the writer never saw.
    _declare(repo)  # a valid menu first
    stored = json.loads(review_state._round_file(str(repo)).read_text())
    stored["gate_demand"]["remedies"] = REMEDIES * 10
    review_state._round_file(str(repo)).write_text(json.dumps(stored))
    assert review_state.read_gate_demand(str(repo)) is None


def test_a_corrupt_round_file_never_raises_into_the_gate(repo, rounds):
    rounds.mkdir(parents=True, exist_ok=True)
    review_state._round_file(str(repo)).write_text("{not json")
    assert review_state.read_gate_demand(str(repo)) is None


@pytest.mark.parametrize(
    "junk", ['{"gate_demand": 42}', '{"gate_demand": []}', '{"gate_demand": null}']
)
def test_a_non_dict_demand_reads_as_absent(repo, rounds, junk):
    rounds.mkdir(parents=True, exist_ok=True)
    review_state._round_file(str(repo)).write_text(junk)
    assert review_state.read_gate_demand(str(repo)) is None


# ─── The chokepoint: counter writers must not destroy a demand ──────────────


@pytest.mark.parametrize(
    "drive",
    [
        pytest.param(
            lambda c: review_state.bump_review_round(cwd=c, clean=False, source="external"),
            id="bump-defects",
        ),
        pytest.param(
            lambda c: review_state.bump_review_round(cwd=c, clean=True, source="external"),
            id="bump-clean",
        ),
        pytest.param(
            lambda c: review_state.bump_review_round(cwd=c, clean=False, source="internal"),
            id="bump-internal",
        ),
        pytest.param(lambda c: review_state.reset_review_round(cwd=c), id="reset"),
        pytest.param(lambda c: review_state.consume_final_accept(cwd=c), id="final-accept"),
    ],
)
def test_no_counter_writer_may_destroy_a_demand(repo, rounds, drive):
    """THE CHOKEPOINT LOCK.

    Every counter-writer builds a FRESH dict literal, so each would otherwise have
    to REMEMBER to carry the demand — and a convention several call sites must
    remember is what reviewers find one instance of at a time. MEASURED during the
    build: `bump_review_round` silently deleted a live demand, and an AST sweep then
    found a second site (`consume_final_accept`) nobody had looked at. The
    obligation lives in `_write_round` now; this is the test that fails if anyone
    routes around it.
    """
    _declare(repo)
    (repo / "f.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    drive(str(repo))
    d = review_state.read_gate_demand(str(repo))
    assert d is not None
    assert [r["key"] for r in d["remedies"]] == ["hand_back", "redesign", "audit"]


def test_something_CAN_change_a_demand_or_the_suite_above_is_vacuous(repo, rounds):
    """Guard the guard: the parametrized test above proves nothing unless some
    operation genuinely alters a demand. If this control ever stops discriminating,
    that whole suite is passing for the wrong reason.

    The operation that genuinely alters a demand is a fresh block: the newest
    canonical set simply wins, which is also how a marker some earlier write left
    unreadable gets repaired.
    """
    _declare(repo)
    _declare(repo, question="A DIFFERENT question", remedies=REMEDIES[:2])
    d = review_state.read_gate_demand(str(repo))
    assert d["question"] == "A DIFFERENT question"
    assert [r["key"] for r in d["remedies"]] == ["hand_back", "redesign"]


def test_the_round_counter_still_works_alongside_a_demand(repo, rounds):
    """The demand rides the round file; it must not disturb what that file is for."""
    _declare(repo)
    (repo / "f.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    assert review_state.bump_review_round(cwd=str(repo), clean=False, source="external") == 1
    assert review_state.get_review_round(str(repo)) == 1


# ─── The DATA-to-PROSE lock ─────────────────────────────────────────────────


def test_every_declared_cap_remedy_appears_in_the_cap_block_message_IN_ORDER():
    """The data and the prose are two copies of one remedy set, and a copy drifts.

    #1971 pinned the PROSE order (hand-back first). This pins that the DATA agrees
    with it — otherwise the gate could print one menu and substitute a different one
    into the ask, which is the founding incident with the roles reversed.
    """
    src = (_REPO_ROOT / "scripts" / "review_enforcement_commit.py").read_text()
    # ANCHOR INSIDE THE BLOCK MESSAGE. Searching the whole file resolved
    # "HAND IT BACK" to the _CAP_REMEDIES DATA literal, which sits ~600 lines ABOVE
    # the prose — so this compared a data offset against prose offsets and stayed
    # true even if the prose hand-back line moved to the END of the message. The
    # test claimed to pin prose order and pinned nothing.
    anchor = src.index("BLOCKED: review escalation cap reached")
    prose_order = ["HAND IT BACK", "(b) REDESIGN", "(c) NARROW", "(d) SHELVE"]
    positions = [src.find(p, anchor) for p in prose_order]
    assert all(p != -1 for p in positions), (
        f"cap prose changed: {dict(zip(prose_order, positions, strict=True))}"
    )
    assert positions == sorted(positions), "the cap message's own option order changed"
    declared = [r["label"] for r in _gate._CAP_REMEDIES]
    assert declared[0] == "HAND IT BACK", "hand-back must be declared FIRST"
    assert [d.split()[0] for d in declared] == ["HAND", "REDESIGN", "NARROW", "SHELVE"], declared


def test_every_declared_mode_switch_remedy_appears_in_its_block_message():
    src = (_REPO_ROOT / "scripts" / "review_enforcement_commit.py").read_text()
    assert "(A) The PREMISE is wrong" in src
    assert "(B) The premise holds" in src
    declared = [r["label"] for r in _gate._MODE_SWITCH_REMEDIES]
    assert declared[0].startswith("(A)"), "hand-back must be declared FIRST at this tier too"
    assert declared[1].startswith("(B)")


# ─── The ask hook, driven as a real subprocess ──────────────────────────────


def _hook(mode: str, payload: dict, home: Path, cwd: Path, **extra):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8", **extra}
    return subprocess.run(
        [sys.executable, str(_ASK_HOOK), mode],
        input=json.dumps(payload),
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _ask(n: int = 1) -> dict:
    return {
        "tool_name": "AskUserQuestion",
        "tool_input": {
            "questions": [
                {
                    "question": f"agent question {i}",
                    "header": f"A{i}",
                    "multiSelect": False,
                    "options": [{"label": "yes", "description": "d"}],
                }
                for i in range(n)
            ]
        },
    }


@pytest.fixture
def live_demand(repo: Path, tmp_path: Path) -> tuple[Path, Path]:
    """A repo with a LIVE demand, written through a subprocess so it lands in the
    test HOME rather than the real ~/.genesis."""
    home = tmp_path / "home"
    home.mkdir()
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, json, os, pathlib;"
            "sys.path.insert(0, sys.argv[1]);"
            "import review_state as rs;"
            "rs._ROUND_DIR = pathlib.Path(os.environ['HOME'])/'.genesis'/'review_rounds';"
            "rs.write_gate_demand(gate='escalation_cap', tier='cap', question=sys.argv[2],"
            " remedies=json.loads(sys.argv[3]), cwd=sys.argv[4])",
            str(_REPO_ROOT / "scripts"),
            Q,
            json.dumps(REMEDIES),
            str(repo),
        ],
        check=True,
        capture_output=True,
        cwd=str(repo),
        env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
    )
    return repo, home


def _read_demand(repo: Path, home: Path):
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, json, os, pathlib;"
            "sys.path.insert(0, sys.argv[1]);"
            "import review_state as rs;"
            "rs._ROUND_DIR = pathlib.Path(os.environ['HOME'])/'.genesis'/'review_rounds';"
            "print(json.dumps(rs.read_gate_demand(sys.argv[2])))",
            str(_REPO_ROOT / "scripts"),
            str(repo),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(repo),
        env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
    )
    return json.loads(out.stdout)


def test_with_no_demand_the_hook_is_silent(repo, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    res = _hook("--pre", _ask(), home, repo)
    assert res.returncode == 0
    assert res.stdout == "", "the ordinary path must emit nothing at all"


def test_a_live_demand_appends_the_gates_own_question(live_demand):
    repo, home = live_demand
    res = _hook("--pre", _ask(), home, repo)
    hs = json.loads(res.stdout)["hookSpecificOutput"]
    qs = hs["updatedInput"]["questions"]
    assert qs[0]["question"] == "agent question 0", "the agent's own question survives"
    assert qs[-1]["question"] == Q, "the gate's question is appended"
    assert [o["label"] for o in qs[-1]["options"]] == [r["label"] for r in REMEDIES]


def test_the_hook_NEVER_emits_a_permissionDecision(live_demand):
    """MEASURED on CC 2.1.246: adding `permissionDecision` breaks an
    AskUserQuestion call into "user did not answer" WITHOUT the user acting — a
    false negative that nearly killed this design. Variant B only."""
    repo, home = live_demand
    res = _hook("--pre", _ask(), home, repo)
    hs = json.loads(res.stdout)["hookSpecificOutput"]
    assert "permissionDecision" not in hs
    assert hs["hookEventName"] == "PreToolUse"


def test_a_call_already_at_the_question_maximum_passes_through_UNMODIFIED(live_demand):
    """Refusing would reintroduce "the gate can wedge asks" — the inverted fail
    direction #1863 defended. The demand simply stays live for the next ask."""
    repo, home = live_demand
    res = _hook("--pre", _ask(4), home, repo)
    assert res.stdout == ""
    assert _read_demand(repo, home) is not None


def test_the_gate_question_is_REPLACED_never_duplicated(live_demand):
    """An already-present gate question is rewritten in place, not appended beside
    itself — and NOT skipped, which was the forgery path (see the regression test
    below). The user sees the decision exactly once, with the gate's own options."""
    repo, home = live_demand
    payload = _ask(1)
    payload["tool_input"]["questions"].append(
        {"question": Q, "header": "Gate decision", "multiSelect": False, "options": []}
    )
    qs = json.loads(_hook("--pre", payload, home, repo).stdout)["hookSpecificOutput"][
        "updatedInput"
    ]["questions"]
    assert [q["question"] for q in qs].count(Q) == 1, "rendered once, not twice"
    assert [o["label"] for o in qs[-1]["options"]] == [r["label"] for r in REMEDIES]


@pytest.mark.parametrize("count", [1, 5, 12])
def test_a_menu_OUTSIDE_the_renderable_range_reaches_no_ask(live_demand, count):
    """Out of range is not a shorter menu -- it is a REJECTED CALL.

    MEASURED in the CC 2.1.246 binary: `options:Me(J7o()).min(2).max(4)`, with the
    steer CC returns on violation -- "This call included a question with fewer than 2
    options, so it was rejected and the person never saw it ... Do not retry this
    call." So an out-of-range append takes the AGENT'S OWN questions down with it and
    tells the agent not to retry: the session loses its ability to ask the user
    anything, which is the one outcome this hook's fail-direction note says it must
    never cause.

    Planted in the file, because the writer refuses these counts too -- going through
    the writer would leave nothing stored and the test would pass on an empty file.

    An OUTCOME lock, not a layer lock, and the distinction is the honest part: the
    enforcement lives at the READ (`review_state._MIN_REMEDIES`/`_MAX_REMEDIES`), and
    an earlier revision ALSO checked in the hook. The verify-RED sweep showed that
    second check was unreachable -- disabling it left this green -- so it was deleted
    rather than left as a guard nothing can exercise. What this asserts is that no
    unrenderable menu reaches an ask, by whichever layer refuses it.
    """
    repo, home = live_demand
    rf = home / ".genesis" / "review_rounds"
    stored = json.loads(next(rf.glob("*.json")).read_text())
    base = stored["gate_demand"]["remedies"][0]
    stored["gate_demand"]["remedies"] = [
        {**base, "key": f"k{i}", "label": f"L{i}"} for i in range(count)
    ]
    next(rf.glob("*.json")).write_text(json.dumps(stored))
    assert _hook("--pre", _ask(), home, repo).stdout == ""


def test_duplicate_option_labels_are_passed_through(live_demand):
    """Same measured schema: labels must be unique within a question. A duplicate
    reaches this only from a corrupt or hand-edited marker, and the next block
    rewrites it from the canonical set."""
    repo, home = live_demand
    rf = home / ".genesis" / "review_rounds"
    stored = json.loads(next(rf.glob("*.json")).read_text())
    base = stored["gate_demand"]["remedies"][0]
    stored["gate_demand"]["remedies"] = [
        {**base, "key": "a", "label": "SAME"},
        {**base, "key": "b", "label": "SAME"},
    ]
    next(rf.glob("*.json")).write_text(json.dumps(stored))
    assert _hook("--pre", _ask(), home, repo).stdout == ""


def test_the_shipped_menus_are_actually_RENDERABLE():
    """The bound is only useful if the real data sits inside it. A gate whose own
    menu cannot be rendered would silently never substitute anything."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_gate_menus", _REPO_ROOT / "scripts" / "review_enforcement_commit.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_gate_menus"] = mod
    try:
        spec.loader.exec_module(mod)
        for name in ("_CAP_REMEDIES", "_MODE_SWITCH_REMEDIES"):
            remedies = getattr(mod, name)
            assert review_state._MIN_REMEDIES <= len(remedies) <= review_state._MAX_REMEDIES, (
                f"{name} has {len(remedies)} options — outside the renderable range"
            )
            labels = [r["label"] for r in remedies]
            assert len(set(labels)) == len(labels), f"{name} has duplicate labels"
    finally:
        sys.modules.pop("_gate_menus", None)


@pytest.mark.parametrize("via", ["env", "marker"])
def test_the_kill_switch_disables_the_hook(live_demand, via):
    repo, home = live_demand
    extra = {}
    if via == "env":
        extra["GENESIS_GATE_ACK_DISABLED"] = "1"
    else:
        marker = home / ".genesis" / "config" / "gate_ack_disabled"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("")
    assert _hook("--pre", _ask(), home, repo, **extra).stdout == ""
    # Disarm before verifying. While the switch is ON, `read_gate_demand` reports
    # "no menu" BY DESIGN — that is precisely how it restores the pre-change
    # behaviour — so reading through it armed would assert on the switch rather
    # than on the marker. Disarming also proves the switch is reversible: the
    # recorded menu was left untouched, not destroyed.
    if via == "marker":
        (home / ".genesis" / "config" / "gate_ack_disabled").unlink()
    assert _read_demand(repo, home) is not None, "the switch suppresses, it does not erase"


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Bash", "tool_input": {"command": "ls"}},
        {"tool_name": "AskUserQuestion", "tool_input": {"questions": "not a list"}},
        {"tool_name": "AskUserQuestion"},
        {},
    ],
)
def test_malformed_or_irrelevant_payloads_emit_nothing(live_demand, payload):
    repo, home = live_demand
    res = _hook("--pre", payload, home, repo)
    assert res.returncode == 0 and res.stdout == ""


def test_garbage_stdin_does_not_crash_the_hook(live_demand):
    repo, home = live_demand
    res = subprocess.run(
        [sys.executable, str(_ASK_HOOK), "--pre"],
        input="not json",
        capture_output=True,
        text=True,
        cwd=str(repo),
        env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
        timeout=30,
    )
    assert res.returncode == 0 and res.stdout == ""


def test_an_unimportable_review_state_FAILS_OPEN(tmp_path, repo):
    """Against the house default, on purpose. A guard that can refuse a question
    could leave a session unable to ask ANYTHING, including how to unwedge itself.
    Its failure would cost more than its miss — so it exits 0 and emits nothing,
    and the fail-CLOSED half lives in the commit gate instead."""
    broken = tmp_path / "broken"
    (broken / "scripts" / "hooks").mkdir(parents=True)
    (broken / "scripts" / "review_state.py").write_text("raise RuntimeError('poisoned')\n")
    (broken / "scripts" / "hooks" / "ask_gate_demand.py").write_text(_ASK_HOOK.read_text())
    home = tmp_path / "home2"
    home.mkdir()
    res = subprocess.run(
        [sys.executable, str(broken / "scripts" / "hooks" / "ask_gate_demand.py"), "--pre"],
        input=json.dumps(_ask()),
        capture_output=True,
        text=True,
        cwd=str(repo),
        env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
        timeout=30,
    )
    assert res.returncode == 0, "exit 2 here would refuse the ask — the wrong direction"
    assert res.stdout == ""


def test_the_commit_gate_NEVER_READS_the_marker():
    """The whole scoping claim, as a lock instead of four docstrings.

    An earlier revision had the commit gate honour a recorded answer; roughly twenty
    findings landed on that half across four reviewers and zero on the substitution
    half. The reader is gone, and every argument in this PR for why the remaining
    failure modes are affordable -- all-or-nothing validation, repair by overwrite,
    fail-open in the hook -- rests on it staying gone. Until now it was asserted in
    four places and enforced in none.

    Deliberately keyed on the symbols that would let the gate CONSULT the marker's
    content. `write_gate_demand` and `retire_gate_demand` are writes and are allowed;
    `gate_ack_disabled` reads the operator's switch, not the marker.
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
    readers = {
        "read_gate_demand",
        "find_session_gate_demand",
        "gate_demand_present",
        "_GATE_DEMAND_KEY",
    }
    assert not (readers & seen), f"the commit gate grew a marker reader: {sorted(readers & seen)}"
    # Control: without this the test also passes on a file that stopped importing
    # anything at all, which is the vacuity shape this suite has already hit twice.
    assert "write_gate_demand" in seen, "control: the writer must still be there"


def test_the_hook_is_wired_as_a_PreToolUse_matcher_ONLY():
    """The wiring is the feature. A hook nobody calls is not a mechanism.

    ONE event, and the singular is load-bearing: an earlier revision also wired a
    PostToolUse recorder, and this test asserted BOTH. That half is gone, so a
    PostToolUse entry reappearing would mean the authorisation path had come back
    by the back door -- which is why this asserts its ABSENCE rather than simply
    not mentioning it.
    """
    settings = json.loads(
        (Path(__file__).resolve().parents[2] / ".claude/settings.json").read_text()
    )
    pre = [
        h["command"]
        for entry in settings["hooks"]["PreToolUse"]
        if entry.get("matcher") == "AskUserQuestion"
        for h in entry["hooks"]
    ]
    assert any("ask_gate_demand.py --pre" in c for c in pre), pre
    post = [
        h["command"] for entry in settings["hooks"].get("PostToolUse", []) for h in entry["hooks"]
    ]
    assert not any("ask_gate_demand" in c for c in post), post


def test_a_forged_question_is_REPLACED_not_skipped(live_demand):
    """B: the dedupe short-circuit WAS the forgery path.

    The hook used to skip appending when the gate's question was already present,
    comparing only the QUESTION STRING and never the options — so an agent that read
    the gate's question (the block message names `review_state.py gate-demand`) could
    emit it carrying one self-authored option, and that menu reached the user.
    MEASURED before the fix: three of four remedies dropped, the survivor reframed,
    and the acked commit exited 0 — the 2026-08-31 incident laundered through the
    mechanism built to prevent it.
    """
    repo, home = live_demand
    forged = {
        "tool_name": "AskUserQuestion",
        "tool_input": {
            "questions": [
                {
                    "question": Q,  # the GATE's question, verbatim
                    "header": "Cap",
                    "multiSelect": False,
                    "options": [{"label": "REDESIGN it", "description": "the only real option"}],
                }
            ]
        },
    }
    res = _hook("--pre", forged, home, repo)
    assert res.stdout != "", "a forged question must be REPLACED, never skipped"
    qs = json.loads(res.stdout)["hookSpecificOutput"]["updatedInput"]["questions"]
    assert len(qs) == 1, "replaced in place, not appended alongside"
    assert [o["label"] for o in qs[0]["options"]] == [r["label"] for r in REMEDIES]
    assert qs[0]["options"][0]["label"] == "HAND IT BACK", "the dropped option is back"


def test_replacing_preserves_the_agents_OTHER_questions(live_demand):
    """Guard the guard: replacement must not eat unrelated questions."""
    repo, home = live_demand
    payload = _ask(1)
    payload["tool_input"]["questions"].append(
        {
            "question": Q,
            "header": "x",
            "multiSelect": False,
            "options": [{"label": "REDESIGN it", "description": ""}],
        }
    )
    qs = json.loads(_hook("--pre", payload, home, repo).stdout)["hookSpecificOutput"][
        "updatedInput"
    ]["questions"]
    assert len(qs) == 2
    assert qs[0]["question"] == "agent question 0", "the agent's own question survives"
    assert [o["label"] for o in qs[1]["options"]] == [r["label"] for r in REMEDIES]


def test_the_kill_switch_suppresses_the_read_AND_the_write(repo, rounds, monkeypatch):
    """The operator lever. Armed, the reader reports nothing and the writer records
    nothing, so the session asks exactly as it did before this change existed."""
    _declare(repo)
    monkeypatch.setenv("GENESIS_GATE_ACK_DISABLED", "1")
    assert review_state.read_gate_demand(str(repo)) is None
    _declare(repo, question="written while disarmed")
    monkeypatch.delenv("GENESIS_GATE_ACK_DISABLED")
    assert review_state.read_gate_demand(str(repo))["question"] == Q, (
        "the disarmed write must not have landed"
    )


def test_the_hook_resolves_the_SESSION_directory_from_the_payload(repo, tmp_path):
    """C: the hook used its own process cwd; the commit gate resolves the COMMIT's
    directory. They disagree in exactly the configuration this repo mandates — a
    session sitting in one worktree while committing with `git -C <another>` — and
    the demand was then unanswerable, so the branch could never proceed.

    The rest of this suite always runs the hook WITH cwd=repo and sets no payload
    cwd, so this whole class was structurally invisible to it.
    """
    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, json, os, pathlib;"
            "sys.path.insert(0, sys.argv[1]);"
            "import review_state as rs;"
            "rs._ROUND_DIR = pathlib.Path(os.environ['HOME'])/'.genesis'/'review_rounds';"
            "rs.write_gate_demand(gate='g', tier='cap', question=sys.argv[2],"
            " remedies=json.loads(sys.argv[3]), cwd=sys.argv[4])",
            str(_REPO_ROOT / "scripts"),
            Q,
            json.dumps(REMEDIES),
            str(repo),
        ],
        check=True,
        capture_output=True,
        cwd=str(repo),
        env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
    )
    payload = _ask()
    payload["cwd"] = str(repo)  # the SESSION is about `repo` ...
    res = subprocess.run(  # ... while the PROCESS runs elsewhere
        [sys.executable, str(_ASK_HOOK), "--pre"],
        input=json.dumps(payload),
        cwd=str(elsewhere),
        env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.stdout != "", "the payload cwd must decide, not the process cwd"
    qs = json.loads(res.stdout)["hookSpecificOutput"]["updatedInput"]["questions"]
    assert qs[-1]["question"] == Q


# ─── Regressions from the cross-model (secondary) review ────────────────────
#
# Found by the gate-fix lane's second round-1 reviewer, after the internal pass had
# already run. Three of them are instances of classes the internal pass and I had
# ALREADY identified and fixed incompletely, which is the finding about the finding:
#   * the cwd class was fixed on the payload-vs-process axis, which was not the live
#     one -- `git -C <other worktree>` still wedged
#   * "absent means allow" was fixed at the read boundary, but `_load_round`'s legacy
#     discard runs BEFORE that boundary and threw the demand away first
#   * the deferred one-shot consume was fixed at the cap tier and not the round-2 tier


def _declare_for_session(repo, home, sid, question=None):
    """Declare a demand as the GATE does: keyed to a worktree, bound to a session."""
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, json, os, pathlib;"
            "sys.path.insert(0, sys.argv[1]);"
            "import review_state as rs;"
            "rs._ROUND_DIR = pathlib.Path(os.environ['HOME'])/'.genesis'/'review_rounds';"
            "rs.write_gate_demand(gate='g', tier='cap', question=sys.argv[2],"
            " remedies=json.loads(sys.argv[3]), cwd=sys.argv[4], session_id=sys.argv[5])",
            str(_REPO_ROOT / "scripts"),
            question or Q,
            json.dumps(REMEDIES),
            str(repo),
            sid,
        ],
        check=True,
        capture_output=True,
        cwd=str(repo),
        env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
    )


def _ask_as(session_dir, home, sid):
    payload = _ask()
    payload["cwd"] = str(session_dir)
    payload["session_id"] = sid
    return subprocess.run(
        [sys.executable, str(_ASK_HOOK), "--pre"],
        input=json.dumps(payload),
        cwd=str(session_dir),
        env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_the_demand_is_found_when_the_COMMIT_targets_another_worktree(repo, tmp_path):
    """The gate keys a demand under the COMMIT's effective directory, which
    `git -C <dir>` moves away from the session's own. The association is RECORDED --
    the gate knows which session it blocked -- so the lookup is exact rather than
    derived from where the hook happens to be standing.

    Two earlier shapes were wrong and are pinned against here: computing the key from
    the hook's PROCESS cwd (missed the case), and computing it from the PAYLOAD cwd
    then taking the only candidate in the round directory (closed the wedge, opened a
    cross-session leak -- see the next test).
    """
    home = tmp_path / "home"
    home.mkdir()
    session_dir = tmp_path / "other_worktree"
    session_dir.mkdir()
    _declare_for_session(repo, home, "session-A")  # demand keyed under `repo`
    res = _ask_as(session_dir, home, "session-A")  # ...session sits elsewhere
    assert res.stdout != "", "the session's own demand must be found"
    qs = json.loads(res.stdout)["hookSpecificOutput"]["updatedInput"]["questions"]
    assert qs[-1]["question"] == Q


def test_a_session_is_NEVER_shown_another_sessions_demand(repo, tmp_path):
    """The defect the previous shape introduced, now unrepresentable.

    Enumerating the round directory and returning the sole candidate meant an
    unrelated session in worktree A could be shown worktree B's menu -- and the
    recorder would write A's answer onto B, potentially authorising B's work. The
    round directory is global; "the only candidate" was never a statement about who
    was asking.
    """
    home = tmp_path / "home"
    home.mkdir()
    session_dir = tmp_path / "unrelated"
    session_dir.mkdir()
    _declare_for_session(repo, home, "session-A")
    assert _ask_as(session_dir, home, "session-B").stdout == "", (
        "session B must not receive session A's decision"
    )
    # ...and the control: A still gets its own, so the assertion above is not
    # passing merely because nothing is ever found.
    assert _ask_as(session_dir, home, "session-A").stdout != ""


def test_a_second_demand_elsewhere_does_not_re_wedge_the_lookup(repo, tmp_path):
    """The sole-candidate shape also re-wedged as soon as a second demand existed
    anywhere, since the disambiguator was "there is only one". Matching on the
    recorded session has no such dependency on what other worktrees are doing."""
    home = tmp_path / "home"
    home.mkdir()
    other = tmp_path / "second_repo"
    other.mkdir()
    subprocess.run(["git", "init", "-b", "feature-x", str(other)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(other), "config", k, v], check=True)
    (other / "s.txt").write_text("s\n")
    subprocess.run(["git", "-C", str(other), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(other), "commit", "-m", "s", "--no-verify"],
        check=True,
        capture_output=True,
    )
    _declare_for_session(repo, home, "session-A")
    _declare_for_session(other, home, "session-C")  # an unrelated second demand

    session_dir = tmp_path / "elsewhere"
    session_dir.mkdir()
    res = _ask_as(session_dir, home, "session-A")
    assert res.stdout != "", "a second demand elsewhere must not hide A's own"
    qs = json.loads(res.stdout)["hookSpecificOutput"]["updatedInput"]["questions"]
    assert qs[-1]["question"] == Q


def test_the_legacy_counter_discard_does_not_take_the_demand_with_it(repo, rounds):
    """`_load_round` discards a pre-source-axis COUNTER, and that discard runs before
    the menu reader sees the file. Returning a bare {} therefore took the recorded
    menu with it.

    The discard is about the counter's provenance; a menu is not a counter.
    """
    _declare(repo)
    stored = json.loads(review_state._round_file(str(repo)).read_text())
    stored["round"] = 3
    stored.pop("last_source", None)  # the single key that triggers the legacy discard
    review_state._round_file(str(repo)).write_text(json.dumps(stored))
    assert review_state.read_gate_demand(str(repo)) is not None


def test_a_corrupt_demand_SELF_HEALS_on_the_next_block(repo, rounds):
    """Repair is what makes all-or-nothing validation affordable.

    A marker that cannot be read costs a menu, and the very next block rewrites it
    from the gate's own canonical set. An earlier revision protected an unreadable
    demand from being overwritten and made corruption permanent instead: the gate
    refused, pointed at a re-open command that reset only part of the record, and the
    next declaration no-op'd on an idempotency check. The kill switch was the only
    way out. Always-overwrite is why that cannot recur.
    """
    _declare(repo)
    stored = json.loads(review_state._round_file(str(repo)).read_text())
    stored["gate_demand"]["remedies"] = [{"key": "x"}]
    review_state._round_file(str(repo)).write_text(json.dumps(stored))
    assert review_state.read_gate_demand(str(repo)) is None, "precondition: unreadable"
    _declare(repo)  # the next block re-declares
    healed = review_state.read_gate_demand(str(repo))
    assert healed is not None
    assert [r["key"] for r in healed["remedies"]] == [r["key"] for r in REMEDIES]


# ─── Regressions from the cross-model reviewer (Codex), round 1 ─────────────
#
# Codex reviewed the head this PR opened with and found six things. THREE of them
# were the same defects the other round-1 reviewer found independently -- the
# `git -C` routing, the unreadable-demand replacement, and the mode-switch consume --
# which is corroboration rather than duplication, and all three were already fixed.
# These two are the ones only Codex saw.


def test_a_demand_declared_while_git_was_DOWN_does_not_vanish(repo, rounds, monkeypatch):
    """`get_current_branch` returns the literal "unknown" on a timeout or OSError, so
    a git hiccup DURING the block persists that as the demand's branch. Once git
    recovers, the branch comparison reads the demand as belonging to somewhere else
    and it silently disappears — taking the newly required user decision with it, and
    letting the next acked attempt through.

    A transient failure must not silently mis-attribute a menu to another branch.
    """
    real_branch = review_state.get_current_branch
    monkeypatch.setattr(review_state, "get_current_branch", lambda cwd=None, **_kw: "unknown")
    _declare(repo)  # declared while git is "down"
    stored = json.loads(review_state._round_file(str(repo)).read_text())
    assert stored["gate_demand"]["branch"] == "unknown", "precondition: it stored the sentinel"

    # Restore ONLY this patch. `monkeypatch.undo()` would also undo the `rounds`
    # fixture's _ROUND_DIR redirection, sending the lookups below at the real home —
    # where there is no demand, so the test would "fail" for a reason that has
    # nothing to do with the code under test.
    monkeypatch.setattr(review_state, "get_current_branch", real_branch)
    assert review_state.read_gate_demand(str(repo)) is None, "unattributable -> unreadable"
    # The exit is the same one every unreadable marker takes: the next block
    # rewrites it, this time with the real branch.
    _declare(repo)
    assert review_state.read_gate_demand(str(repo)) is not None
