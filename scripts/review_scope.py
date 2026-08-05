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

# Reuse ONLY the sibling github-aware docs/config classifier (scripts/ is not a
# package — same sys.path trick the other enforcement hooks use).
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
try:
    from review_enforcement_commit import _is_docs_or_config
except ImportError:  # pragma: no cover - sibling always present in scripts/

    def _is_docs_or_config(path: str) -> bool:
        _, ext = os.path.splitext(path)
        return ext.lower() in {".md", ".rst", ".txt", ".yaml", ".yml", ".toml", ".ini", ".cfg"}


_GIT_TIMEOUT = 10
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
def _git(args: list[str], cwd: str | None) -> str | None:
    """Run a git command; return stdout, or None on ANY error (fail-open)."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            cwd=cwd,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, UnicodeDecodeError):
        # UnicodeDecodeError: text=True strict-decodes stdout; a filename with
        # invalid UTF-8 bytes would otherwise raise it (not an OSError subclass)
        # and crash a standalone `main()` call, violating the fail-open contract.
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _base_ref(cwd: str | None) -> str | None:
    """Resolve the base ref to diff against.

    Primary: ``origin/HEAD`` symbolic ref. But that is UNSET on most fresh
    clones / CI checkouts, so the fallback chain (origin/main → origin/master →
    main → master) is the common path, not a rare one.
    """
    out = _git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd)
    if out and out.strip().startswith("refs/remotes/"):
        return out.strip()[len("refs/remotes/") :]  # e.g. "origin/main"
    for cand in ("origin/main", "origin/master", "main", "master"):
        if _git(["rev-parse", "--verify", "--quiet", cand], cwd) not in (None, ""):
            return cand
    return None


def _merge_base(base: str, cwd: str | None) -> str | None:
    out = _git(["merge-base", base, "HEAD"], cwd)
    if not out or not out.strip():
        return None
    return out.strip()


def _parse_name_status_z(text: str) -> list[dict]:
    """Parse ``git diff -z --name-status -M`` (NUL-delimited) into per-file records.

    -z emits paths VERBATIM (no quoting/escaping), so paths containing tabs or
    newlines parse correctly — unlike the default tab-and-newline format. Tokens:
    ``<status>\\0<path>\\0`` for A/M/D; ``<status>\\0<src>\\0<dst>\\0`` for R/C,
    where the DEST is the current reviewable file.
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
            path = tokens[i + 2]  # dst
            i += 3
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
    base_ref = base or _base_ref(cwd)
    if not base_ref:
        return None
    mb = _merge_base(base_ref, cwd)
    if not mb:
        return None
    name_status = _git(["diff", "-z", "--name-status", "-M", mb], cwd)
    if name_status is None:
        return None

    # Fail-open must be all-or-nothing: if numstat fails after name-status
    # succeeded, a partial manifest would carry a WRONG diff_lines (→ wrong
    # specialist gating) and skip binary exclusion. Return None instead.
    numstat = _git(["diff", "-z", "--numstat", "-M", mb], cwd)
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
            f"Changeset is {manifest.get('diff_lines', 0)} lines (<50) — /review "
            "skips specialists; still cover every file above."
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
