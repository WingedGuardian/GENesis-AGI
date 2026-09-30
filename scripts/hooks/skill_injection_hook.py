#!/usr/bin/env python3
"""UserPromptSubmit hook: on-demand skill injection.

Checks prompt keywords against the skill catalog and injects a light
pointer (~30 tokens) for matching skills. Does NOT inject full skill
content — Genesis decides whether to load.

Budget: <50ms (JSON file read + keyword match).
"""
from __future__ import annotations

import functools
import json
import re
import sys
from pathlib import Path

# Skill injection fires for ALL sessions (foreground + dispatched).
# Background sessions need skill nudges just as much — without them,
# dispatched sessions have tools but no knowledge of how to use them.

CATALOG_PATH = Path.home() / ".genesis" / "skill_catalog.json"
_CATALOG_MAX_AGE_S = 3600  # Regenerate catalog if older than 1h

# Minimum raw match score — the skill's FULL name or one explicit-keyword hit
# scores 2 points and is enough to surface the skill. One token of a
# multi-word name scores 1 and is not (see `_score_skill`). Raw points, NOT normalized by prompt keyword count:
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


def _score_skill(
    skill: dict, keywords: list[str], prompt_text: str | None = None
) -> float:
    """Score a skill against prompt keywords. Returns raw match points.

    Scores the CURATED signals only: the skill's WHOLE name (its tokens adjacent
    and in order) or an explicit frontmatter-KEYWORD hit is worth 2 points each;
    loose tokens of a multi-word name are worth 1 IN TOTAL, however many appear. DESCRIPTION prose
    is deliberately NOT scored. Free-text descriptions name many tools in
    passing (an AWS skill's prose mentions "SageMaker", "usage plan",
    "timeout"), so the old substring match over descriptions surfaced skills
    on generic words — "plan" inside "usage plan", "out" inside "timeout" —
    summing two incidental hits to the firing threshold. A term distinctive
    enough to nudge a skill belongs in that skill's `keywords:` frontmatter,
    not mined from prose. Matching is whole-word (token membership), so "aws"
    counts toward the name "aws-lambda" but "awesome" does not. Deliberately NOT
    normalized by prompt length — a long prompt must not dilute a genuine hit
    below the firing threshold.
    """
    if not keywords:
        return 0.0

    # Whole-word name tokens (hyphens/underscores → spaces, then split).
    name_tokens = set(
        skill.get("name", "").lower().replace("-", " ").replace("_", " ").split()
    )
    skill_kws = {kw.lower() for kw in skill.get("keywords", [])}
    kw_words = {kw.lower() for kw in keywords}

    # The WHOLE name in the prompt is a curated signal: it scores like a keyword.
    # Loose tokens of a multi-word name are not — together they score at most
    # 1, below `_MIN_SCORE`, so "closing the loop on this session" does not add
    # up to closing-session. MEASURED 2026-09-30 by replaying 6,803 user prompts
    # from one install's transcripts (compaction summaries and headless-judge
    # prompts excluded): scoring each name token 2 put a catalog nudge on 28.5%
    # of prompts, "session" -> closing-session alone on 344 of them;
    # `/claude-api` fired api-gateway on "api". A token distinctive enough to
    # fire alone belongs in the skill's `keywords:` frontmatter, the same rule
    # the docstring states for description prose.
    #
    # "Whole name" means the tokens appear ADJACENT and IN ORDER, as a phrase
    # (`closing-session`, `closing session`, `closing_session`); a long prompt
    # contains most common words somewhere, so mere presence is not a name. The
    # test reads the prompt TEXT rather than the keyword list, because
    # `_extract_keywords` drops short words and stopwords (`cc`, `use`).
    # A one-word name keeps the keyword-window rule it always had; only a
    # multi-word name needs the phrase search.
    if len(name_tokens) <= 1 or not prompt_text:
        full_name = bool(name_tokens) and name_tokens <= kw_words
    else:
        full_name = _names_skill(skill.get("name", ""), prompt_text)

    matches = 2 if full_name else 0
    partial = False
    for kw in kw_words:
        if kw in skill_kws:
            matches += 2  # Explicit frontmatter-keyword match
        elif kw in name_tokens:
            partial = True  # Loose name token: supporting evidence only
        # Description prose intentionally not scored (see docstring).
    if partial and not full_name:
        matches += 1  # At most 1 in total, so loose tokens never reach the threshold

    return float(matches)


# `[^<]*` rather than `.*?`: a command name holds no `<`, and a lazy scan over
# unclosed tags is quadratic (MEASURED: 37.7s on 280 KB of them).
_COMMAND_TAG = re.compile(r"<command-(?:name|message)>[^<]*</command-(?:name|message)>")
_COMMAND_ARGS_MARKER = re.compile(r"</?command-args>")
# Only a bare command token: `/api/v1/users` is a path, not a command, and keeps
# its words.
_LEADING_SLASH_COMMAND = re.compile(r"\A\s*/[\w:.-]+(?=\s|\Z)")


def _prompt_for_matching(prompt: str) -> str:
    """Drop a slash command's NAME; keep its arguments.

    The user already chose that skill or command by typing it, so its name is
    not evidence of what the task is — `/claude-api prompt audit` split into
    "api" and fired a TDD nudge and an unrelated AWS `api-gateway` skill. The
    arguments ARE the task, so they stay. Handles both the raw form
    (`/name args`) and the tagged form Claude Code records
    (`<command-name>/name</command-name>…<command-args>args</command-args>`).
    """
    if _COMMAND_TAG.search(prompt):
        # Tagged form: the name lives in the tags, so the arguments are kept
        # whole even when they start with a `/` (`<command-args>/api build`).
        return _COMMAND_ARGS_MARKER.sub(" ", _COMMAND_TAG.sub(" ", prompt))
    return _LEADING_SLASH_COMMAND.sub(" ", prompt, count=1)


@functools.lru_cache(maxsize=256)
def _name_pattern(name: str) -> re.Pattern[str] | None:
    tokens = name.lower().replace("-", " ").replace("_", " ").split()
    if not tokens:
        return None
    return re.compile(
        r"(?<![a-z0-9])" + r"[\s_-]+".join(map(re.escape, tokens)) + r"(?![a-z0-9])"
    )


@functools.lru_cache(maxsize=4)
def _lower(text: str) -> str:
    return text.lower()


@functools.lru_cache(maxsize=4)
def _words(text: str) -> frozenset[str]:
    """Every lowercase alphanumeric word in the prompt, computed once per prompt."""
    return frozenset(re.findall(r"[a-z0-9]+", _lower(text)))


def _names_skill(name: str, text: str) -> bool:
    """True when the prompt names the skill as a phrase, in any separator."""
    pattern = _name_pattern(name)
    if pattern is None:
        return False
    # Every name token must be a word of the prompt before the phrase regex
    # runs. The word set is built once per prompt, so a skill whose name is not
    # present costs a set lookup rather than a full-text scan: without this the
    # hook did one scan per multi-word skill (MEASURED: 426 ms on a 195 KB
    # prompt with 67 skills, against ~50 ms with the gate).
    tokens = name.lower().replace("-", " ").replace("_", " ").split()
    if not set(tokens) <= _words(text):
        return False
    return pattern.search(_lower(text)) is not None


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

        text = _prompt_for_matching(prompt)
        keywords = _extract_keywords(text)
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
            score = _score_skill(skill, keywords, text)
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
