#!/usr/bin/env python3
"""Deterministic per-file review-coverage manifest.

Genesis's ``/review`` dispatches specialist reviewers by aggregate scope
signals, but each specialist independently runs ``git diff`` and self-selects
hunks — nothing enumerates the changed files or guarantees each is covered, so
on a large changeset a file can go unreviewed by all of them. This module emits
a deterministic manifest of the branch changeset (merge-base..working-tree) that
``review_enforcement_prompt.py`` injects into the review reminder, naming every
code file that MUST be covered. It is model-independent and strictly additive:
never blocks, never gates — an annotation on top of the existing reminder.

Design (self-contained ON PURPOSE):
  * NO ``import genesis`` — the enforcement hooks run on the MAIN repo's venv
    even from a worktree (editable install → main's ``src``), and the gstack
    skill lives in ``~/.claude`` (absent on most installs). The only repo reuse
    is the sibling ``_is_docs_or_config`` (github-aware), imported the same way
    the other enforcement hooks import their siblings.
  * ``scope_tag`` is ONE category per file, FIRST-MATCH-WINS, in the exact case
    order of ``~/.claude/skills/gstack/bin/gstack-diff-scope`` (the source of
    truth — kept in sync by ``test_review_scope.py``'s conformance test). A
    multi-tag port would over-set the aggregate specialist gating (e.g.
    ``auth_controller.py`` must resolve to API only, never AUTH).
  * ``category`` (code/test/fixture/docs-config) is a SEPARATE axis from
    ``scope_tag``; the MUST-cover list is the ``code`` files, decided by the
    github-aware ``_is_docs_or_config`` so ``.github/`` workflows stay in-scope.
  * ``diff_lines`` is the UNFILTERED whole-diff line sum (matches the skill's
    ``<50`` specialist-skip gate).
  * Everything fail-opens to ``None`` — ``timeout=10`` on every git call (it
    runs on every user prompt), and any git error yields ``None`` so the hook
    prints its base reminder unchanged.
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys
import time

# Reuse ONLY the sibling github-aware docs/config classifier (scripts/ is not a
# package — same sys.path trick the other enforcement hooks use).
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
try:
    from review_deadline import DeadlineExpired, propagate_deadline_timeout  # noqa: E402
except Exception:  # Reverse-version skew: advisory scope reads must stay available.

    class DeadlineExpired(RuntimeError):
        """Local reverse-skew equivalent of the shared deadline exception."""

    def propagate_deadline_timeout(deadline, error):
        if deadline is not None:
            raise DeadlineExpired(
                "aggregate review-gate deadline expired during subprocess"
            ) from error

try:
    from review_enforcement_commit import _is_docs_or_config, _is_prompt_surface
except ImportError:  # pragma: no cover - sibling always present in scripts/

    def _is_docs_or_config(path: str) -> bool:
        _, ext = os.path.splitext(path)
        return ext.lower() in {".md", ".rst", ".txt", ".yaml", ".yml", ".toml", ".ini", ".cfg"}

    def _is_prompt_surface(path: str) -> bool:
        norm = path.replace("\\", "/")
        return norm.startswith(
            (
                ".claude/agents/",
                ".claude/commands/",
                ".claude/skills/",
                "src/genesis/skills/",
                "src/genesis/identity/",
            )
        ) or (norm.startswith("src/genesis/") and "/prompts/" in norm)


_GIT_TIMEOUT = 10  # per-call ceiling when no deadline is in force
# The UserPromptSubmit hook runs with a 10s timeout. build_manifest bounds its
# TOTAL serial git time under that so it never overruns the hook (which, on a
# block-buffered pipe, could otherwise get killed before the base reminder flushes
# — but the hook flushes first, so the worst case is a missing manifest, never a
# missing reminder). Headroom left for the reminder print + Python startup.
_MANIFEST_GIT_BUDGET = 6.0
_MAX_LISTED_FILES = 50  # LOUD-truncate beyond this; the true total is always printed

# ── scope taxonomy — MIRRORS gstack-diff-scope, first-match-wins ──────────────
# Order is load-bearing: it is the exact `case` order in
# ~/.claude/skills/gstack/bin/gstack-diff-scope. fnmatch's `*` spans `/` just
# like a bash `case` glob, so these patterns match the same paths. If gstack's
# patterns change (e.g. it grew .mjs/.cjs in #1810), update this list AND the
# conformance test that pins the two together.
_SCOPE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    (
        "frontend",
        (
            "*.css",
            "*.scss",
            "*.less",
            "*.sass",
            "*.pcss",
            "*.module.css",
            "*.module.scss",
            "*.tsx",
            "*.jsx",
            "*.vue",
            "*.svelte",
            "*.astro",
            "*.erb",
            "*.haml",
            "*.slim",
            "*.hbs",
            "*.ejs",
            "*.html",
            "tailwind.config.*",
            "postcss.config.*",
            "app/views/*",
            "*/components/*",
            "styles/*",
            "css/*",
            "app/assets/stylesheets/*",
        ),
    ),
    (
        "prompts",
        (
            "*prompt_builder*",
            "*generation_service*",
            "*writer_service*",
            "*designer_service*",
            "*evaluator*",
            "*scorer*",
            "*classifier_service*",
            "*analyzer*",
            "*voice*.rb",
            "*writing*.rb",
            "*prompt*.rb",
            "*token*.rb",
            "app/services/chat_tools/*",
            "app/services/x_thread_tools/*",
            "config/system_prompts/*",
        ),
    ),
    (
        "tests",
        (
            "*.test.*",
            "*.spec.*",
            "*_test.*",
            "*_spec.*",
            "test/*",
            "tests/*",
            "spec/*",
            "__tests__/*",
            "cypress/*",
            "e2e/*",
        ),
    ),
    ("docs", ("*.md",)),
    (
        "config",
        (
            "package.json",
            "package-lock.json",
            "yarn.lock",
            "bun.lock",
            "bun.lockb",
            "Gemfile",
            "Gemfile.lock",
            "*.yml",
            "*.yaml",
            ".github/*",
            "requirements.txt",
            "pyproject.toml",
            "go.mod",
            "Cargo.toml",
            "composer.json",
        ),
    ),
    ("migrations", ("db/migrate/*", "*/migrations/*", "alembic/*", "prisma/migrations/*")),
    (
        "api",
        (
            "*controller*",
            "*route*",
            "*endpoint*",
            "*/api/*",
            "*.graphql",
            "*.gql",
            "openapi.*",
            "swagger.*",
        ),
    ),
    ("auth", ("*auth*", "*session*", "*jwt*", "*oauth*", "*permission*", "*role*")),
    (
        "backend",
        (
            "*.rb",
            "*.py",
            "*.go",
            "*.rs",
            "*.java",
            "*.php",
            "*.ex",
            "*.exs",
            "*.ts",
            "*.js",
            "*.mjs",
            "*.cjs",
            "*.mts",
            "*.cts",
        ),
    ),
]

_TEST_GLOBS = ("*.test.*", "*.spec.*", "*_test.*", "*_spec.*")
_TEST_DIRS = {"test", "tests", "spec", "__tests__", "cypress", "e2e"}

# Vendored / generated / minified / lockfiles — not our code to review. Mirrors
# ocr's `default_path` exclusion class (validated by the 2026-08-05 A-B against
# `ocr review --preview`), plus common generated-artifact shapes. fnmatch `*`
# spans `/`, so the `*/x/*` forms catch nested occurrences.
_VENDOR_GLOBS = (
    "node_modules/*",
    "*/node_modules/*",
    "vendor/*",
    "*/vendor/*",
    "dist/*",
    "*/dist/*",
    "build/*",
    "*/build/*",
    ".venv/*",
    "*/.venv/*",
    "*/site-packages/*",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*.generated.*",
    "generated/*",
    "*/generated/*",
)


def _is_vendored(path: str) -> bool:
    p = path.replace("\\", "/")
    return any(fnmatch.fnmatchcase(p, g) for g in _VENDOR_GLOBS)


def _scope_tag(path: str) -> str:
    """Single gstack scope category for a path (first-match-wins), or "" if none."""
    p = path.replace("\\", "/")
    for tag, patterns in _SCOPE_PATTERNS:
        if any(fnmatch.fnmatchcase(p, pat) for pat in patterns):
            return tag
    return ""


def _category(path: str) -> str:
    """Coverage axis: fixture | test | docs-config | code (independent of scope_tag).

    Test/fixture path membership is checked BEFORE docs/config so a data asset
    UNDER a test tree (``tests/fixtures/case.yaml``, ``tests/config.yml``,
    ``tests/snapshots/out.md``) is classified as a reviewable test/fixture — not
    silently dropped as ``docs-config``. Genuine docs/config (README.md,
    config/app.yaml, pyproject.toml) still excludes via the github-aware sibling
    ``_is_docs_or_config`` (so a ``.github/`` workflow stays code, never docs).
    """
    p = path.replace("\\", "/")
    parts = p.split("/")
    base = parts[-1]
    if "fixtures" in parts:
        return "fixture"
    if any(fnmatch.fnmatchcase(base, g) for g in _TEST_GLOBS):
        return "test"
    if any(d in _TEST_DIRS for d in parts[:-1]):
        return "test"
    if _is_docs_or_config(p):
        return "docs-config"
    return "code"


def _specialists(scope_tags: set[str], diff_lines: int) -> list[str]:
    """Scope-indicated specialist set, BEFORE the skill's adaptive/hit-rate gating.

    Mirrors the gstack skill's gating (SKILL.md Step 4.5): under 50 changed
    lines the skill skips all specialists; otherwise Testing+Maintainability are
    always-on and the rest are conditional on aggregate scope flags.
    """
    if diff_lines < 50:
        return []
    picked = {"testing", "maintainability"}
    if "auth" in scope_tags or ("backend" in scope_tags and diff_lines > 100):
        picked.add("security")
    if "backend" in scope_tags or "frontend" in scope_tags:
        picked.add("performance")
    if "migrations" in scope_tags:
        picked.add("data-migration")
    if "api" in scope_tags:
        picked.add("api-contract")
    if "frontend" in scope_tags:
        picked.add("design")
    return sorted(picked)


# ── git plumbing — fail-open, always timed ────────────────────────────────────
def _git(
    args: list[str],
    cwd: str | None,
    deadline: float | None = None,
    *,
    strict_deadline: bool = False,
) -> str | None:
    """Run a git command; return stdout, or None on an ordinary error.

    ``deadline`` (a ``time.monotonic()`` value) caps the per-call timeout by the
    time remaining in the manifest's total git budget — so several serial calls
    cannot collectively overrun the hook timeout. Past the deadline returns
    ``None`` for advisory callers or raises ``DeadlineExpired`` for a strict
    commit check.
    """
    timeout = _GIT_TIMEOUT
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if strict_deadline:
                raise DeadlineExpired("aggregate review-gate deadline expired")
            return None
        timeout = min(_GIT_TIMEOUT, remaining)
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        if strict_deadline:
            propagate_deadline_timeout(deadline, exc)
        return None
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        # UnicodeDecodeError: text=True strict-decodes stdout; a filename with
        # invalid UTF-8 bytes would otherwise raise it (not an OSError subclass)
        # and crash a standalone `main()` call, violating the fail-open contract.
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _base_ref(cwd: str | None, deadline: float | None = None) -> str | None:
    """Resolve the base ref to diff against.

    Primary: ``origin/HEAD`` symbolic ref. But that is UNSET on most fresh
    clones / CI checkouts, so the fallback chain (origin/main → origin/master →
    main → master) is the common path, not a rare one.
    """
    out = _git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd, deadline)
    if out and out.strip().startswith("refs/remotes/"):
        return out.strip()[len("refs/remotes/") :]  # e.g. "origin/main"
    for cand in ("origin/main", "origin/master", "main", "master"):
        if _git(["rev-parse", "--verify", "--quiet", cand], cwd, deadline) not in (None, ""):
            return cand
    return None


def _merge_base(base: str, cwd: str | None, deadline: float | None = None) -> str | None:
    out = _git(["merge-base", base, "HEAD"], cwd, deadline)
    if not out or not out.strip():
        return None
    return out.strip()


def _parse_name_status_z(text: str) -> list[dict]:
    """Parse ``git diff -z --name-status -M`` (NUL-delimited) into per-file records.

    -z emits paths VERBATIM (no quoting/escaping), so paths containing tabs or
    newlines parse correctly — unlike the default tab-and-newline format. Tokens:
    ``<status>\\0<path>\\0`` for A/M/D; ``<status>\\0<src>\\0<dst>\\0`` for R/C.
    For a rename/copy BOTH sides are emitted as records (mirroring the commit
    gate's ``_staged_files``): a rename FROM reviewable code TO an excluded dest
    (``src/auth.py -> README.md``) would otherwise drop the removed code from
    coverage entirely.
    """
    tokens = text.split("\x00")
    files: list[dict] = []
    i = 0
    n = len(tokens)
    while i < n:
        status = tokens[i]
        if not status:  # trailing empty token after the final NUL
            i += 1
            continue
        ct = status[:1]
        if ct in ("R", "C"):
            if i + 2 >= n:
                break  # malformed tail — stop rather than misparse
            src, dst = tokens[i + 1], tokens[i + 2]
            i += 3
            for p in (src, dst):  # both sides — either may be the reviewable one
                if p:
                    files.append({"path": p, "change_type": ct})
        else:
            if i + 1 >= n:
                break
            path = tokens[i + 1]
            i += 2
            if path:
                files.append({"path": path, "change_type": ct})
    return files


def _parse_numstat_z(text: str) -> tuple[int, set[str]]:
    """Return (total added+deleted across ALL files, set of binary file paths).

    Parses ``git diff -z --numstat -M`` (NUL-delimited, verbatim paths). Record
    shapes: ``<added>\\t<deleted>\\t<path>\\0`` for a normal file, and
    ``<added>\\t<deleted>\\t\\0<src>\\0<dst>\\0`` for a rename/copy (the stats
    token ends with a trailing tab and the two paths follow as separate NUL
    tokens). diff_lines is UNFILTERED (matches the skill's <50 gate); binary rows
    carry ``-`` counts and contribute no lines but ARE collected so the caller can
    exclude them (a binary can't be code-reviewed).
    """
    tokens = text.split("\x00")
    total = 0
    binary: set[str] = set()
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if not tok:
            i += 1
            continue
        parts = tok.split("\t")
        if len(parts) < 3:
            i += 1
            continue
        # A path may itself contain literal tabs (verbatim under -z); rejoin
        # everything past the two count fields so it isn't truncated (else the
        # binary path wouldn't match the -z name-status path and the file would
        # wrongly stay reviewable).
        added, deleted = parts[0], parts[1]
        inline_path = "\t".join(parts[2:])
        if inline_path == "":  # rename/copy: dst is the token after next (src)
            path = tokens[i + 2] if i + 2 < n else ""
            i += 3
        else:
            path = inline_path
            i += 1
        if added == "-" or deleted == "-":  # binary
            if path:
                binary.add(path)
            continue
        try:
            total += int(added) + int(deleted)
        except ValueError:
            continue
    return total, binary


def build_manifest(cwd: str | None = None, base: str | None = None) -> dict | None:
    """Build the review-coverage manifest for the branch changeset, or None.

    Returns ``{base, diff_lines, counts, files:[{path,change_type,category,
    scope_tag}], specialists}``. None on any git failure / no resolvable base /
    no merge-base — the caller then prints its base reminder unchanged.
    """
    # Bound TOTAL serial git time under the 10s hook timeout (the base reminder is
    # already flushed, so an overrun only costs the manifest, never the reminder).
    deadline = time.monotonic() + _MANIFEST_GIT_BUDGET
    base_ref = base or _base_ref(cwd, deadline)
    if not base_ref:
        return None
    mb = _merge_base(base_ref, cwd, deadline)
    if not mb:
        return None
    name_status = _git(["diff", "-z", "--name-status", "-M", mb], cwd, deadline)
    if name_status is None:
        return None

    # Fail-open must be all-or-nothing: if numstat fails after name-status
    # succeeded, a partial manifest would carry a WRONG diff_lines (→ wrong
    # specialist gating) and skip binary exclusion. Return None instead.
    numstat = _git(["diff", "-z", "--numstat", "-M", mb], cwd, deadline)
    if numstat is None:
        return None
    diff_lines, binary_paths = _parse_numstat_z(numstat)

    records = _parse_name_status_z(name_status)
    counts = {
        "code": 0,
        "test": 0,
        "fixture": 0,
        "docs-config": 0,
        "review_required": 0,
        "excluded": 0,
    }
    scope_tags: set[str] = set()
    files: list[dict] = []
    for rec in records:
        path = rec["path"]
        category = _category(path)
        tag = _scope_tag(path)
        counts[category] = counts.get(category, 0) + 1

        # Reviewability is a SEPARATE decision from category. Exclude what can't
        # or shouldn't be code-reviewed, in precedence order: binary (can't) →
        # vendored/generated (not ours) → docs-config (Genesis policy: docs-only
        # commits are review-exempt, matching the commit gate's _is_docs_or_config).
        # Everything else — code, tests, fixtures — MUST be covered.
        if path in binary_paths:
            reason: str | None = "binary"
        elif _is_vendored(path):
            reason = "vendored"
        elif category == "docs-config":
            reason = "docs"
        else:
            reason = None
        review_required = reason is None

        if review_required:
            counts["review_required"] += 1
            if tag:
                scope_tags.add(tag)  # excluded files don't drive specialist gating
        else:
            counts["excluded"] += 1

        files.append(
            {
                "path": path,
                "change_type": rec["change_type"],
                "category": category,
                "scope_tag": tag,
                "review_required": review_required,
                "exclude_reason": reason,
            }
        )

    return {
        "base": base_ref,
        "diff_lines": diff_lines,
        "counts": counts,
        "files": files,
        "specialists": _specialists(scope_tags, diff_lines),
    }


# ── Substantiality classifier (commit-time review-DEPTH gate) ─────────────────
# The depth gate distinguishes a SUBSTANTIAL change (needs an adversarial
# /review-level audit) from a small inline one. It reads the STAGED index
# (``--cached``), NOT build_manifest's merge-base..working-tree basis: the review
# marker is keyed to the staged diff (review_state.get_current_diff_hash hashes
# ``git diff --cached``), so the classifier MUST share that basis or the mark-time
# level and the commit-time re-check could disagree on a byte-identical staged
# diff (level thrashing / spurious blocks). Fail-opens to "unknown" so a git error
# never fabricates a depth requirement — the CI review-depth check is the real
# backstop; this local layer is advisory anti-autopilot friction.
_SUBSTANTIAL_DIFF_LINES = 50
_DOMAIN_SENSITIVE_TAGS = frozenset({"auth", "api", "migrations"})


def _parse_numstat_perfile(text: str) -> tuple[dict[str, int], set[str]]:
    """Parse ``git diff -z --numstat -M`` → ``(per_file_lines, binary_paths)``.

    ``per_file_lines`` maps each path → added+deleted (unlike ``_parse_numstat_z``'s
    total) so the caller can sum only the REVIEWABLE lines. ``binary_paths`` is the
    set of files with ``-`` counts (a binary can't be line-counted OR code-reviewed,
    so the caller excludes them from both line and file counts). For a rename the
    stats attach to the DST only (one key per rename → no double-count). Mirrors
    ``_parse_numstat_z``'s -z record shapes (normal: ``<a>\\t<d>\\t<path>\\0``;
    rename: ``<a>\\t<d>\\t\\0<src>\\0<dst>\\0``).
    """
    tokens = text.split("\x00")
    out: dict[str, int] = {}
    binary: set[str] = set()
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if not tok:
            i += 1
            continue
        parts = tok.split("\t")
        if len(parts) < 3:
            i += 1
            continue
        added, deleted = parts[0], parts[1]
        inline_path = "\t".join(parts[2:])
        if inline_path == "":  # rename/copy: dst is the token after next (src)
            path = tokens[i + 2] if i + 2 < n else ""
            i += 3
        else:
            path = inline_path
            i += 1
        if not path:
            continue
        if added == "-" or deleted == "-":  # binary — not line-countable
            out[path] = 0
            binary.add(path)
            continue
        try:
            out[path] = int(added) + int(deleted)
        except ValueError:
            out[path] = 0
    return out, binary


def _substantiality_level(records: list[dict], per_file: dict[str, int], binary: set[str]) -> str:
    """Pure substantiality predicate over parsed diff records — the SHARED core of
    the staged (--cached) and PR-range (base...HEAD) paths.

    A surface-area × risk model. Substantial when ANY holds over the reviewable files
    (docs-config/binary/vendored excluded): reviewable lines ≥ ``_SUBSTANTIAL_DIFF_LINES``
    (50) OR >1 distinct code file (surface area), OR a domain-sensitive ``scope_tag``
    (auth/api/migrations — risk/importance), OR an executable prompt/agent/skill SURFACE
    (behavior risk — a change to what shapes autonomous LLM behavior). NOT triggered by
    mere newness: a trivial single new file is INLINE (Rule 2 still requires *a* review,
    just not an adversarial audit), but a small change to a critical file (auth/api/
    migration or a prompt surface) IS substantial. An adversarial audit tracks
    consequence, not line count alone.
    """

    def _reviewable(p: str) -> bool:
        return p not in binary and not _is_vendored(p) and _category(p) != "docs-config"

    reviewable_lines = sum(n for p, n in per_file.items() if _reviewable(p))
    # Distinct code files from the numstat MAP — ONE key per rename (dst), so a file
    # move counts once, not twice (name-status emits both src+dst → the double-count
    # bug this replaces). Binary excluded (a binary asset is not code to review).
    code_paths = {p for p in per_file if _reviewable(p) and _category(p) == "code"}
    scope_tags: set[str] = set()
    prompt_surface = False
    for rec in records:  # both rename sides here → domain-sensitivity on either side
        path = rec["path"]
        if not _reviewable(path):
            continue
        tag = _scope_tag(path)
        if tag:
            scope_tags.add(tag)
        if _is_prompt_surface(path):  # behavior-shaping surface, even a small edit
            prompt_surface = True
    substantial = (
        reviewable_lines >= _SUBSTANTIAL_DIFF_LINES
        or len(code_paths) > 1
        or bool(scope_tags & _DOMAIN_SENSITIVE_TAGS)
        or prompt_surface
    )
    return "substantial" if substantial else "inline"


def _classify_diff(
    diff_args: list[str], cwd: str | None, deadline: float | None = None
) -> str:
    """Run the two -z diffs, parse, and classify — or ``"unknown"`` on any git error
    (fail OPEN: no fabricated depth requirement; the CI check is the backstop)."""
    name_status = _git(
        ["diff", *diff_args, "-z", "--name-status", "-M"],
        cwd,
        deadline,
        strict_deadline=deadline is not None,
    )
    numstat = _git(
        ["diff", *diff_args, "-z", "--numstat", "-M"],
        cwd,
        deadline,
        strict_deadline=deadline is not None,
    )
    if name_status is None or numstat is None:
        return "unknown"
    per_file, binary = _parse_numstat_perfile(numstat)
    return _substantiality_level(_parse_name_status_z(name_status), per_file, binary)


# ── review LANE — how much consequence a change carries ─────────────────────
#
# ORTHOGONAL to substantiality. Substantiality asks "is this big enough to need a
# deep review"; the lane asks "how much does it cost to be wrong". A one-line edit
# to an enforcement hook is `inline` and `critical` at once.
#
# ── the lane's OWN path vocabulary ───────────────────────────────────────────
#
# WHY THIS DOES NOT REUSE `_scope_tag` / `_DOMAIN_SENSITIVE_TAGS`, which is the
# obvious economy and was the first two attempts at this lane.
#
# `_SCOPE_PATTERNS` is a Rails-flavoured taxonomy mirrored in from another
# codebase, and it answers a DIFFERENT question: "what kind of file is this, in
# Rails terms". The lane asks "how much does it cost to be wrong here". Building
# the second on the first produced SEVEN distinct defects, every finding this
# change has attracted from either reviewer or from its own author:
#
#   `auth` is 98% "session"      58 of 59 tagged files are CC-session machinery
#   `prompts` is 100% wrong      matches 10 scorers, misses all 84 prompt surfaces
#   `.txt` admits dependency pins  requirements.txt reached the light lane
#   `api` globs miss `api.py`    outreach/api.py defines Flask routes, tagged backend
#   `.markdown`/`.adoc` unreachable  listed, but `_category` calls them code first
#   extensionless stems unreachable  same cause, found a round later
#   `data_migrations` tags four ways  one directory, four different tags
#
# One class, seven faces: a vocabulary that does not describe this repo. Each fix
# patched one word of it, and the next round found the next word. So the lane now
# states its own consequence surfaces explicitly, for THIS repo, and the
# distribution is re-measured rather than assumed (see `classify_lane`).
#
# The narrow, DELIBERATE exception is test/fixture detection below, which stays on
# `_category`: that half keys on `_TEST_GLOBS`/`_TEST_DIRS`, which are ordinary
# naming conventions rather than domain vocabulary, and no finding has ever
# touched it.

#: Directory prefixes whose contents are consequence surfaces. Anchored at a path
#: boundary — a PREFIX, never a substring — so `src/genesis/db/migrations_notes/`
#: does not match `.../migrations/` by accident.
_LANE_CRITICAL_PREFIXES = (
    ".github/",  # can disable a required check
    "src/genesis/db/migrations/",  # schema
    "src/genesis/db/data_migrations/",  # data; MISSED by the inherited glob
    "src/genesis/dashboard/routes/",  # HTTP surface
    "scripts/ci/",  # implementations behind required checks — see below
)

# `.github/**` is critical because a change there can disable a required check.
# The IMPLEMENTATION behind that check disables it just as effectively, and until
# this was added only the YAML layer was protected: MEASURED, all 15 scripts
# `ci.yml` actually invokes — the leak scanner, the private-pattern scan, the
# migration-prefix check, the review-depth gate — took the standard threshold,
# so three unresolved P2s passed in a leak scanner where two would have blocked
# in the workflow that merely calls it. Covered by `scripts/ci/` above plus the
# `check_` basename prefix below; `test_required_check_implementations_are_critical`
# re-derives the invocation list from `ci.yml` so a required check added later
# fails that test until its path is covered here.


# THE LANE CONSULTS NO `_scope_tag` VALUE AT ALL, and the third attempt at this
# is the reason. The history is kept because each attempt looked correct:
#
#   1. Inherit `_DOMAIN_SENSITIVE_TAGS` wholesale -> seven defects, listed above.
#   2. Replace the taxonomy entirely -> silently NARROWED the lane, because the
#      `api` globs (`*controller*`, `*route*`, `*endpoint*`, `*/api/*`) reached
#      HTTP surfaces no explicit rule here named.
#   3. Re-admit `api` + `migrations`, the two the audit measured CLEAN -> BOTH
#      over- and under-classifies. `*route*` is a NAME pattern, so it dragged in
#      7 non-HTTP modules (`routing/router.py` is the LLM router;
#      `reflection/output_router.py` routes reflection output), while `*/api/*`
#      and `*/migrations/*` need a preceding path component, so a ROOT-level
#      `api/` or `migrations/` directory would read `standard`.
#
# The audit behind attempt 3 said `api` was "57 files, clean". That was a COUNT,
# checked by eye; it never asked whether those files were HTTP surfaces, which is
# the same error the `auth` tag was indicted for (98% matching on `*session*`).
#
# MEASURED before deleting, both directions, which is what attempt 3 lacked:
# dropping the tags leaves 58 of 58 route-defining modules in `critical` — zero
# misses, because the prefixes and basenames above already cover them — and
# removes all 7 over-classifications. This repo has no root-level `api/` or
# `migrations/` directory, so that half costs nothing either. The tags were pure
# cost by the time they were measured on both sides.
#
# So: a NAME pattern cannot answer "is this an HTTP surface". A path boundary can,
# and `test_every_route_defining_module_is_critical` /
# `test_no_non_HTTP_module_is_dragged_into_critical` hold both directions of that
# claim against the tracked population rather than against remembered examples.

#: Exact paths and basename shapes that are consequence surfaces wherever they sit.
#: `_blueprint.py` is here rather than in the prefixes because it is a route
#: registrar that lives beside ordinary dashboard code — see the enumeration test.
_LANE_CRITICAL_BASENAMES = frozenset(
    {
        "api.py",
        "auth.py",
        "secrets.py",
        "credentials.py",
        "_blueprint.py",
        # Required-check implementation that matches no prefix below.
        "assemble_changelog.py",
        # HTTP servers under `hosting/`. Named individually rather than fencing
        # the subtree: only 3 of its 17 tracked files define routes, and the
        # broad prefix put CSS, SVG assets, `types.py` and lifecycle adapters in
        # the strictest lane. The list cannot go stale unnoticed —
        # `test_every_route_defining_module_is_critical` enumerates route
        # definitions across the tracked tree and fails if one is uncovered.
        "standalone.py",
        "completions.py",
        "overlay.py",
    }
)

#: Basename PREFIXES, on an executable extension. `.sh` counts as well as `.py`
#: because two required checks are shell (`check_portability.sh`,
#: `check_hook_versions_complete.sh`) — restricting this to `.py` would have left
#: them behind exactly the way the YAML-only boundary left all 15 behind.
#:
#: MEASURED both directions before adding `check_`: the rules cover 15 of 15
#: scripts `ci.yml` invokes with ZERO misses, and match 4 further tracked files
#: that CI does not invoke — `check_cc_running_versions.sh`, `check_hook_versions.sh`,
#: `check_stale_pending.py`, `scripts/ci/cc_pin_parse.py`. Those four are checkers
#: and CI helpers themselves, i.e. the same KIND of surface, which is what makes
#: this a boundary rather than the `*route*` shape that matched the LLM router.
_LANE_CRITICAL_BASENAME_PREFIXES = ("api_", "auth_", "check_")
_LANE_CRITICAL_BASENAME_EXTS = (".py", ".sh")

#: TRUST BOUNDARIES — approval and outbound-action gates. Without this the lane
#: RELAXES review on `autonomy/approval_gate.py`, `email_gate.py` and
#: `cli_policy.py` from two unresolved P2s to four, because nothing else here
#: matches them. That is the one direction this change must never move: the
#: autonomous-CLI approval gate is a standing non-negotiable, and a lane that
#: quietly widens its finding budget is a downgrade wearing a refactor.
#:
#: The asymmetry that decides the shape, and it is why a `_gate.py` suffix is
#: acceptable here where a name pattern was rejected for HTTP surfaces: main runs
#: a FLAT 1.0 threshold, so every file blocks at two P2s today. Over-matching
#: therefore costs NOTHING relative to the status quo — it only withholds a
#: relaxation — while under-matching is a live weakening. MEASURED: the rule takes
#: 15 tracked modules, of which 4 (`version_gate`, `validation_gate`,
#: `question_gate`, `rate_gate`) are flow-control rather than authorization. Those
#: four keep today's bar; they are not made stricter than main, and saying that
#: plainly is the point — this is a deliberate over-match, not an unnoticed one.
_LANE_CRITICAL_BASENAME_SUFFIXES = ("_gate.py",)
_LANE_CRITICAL_TRUST_BASENAMES = frozenset({"approval.py", "cli_policy.py"})

#: Prose. `.txt` is NOT prose on its own — `requirements.txt` and
#: `config/az-pip-constraints.txt` are dependency pins — so it counts only on a
#: known documentation STEM, the same split `git_push_guard._is_doc_path` makes.
#: Unlike the previous version these are matched DIRECTLY rather than behind
#: `_category`, so every spelling listed is actually reachable.
_LANE_PROSE_EXTS = frozenset({".md", ".rst", ".markdown", ".adoc"})
_LANE_PROSE_STEMS = frozenset(
    {"CHANGELOG", "README", "LICENSE", "NOTICE", "COPYING", "AUTHORS", "CONTRIBUTING"}
)
_LANE_PROSE_STEM_EXTS = _LANE_PROSE_EXTS | {".txt", ""}


#: Fixture CORPORA — checked-in sample programs the eval harness loads as DATA.
#: They read like source (`calc/api.py`, `statslib/core.py`) and are not: nothing
#: here runs in production, and `gauntlet.py` loads the tree wholesale.
#:
#: ANCHORED as a directory prefix, never a substring or a `*fixture*` glob. A
#: substring carve-out would exempt every continuation — `fixtures_live/`,
#: `my_fixtures.py` — and this is the one rule in the lane that makes a change
#: LIGHTER, so a loose spelling here is the only way this file can widen a budget
#: by accident.
#:
#: MEASURED: 19 tracked files under this root reached standard (15), critical (1)
#: and light (3). The CRITICAL one was `calc_longhorizon/calc/api.py`, pulled in by
#: this module's OWN `api.py` basename rule — a sample program in the strictest
#: lane, which is the same over-classification shape as the `*route*` glob, this
#: time self-inflicted.
_LANE_FIXTURE_ROOTS = ("src/genesis/eval/gauntlet_fixtures/",)


def _is_lane_fixture_corpus(path: str) -> bool:
    """Is *path* inside a declared fixture corpus? Prefix-anchored (see above)."""
    return path.startswith(_LANE_FIXTURE_ROOTS)


def _is_lane_critical_path(path: str) -> bool:
    """Is this path a consequence surface — somewhere it costs a lot to be wrong?

    PATH BOUNDARIES AND BASENAMES ONLY — no `_scope_tag` value is consulted, for
    the reason set out at the constants above: a NAME pattern cannot answer "is
    this an HTTP surface", and three rounds of findings on this function were all
    that question being asked of one.

    A path matching nothing here is ordinary, which is the safe direction: the
    lane only ever RELAXES a threshold below critical, so a surface nobody thought
    of is reviewed at the standard bar rather than the widest one.

    NOT here, deliberately: destructive capability, external egress and financial
    logic. Each is named in the lane's design as critical and none has a path
    definition anywhere in this repo, so including them would mean inventing three
    taxonomies inside this change. `config/` was briefly in the prefix list for the
    same impulse and is gone for the same reason — config is ORDINARY, which is
    what the instruction files this change also edits already say.
    """
    if _is_lane_fixture_corpus(path):
        return False  # sample programs, not production surface
    if any(path.startswith(prefix) for prefix in _LANE_CRITICAL_PREFIXES):
        return True
    base = os.path.basename(path)
    if base in _LANE_CRITICAL_BASENAMES:
        return True
    if base in _LANE_CRITICAL_TRUST_BASENAMES or base.endswith(
        _LANE_CRITICAL_BASENAME_SUFFIXES
    ):
        return True
    return base.endswith(_LANE_CRITICAL_BASENAME_EXTS) and base.startswith(
        _LANE_CRITICAL_BASENAME_PREFIXES
    )


def _is_lane_light(path: str) -> bool:
    """Prose, tests and fixtures — the material a wider finding budget suits."""
    if _is_lane_fixture_corpus(path):
        return True
    if _category(path) in ("test", "fixture"):
        return True
    base = os.path.basename(path)
    stem, dot, ext = base.rpartition(".")
    ext = f".{ext.lower()}" if dot else ""
    if (stem if dot else base).upper() in _LANE_PROSE_STEMS:
        return ext in _LANE_PROSE_STEM_EXTS
    return ext in _LANE_PROSE_EXTS


def classify_lane(paths: list[str], *, hook_surface: bool) -> str:
    """The consequence lane of a change: ``"critical" | "standard" | "light"``.

    *hook_surface* is passed IN rather than computed here. The authority for that
    membership is ``_is_hook_surface_path`` in ``scripts/hooks/git_push_guard.py``,
    which owns the constant, its AST-diff tests and its two declared mirrors
    (``.github/labeler.yml``, CODEOWNERS). This module must not import the guard —
    it is deliberately dependency-free (see the module docstring) — and a second
    copy of that fence here would be the drift those mirrors exist to prevent.
    This mirrors what ``git_push_guard._classify_post_review_delta`` already does:
    settle the hook surface first, then ask this module.

    FAIL-CLOSED on an unknown scope. An empty *paths* means the caller could not
    read the change (a failed API call reaches us as ``[]``), and a change nobody
    can see is treated as consequential rather than waved through.

    MEASURED over the 40 most recently merged PRs (2026-09-13): critical 35.0%,
    standard 20.0%, light 45.0%. The bar, set before measuring, was that critical
    stay at or under 50% — a lane that calls everything critical decides nothing.

    THE DENOMINATOR MOVES, so re-running this will not reproduce the figure and a
    difference is not evidence of a regression. "The 40 most recently merged" is a
    sliding window: the same probe read 32.5 / 22.5 / 45.0 an hour earlier, and the
    2.5-point shift was entirely two PRs merging INTO the window (one of them
    hook-surface, hence critical) — the classifier returned an identical lane for
    every PR common to both runs. To compare classifiers, hold the PR set fixed and
    diff the per-PR ASSIGNMENTS; comparing two totals taken at different times
    measures the merge queue, not the code.

    Against THE VERSION THIS REPLACES — the committed tag-based classifier, which
    measures 30.0 / 60.0 / 10.0 over the same 40 PRs — TWO movements account for
    the difference, both derived per-PR rather than inferred from the totals:

    * **14 PRs moved standard -> light**, every one a PROMPT SURFACE (``SKILL.md``,
      ``.claude/commands/*.md``, ``src/genesis/skills/**``) — which ``_category``
      calls ``code`` and this vocabulary reads as prose. That is the intended
      reading; rule-docs belong in the widest budget. It is also mostly INERT
      rather than a loosening: 11 of the 14 contain nothing whose findings score
      at all, because every path in them is a ``git_push_guard._is_doc_path`` and
      that gate's ``doc_findings`` mode defaults to ``skip``. The 3 that do change
      behaviour are prose-plus-tests PRs whose TEST findings now clear at 3.0
      instead of 2.0.

      That inertness is a DEFAULT, not a structural bound. ``doc_findings`` is
      read from ``merge_gate`` in ``~/.genesis/config/genesis.yaml``, so an install
      setting ``score`` gets the full 3.0 budget on prompt surfaces — including
      ``src/genesis/identity/`` and the executor/sentinel prompt directories. This
      repo's own rule for this shape says an exemption may cite a bound
      configuration CANNOT change, never a default; so the honest version is that
      the loosening is bounded on THIS install's settings, and that is the case to
      think about before widening the prose set further.
    * **1 PR moved standard -> critical** — #1874, which touches
      ``src/genesis/hosting/standalone.py``, the module serving ``/genesis/login``.
      That is the route-surface gap below arriving on a real merged PR rather than
      only on a constructed one.

    An intermediate, NEVER-COMMITTED draft additionally carried a blanket
    ``config/`` critical prefix and measured 32.5 / 57.5 / 10.0; dropping that
    prefix is what moved its one config PR back to standard. That figure is
    narrative about a draft, not a delta against this diff's base — recorded
    separately because stating it as "the previous measurement" is exactly the
    permanent-record error this docstring is otherwise careful about.

    A DISTRIBUTION IS NOT A COVERAGE PROOF, which is the lesson worth keeping:
    critical sat unchanged across that draft while it had silently stopped
    classifying ``*route*``/``*controller*``/``*endpoint*`` paths as critical.
    Same percentage, different membership; only a constructed test case found it.
    A LATER enumeration found four more the constructed cases also missed — route
    definitions in ``src/genesis/hosting/`` and ``dashboard/_blueprint.py``, one of
    them serving ``/genesis/login`` — which is why the lock for that class is an
    enumeration over every tracked module rather than another example. The
    always-fix floor still blocks a P1 or a CodeRabbit Critical/Major in every
    lane regardless of any of this.
    """
    if not paths:
        return "critical"
    # EVERY consequence AUTHORITY is settled before the vendored heuristic,
    # because `_is_vendored` strips a path from `reviewable` entirely and a
    # stripped path can no longer be judged by anything below. MEASURED, both
    # spellings: `scripts/hooks/generated/x.py` (hook surface) and
    # `.github/generated/ci.yml` (CI config) are each `_is_vendored` via
    # `*/generated/*`, and each returned `light` while these checks sat under
    # the strip. The first was found by review and fixed HERE ONLY; the second
    # was the same class one line away, and a reviewer had to find it too.
    # Anything added below that must outrank a vendor glob belongs in THIS
    # block, not after the strip.
    if hook_surface:
        return "critical"
    # Consequence surfaces are tested against the FULL path list, not the
    # vendor-stripped one, for the reason above: a vendor glob must not be able to
    # hide one. `_is_lane_critical_path` is now the entire critical vocabulary, so
    # nothing is left below the strip that needs to outrank it.
    if any(_is_lane_critical_path(p) for p in paths):
        return "critical"
    reviewable = [p for p in paths if not _is_vendored(p)]
    if not reviewable:
        # Vendored-only: a lockfile refresh or a regenerated bundle.
        return "light"
    if all(_is_lane_light(p) for p in reviewable):
        return "light"
    return "standard"


def classify_change_substantiality(
    cwd: str | None = None, *, deadline: float | None = None
) -> str:
    """Substantiality of the STAGED change (--cached) — for the commit-time depth gate.

    Uses the staged index so it shares the review marker's basis (which hashes
    ``git diff --cached``); a different basis would let the mark-time level and the
    commit-time re-check disagree on a byte-identical staged diff. Returns
    ``"substantial"`` | ``"inline"`` | ``"unknown"``.
    """
    return _classify_diff(["--cached"], cwd, deadline)


def classify_range_substantiality(base: str, cwd: str | None = None) -> str:
    """Substantiality of ``base...HEAD`` — for the CI review-depth check (a PR range,
    not a staged index). Same predicate, different basis. ``"unknown"`` on git error."""
    return _classify_diff([f"{base}...HEAD"], cwd)


def classify_compare_substantiality(files: list[dict] | None) -> str:
    """Substantiality of a GitHub COMPARE-API ``files[]`` list — for the merge
    review-freshness gate (the delta the independent reviewer has NOT seen).

    Reuses the shared :func:`_substantiality_level` predicate over records built from
    the compare payload instead of a local ``git diff`` — the merge hook runs locally
    and the reviewed SHA may not be in the local object store (a past PR head), so the
    delta is fetched remotely (``gh api repos/…/compare/{base}...{head}``) and passed
    here. An empty / ``None`` list means no reviewable delta → ``"inline"``. Never
    touches git; never raises.

    Each compare file record carries ``filename``, ``additions``, ``deletions``,
    ``status`` (added/modified/renamed/…), optional ``previous_filename`` (rename src),
    and ``has_patch`` (False for binary/too-large). Binary detection is best-effort: a
    file with no patch and zero line changes that is not a pure rename is treated as a
    binary asset and excluded (mirrors the ``binary`` exclusion the git-diff path gets
    from ``--numstat`` ``-`` counts).
    """
    records: list[dict] = []
    per_file: dict[str, int] = {}
    binary: set[str] = set()
    for f in files or []:
        if not isinstance(f, dict):
            continue
        path = f.get("filename")
        if not path:
            continue
        untrusted = False
        try:
            lines = int(f.get("additions") or 0) + int(f.get("deletions") or 0)
        except (TypeError, ValueError):
            lines = 0
            untrusted = True  # malformed counts — can't lean "trivial" on them
        per_file[path] = lines
        records.append({"path": path})
        prev = f.get("previous_filename")  # rename source — domain-sensitivity on either side
        if prev:
            records.append({"path": prev})
            # Attribute the rename's line count to the SOURCE too (Codex P2 #1373): a
            # rename FROM code TO an excluded dest (`src/foo.py → docs/foo.md`) would
            # otherwise contribute NOTHING — the docs dest is excluded and the source
            # carried no count — so a 200-line code removal/rewrite classified "inline"
            # and let a stale review pass. ``max`` avoids double-inflation while
            # ensuring the reviewable side (whichever it is) carries the magnitude;
            # _substantiality_level only counts reviewable paths, so an excluded prev
            # is harmless.
            per_file[prev] = max(per_file.get(prev, 0), lines)
        has_patch = f.get("has_patch")
        if has_patch is None:
            has_patch = "patch" in f
        if not has_patch and lines == 0 and f.get("status") not in ("renamed", "copied"):
            # No patch + no counted lines: for an untagged asset (image, archive —
            # ``_scope_tag`` maps no pattern, and note ``_category`` calls every
            # non-docs/test path "code", so category alone cannot tell a .png from
            # a .py) that is the binary shape — exclude like the git-diff path
            # does. But a SOURCE-shaped file (a recognized scope tag: backend/
            # frontend/api/…) in that shape is an over-limit/suppressed text diff:
            # its content is unverifiable, and "trivial" needs positive evidence.
            if _category(path) == "code" and _scope_tag(path):
                untrusted = True
            else:
                binary.add(path)
        if untrusted and not _is_vendored(path) and _category(path) == "code" and _scope_tag(path):
            return "substantial"  # unverifiable source delta — fail toward review
    return _substantiality_level(records, per_file, binary)


def render_reminder_block(manifest: dict | None) -> str:
    """Human-readable manifest for the review reminder, or "" when nothing to add.

    Returns "" for a missing manifest or a changeset with no reviewable files
    (docs/binary/vendored-only) — the hook then prints only its base reminder.
    Lists the reviewable set (code + tests + fixtures), and accounts for excluded
    files with a count so nothing is silently dropped.
    """
    if not manifest:
        return ""
    reviewable = [f for f in manifest.get("files", []) if f.get("review_required")]
    if not reviewable:
        return ""

    total = len(reviewable)
    lines = [
        f"DETERMINISTIC REVIEW SCOPE — these {total} file(s) MUST each be covered "
        f"by the review (branch changeset vs {manifest.get('base', '?')}):"
    ]
    for f in reviewable[:_MAX_LISTED_FILES]:
        tag = f.get("scope_tag") or f.get("category") or f.get("change_type", "?")
        lines.append(f"  - {f['path']} [{tag}]")
    if total > _MAX_LISTED_FILES:
        lines.append(f"  … and {total - _MAX_LISTED_FILES} more ({total} files total)")

    excluded = manifest.get("counts", {}).get("excluded", 0)
    if excluded:
        lines.append(
            f"  ({excluded} other changed file(s) excluded — docs/binary/vendored, "
            "not reviewable code)"
        )

    specialists = manifest.get("specialists") or []
    if specialists:
        lines.append(
            "Scope-indicated specialists (BEFORE the skill's adaptive/hit-rate "
            "gating; treat as input, not a verdict): " + ", ".join(specialists) + "."
        )
    else:
        lines.append(
            f"Changeset is {manifest.get('diff_lines', 0)} lines (<50) — "
            "specialists are skipped; still cover every file above."
        )
    return "\n".join(lines)


def main() -> None:
    as_json = "--json" in sys.argv
    base = None
    if "--base" in sys.argv:
        i = sys.argv.index("--base")
        if i + 1 < len(sys.argv):
            base = sys.argv[i + 1]
    manifest = build_manifest(base=base)
    if as_json:
        print(json.dumps(manifest, indent=2))
        return
    if manifest is None:
        print("No review manifest (no resolvable base / merge-base / not a git repo).")
        return
    block = render_reminder_block(manifest)
    print(block or f"No reviewable files in changeset ({manifest['counts']}).")


if __name__ == "__main__":
    main()
