#!/usr/bin/env python3
"""UserPromptSubmit hook: on-demand skill injection.

Checks prompt keywords against the skill catalog and injects a light
pointer (~30 tokens) for matching skills. Does NOT inject full skill
content — Genesis decides whether to load.

Budget: <50ms (JSON file read + keyword match).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Skill injection fires for ALL sessions (foreground + dispatched).
# Background sessions need skill nudges just as much — without them,
# dispatched sessions have tools but no knowledge of how to use them.

CATALOG_PATH = Path.home() / ".genesis" / "skill_catalog.json"
_CATALOG_MAX_AGE_S = 3600  # Regenerate catalog if older than 1h

# Minimum raw match score — a single name or explicit-keyword hit scores
# 2 points and is enough to surface the skill. A lone description hit
# (1 point) is not. Raw points, NOT normalized by prompt keyword count:
# normalization made keyword-rich prompts dilute genuine hits below the
# threshold, so catalog nudges near-never fired.
_MIN_SCORE = 2
# Catalog nudge budget per prompt. Process (superpowers) nudges have their
# own separate budget and never compete for these slots.
_MAX_CATALOG_NUDGES = 2

#: Hard ceiling on ONE rendered nudge line. The bound belongs on the LINE, not on
#: the identifiers inside it -- an earlier revision sliced `name` to 80 and `path`
#: to 120, which kept the cap claim true by emitting a `/skill` argument and a
#: `Read <path>/SKILL.md` that pointed nowhere. A truncated label is a cosmetic
#: loss; a truncated IDENTIFIER is a wrong instruction, and this hook exists to
#: hand the model something it can act on. So: identifiers are emitted WHOLE or
#: not at all, and the line degrades through forms that stay correct.
#: _MAX_CATALOG_NUDGES * _MAX_NUDGE_LINE is the structural ceiling the
#: hook-output contract test's exemption cites.
_MAX_NUDGE_LINE = 400

#: Words that never score as skill-NAME tokens. Whole-word name scoring stays
#: (so "aws" still reaches "aws-lambda"), but a name built from an everyday
#: word fired on every prompt that used the word. MEASURED 2026-09-29 by
#: replaying one live install's last 30 days of foreground prompts (2,346
#: prompts across 385 sessions) through the old and new scorer: catalog nudges
#: fell 570 -> 391, with user_evaluate 58 -> 1, genesis-voice 34 -> 2,
#: code-intelligence 18 -> 1, hyperpod-issue-report 18 -> 0, and every
#: vendor-library skill but aws-lambda to 0 (with the gate below), while
#: closing-session held at 191 and genesis-development at 81 -> 83. Two skills
#: whose NAME was all stoplist words (genesis-development's "genesis",
#: cc-update's "update") declare `keywords:` instead. An explicit frontmatter
#: `keywords:` entry still scores
#: even when it is on this list — a skill that really wants one of these
#: words says so there. ("use" is absent: the prompt extractor already drops
#: it.) Inside an OPEN vendor gate the stoplist does not apply — see below.
_NAME_TOKEN_STOPLIST = frozenset({
    "genesis", "user", "issue", "report", "content", "code", "browser",
    "deploy", "update", "plan", "planning", "model", "document", "service",
    "api", "case", "specification",
})

#: Vendor namespace gate. A skill filed in a vendor plugin bundle of the skill
#: library (``<library>/<vendor>/<bundle>/skills/<skill>``, the layout the
#: catalog generator documents) only scores when the prompt also names that
#: vendor: every word of the folder name, or one of the aliases below. Vendor
#: bundles ship dozens of skills with generic names (planning, deploy,
#: model-evaluation), so without the gate an unrelated prompt reaches them.
#: Once the vendor IS named the gate has already supplied the precision, so the
#: name-token stoplist is skipped there — "aws planning" reaches `planning`.
#: The vendor is derived from the catalog PATH, never hardcoded; a newly
#: installed bundle is gated by its own folder name with no alias entry, and a
#: folder name with no word the prompt extractor can emit (e.g. two-letter
#: words only) and no alias is left ungated rather than silently muted.
#: The vendor word and a name word must both fall within the extractor's
#: first 12 significant words.
_SKILL_LIBRARY_DIR = Path.home() / ".genesis" / "skill-library"
_VENDOR_ALIASES: dict[str, frozenset[str]] = {
    # Only words nobody types outside this vendor's context. "lambda" and
    # "amazon" are deliberately absent: an open gate skips the stoplist, so an
    # ordinary Python "lambda" would re-open every everyday-word misfire.
    # Cost, accepted: "lambda" alone no longer reaches aws-lambda; "aws
    # lambda" does.
    "aws": frozenset({"aws", "sagemaker", "bedrock", "hyperpod"}),
}

# --- Process Discipline Detection ---
# Superpowers skills aren't in the Genesis catalog but need nudges
# when their workflow context is detected.

# Intent: "about to plan or build non-trivial work"
_PLAN_INTENT_KEYWORDS = {
    "plan", "implement", "build", "feature", "architect", "design",
    "refactor", "redesign", "rewrite", "migrate", "integrate",
    "overhaul", "consolidate", "extract", "split", "decouple", "scaffold",
}

# Intent: "about to write code that should be test-driven"
_CODE_INTENT_KEYWORDS = {
    "implement", "build", "add", "create", "fix", "feature",
    "endpoint", "handler", "function", "class", "module",
    "refactor", "wire", "connect",
    "api", "route", "service", "hook", "guard", "plugin", "migration", "schema",
}

# These don't need TDD/brainstorming nudges
_EXCLUDE_KEYWORDS = {
    "docs", "documentation", "readme", "config", "yaml", "markdown",
    "comment", "typo", "rename", "changelog", "version", "memory",
    "evaluate", "research", "look", "check", "review", "read",
    "status", "list", "show", "explain", "describe", "summarize", "analyze",
}


def _ensure_catalog_fresh() -> None:
    """Spawn a detached catalog regeneration if it's missing or stale (>1h).

    Regeneration is out-of-band: the spawn is fire-and-forget and the
    current prompt uses the stale catalog, so the hook's 500ms timeout can
    never kill nudge output while waiting on the generator. The refreshed
    catalog serves the next prompt.
    """
    try:
        if CATALOG_PATH.exists():
            import time
            age = time.time() - CATALOG_PATH.stat().st_mtime
            if age < _CATALOG_MAX_AGE_S:
                return
        # Locate the generator and spawn it detached
        gen_script = Path(__file__).resolve().parents[1] / "generate_skill_catalog.py"
        if gen_script.exists():
            import subprocess
            subprocess.Popen(
                [sys.executable, str(gen_script)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception as exc:
        # Never block prompt, but emit diagnostics
        print(f"Catalog refresh spawn failed: {exc}", file=sys.stderr)


def _load_catalog() -> dict:
    """Load the skill catalog from disk."""
    if not CATALOG_PATH.exists():
        return {"tier1": [], "tier2": []}
    try:
        return json.loads(CATALOG_PATH.read_text())
    except Exception:
        return {"tier1": [], "tier2": []}


def _session_nudges_path(session_id: str) -> Path | None:
    """Return the path for session nudge tracking, or None if invalid."""
    if not session_id or "/" in session_id or ".." in session_id:
        return None
    return Path.home() / ".genesis" / "sessions" / session_id / "skill_nudges.json"


def _load_session_nudges(session_id: str) -> set[str]:
    """Load which skills have already been nudged this session."""
    path = _session_nudges_path(session_id)
    if not path or not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()))
    except Exception:
        return set()


def _save_session_nudge(session_id: str, skill_name: str) -> None:
    """Record that a skill was nudged in this session."""
    path = _session_nudges_path(session_id)
    if not path:
        return
    existing = _load_session_nudges(session_id)
    existing.add(skill_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(existing)))


def _vendor_of(skill: dict) -> str | None:
    """Return the vendor a library skill's plugin bundle belongs to, else None.

    Only the vendor-bundle layout the catalog generator documents is gated:
    ``<library>/<vendor>/<bundle>/skills/<skill>`` → ``<vendor>``. A skill
    directly in the library (``<library>/<skill>``), one in a plain grouping
    folder (``<library>/writing/<skill>``), and repo skills (relative paths
    outside the library) have no vendor. A malformed path value is treated as
    no vendor rather than aborting the scoring loop.
    """
    path = skill.get("path")
    if not isinstance(path, str) or not path:
        return None
    try:
        rel = Path(path).relative_to(_SKILL_LIBRARY_DIR)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) < 4 or parts[-2] != "skills":
        return None
    return parts[0].lower()


def _score_skill(skill: dict, keywords: list[str]) -> float:
    """Score a skill against prompt keywords. Returns raw match points.

    Scores the CURATED signals only: a whole-word skill-NAME token hit or an
    explicit frontmatter-KEYWORD hit is worth 2 points each. DESCRIPTION prose
    is deliberately NOT scored. Free-text descriptions name many tools in
    passing (an AWS skill's prose mentions "SageMaker", "usage plan",
    "timeout"), so the old substring match over descriptions surfaced skills
    on generic words — "plan" inside "usage plan", "out" inside "timeout" —
    summing two incidental hits to the firing threshold. A term distinctive
    enough to nudge a skill belongs in that skill's `keywords:` frontmatter,
    not mined from prose. Matching is whole-word (token membership), so "aws"
    matches the name "aws-lambda" but not "awesome". Deliberately NOT
    normalized by prompt length — a long prompt must not dilute a genuine hit
    below the firing threshold.

    Two precision rules sit on top of that:

    - NAME-token stoplist (`_NAME_TOKEN_STOPLIST`): an everyday word such as
      "user", "genesis" or "plan" never scores as a NAME token, because a
      skill named after one ("user_evaluate", "genesis-voice", "planning")
      otherwise fires on any prompt using it. It removes the word from the
      name only — an explicit frontmatter keyword still scores, stoplisted or
      not.
    - Vendor namespace gate (`_vendor_of`, `_VENDOR_ALIASES`): a skill in a
      vendor plugin bundle of the skill library scores 0 unless the prompt
      also names that vendor (every word of the folder name, or an alias).
      Frontmatter keywords do not open the gate. Once the gate is OPEN the
      stoplist is skipped, because the vendor word already supplied the
      precision: "sagemaker planning" reaches the bundle's `planning` skill,
      while "planning the report" reaches no vendor skill at all.
    """
    if not keywords:
        return 0.0

    kw_set = {kw.lower() for kw in keywords}
    stoplist = _NAME_TOKEN_STOPLIST
    vendor = _vendor_of(skill)
    if vendor is not None:
        # Tokenize the folder name exactly as the prompt is tokenized, so a
        # word the prompt side can never emit (punctuation, <3 chars, the
        # extractor's stop words) cannot keep the gate shut forever. The folder
        # counts when EVERY such word is in the prompt ("google-cloud" needs
        # both). A folder yielding no word and having no alias stays ungated.
        folder_words = set(_extract_keywords(vendor))
        aliases = _VENDOR_ALIASES.get(vendor, frozenset())
        if folder_words or aliases:
            named = bool(folder_words) and folder_words <= kw_set
            if not named and not kw_set & aliases:
                return 0.0
            stoplist = frozenset()

    # Whole-word name tokens (hyphens/underscores → spaces, then split),
    # minus the everyday words that never identify a skill.
    name_tokens = set(
        skill.get("name", "").lower().replace("-", " ").replace("_", " ").split()
    ) - stoplist
    skill_kws = {kw.lower() for kw in skill.get("keywords", [])}

    matches = 0
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower in name_tokens:
            matches += 2  # Whole-word name-token match
        elif kw_lower in skill_kws:
            matches += 2  # Explicit frontmatter-keyword match
        # Description prose intentionally not scored (see docstring).

    return float(matches)


def _extract_keywords(prompt: str) -> list[str]:
    """Extract significant keywords from prompt (minimal, no deps)."""
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in prompt)
    words = cleaned.lower().split()
    stop = {
        "the", "is", "are", "was", "and", "or", "but", "for", "with",
        "this", "that", "can", "you", "how", "what", "when", "where",
        "why", "not", "let", "lets", "use", "need", "want", "please", "just",
        "some", "make", "get", "also", "then", "into", "them", "out",
    }
    return [w for w in words if len(w) >= 3 and w not in stop][:12]


def _check_process_discipline(
    keywords: list[str], already_nudged: set[str], session_id: str
) -> list[str]:
    """Detect when process discipline skills should be nudged.

    Returns list of nudge strings to emit (may be empty).
    Checks for plan-intent (brainstorming) and code-intent (TDD).
    """
    nudges: list[str] = []
    kw_set = set(keywords)

    # Skip process nudges only when the prompt is PURELY non-code work
    # (exclude keywords present but NO code/plan intent keywords)
    has_code_intent = bool(kw_set & _CODE_INTENT_KEYWORDS)
    has_plan_intent = bool(kw_set & _PLAN_INTENT_KEYWORDS)
    has_exclude = bool(kw_set & _EXCLUDE_KEYWORDS)
    if has_exclude and not has_code_intent and not has_plan_intent:
        return nudges

    # --- Brainstorming nudge ---
    # When plan/build intent detected and brainstorming hasn't been used
    if (
        kw_set & _PLAN_INTENT_KEYWORDS
        and "superpowers:brainstorming" not in already_nudged
    ):
        nudges.append(
            "[Process] Non-trivial work detected. Consider requirements "
            "gathering before planning — use superpowers:brainstorming to "
            "structure design decisions and identify unknowns before entering "
            "plan mode. Vertical slices > horizontal layers."
        )
        _save_session_nudge(session_id, "superpowers:brainstorming")

    # --- TDD nudge ---
    # When code-modification intent detected for features/bugfixes
    if (
        kw_set & _CODE_INTENT_KEYWORDS
        and "superpowers:test-driven-development" not in already_nudged
    ):
        nudges.append(
            "[Process] Code work detected. TDD applies for features and "
            "bug fixes: write a failing test FIRST, then implement. Use "
            "superpowers:test-driven-development. Skip for docs, config, "
            "or refactoring with existing test coverage."
        )
        _save_session_nudge(session_id, "superpowers:test-driven-development")

    return nudges


def main() -> None:
    """Hook entry point."""
    try:
        _ensure_catalog_fresh()

        raw = sys.stdin.read()
        if not raw.strip():
            return
        data = json.loads(raw)
        prompt = data.get("prompt", "")
        session_id = data.get("session_id", "")

        if not prompt:
            return

        catalog = _load_catalog()

        keywords = _extract_keywords(prompt)
        if not keywords:
            return

        already_nudged = _load_session_nudges(session_id)

        # --- Process discipline nudges (superpowers) ---
        process_nudges = _check_process_discipline(
            keywords, already_nudged, session_id
        )
        for nudge in process_nudges:
            print(nudge)

        # --- Genesis skill catalog nudges ---
        if not catalog.get("tier1") and not catalog.get("tier2"):
            sys.stdout.flush()
            return

        # Score all skills
        candidates = []
        for skill in catalog.get("tier1", []) + catalog.get("tier2", []):
            name = skill.get("name", "")
            if name in already_nudged:
                continue
            score = _score_skill(skill, keywords)
            if score >= _MIN_SCORE:
                candidates.append((score, skill))

        candidates.sort(key=lambda x: x[0], reverse=True)

        # Catalog budget is independent of process nudges — process nudges
        # keep their own slots and never crowd out skill suggestions.
        for _score, skill in candidates[:_MAX_CATALOG_NUDGES]:
            # `name` stays WHOLE. It is this skill's identity in three places:
            # the `already_nudged` membership test above, the state written by
            # _save_session_nudge below, and the `/skill` argument the model is
            # told to type. Slicing it to 80 broke all three -- a name longer
            # than 80 was saved truncated and so never matched the full name on
            # the next prompt, re-nudging the same skill forever.
            #
            # There is deliberately NO display copy. A first fix kept `label =
            # name[:80]` for the quoted part and an adversarial audit showed the
            # tier-1 branch, which has no path or /skill fallback, then emitted a
            # CUT name as the only identifier on its line -- the same defect,
            # surviving in the one branch that was not part of the ladder. The
            # name a tier-1 nudge quotes IS how the model invokes that skill, so
            # it is an identifier too, not decoration. Every branch below emits
            # `name` whole, and length is handled by degrading the LINE.
            name = skill.get("name", "")
            tier = skill.get("tier", "?")
            desc = skill.get("description", "")

            # Degrade through forms that stay CORRECT rather than truncating the
            # identifier: the tier's own form, else the /skill command if IT
            # fits, else a path-only Read that drops the NAME but keeps the PATH,
            # else a form carrying no identifier at all. `_MAX_NUDGE_LINE` bounds
            # each, and `named` records whether an identifier actually survived.
            # Keep this list in step with the rungs below -- an audit caught it
            # naming three when the code had four, in the very commit that added
            # the fourth.
            named = True
            path = skill.get("path")
            if tier == 1:
                line = f"[Skill] The '{name}' skill is relevant here. {desc[:80]}"
            elif path:
                line = (
                    f"[Skill] The '{name}' skill matches this task. "
                    f"Read {path}/SKILL.md. {desc[:60]}"
                )
            else:
                line = (
                    f"[Skill] The '{name}' skill matches this task. "
                    f"Load with /skill {name}. {desc[:60]}"
                )
            if len(line) > _MAX_NUDGE_LINE:
                # /skill IS A ROUTE ONLY FOR TIER 1. Tier-1 skills are always
                # indexed, so the command resolves. Tier-2 skills live under
                # src/genesis/skills/ and ~/.genesis/skill-library/, are NOT in
                # the listing, and must be READ by path (CLAUDE.md "Skill
                # Library"; .claude/commands/list-skills.md). The previous ladder
                # tried /skill first for EVERY tier on the reasoning that it
                # "carries both the name and an invocation, so it is strictly
                # more informative" -- true, and irrelevant when the invocation
                # does not resolve. For a tier-2 entry it replaced a WORKING Read
                # with a command that fails, then recorded the nudge as delivered
                # (`named` stayed True), suppressing the retry for the session.
                # A more informative line that cannot be acted on is worth less
                # than a plainer one that can.
                #
                # There is no tier-1 rung here, and that is the sharper half.
                # MEASURED 2026-09-09 against the live cap of 400: the tier-1
                # base form embeds no path, so it overflows only on a name past
                # ~276 chars -- and at every such length `skill_form`, which
                # repeats the name, is ALREADY over (name 275 -> base 394 fits;
                # name 300 -> base 419, skill_form 719). No name length exists
                # where the tier-1 form overflows and /skill fits. So the /skill
                # rung was only ever REACHABLE for tier 2, the one tier it is
                # wrong for. Its justifying measurement ("a 1-char name with a
                # 284-char path overflows Read at 401 while /skill is 121") was
                # taken on a tier-2 fixture and generalised to a tier that cannot
                # reach it.
                skill_form = (
                    f"[Skill] The '{name}' skill matches this task. "
                    f"Load with /skill {name}. {desc[:60]}"
                )
                # It is the NAME that overflows, not the path. Drop the name and
                # keep the path: a path is an actionable identifier on its own,
                # and a Read that works beats a notice naming nothing.
                path_form = (
                    f"[Skill] A skill matches this task. "
                    f"Read {path}/SKILL.md. {desc[:60]}"
                ) if path else None
                if path_form is not None and len(path_form) <= _MAX_NUDGE_LINE:
                    line = path_form
                elif path is None and len(skill_form) <= _MAX_NUDGE_LINE:
                    # No path to offer at all. MEASURED 2026-09-09: every one of
                    # the 65 live catalog entries (18 tier-1, 47 tier-2) carries
                    # a path, so this rung is defensive rather than live. /skill
                    # at least names the skill; for tier 2 it may not resolve,
                    # which is why it ranks BELOW the path rung, never above it.
                    line = skill_form
                # A tier-2 entry whose PATH does not fit deliberately falls
                # through to the unnamed notice below rather than to /skill:
                # unnamed is not recorded, so the skill can be nudged again,
                # whereas a non-actionable /skill would burn the one chance.
            if len(line) > _MAX_NUDGE_LINE:
                # The identifier itself is pathological. Say so rather than emit
                # a cut one: a wrong path costs a failed Read, and a cut /skill
                # argument costs a failed command.
                named = False
                # Name WHICH identifier overflowed. Reporting len(name) when it
                # was the PATH that did not fit reads as nonsense -- a tier-2
                # skill with a 3-char name and a 400-char path announced itself
                # as "too long to print (3 chars)".
                overflow = (
                    f"{len(path)}-char path" if path else f"{len(name)}-char name"
                )
                line = (
                    f"[Skill] A matching skill cannot be named in one line "
                    f"({overflow}); see the skill catalog."
                )
            print(line)

            # Only record a nudge the model can act on. Recording the unnamed
            # fallback would suppress this skill for the rest of the session on
            # the strength of a line that never told anyone which skill it was.
            if named:
                _save_session_nudge(session_id, name)

        sys.stdout.flush()
    except Exception:
        import traceback

        print(traceback.format_exc(), file=sys.stderr)


if __name__ == "__main__":
    main()
