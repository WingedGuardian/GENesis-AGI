"""Tests for scripts/review_scope.py — the deterministic review-coverage manifest.

The manifest enumerates the branch changeset (merge-base..working-tree) so the
review reminder can name every code file that MUST be covered — closing the gap
where /review specialists each self-select hunks and a file can go unreviewed.

Invariants under test (architect-reviewed 2026-08-05):
  * scope_tag is ONE category per file, FIRST-MATCH-WINS in gstack's case order
    (a multi-tag port would over-set aggregate specialist gating);
  * category (code/test/fixture/docs-config) is a SEPARATE axis via the
    github-aware `_is_docs_or_config`, so `.github/` workflows stay in-scope;
  * diff_lines is the UNFILTERED whole-diff sum (matches the skill's <50 gate);
  * everything fail-opens to None (never crashes/blocks the enforcement hook);
  * the hook prints its base reminder UNCONDITIONALLY, manifest strictly additive.

All git fixtures are synthetic tmp_path repos — no dependence on the live repo,
network, or gh auth (install-agnostic).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import private_module

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_SCRIPT_PATH = _SCRIPTS / "review_scope.py"
# Via conftest so the SHARED name is restored afterwards. `review_scope` is
# imported at CALL time by review_enforcement_commit and git_push_guard, so a
# leaked private copy here defeats any monkeypatch of it elsewhere — the same
# defect as the `review_state` leak, one name over.
_rs = private_module("review_scope", _SCRIPT_PATH)


# --------------------------------------------------------------------------- #
# git fixture helper
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return out.stdout


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "seed.md").write_text("seed\n")
    _git(repo, "add", "seed.md")
    _git(repo, "commit", "-qm", "seed")
    return repo


# --------------------------------------------------------------------------- #
# _scope_tag — ONE tag per file, first-match-wins (gstack case order)
# --------------------------------------------------------------------------- #
def test_scope_tag_auth_controller_is_api_not_auth():
    # THE critical regression: gstack matches *controller* (API) before *auth*.
    assert _rs._scope_tag("app/auth_controller.py") == "api"


def test_scope_tag_evaluator_is_prompts_not_backend():
    assert _rs._scope_tag("services/foo_evaluator.py") == "prompts"


def test_scope_tag_tests_path_is_tests_not_migrations():
    assert _rs._scope_tag("tests/db/migrations/test_x.py") == "tests"


def test_scope_tag_github_workflow_is_config():
    assert _rs._scope_tag(".github/workflows/ci.yml") == "config"


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/App.tsx", "frontend"),
        ("web/styles.css", "frontend"),
        ("src/core/engine.py", "backend"),
        ("cmd/main.go", "backend"),
        ("lib/util.mjs", "backend"),
        ("alembic/versions/0001_x.py", "migrations"),
        ("src/api/routes.py", "api"),
        ("auth/session_store.py", "auth"),
        ("docs/guide.md", "docs"),
        ("notes.txt", ""),  # unmatched by any gstack case
    ],
)
def test_scope_tag_table(path, expected):
    assert _rs._scope_tag(path) == expected


# --------------------------------------------------------------------------- #
# _category — code/test/fixture/docs-config (github-aware, separate axis)
# --------------------------------------------------------------------------- #
def test_category_github_workflow_is_code():
    # _is_docs_or_config returns False for .github/ → must be reviewed as code.
    assert _rs._category(".github/workflows/ci.yml") == "code"


@pytest.mark.parametrize(
    "path,expected",
    [
        ("README.md", "docs-config"),
        ("config/app.yaml", "docs-config"),
        ("src/core/engine.py", "code"),
        ("tests/test_engine.py", "test"),
        ("tests/fixtures/sample.json", "fixture"),
    ],
)
def test_category_table(path, expected):
    assert _rs._category(path) == expected


# --------------------------------------------------------------------------- #
# _specialists — aggregate scope OR → skill gating thresholds
# --------------------------------------------------------------------------- #
def test_specialists_small_diff_none():
    assert _rs._specialists({"backend"}, 40) == []


def test_specialists_backend_under_100_no_security():
    got = set(_rs._specialists({"backend"}, 60))
    assert got == {"testing", "maintainability", "performance"}


def test_specialists_backend_over_100_adds_security():
    got = set(_rs._specialists({"backend"}, 150))
    assert got == {"testing", "maintainability", "performance", "security"}


def test_specialists_auth_triggers_security_not_performance():
    got = set(_rs._specialists({"auth"}, 60))
    assert got == {"testing", "maintainability", "security"}


def test_specialists_frontend_adds_design_and_performance():
    got = set(_rs._specialists({"frontend"}, 60))
    assert got == {"testing", "maintainability", "performance", "design"}


def test_specialists_migrations_and_api():
    got = set(_rs._specialists({"migrations", "api"}, 60))
    assert got == {"testing", "maintainability", "data-migration", "api-contract"}


# --------------------------------------------------------------------------- #
# build_manifest — integration against real tmp git repos
# --------------------------------------------------------------------------- #
def test_build_manifest_enumerates_branch_changeset(tmp_path):
    repo = _mk_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat/x")
    (repo / "src").mkdir()
    (repo / "src" / "engine.py").write_text("def f():\n    return 1\n" * 30)
    (repo / "README.md").write_text("seed\nmore docs\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "work")

    m = _rs.build_manifest(cwd=str(repo))
    assert m is not None
    paths = {f["path"] for f in m["files"]}
    assert "src/engine.py" in paths
    assert "README.md" in paths
    eng = next(f for f in m["files"] if f["path"] == "src/engine.py")
    assert eng["category"] == "code"
    assert eng["scope_tag"] == "backend"
    assert m["counts"]["code"] >= 1
    assert m["diff_lines"] >= 30  # unfiltered: counts docs too


def test_build_manifest_includes_uncommitted_worktree(tmp_path):
    # Two-dot `git diff <merge-base>` must include NOT-yet-committed changes
    # (staged + tracked working-tree edits) — matching the specialists' exact
    # `git diff $DIFF_BASE`. Note: truly UNTRACKED (never `git add`ed) files are
    # invisible to `git diff` and thus to the specialists too, so they are
    # correctly absent from the manifest — they aren't part of the changeset.
    repo = _mk_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat/y")
    (repo / "live.py").write_text("x = 1\n")
    _git(repo, "add", "live.py")  # staged, uncommitted → in the changeset
    (repo / "seed.md").write_text("seed\nedited unstaged\n")  # tracked, unstaged
    m = _rs.build_manifest(cwd=str(repo))
    assert m is not None
    paths = {f["path"] for f in m["files"]}
    assert "live.py" in paths  # staged new file appears
    assert "seed.md" in paths  # unstaged tracked edit appears


def test_build_manifest_rename_captured(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "old.py").write_text("y = 2\n" * 20)
    _git(repo, "add", "old.py")
    _git(repo, "commit", "-qm", "add old")
    base_tip = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", "-b", "feat/rename")
    _git(repo, "mv", "old.py", "new.py")
    _git(repo, "commit", "-qm", "rename")
    m = _rs.build_manifest(cwd=str(repo), base=base_tip)
    assert m is not None
    paths = {f["path"] for f in m["files"]}
    assert "new.py" in paths  # dest side captured, no crash


def test_build_manifest_includes_test_files_as_reviewable(tmp_path):
    # A-B finding (vs ocr): test files ARE reviewable and must appear in the
    # coverage set (Testing specialist owns them) — not dropped like docs.
    repo = _mk_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat/tests")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_new.py").write_text("def test_a():\n    assert True\n" * 10)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "tests")
    m = _rs.build_manifest(cwd=str(repo))
    assert m is not None
    rec = next(f for f in m["files"] if f["path"] == "tests/test_new.py")
    assert rec["category"] == "test"
    assert rec["review_required"] is True
    assert rec["exclude_reason"] is None


def test_build_manifest_excludes_binary(tmp_path):
    repo = _mk_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat/bin")
    (repo / "logo.png").write_bytes(b"\x89PNG\r\n\x00\x00binary\x00data\xff\xfe")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "bin")
    m = _rs.build_manifest(cwd=str(repo))
    assert m is not None
    rec = next(f for f in m["files"] if f["path"] == "logo.png")
    assert rec["review_required"] is False
    assert rec["exclude_reason"] == "binary"


def test_build_manifest_excludes_vendored(tmp_path):
    repo = _mk_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat/vendor")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "lib.js").write_text("module.exports = 1\n" * 10)
    (repo / "app.min.js").write_text("var a=1;\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "vendor")
    m = _rs.build_manifest(cwd=str(repo))
    assert m is not None
    vend = next(f for f in m["files"] if f["path"] == "node_modules/lib.js")
    assert vend["review_required"] is False
    assert vend["exclude_reason"] == "vendored"
    minified = next(f for f in m["files"] if f["path"] == "app.min.js")
    assert minified["review_required"] is False
    assert minified["exclude_reason"] == "vendored"


def test_build_manifest_fail_open_outside_git(tmp_path):
    non_repo = tmp_path / "plain"
    non_repo.mkdir()
    assert _rs.build_manifest(cwd=str(non_repo)) is None


def test_build_manifest_empty_diff_on_base(tmp_path):
    # On the base branch with nothing new → no files, but not a crash.
    repo = _mk_repo(tmp_path)
    m = _rs.build_manifest(cwd=str(repo))
    # Either None (no merge-base delta) or an empty file list — never raises.
    assert m is None or m["files"] == []


# --------------------------------------------------------------------------- #
# render_reminder_block — additive, LOUD truncation, docs-only omission
# --------------------------------------------------------------------------- #
def _f(path, category, scope_tag, review_required=True, exclude_reason=None):
    return {
        "path": path,
        "change_type": "M",
        "category": category,
        "scope_tag": scope_tag,
        "review_required": review_required,
        "exclude_reason": exclude_reason,
    }


def test_render_lists_reviewable_files_and_specialists():
    manifest = {
        "base": "abc",
        "diff_lines": 120,
        "counts": {"review_required": 1, "excluded": 0},
        "files": [_f("src/a.py", "code", "backend")],
        "specialists": ["maintainability", "performance", "testing"],
    }
    block = _rs.render_reminder_block(manifest)
    assert "src/a.py" in block
    assert "MUST" in block
    assert "performance" in block
    assert "before" in block.lower()  # "before adaptive gating" framing


def test_render_includes_tests_in_coverage():
    # A-B finding: ocr reviews test files; the manifest must NOT drop them, else
    # a test-only PR yields an empty coverage list.
    manifest = {
        "base": "abc",
        "diff_lines": 60,
        "counts": {"review_required": 1, "excluded": 0},
        "files": [_f("tests/test_x.py", "test", "tests")],
        "specialists": ["maintainability", "testing"],
    }
    assert "tests/test_x.py" in _rs.render_reminder_block(manifest)


def test_render_docs_only_returns_empty():
    manifest = {
        "base": "abc",
        "diff_lines": 10,
        "counts": {"review_required": 0, "excluded": 1},
        "files": [
            _f("README.md", "docs-config", "docs", review_required=False, exclude_reason="docs")
        ],
        "specialists": [],
    }
    assert _rs.render_reminder_block(manifest) == ""


def test_render_accounts_for_excluded_files_loudly():
    # Excluded files (docs/binary/vendored) are counted, never silently dropped.
    manifest = {
        "base": "abc",
        "diff_lines": 60,
        "counts": {"review_required": 1, "excluded": 3},
        "files": [
            _f("src/a.py", "code", "backend"),
            _f("logo.png", "code", "", review_required=False, exclude_reason="binary"),
            _f(
                "node_modules/x.js",
                "code",
                "backend",
                review_required=False,
                exclude_reason="vendored",
            ),
            _f("README.md", "docs-config", "docs", review_required=False, exclude_reason="docs"),
        ],
        "specialists": ["testing", "maintainability"],
    }
    block = _rs.render_reminder_block(manifest)
    assert "src/a.py" in block
    assert "logo.png" not in block  # excluded files not in the MUST-cover list
    assert "3" in block  # excluded count surfaced


def test_render_truncates_loudly():
    files = [_f(f"src/f{i}.py", "code", "backend") for i in range(80)]
    manifest = {
        "base": "abc",
        "diff_lines": 800,
        "counts": {"review_required": 80, "excluded": 0},
        "files": files,
        "specialists": ["testing"],
    }
    block = _rs.render_reminder_block(manifest)
    assert "80" in block  # true total surfaced
    assert "more" in block.lower()  # explicit truncation marker


def test_render_none_manifest_is_empty():
    assert _rs.render_reminder_block(None) == ""


# --------------------------------------------------------------------------- #
# Codex-review fixes: category precedence, numstat fail-open, -z parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path,expected",
    [
        ("tests/fixtures/case.yaml", "fixture"),  # under tests, .yaml → fixture, NOT docs
        ("tests/config.yml", "test"),  # under tests, .yml → test, NOT docs
        ("tests/snapshots/out.md", "test"),  # under tests, .md → test, NOT docs
        ("config/app.yaml", "docs-config"),  # genuine config → excluded
        ("docs/guide.md", "docs-config"),  # genuine docs → excluded
    ],
)
def test_category_test_tree_beats_docs(path, expected):
    # Codex P1: test/fixture path membership must win over docs/config so a data
    # asset under a test tree is not silently dropped from coverage.
    assert _rs._category(path) == expected


def test_build_manifest_fail_open_when_numstat_fails(tmp_path, monkeypatch):
    # Codex P1: partial git failure (name-status ok, numstat fails) must return
    # None — never a manifest with wrong diff_lines / no binary exclusion.
    repo = _mk_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat/partial")
    (repo / "m.py").write_text("x = 1\n" * 30)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "work")
    real_git = _rs._git

    def fake_git(args, cwd, deadline=None):
        if "--numstat" in args:
            return None
        return real_git(args, cwd, deadline)

    monkeypatch.setattr(_rs, "_git", fake_git)
    assert _rs.build_manifest(cwd=str(repo)) is None


def test_parse_name_status_z_handles_rename_and_tab_paths():
    # Codex P2: -z emits paths verbatim; a literal tab in a path must survive.
    text = "M\x00src/a.py\x00A\x00weird\tname.py\x00R100\x00old.py\x00new.py\x00"
    recs = _rs._parse_name_status_z(text)
    got = {r["path"]: r["change_type"] for r in recs}
    # Rename emits BOTH sides now (src+dst) so a code source isn't dropped.
    assert got == {"src/a.py": "M", "weird\tname.py": "A", "old.py": "R", "new.py": "R"}


def test_parse_numstat_z_normal_binary_and_rename():
    # normal (5+3), binary (-,- skipped but collected), rename (2+1, dst path).
    text = "5\t3\tsrc/a.py\x00-\t-\timg.bin\x002\t1\t\x00old.py\x00new.py\x00"
    total, binary = _rs._parse_numstat_z(text)
    assert total == 11  # 5+3 + 2+1 ; binary contributes 0
    assert binary == {"img.bin"}


def test_parse_numstat_z_preserves_tab_in_path():
    # A binary file whose name contains a literal tab: the full path must be
    # reconstructed (parts[2:] joined) so it matches the -z name-status path.
    text = "-\t-\tweird\tname.bin\x005\t3\tok\tpath.py\x00"
    total, binary = _rs._parse_numstat_z(text)
    assert binary == {"weird\tname.bin"}  # not truncated to "weird"
    assert total == 8  # the tab-named text file still counts


def test_git_fail_open_on_unicode_decode_error(monkeypatch):
    def boom(*a, **k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(_rs.subprocess, "run", boom)
    assert _rs._git(["diff"], None) is None


def test_git_returns_none_past_deadline(monkeypatch):
    # Codex P2: a deadline in the past must short-circuit to None (bounds the
    # manifest's total git time under the hook timeout) without even calling git.
    called = {"n": 0}

    def spy(*a, **k):
        called["n"] += 1
        raise AssertionError("should not run past deadline")

    monkeypatch.setattr(_rs.subprocess, "run", spy)
    assert _rs._git(["diff"], None, deadline=_rs.time.monotonic() - 1) is None
    assert called["n"] == 0


def test_parse_name_status_z_rename_emits_both_sides():
    # Codex P2: rename FROM reviewable code TO an excluded dest must still surface
    # the source, else removed code is dropped from coverage.
    text = "R100\x00src/auth.py\x00README.md\x00"
    recs = _rs._parse_name_status_z(text)
    paths = {r["path"] for r in recs}
    assert paths == {"src/auth.py", "README.md"}
    assert all(r["change_type"] == "R" for r in recs)


def test_build_manifest_rename_to_docs_keeps_code_source(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "auth.py").write_text("def login():\n    return 1\n" * 20)
    _git(repo, "add", "auth.py")
    _git(repo, "commit", "-qm", "add code")
    base_tip = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", "-b", "feat/rename-to-docs")
    _git(repo, "mv", "auth.py", "NOTES.md")  # code -> docs rename
    _git(repo, "commit", "-qm", "rename to docs")
    m = _rs.build_manifest(cwd=str(repo), base=base_tip)
    assert m is not None
    reviewable = {f["path"] for f in m["files"] if f["review_required"]}
    assert "auth.py" in reviewable  # removed code still named
    excluded = {f["path"] for f in m["files"] if not f["review_required"]}
    assert "NOTES.md" in excluded  # docs dest excluded


# --------------------------------------------------------------------------- #
# hook wiring — base reminder ALWAYS prints; manifest strictly additive
# --------------------------------------------------------------------------- #
def _run_prompt_hook(repo: Path) -> str:
    env = {k: v for k, v in os.environ.items() if k != "GENESIS_CC_SESSION"}
    out = subprocess.run(
        [sys.executable, str(_SCRIPTS / "review_enforcement_prompt.py")],
        capture_output=True,
        text=True,
        cwd=str(repo),
        env=env,
    )
    return out.stdout


def test_hook_emits_base_reminder_and_manifest_on_unreviewed_code(tmp_path):
    repo = _mk_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat/z")
    (repo / "mod.py").write_text("def g():\n    return 2\n" * 30)
    _git(repo, "add", "mod.py")  # staged → triggers the hook
    out = _run_prompt_hook(repo)
    assert "Unreviewed code changes detected" in out  # base reminder present
    assert "mod.py" in out  # manifest appended


def test_hook_base_reminder_survives_when_manifest_unavailable(tmp_path):
    # Orphan branch → no merge-base with the base ref → build_manifest returns
    # None, but staged code still triggers the hook. The base reminder MUST
    # still print (manifest is strictly additive, never load-bearing).
    repo = _mk_repo(tmp_path)
    _git(repo, "checkout", "-q", "--orphan", "feat/orphan")
    _git(repo, "reset", "-q")  # drop the index carried over from main
    (repo / "mod.py").write_text("def g():\n    return 2\n" * 30)
    _git(repo, "add", "mod.py")
    out = _run_prompt_hook(repo)
    assert "Unreviewed code changes detected" in out  # base reminder survives


def test_hook_silent_on_clean_tree(tmp_path):
    repo = _mk_repo(tmp_path)
    out = _run_prompt_hook(repo)
    assert out.strip() == ""


# --------------------------------------------------------------------------- #
# classify_compare_substantiality — GitHub compare-API files[] → substantiality
# (the merge review-freshness gate's delta classifier). Pure; no git/network.
# --------------------------------------------------------------------------- #
def _cf(filename, additions=0, deletions=0, status="modified", has_patch=True, previous_filename=None):
    f = {
        "filename": filename,
        "additions": additions,
        "deletions": deletions,
        "status": status,
        "has_patch": has_patch,
    }
    if previous_filename:
        f["previous_filename"] = previous_filename
    return f


def test_compare_empty_is_inline():
    assert _rs.classify_compare_substantiality([]) == "inline"
    assert _rs.classify_compare_substantiality(None) == "inline"


def test_compare_large_single_code_file_is_substantial():
    assert _rs.classify_compare_substantiality([_cf("src/app.py", additions=60, deletions=5)]) == "substantial"


def test_compare_small_single_code_file_is_inline():
    assert _rs.classify_compare_substantiality([_cf("src/app.py", additions=8, deletions=1)]) == "inline"


def test_compare_two_code_files_is_substantial():
    files = [_cf("a.py", additions=3), _cf("b.py", additions=4)]
    assert _rs.classify_compare_substantiality(files) == "substantial"


def test_compare_docs_only_is_inline():
    assert _rs.classify_compare_substantiality([_cf("README.md", additions=200)]) == "inline"


def test_compare_domain_sensitive_small_is_substantial():
    assert _rs.classify_compare_substantiality([_cf("src/auth_session.py", additions=3)]) == "substantial"


def test_compare_binary_asset_excluded():
    # Binary asset (no patch, 0 lines, not a rename) must NOT count as a code file:
    # binary + ONE small code file = 1 reviewable code file → inline. If the binary
    # were wrongly counted it would be 2 code files → substantial (discriminating).
    files = [
        _cf("logo.png", additions=0, deletions=0, status="added", has_patch=False),
        _cf("src/app.py", additions=4, deletions=1),
    ]
    assert _rs.classify_compare_substantiality(files) == "inline"


def test_compare_rename_side_domain_sensitivity():
    files = [_cf("src/x.py", additions=0, deletions=0, status="renamed", previous_filename="src/auth.py")]
    assert _rs.classify_compare_substantiality(files) == "substantial"


def test_compare_non_dict_entry_skipped():
    files = [None, "junk", _cf("src/app.py", additions=4)]
    assert _rs.classify_compare_substantiality(files) == "inline"


def test_compare_suppressed_code_file_is_substantial():
    # Architect F4: a CODE-category file with no patch and zero counted lines is
    # UNVERIFIABLE (an over-limit/suppressed text diff, if the API emits one) —
    # it must fail toward review, never silently exclude like a binary asset.
    files = [_cf("src/generated_big.py", additions=0, deletions=0, status="added", has_patch=False)]
    assert _rs.classify_compare_substantiality(files) == "substantial"


def test_compare_malformed_counts_on_code_file_is_substantial():
    # Architect F7: unparseable additions/deletions on a code file must not lean
    # "trivial" (lines=0) — malformed data on reviewable code fails toward review.
    files = [{"filename": "src/app.py", "additions": "lots", "deletions": 0,
              "status": "modified", "has_patch": True}]
    assert _rs.classify_compare_substantiality(files) == "substantial"


def test_compare_binary_noncode_asset_still_excluded():
    # The F4 fix must NOT flip genuine binary assets: a no-patch/0-lines PNG is
    # still excluded (non-code category), so binary+small-code stays inline.
    files = [
        _cf("logo.png", additions=0, deletions=0, status="added", has_patch=False),
        _cf("src/app.py", additions=4, deletions=1),
    ]
    assert _rs.classify_compare_substantiality(files) == "inline"


def test_compare_rename_code_to_docs_counts_source():
    # Codex P2 #1373: a 200-line rename FROM code TO an excluded docs dest must still
    # classify substantial — the source (code) carries the magnitude, not the dest.
    files = [_cf("docs/foo.md", additions=150, deletions=50, status="renamed",
                 previous_filename="src/foo.py")]
    assert _rs.classify_compare_substantiality(files) == "substantial"


def test_compare_rename_docs_to_docs_still_inline():
    # A docs->docs rename (neither side reviewable code) stays inline.
    files = [_cf("docs/b.md", additions=150, status="renamed", previous_filename="docs/a.md")]
    assert _rs.classify_compare_substantiality(files) == "inline"


# --------------------------------------------------------------------------- #
# classify_lane — the CONSEQUENCE axis
#
# Orthogonal to substantiality: substantiality asks "is this big enough to need a
# deep review", the lane asks "how much does it cost to be wrong". A one-line edit
# to an enforcement hook is `inline` and `critical` at once.
#
# The hook-surface verdict is passed IN (its authority is git_push_guard's
# constant), so these tests drive that parameter directly rather than duplicating
# the fence.
# --------------------------------------------------------------------------- #


def test_lane_hook_surface_is_critical_however_small():
    """A one-line guard edit outranks every other signal."""
    assert _rs.classify_lane(["scripts/hooks/git_push_guard.py"], hook_surface=True) == "critical"


def test_lane_github_config_is_critical():
    """Not hook surface, but a change here can disable a required check as
    effectively as editing a gate — the rationale in .github/labeler.yml."""
    assert _rs.classify_lane([".github/workflows/ci.yml"], hook_surface=False) == "critical"


def test_lane_route_and_migration_DIRECTORIES_are_critical():
    """The explicit PREFIXES, not the tags.

    Renamed from `test_lane_api_and_migrations_are_critical`, which claimed the
    tags while both of its paths also satisfy `_LANE_CRITICAL_PREFIXES` — so it
    passed with the tag rule deleted, and a test that cannot say which of two
    rules it proves is a lock on neither. The tag lock is
    `test_lane_keeps_the_api_and_migrations_tags_that_MEASURED_clean`, which does
    go red under that mutation.
    """
    assert _rs.classify_lane(["src/genesis/dashboard/routes/x.py"], hook_surface=False) == "critical"
    assert _rs.classify_lane(["src/genesis/db/migrations/0001_x.py"], hook_surface=False) == "critical"


def test_the_lane_consults_no_scope_tag():
    """The lane reads path boundaries, never `_scope_tag`. Structural lock.

    Three rounds of findings on `_is_lane_critical_path` were one question asked
    of a NAME pattern: "is this an HTTP surface". `*route*` cannot answer it —
    it matched `routing/router.py` (the LLM router) and
    `reflection/output_router.py` while missing a root-level `api/` directory,
    because `*/api/*` needs a preceding path component.

    Re-admitting a tag is the obvious economy the next reader will reach for, and
    it reads as a smaller change than it is. This test is what stops it: the tag
    that would be re-admitted demonstrably classifies a non-HTTP module, so the
    two assertions below cannot both hold while the lane consults tags.
    """
    assert _rs._scope_tag("src/genesis/routing/router.py") == "api", (
        "precondition: the inherited glob still claims this non-HTTP module, "
        "or this test no longer demonstrates why the lane ignores tags"
    )
    assert _rs.classify_lane(["src/genesis/routing/router.py"], hook_surface=False) == (
        "standard"
    )


def test_every_route_defining_module_is_critical():
    """The lock for the API class is an ENUMERATION, not another example.

    The lane's own operator message names "API surfaces" as critical. MEASURED
    2026-09-13 over every tracked `.py`: 54 of 58 route-defining modules reached
    `critical`, and the four misses were `src/genesis/hosting/{standalone,
    openclaw/completions,agent_zero/overlay}.py` and `dashboard/_blueprint.py` —
    `standalone.py:647` serves `/genesis/login`, and it was reachable by neither
    the `api` tag nor an `api.py`/`auth.py` basename.

    Why this shape: neither of the change's own two methods could produce that
    finding. The 40-PR distribution reproduced to the decimal across it, and every
    constructed case passed. A population check is the only thing that fails when
    a new route surface appears somewhere nobody listed.
    """
    root = Path(__file__).resolve().parents[2]
    pat = re.compile(r"^\s*@\w+\.route\(|Blueprint\(|add_url_rule\(", re.M)
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "*.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert len(tracked) > 500, "precondition: ls-files returned a plausible population"
    routed = [
        f
        for f in tracked
        if not f.startswith("tests/") and pat.search((root / f).read_text(errors="ignore"))
    ]
    assert routed, "precondition: the detector finds route definitions at all"
    misses = [f for f in routed if _rs.classify_lane([f], hook_surface=False) != "critical"]
    assert not misses, (
        f"{len(misses)} of {len(routed)} route-defining modules sit outside the "
        f"critical lane, which the gate's own message promises covers API "
        f"surfaces: {misses}"
    )


def _ci_invoked_scripts(workflow: Path) -> list[str]:
    """Every `scripts/...` path the workflow EXECUTES, from its `run:` blocks.

    Parsed from the YAML rather than regexed over the raw file, because the two
    failure directions pull against each other and a flat regex loses both:

      * TOO NARROW — the first version matched only `python3?|bash` followed
        immediately by the path, so `python -u scripts/x.py`, `bash -e scripts/x.sh`
        and a direct `./scripts/x.sh` all slipped past. A required check added in
        any of those forms would never enter this list, and the count precondition
        would not notice because the other 15 still matched.
      * TOO BROAD — matching bare paths anywhere in the file picks up mentions in
        COMMENTS (`scripts/genesis_mcp_server.py`, `scripts/lib/cc_version.sh` are
        both named in prose here). Neither is a consequence surface, so a broad
        matcher would demand they be critical and fail for the wrong reason.

    Restricting to `run:` blocks separates the two: only executed text is
    considered, and within it both interpreter-with-flags and direct execution.
    """
    import yaml

    doc = yaml.safe_load(workflow.read_text())
    runs: list[str] = []
    for job in (doc.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                runs.append(step["run"])

    invoked: set[str] = set()
    # An interpreter, any number of its own flags, then the script.
    interp = re.compile(
        r"\b(?:python3?|bash|sh)\b(?:\s+-[^\s]+)*\s+((?:\./)?scripts/[A-Za-z0-9_/.-]+\.(?:py|sh))"
    )
    # Or the script executed directly.
    direct = re.compile(r"(?:^|\s)(\./scripts/[A-Za-z0-9_/.-]+\.(?:py|sh))")
    for text in runs:
        invoked.update(m.lstrip("./") for m in interp.findall(text))
        invoked.update(m.lstrip("./") for m in direct.findall(text))
    return sorted(invoked)


def test_required_check_implementations_are_critical():
    """A required check's IMPLEMENTATION is the same consequence surface as the
    workflow that invokes it — and the list is DERIVED, not remembered.

    `.github/**` was critical from the first version of this lane, on the stated
    reason that a change there can disable a required check. The implementation
    disables it just as effectively, and for three rounds only the YAML layer was
    covered: MEASURED, all 15 scripts `ci.yml` invokes took the standard
    threshold, so three unresolved P2s passed in the leak scanner or the
    review-depth gate where two would have blocked in the workflow calling them.

    Derived from `ci.yml` ON PURPOSE. A hardcoded list is the shape that went
    stale three times in this file; re-parsing the workflow means a required
    check added next month fails HERE until someone puts its path in the lane's
    vocabulary, which is the only version of this that survives its author.
    """
    root = Path(__file__).resolve().parents[2]
    workflow = root / ".github" / "workflows" / "ci.yml"
    assert workflow.exists(), "precondition: the required-CI workflow is where we think"

    invoked = _ci_invoked_scripts(workflow)
    assert len(invoked) >= 10, (
        f"precondition: expected the workflow to invoke many checkers, found "
        f"{len(invoked)} — if the invocation SPELLING changed, this test is "
        f"measuring nothing and must be updated before it is trusted"
    )

    missed = [p for p in invoked if _rs.classify_lane([p], hook_surface=False) != "critical"]
    assert not missed, (
        f"{len(missed)} of {len(invoked)} required-check implementations are "
        f"outside the critical lane — changing them disables enforcement as "
        f"effectively as editing the workflow: {missed}"
    )


def test_the_extractor_sees_every_invocation_form():
    """Guard the guard on the EXTRACTOR, not just on its current output.

    The lock above is only as good as what it can see, and its first version was
    measurably blind: `python -u`, `bash -e` and `./scripts/x.sh` all went
    unnoticed while the count stayed at 15, so the precondition could not fire
    either. A reviewer found that; nothing here could have.

    Synthetic workflow rather than the live one, so this keeps testing the
    extractor after `ci.yml` changes — and it asserts the NEGATIVE case too,
    because the naive fix for the blind spot (match bare paths anywhere) picks up
    comment-only mentions and fails for the wrong reason.
    """
    import textwrap

    wf = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    assert wf.exists(), "precondition: real workflow present for the live test above"

    import tempfile

    synthetic = textwrap.dedent(
        """\
        jobs:
          probe:
            steps:
              - run: python -u scripts/flagged_interp.py
              - run: bash -e scripts/flagged_shell.sh
              - run: ./scripts/direct_exec.sh
              - run: python3 scripts/plain.py
              - run: |
                  # scripts/only_a_comment.py is named but never run
                  echo done
        """
    )
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
        fh.write(synthetic)
        tmp = Path(fh.name)
    try:
        found = _ci_invoked_scripts(tmp)
    finally:
        tmp.unlink()

    assert "scripts/flagged_interp.py" in found, "interpreter flags must not hide a checker"
    assert "scripts/flagged_shell.sh" in found, "shell flags must not hide a checker"
    assert "scripts/direct_exec.sh" in found, "direct execution must not hide a checker"
    assert "scripts/plain.py" in found
    assert "scripts/only_a_comment.py" not in found, (
        "a path mentioned in a comment is not an invocation — matching it would "
        "demand the critical lane for files that are not consequence surfaces"
    )


def test_trust_boundaries_are_never_relaxed_below_todays_bar():
    """Approval and outbound-action gates must not get a wider finding budget.

    This lane RELAXES thresholds, and main runs a flat 1.0 — so every file blocks
    at two unresolved P2s today. Anything this change moves to `standard` gets
    FOUR. For `autonomy/approval_gate.py`, `email_gate.py` and `cli_policy.py`
    that is a live weakening of review on the autonomous-CLI approval gate, which
    is a standing non-negotiable in this repo. MEASURED before the fix: all five
    named modules classified `standard`.

    Pinned as a FLOOR, not a preference. A future edit that widens the budget on
    these fails here, which is the point — a lane that quietly relaxes a
    sovereignty gate is a downgrade wearing a refactor.
    """
    for path in (
        "src/genesis/autonomy/approval_gate.py",
        "src/genesis/autonomy/email_gate.py",
        "src/genesis/autonomy/cli_policy.py",
        "src/genesis/autonomy/approval.py",
        "src/genesis/autonomy/dispatch_gate.py",
    ):
        assert _rs.classify_lane([path], hook_surface=False) == "critical", (
            f"{path} is a trust boundary; the lane must not widen its budget"
        )


def test_a_fixture_corpus_is_light_even_when_it_looks_like_source():
    """Sample programs the eval harness loads as DATA are not consequence surfaces.

    MEASURED: 19 tracked files under `gauntlet_fixtures/` split standard 15 /
    critical 1 / light 3, and the CRITICAL one was `calc_longhorizon/calc/api.py`
    — dragged in by THIS module's own `api.py` basename rule. A sample program in
    the strictest lane is the same over-classification shape as the `*route*`
    glob, self-inflicted this time.

    The exemption is the one rule here that makes a change LIGHTER, so it is
    anchored as a directory PREFIX and the negative case is asserted: a sibling
    directory whose name merely STARTS with the exempt one must not inherit it.
    A substring or `*fixture*` spelling would exempt every continuation, which is
    how a loosening rule widens a budget by accident.
    """
    real = "src/genesis/eval/gauntlet_fixtures/calc_longhorizon/calc/api.py"
    assert _rs.classify_lane([real], hook_surface=False) == "light"

    # Controls, both directions.
    assert _rs.classify_lane(["src/genesis/outreach/api.py"], hook_surface=False) == "critical", (
        "a REAL api module must be unaffected, or the exemption is too wide"
    )
    assert _rs.classify_lane(
        ["src/genesis/eval/gauntlet_fixtures_live/api.py"], hook_surface=False
    ) == "critical", "a continuation of the prefix must NOT inherit the exemption"


def test_the_explicit_rules_FULLY_EXPLAIN_the_critical_set():
    """Nothing may reach `critical` for a reason the module does not state.

    This is the lock on the defect that survived two rounds: `_scope_tag` — a
    NAME vocabulary living outside this module — pulled 7 non-HTTP files into the
    strictest lane (`routing/router.py` is the LLM router). The enumeration test
    above could not see it, because it measures MISSES and that was the opposite
    direction.

    Stated as a STRUCTURAL property rather than an exemption list: re-derive the
    critical set from the declared constants alone and require it to equal what
    `classify_lane` actually produces. An exemption list would be a second copy
    of those constants, and a drifting replica is the shape this file keeps
    finding defects in. If the two sets ever disagree, something outside
    `_LANE_CRITICAL_*` is classifying — which is exactly how the tag crept back
    in twice.
    """
    root = Path(__file__).resolve().parents[2]
    tracked = [
        f
        for f in subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        if not f.startswith("tests/")
    ]
    assert len(tracked) > 500, "precondition: ls-files returned a plausible population"

    def declared(path: str) -> bool:
        """The critical rules, re-expressed from the module's own constants."""
        if any(path.startswith(pre) for pre in _rs._LANE_CRITICAL_PREFIXES):
            return True
        base = os.path.basename(path)
        if base in _rs._LANE_CRITICAL_BASENAMES:
            return True
        if base in _rs._LANE_CRITICAL_TRUST_BASENAMES or base.endswith(
            _rs._LANE_CRITICAL_BASENAME_SUFFIXES
        ):
            return True
        return base.endswith(_rs._LANE_CRITICAL_BASENAME_EXTS) and base.startswith(
            _rs._LANE_CRITICAL_BASENAME_PREFIXES
        )

    unexplained = [
        f
        for f in tracked
        if _rs.classify_lane([f], hook_surface=False) == "critical" and not declared(f)
    ]
    assert not unexplained, (
        f"{len(unexplained)} file(s) reach the critical lane without matching any "
        f"declared rule — something outside _LANE_CRITICAL_* is classifying, which "
        f"is the shape that put the LLM router in the strictest lane: {unexplained}"
    )


def test_the_prose_vocabulary_mirrors_the_guards_doc_vocabulary():
    """Third replica of ONE vocabulary; only two of the three were locked.

    `_LANE_PROSE_*` here and `_DOC_*` in `git_push_guard` must agree, and the
    docstring above says so in prose — which is a convention at a call site that
    has to remember. `tests/test_session_awareness/test_doc_paths.py` already locks
    the guard-vs-doc_paths pair the same way; this closes the third edge, so `.adoc`
    cannot be added to one side while the lane and the doc-findings filter start
    disagreeing about what prose is.

    Loaded via `private_module` rather than a hand-rolled register/exec/restore.
    This test originally did the latter, and the two changes met in a way neither
    diff showed: the shared helper landed on main and removed this file's
    `import importlib.util`, while this branch added a USE of it. The hunks are
    far apart, so git merged both cleanly and CI failed on an undefined name that
    exists in neither branch alone.
    """
    guard = private_module("_gpg_for_prose_parity", _SCRIPTS / "hooks" / "git_push_guard.py")
    assert {e.lstrip(".") for e in _rs._LANE_PROSE_EXTS} == guard._DOC_EXTS
    assert {s.lower() for s in _rs._LANE_PROSE_STEMS} == guard._DOC_STEMS
    assert {e.lstrip(".") for e in _rs._LANE_PROSE_STEM_EXTS} == guard._DOC_STEM_EXTS


def test_lane_config_is_ORDINARY_not_critical():
    """`config/` was briefly a critical prefix, on the reasoning that config arms
    behaviour. Removed: the genuinely arming directory, `config/behavioral_rules/`,
    is already hook surface and reaches critical that way, while a blanket prefix
    put routine threshold edits in the strictest lane AND contradicted the
    instruction-file text this same change adds ("config is NOT prose, so a
    `.yaml`/`.toml` change is ordinary").

    Destructive-capability, egress and financial paths are the same shape and are
    likewise out: naming them needs a taxonomy this repo does not yet have.
    """
    assert _rs.classify_lane(["config/reflex.yaml"], hook_surface=False) == "standard"
    assert _rs.classify_lane(["config/github_steward.yaml"], hook_surface=False) == "standard"


def test_lane_auth_tag_is_NOT_critical():
    """The `auth` glob is `*auth* *session* ...`, and this repo is built on CC
    SESSIONS. MEASURED 2026-09-13 over 3,999 tracked files: 59 tag `auth` and 58
    of them (98%) matched on "session" — session_cache.py, session_cap.py,
    genesis_session_context.py. Inheriting `_DOMAIN_SENSITIVE_TAGS` wholesale
    would make 58 session files critical for a reason nobody intended.

    This test is the lock on that decision: re-adding `auth` to the lane's
    sensitive set fails here, with this docstring as the reason.
    """
    assert _rs._scope_tag("src/genesis/cc/session_cache.py") == "auth", (
        "precondition: this path must still hit the auth glob, or the test proves nothing"
    )
    assert _rs.classify_lane(["src/genesis/cc/session_cache.py"], hook_surface=False) == "standard"


def test_lane_ordinary_code_is_standard():
    assert _rs.classify_lane(["src/genesis/memory/store.py"], hook_surface=False) == "standard"


def test_lane_docs_and_tests_only_is_light():
    assert _rs.classify_lane(
        ["docs/a.md", "tests/test_x.py", "CHANGELOG.md"], hook_surface=False
    ) == "light"


def test_lane_prompt_surfaces_are_light():
    """A rule-doc belongs in the widest budget, and this is the one behaviour the
    vocabulary change actually moved.

    MEASURED over the 40 most recently merged PRs (2026-09-13): 14 moved
    `standard` -> `light` versus the tag-based draft, and every one is a prompt
    surface — `_category` calls these `code`, so the inherited classifier put
    them in `standard` while the plan's own lane table said rule-docs were light.
    The move is mostly inert rather than a loosening: 11 of the 14 contain
    nothing whose findings score at all, since every path in them is a
    `git_push_guard._is_doc_path` and `doc_findings` defaults to `skip`.

    The `_category` assertions are the precondition. Without them this test
    passes for free the day something reclassifies these as docs, and would stop
    being evidence that the lane makes its own decision here.
    """
    for path in (
        ".claude/skills/genesis-development/SKILL.md",
        ".claude/commands/deep-review.md",
        "src/genesis/skills/voice-master/references/anti-slop.md",
    ):
        assert _rs._category(path) == "code", (
            f"precondition: {path} must still reach _category()=='code', "
            "or this test no longer shows the lane deciding for itself"
        )
        assert _rs.classify_lane([path], hook_surface=False) == "light"


def test_lane_one_code_file_among_docs_is_not_light():
    """The light lane is ALL-or-nothing: one real code file disqualifies it."""
    assert _rs.classify_lane(["docs/a.md", "src/genesis/memory/store.py"], hook_surface=False) == (
        "standard"
    )


def test_lane_unknown_scope_fails_CLOSED():
    """An unreadable file list reaches us as []. The lane RELAXES a threshold, so
    the safe default is the one that relaxes nothing."""
    assert _rs.classify_lane([], hook_surface=False) == "critical"


def test_lane_vendored_only_is_light():
    """A lockfile refresh carries no reviewable code."""
    assert _rs.classify_lane(["package-lock.json", "node_modules/x/y.js"], hook_surface=False) == (
        "light"
    )


def test_lane_dependency_pins_are_NOT_light():
    """`.txt` alone is not prose. `requirements.txt` and
    `config/az-pip-constraints.txt` are dependency pins that reach
    `_category() == "docs-config"`; admitting every `.txt` gave them a 3.0
    budget one line under a comment saying config is not prose.

    Each path is asserted against `_is_lane_light` as well as the lane, because a
    lane assertion alone cannot tell "`.txt` is not prose" from "something else
    made this non-light". The direct call is the rule actually under test.
    """
    for path in ("requirements.txt", "config/az-pip-constraints.txt"):
        assert not _rs._is_lane_light(path)
        assert _rs.classify_lane([path], hook_surface=False) == "standard"


def test_lane_a_doc_stem_with_txt_is_still_light():
    """The other half of that split, so tightening `.txt` did not take prose with
    it: a KNOWN doc stem keeps `.txt`, mirroring `git_push_guard._is_doc_path`."""
    assert _rs.classify_lane(["CHANGELOG.txt"], hook_surface=False) == "light"
    assert _rs.classify_lane(["LICENSE"], hook_surface=False) == "light"


def test_lane_every_authority_outranks_the_vendored_strip():
    """`_is_vendored` REMOVES a path from `reviewable`, so anything that must
    outrank a vendor glob has to be checked before the strip — not after it.

    Both spellings measured: each of these is `_is_vendored` via `*/generated/*`
    and each returned `light` while its check sat below the strip. The hook-surface
    one was found first and fixed alone; `.github/` was the same class one line
    away and a second reviewer had to find it.

    These two are the MEASURED regressions, and that is all this test pins. The
    general property is carried by STRUCTURE, not by these assertions:
    `_is_lane_critical_path` is the entire critical vocabulary and it is called
    above the strip, so an authority added inside it inherits the ordering for
    free. An authority added as a separate `if` AFTER the strip would still slip
    past, and nothing here can see that — said plainly because the earlier wording
    credited this test with a guarantee only the structure provides.
    """
    assert _rs._is_vendored("scripts/hooks/generated/x.py"), "precondition: vendored"
    assert _rs._is_vendored(".github/generated/ci.yml"), "precondition: vendored"
    assert _rs.classify_lane(
        ["scripts/hooks/generated/x.py"], hook_surface=True
    ) == "critical"
    assert _rs.classify_lane([".github/generated/ci.yml"], hook_surface=False) == "critical"


def test_lane_real_api_modules_are_critical():
    """`_SCOPE_PATTERNS`' api globs are `*controller* *route* *endpoint* */api/*`,
    which miss a module simply NAMED `api.py` or `api_*.py` — those tag `backend`.
    MEASURED: `src/genesis/outreach/api.py` defines Flask routes and classified
    `standard`. Closed by name in `_is_lane_critical_path` rather than by widening
    `_SCOPE_PATTERNS`, whose blast radius includes the blocking depth gate."""
    assert _rs._scope_tag("src/genesis/outreach/api.py") == "backend", (
        "precondition: the inherited tagger still misses this, or the test proves nothing"
    )
    assert _rs.classify_lane(["src/genesis/outreach/api.py"], hook_surface=False) == "critical"
    assert _rs.classify_lane(
        ["az_plugins/genesis/api_health.py"], hook_surface=False
    ) == "critical"


def test_lane_critical_beats_light_when_mixed():
    """Guard the guard on ordering: a docs-heavy PR that also touches a migration
    is critical, not light. The checks must not be order-dependent in the wrong
    direction."""
    assert _rs.classify_lane(
        ["docs/a.md", "README.md", "src/genesis/db/migrations/0009_x.py"], hook_surface=False
    ) == "critical"
