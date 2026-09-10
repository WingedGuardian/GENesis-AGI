"""The adopt-first gate: does the trigger actually fire, and does it stay quiet.

The gate exists because the POLICY already existed three times over and was
inert every time (see the hook's module docstring). So the thing under test is
not "is adopt-first stated" — it is "does the question get ASKED at the two
moments where it is cheap to answer, and does it then shut up."

Two properties carry the design:

* the plan gate BLOCKS, because a plan proposing source files is the last moment
  the answer can still change what gets built;
* the new-file gate is ADVISORY and fires ONCE PER BRANCH, because a nudge that
  fires per-file is one you learn to tune out — and a tuned-out gate is
  indistinguishable from no gate at all, which is exactly the failure being fixed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HOOK = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "adopt_first_gate.py"


def _run(mode: str, payload: dict, home: Path) -> subprocess.CompletedProcess:
    """Invoke the hook exactly as Claude Code does: argv mode, JSON on stdin."""
    body = {"hook_event_name": "PreToolUse", "session_id": "test", **payload}
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        [sys.executable, str(_HOOK), mode],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A throwaway HOME so the real ~/.genesis state is never touched."""
    h = tmp_path / "home"
    (h / ".claude" / "plans").mkdir(parents=True)
    return h


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo — the new-file gate keys its state on worktree + branch."""
    r = tmp_path / "repo"
    (r / "src" / "genesis" / "autonomy").mkdir(parents=True)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
    ):
        subprocess.run(args, cwd=r, check=True, capture_output=True)
    return r


def _plan(home: Path, text: str) -> Path:
    p = home / ".claude" / "plans" / "plan.md"
    p.write_text(text, encoding="utf-8")
    return p


def _plan_payload(path: Path) -> dict:
    """The REAL ExitPlanMode shape, not a convenient one.

    MEASURED across 1,862 live ExitPlanMode payloads on this install:
    `planFilePath` is present in 1862/1862, and `plan` carries the full markdown
    and serializes FIRST. An earlier version of this harness passed only
    ``{"plan": <path>}`` — a shape CC never sends — so the production resolution
    path went unexercised and five real defects sat green under a 15/15 suite.
    Passing both fields is what makes these tests evidence."""
    return {
        "tool_name": "ExitPlanMode",
        "tool_input": {
            "plan": path.read_text(encoding="utf-8"),
            "planFilePath": str(path),
        },
    }


# ── the plan gate ────────────────────────────────────────────────────────────


def test_the_acceptance_bar_replay_the_real_defect(home: Path):
    """THE case this gate is made of, reconstructed.

    2026-09-09: a plan proposing `src/genesis/autonomy/desktop_gate.py` was
    approved with no adopt/build verdict and no search for alternatives, and the
    session then spent a day and ~5,300 reviewed lines building a capability
    that already existed free and open-source. If the gate does not catch this
    exact shape it does not ship, whatever else it passes."""
    p = _plan(
        home,
        "# PR-2 — the approval gate\n\n"
        "Build the gate in `src/genesis/autonomy/desktop_gate.py`, with the\n"
        "classifier in `src/genesis/autonomy/classification.py`.\n",
    )
    r = _run("--plan", _plan_payload(p), home)
    assert r.returncode == 2, r.stdout
    assert "adopt-first" in r.stderr
    assert "desktop_gate.py" in r.stderr, "name the files, so the block is checkable"
    assert "/evaluate" in r.stderr, "the remedy must point at the EXISTING skill"


def test_a_verdict_clears_the_gate(home: Path):
    p = _plan(
        home,
        "# Plan\nAdd `src/genesis/autonomy/desktop_gate.py`.\n\n"
        "## Adopt / Adapt / Build\n"
        "BUILD — cognitive core, no external substitute. Searched: desktop\n"
        "automation, approval gate. Found: none applicable.\n",
    )
    assert _run("--plan", _plan_payload(p), home).returncode == 0


@pytest.mark.parametrize(
    "heading",
    ["## Adopt / Adapt / Build", "## Adopt/Adapt/Build", "### ADOPT vs BUILD", "# adopt - build"],
)
def test_the_heading_spellings_a_writer_will_actually_use(home: Path, heading: str):
    """A gate that only accepts one spelling teaches people to fight the gate."""
    p = _plan(home, f"# Plan\nAdd `src/genesis/x.py`.\n\n{heading}\nADOPT — use the library.\n")
    assert _run("--plan", _plan_payload(p), home).returncode == 0, heading


def test_a_plan_that_only_touches_EXISTING_modules_is_silent(home: Path, repo: Path):
    """Adopt-vs-build is a question about NEW capability. "Fix a bug in
    src/genesis/y.py" is not that question, and firing on it is what would teach
    tune-out.

    MEASURED over 205 real plans in ~/.claude/plans/: triggering on any source
    path fired 135 times (65.9%) — two of every three plans. Restricting to
    paths that do not yet exist drops it to ~20% (41/205 point-in-time; 28/205
    if replayed against today's tree, which is biased low because everything
    that got built now looks pre-existing). The acceptance-bar replay above
    still catches the defect this gate was built for — both were re-run
    together, because a filter that improves its rate by blinding the gate has
    made things worse."""
    existing = repo / "src" / "genesis" / "autonomy" / "already_here.py"
    existing.write_text("x = 1\n", encoding="utf-8")
    p = _plan(home, "# Plan\nFix the bug in `src/genesis/autonomy/already_here.py`.\n")
    payload = {**_plan_payload(p), "cwd": str(repo)}
    assert _run("--plan", payload, home).returncode == 0

    # CONTROL: a NEW module in the same plan still fires, or the refinement has
    # simply blinded the gate rather than sharpened it.
    p2 = _plan(
        home,
        "# Plan\nFix `src/genesis/autonomy/already_here.py` and add\n"
        "`src/genesis/autonomy/brand_new_thing.py`.\n",
    )
    r = _run("--plan", {**_plan_payload(p2), "cwd": str(repo)}, home)
    assert r.returncode == 2
    assert "brand_new_thing.py" in r.stderr
    assert "already_here.py" not in r.stderr, "name only the NEW files"


def test_a_plan_with_no_source_files_is_not_this_gates_business(home: Path):
    """Docs, config and research plans pass untouched. The trigger is a concrete
    source path, deliberately NOT prose like "create" or "new file" — a gate
    that fires on every plan is one that gets acked past reflexively."""
    p = _plan(home, "# Plan\nRewrite the README and update `config/x.yaml`.\n")
    assert _run("--plan", _plan_payload(p), home).returncode == 0


def test_an_empty_verdict_section_does_not_satisfy_the_gate(home: Path):
    """The header alone is not a verdict.

    This works ONLY because the token match is case-SENSITIVE: the heading
    itself reads "Adopt / Adapt / Build", so adding re.IGNORECASE would let the
    heading satisfy its own requirement and every section could be left blank.
    That is the whole reason this test exists."""
    p = _plan(home, "# Plan\nAdd `src/genesis/x.py`.\n\n## Adopt / Adapt / Build\n\n(tbd)\n")
    assert _run("--plan", _plan_payload(p), home).returncode == 2


def test_an_unreadable_plan_never_blocks(home: Path):
    """Fail OPEN. Wedging plan mode over our own inability to find a file would
    be a far worse defect than the one this gate prevents."""
    payload = {"tool_name": "ExitPlanMode", "tool_input": {"plan": "/nonexistent/x.md"}}
    assert _run("--plan", payload, home).returncode == 0


# ── the review's findings, each locked ───────────────────────────────────────


def test_an_unknowable_repo_root_never_blocks(home: Path, tmp_path: Path):
    """THE BLOCKER. An earlier `_repo_root` fell back to `Path(cwd)` when git
    failed or the cwd was outside a repo. Every `src/genesis/*.py` then resolved
    under a directory with no `src/`, read as "does not exist yet", and the gate
    blocked EVERY plan — silently reverting to the 65.9% fire rate the design
    exists to avoid, while the module docstring still promised fail-open.

    Reproduced at the time: the plan "fix a bug in src/genesis/memory/retrieval.py"
    exited 2 from a non-repo cwd and 0 from the repo."""
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    p = _plan(home, "# Plan\nFix a bug in `src/genesis/memory/retrieval.py`.\n")
    r = _run("--plan", {**_plan_payload(p), "cwd": str(outside)}, home)
    assert r.returncode == 0, "unknowable root must fail OPEN, never block"


def test_a_repo_without_a_source_tree_is_also_unknowable(home: Path, tmp_path: Path):
    """A root that resolves but has no `src/genesis` cannot answer "does this
    file exist yet" either, so it gets the same fail-open treatment."""
    bare = tmp_path / "empty-repo"
    bare.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=bare, check=True, capture_output=True)
    p = _plan(home, "# Plan\nAdd `src/genesis/autonomy/thing.py`.\n")
    assert _run("--plan", {**_plan_payload(p), "cwd": str(bare)}, home).returncode == 0


def test_an_all_caps_heading_does_not_satisfy_its_own_section(home: Path, repo: Path):
    """`### ADOPT vs BUILD` + `(tbd)` used to PASS: the heading is itself a
    matching token, and the check scanned the whole document. The section BODY
    is what has to carry the answer."""
    p = _plan(home, "# Plan\nAdd `src/genesis/autonomy/new.py`.\n\n### ADOPT vs BUILD\n\n(tbd)\n")
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 2


def test_a_lowercase_prose_verdict_is_accepted(home: Path, repo: Path):
    """The other direction of the same defect: a genuine verdict written in
    ordinary prose was BLOCKED, which teaches people to fight the gate."""
    p = _plan(
        home,
        "# Plan\nAdd `src/genesis/autonomy/new.py`.\n\n"
        "## Adopt / Adapt / Build\n"
        "We should adopt the upstream library — searched pypi and github, found "
        "two candidates, hours to wire vs a week to write.\n",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0


def test_a_path_inside_a_code_fence_is_quoted_not_proposed(home: Path, repo: Path):
    """Showing an example is not proposing to build it."""
    p = _plan(
        home,
        "# Plan\nUpdate the docs.\n\n```python\n"
        "# e.g. src/genesis/autonomy/example_only.py\n```\n",
    )
    assert _run("--plan", {**_plan_payload(p), "cwd": str(repo)}, home).returncode == 0


def test_the_authoritative_plan_field_wins_over_prose(home: Path, repo: Path):
    """MEASURED: `planFilePath` is present in 1862/1862 real payloads, but `plan`
    (the full markdown) serializes FIRST — so a blob-wide regex could return a
    path quoted in the plan's own prose and gate on a different document."""
    decoy = home / ".claude" / "plans" / "some-other-spec.md"
    decoy.write_text("# Other\nAdd `src/genesis/autonomy/decoy.py`.\n", encoding="utf-8")
    real = _plan(
        home,
        f"# Plan\nSee the parent spec at {decoy} for context.\n"
        "This plan only edits documentation.\n",
    )
    r = _run("--plan", {**_plan_payload(real), "cwd": str(repo)}, home)
    assert r.returncode == 0, "must read planFilePath, not the first path in the prose"


# ── the new-file gate, and the anti-annoyance property ───────────────────────


def test_a_new_module_nudges_once_then_stays_silent(home: Path, repo: Path):
    """The property that keeps this from becoming noise.

    Cost is proportional to how often you START work, never to how much you
    type — so the second, third and hundredth new file on the same branch are
    silent. A per-file nudge would be tuned out within a day, and a tuned-out
    gate is worth exactly nothing."""
    first = repo / "src" / "genesis" / "autonomy" / "brand_new.py"
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(first)}, "cwd": str(repo)}

    r1 = _run("--new-file", payload, home)
    assert r1.returncode == 0, "advisory only — it must never block an edit"
    assert "ADOPT" in r1.stdout
    assert "hookSpecificOutput" in r1.stdout, "PreToolUse advisories reach the model only here"

    second = repo / "src" / "genesis" / "autonomy" / "another_new.py"
    r2 = _run(
        "--new-file",
        {"tool_name": "Write", "tool_input": {"file_path": str(second)}, "cwd": str(repo)},
        home,
    )
    assert r2.returncode == 0
    assert r2.stdout.strip() == "", "second new file on the same branch must be silent"


def test_a_new_branch_gets_its_own_nudge(home: Path, repo: Path):
    """State is keyed per (worktree, branch): starting new work asks again."""
    f = repo / "src" / "genesis" / "autonomy" / "thing.py"
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(f)}, "cwd": str(repo)}
    assert "ADOPT" in _run("--new-file", payload, home).stdout

    subprocess.run(["git", "checkout", "-q", "-b", "feat/other"], cwd=repo, check=True)
    assert "ADOPT" in _run("--new-file", payload, home).stdout, "new branch, new question"


def test_editing_existing_code_is_silent(home: Path, repo: Path):
    """The trigger is CREATION. Ordinary work on code that already exists is
    none of this gate's business."""
    existing = repo / "src" / "genesis" / "autonomy" / "existing.py"
    existing.write_text("x = 1\n", encoding="utf-8")
    r = _run(
        "--new-file",
        {"tool_name": "Edit", "tool_input": {"file_path": str(existing)}, "cwd": str(repo)},
        home,
    )
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_files_outside_the_source_tree_are_silent(home: Path, repo: Path):
    """Tests, docs and scratch files are not new capabilities."""
    for rel in ("tests/test_x.py", "docs/x.md", "scratch.py"):
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        r = _run(
            "--new-file",
            {"tool_name": "Write", "tool_input": {"file_path": str(target)}, "cwd": str(repo)},
            home,
        )
        assert r.stdout.strip() == "", rel


# ── wiring: a hook nobody registered is a hook that does nothing ─────────────


def test_registered_in_settings_json():
    """Both modes must be wired, or the whole exercise is decorative — which is
    precisely the failure mode this gate was built to fix."""
    settings = json.loads(
        (Path(__file__).resolve().parents[2] / ".claude" / "settings.json").read_text()
    )
    pre = settings["hooks"]["PreToolUse"]
    commands = [h.get("command", "") for block in pre for h in block.get("hooks", [])]
    assert any("adopt_first_gate.py --plan" in c for c in commands), "plan gate not wired"
    assert any("adopt_first_gate.py --new-file" in c for c in commands), "new-file gate not wired"

    plan_matchers = [
        block.get("matcher", "")
        for block in pre
        if any("adopt_first_gate.py --plan" in h.get("command", "") for h in block.get("hooks", []))
    ]
    assert any("ExitPlanMode" in m for m in plan_matchers), (
        f"the plan gate must match ExitPlanMode, got {plan_matchers}"
    )
