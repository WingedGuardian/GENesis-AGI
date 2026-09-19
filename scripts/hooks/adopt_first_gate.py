#!/usr/bin/env python3
"""Make "adopt before you build" fire, instead of being stated and ignored.

WHY THIS EXISTS, and why it is a trigger rather than another statement of the
principle. Three artifacts in this install already said "adopt first" and all
three were inert:

  1. CC memory ``adopt_first_speed_is_a_factor`` — conditioned on "when
     recommending adopt vs adapt vs build", a moment that never arrives when the
     work goes from *gap identified* straight to *building*.
  2. ``src/genesis/skills/evaluate/SKILL.md`` — the whole framework, including an
     explicit ban on prose claims like "we already have this" in favour of an
     Overlap Comparison table. Nothing connects a candidate tool to invoking it.
  3. ``CLAUDE.md`` "Flexibility > lock-in" — governs a dependency already taken
     on, not the decision to take one on.

MEASURED 2026-09-09: a session spent a day and ~5,300 reviewed lines building a
desktop-takeover gate through six external review rounds, while a free,
open-source, actively-developed implementation of the entire capability existed
and was never searched for. Three searches were logged, all for teardowns of the
named product; zero for alternatives.

So this file adds no policy. It adds the two moments where the existing policy
gets asked for, and it points at ``/evaluate`` rather than inventing a rival
vocabulary.

TWO MODES, deliberately different strengths:

``--plan`` (PreToolUse: ExitPlanMode) — BLOCKS. A plan proposing new source
files must carry an adopt/adapt/build verdict. This is the cheap moment: the
question costs one line before any effort is spent, and it is the only moment
where the answer can still change what gets built.

``--new-file`` (PreToolUse: Write|Edit) — ADVISORY, and fires ONCE PER BRANCH.
The net for work that skipped plan mode. Advisory because a block here would
land on legitimate cognitive-core work at the worst possible moment, and the
standing design axiom is that advisory is the default while a block needs a
specific measured reason. The per-branch sentinel is the whole anti-annoyance
design: cost is proportional to how often you START work, never to how much you
type, so it cannot become the kind of noise you learn to tune out.

Neither mode ever ASKS the user for approval — both demand WORK (a recorded
verdict), which a background session can satisfy alone. Background stays exactly
as capable as foreground.

Fails OPEN on any internal error: a crash here must not wedge planning or
editing. Nothing in this file is a safety boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Self-locate so hook_input resolves whether run as a script or imported (tests).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import field, read_payload, tool_input  # noqa: E402

_STATE_DIR = Path.home() / ".genesis" / "adopt_first"

#: A plan "proposes new source files" if it names a source path. Deliberately
#: NOT a prose heuristic ("create", "new file") — those fire on any plan that
#: discusses files at all, and a gate that fires on everything is one you learn
#: to ack past. A concrete path is the honest signal that code is coming.
_SOURCE_PATH = re.compile(r"\bsrc/[\w./-]+\.py\b")

#: The verdict header, tolerant of the spellings a writer will actually use:
#: "## Adopt / Adapt / Build", "## Adopt/Adapt/Build", "### ADOPT vs BUILD".
_VERDICT_HEADER = re.compile(
    r"^#{1,6}\s*adopt\s*[/|vs.．\- ]+\s*(adapt|build)", re.IGNORECASE | re.MULTILINE
)

#: The evaluate skill's vocabulary, matched in the section BODY only.
#:
#: An earlier version scanned the whole document and leaned on case-sensitivity
#: to stop the heading satisfying its own requirement. That reasoning was wrong
#: in both directions and both were reproduced: "### ADOPT vs BUILD" followed by
#: "(tbd)" PASSED (the all-caps heading is itself a matching token), while a
#: genuine lower-case prose verdict under "## Adopt / Adapt / Build" was BLOCKED.
#: Slicing the body first is what makes the question well-posed, so this can now
#: be case-insensitive and mean what it says.
_VERDICT_TOKEN = re.compile(r"\b(ADOPT|ADAPT|BUILD|WATCH|IGNORE)\b", re.IGNORECASE)

#: Fenced code blocks are quoted material, not proposals. A plan showing an
#: example snippet that names a path is not proposing to create it.
_FENCE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)

_GATED_PREFIX = "src/genesis/"


# ── state: one record per (worktree, branch) ─────────────────────────────────
def _run(args: list[str], cwd: str | None = None) -> str:
    try:
        out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=5, check=False)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _worktree_key(cwd: str) -> str:
    """sha256-truncated worktree root, mirroring ``review_state._worktree_key``.

    Per-location on purpose. A shared fallback constant is what let concurrent
    sessions clobber each other's state in the review markers (#1244); the same
    trap applies here, so the same fix is used rather than a new one.
    """
    root = _run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if not root:
        probe = Path(cwd).resolve()
        for parent in [probe, *probe.parents]:
            if (parent / ".git").exists():
                root = str(parent)
                break
        else:
            root = str(probe)
    return hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:12]


def _branch(cwd: str) -> str:
    return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd) or "unknown"


def _sentinel(cwd: str) -> Path:
    """One empty file per (worktree, branch). Existence IS the state.

    An empty sentinel rather than a JSON dict, for two reasons. It matches what
    three sibling hooks already do for once-per-X nudges
    (``agent_tool_guidance``, ``stealth_skill_nudge``, ``subsystem_traps_hook``)
    — adding a fifth mechanism to a change whose whole subject is "stop
    reinventing what exists" would have been the funniest possible defect. And
    it is ATOMIC: the previous JSON version did a read-modify-write, and several
    sessions run concurrently on this box, so two starting work at once could
    lose one another's record.

    The branch is hashed rather than slugged because branch names contain "/".
    """
    branch = hashlib.sha256(_branch(cwd).encode()).hexdigest()[:8]
    return _STATE_DIR / f"{_worktree_key(cwd)}-{branch}"


def _already_nudged(cwd: str) -> bool:
    try:
        return _sentinel(cwd).exists()
    except OSError:
        return False


def _mark_nudged(cwd: str) -> None:
    """Best-effort and atomic. Failing to record costs one extra nudge, which is
    strictly better than failing the edit."""
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        # O_EXCL: two concurrent sessions race harmlessly, one wins, neither errors.
        os.close(os.open(_sentinel(cwd), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    except FileExistsError:
        pass
    except OSError:
        pass


# ── plan resolution ──────────────────────────────────────────────────────────
def _plan_path(payload: dict) -> str:
    """The plan file this ExitPlanMode is about.

    Same two-step as ``plan_bookmark_hook._extract_plan_info``: look for an
    explicit path in the payload, else fall back to the most recently modified
    plan. Duplicated rather than imported because that module is a PostToolUse
    hook with its own bookmark side effects, and importing it to reuse thirty
    lines would drag those along.
    """
    # The AUTHORITATIVE field first. MEASURED across 1,862 real ExitPlanMode
    # payloads on this install: `planFilePath` was present in 1862/1862. The
    # blob regex below is a fallback for older shapes — and it is a fallback for
    # a reason: `plan` (the full markdown) serializes BEFORE `planFilePath`, so
    # a first-match scan can return a path quoted inside the plan's own prose
    # and then gate on some entirely different document.
    for key in ("planFilePath", "plan_file_path"):
        direct = tool_input(payload).get(key) or payload.get(key)
        if isinstance(direct, str) and direct.endswith(".md"):
            return direct
    blob = json.dumps(payload)
    match = re.search(r"(/[^\s\"']+\.claude/plans/[^\s\"']+\.md)", blob)
    if match:
        return match.group(1)
    plans = Path.home() / ".claude" / "plans"
    if plans.is_dir():
        try:
            candidates = sorted(plans.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            if candidates:
                return str(candidates[0])
        except OSError:
            pass
    return ""


def _repo_root(payload: dict) -> Path | None:
    """Where to resolve the plan's source paths from, or None if unknowable.

    The plan writes repo-relative paths (`src/genesis/x.py`), so they resolve
    against the repo root, not the process cwd — which for a hook is whatever
    directory the tool call happened to be made from.

    RETURNS None RATHER THAN GUESSING, and the caller then refuses to block.
    An earlier version fell back to ``Path(cwd)``, which looked harmless and was
    the worst defect in this file: under a git timeout, a detached/broken repo,
    or simply a cwd outside the tree, EVERY source path resolves under a
    directory with no `src/`, every one reads as "does not exist yet", and the
    gate blocks every plan it sees. MEASURED: the plan "fix a bug in
    src/genesis/memory/retrieval.py" exited 2 from a non-repo cwd and 0 from the
    repo. That is both a silent reversion to the 65.9% fire rate this design
    exists to avoid, and a direct contradiction of the fail-open invariant this
    module claims in its own docstring.

    The `src/genesis` probe is the load-bearing half: a root that resolves but
    has no source tree cannot answer "does this file exist yet", so it is
    unknowable too.
    """
    cwd = payload.get("cwd") or os.getcwd()
    root = _run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if not root:
        return None
    candidate = Path(root)
    return candidate if (candidate / "src" / "genesis").is_dir() else None


_REMEDY = (
    "Add a section to the plan before presenting it:\n"
    "\n"
    "  ## Adopt / Adapt / Build\n"
    "  <ADOPT|ADAPT|BUILD> — <why>. Searched: <terms>. Found: <candidates, or none>.\n"
    "  Time-to-capability: adopt <hours> vs build <hours>.\n"
    "\n"
    "Use the `evaluate` skill's vocabulary (ADOPT | WATCH | IGNORE | ADAPT) — run\n"
    "`/evaluate` on any candidate rather than inventing a fresh comparison. Its\n"
    'Overlap Comparison table exists specifically to replace the sentence "we\n'
    'already have this", which is the phrasing this gate is here to catch.\n'
    "\n"
    "One line is enough when building really is right:\n"
    "  BUILD — cognitive core, no external substitute. Searched: <terms>.\n"
    "\n"
    'But RUN THE SEARCH first. "Nothing adoptable exists" is a claim that needs a\n'
    "logged search behind it, and the measured failure this gate is made of had\n"
    "three searches logged for the named product and ZERO for alternatives."
)


def _has_verdict(content: str) -> bool:
    """A verdict is a HEADER with a real answer UNDER it.

    The body runs from the end of the header line to the next heading of the
    same-or-shallower depth, which is what makes "the section is empty" and "the
    section says BUILD" distinguishable at all.
    """
    m = _VERDICT_HEADER.search(content)
    if not m:
        return False
    depth = len(m.group(0)) - len(m.group(0).lstrip("#"))
    # From the end of the HEADER LINE, not the end of the regex match. The
    # pattern stops at the first of adapt|build, so "## Adopt / Adapt / Build"
    # leaves "/ Build" unconsumed — and that remnant is itself a matching token,
    # so slicing at m.end() let every heading satisfy its own section.
    line_end = content.find("\n", m.end())
    rest = content[line_end + 1:] if line_end != -1 else ""
    nxt = re.search(rf"^#{{1,{max(depth, 1)}}}\s", rest, re.MULTILINE)
    body = rest[: nxt.start()] if nxt else rest
    return bool(_VERDICT_TOKEN.search(body))


def _check_plan(payload: dict) -> int:
    plan_path = _plan_path(payload)
    if not plan_path:
        return 0  # nothing to read — never block on our own inability to find it
    try:
        content = Path(plan_path).read_text(encoding="utf-8")
    except OSError:
        return 0

    named = sorted(set(_SOURCE_PATH.findall(_FENCE.sub('', content))))
    if not named:
        return 0  # proposes no source files — not this gate's business

    # Only files that do NOT YET EXIST. Adopt-vs-build is a question about NEW
    # capability; "fix a bug in src/genesis/y.py" is not that question.
    #
    # MEASURED against 205 real plans: the path-only trigger fired on 135
    # (65.9%) — two of every three, mostly ordinary fixes to existing modules.
    # A gate firing that often gets acked reflexively, which is the same as no
    # gate at all.
    #
    # The honest rate for THIS filter is ~20%, measured POINT-IN-TIME via
    # `git log --diff-filter=A` — i.e. was the file new *when the plan was
    # written*. Two independent passes with slightly different "new at the time"
    # rules got 41/205 and 42/205, and that one-plan spread is the real
    # precision of the number. Replaying against today's tree instead gives
    # 28/205 (13.7%), which is biased LOW because every proposal that actually
    # got built now scores as "nothing new" — and 13.7% is exactly the figure a
    # reader would naively reproduce and wrongly trust, which is why both are
    # recorded here.
    root = _repo_root(payload)
    if root is None:
        return 0  # cannot tell new from existing — never block on our own blindness
    sources = [s for s in named if not (root / s).exists()]
    if not sources:
        return 0

    if _has_verdict(content):
        return 0

    shown = ", ".join(sources[:4]) + (" …" if len(sources) > 4 else "")
    print(
        "BLOCKED (adopt-first): this plan proposes source files "
        f"({len(sources)}: {shown}) and carries no adopt/adapt/build verdict.\n"
        "\n"
        "This is the cheap moment to ask. The question costs one line here and a\n"
        "day of rework later — MEASURED 2026-09-09: ~5,300 reviewed lines and six\n"
        "external review rounds building a capability that already existed, free\n"
        "and open-source, and was never searched for.\n"
        "\n" + _REMEDY,
        file=sys.stderr,
    )
    return 2


def _check_new_file(payload: dict) -> int:
    raw = field(payload, "file_path")
    if not raw:
        return 0
    try:
        path = Path(raw)
    except (ValueError, OSError):
        return 0

    posix = path.as_posix()
    if _GATED_PREFIX not in posix:
        return 0
    if path.exists():
        return 0  # an edit to existing code, not a new module

    cwd = payload.get("cwd") or os.getcwd()
    if _already_nudged(cwd):
        return 0  # once per branch — this is the whole anti-annoyance design

    _mark_nudged(cwd)
    nudge = (
        f"New module: {posix}\n"
        "Before building it: is there something to adopt? Default order is "
        "ADOPT > ADAPT > build, and the effort belongs in the GLUE around what "
        "already exists. Run `/evaluate` on any candidate; compare user-visible "
        'CAPABILITY, not architectural depth — "ours is more sophisticated" is a '
        "reason to upgrade, never a reason to build.\n"
        "If you already recorded a verdict, ignore this: it fires once per branch, "
        "not once per file."
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": nudge,
                }
            }
        )
    )
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = read_payload()
        if not payload:
            return 0
        if mode == "--plan":
            return _check_plan(payload)
        if mode == "--new-file":
            return _check_new_file(payload)
    except Exception:  # noqa: BLE001 — fail open; this is not a safety boundary
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
