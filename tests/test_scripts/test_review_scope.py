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

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_SCRIPT_PATH = _SCRIPTS / "review_scope.py"
_spec = importlib.util.spec_from_file_location("review_scope", _SCRIPT_PATH)
_rs = importlib.util.module_from_spec(_spec)
sys.modules["review_scope"] = _rs
_spec.loader.exec_module(_rs)


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
