"""Tests for the hook-surface merge teeth in git_push_guard.

Two mechanical rules scoped to the enforcement-hook surface (scripts/hooks/**,
bash_safety_hook.sh, review_scope/review_state, .claude/settings.json,
.claude/hooks/**):

1. A stale-review delta touching the hook surface is NEVER "review-trivial" —
   a "small single-file touch-up" to an enforcement hook is exactly what must
   not skip re-review (_classify_post_review_delta returns "substantial").
2. ``# stale-review-override`` on a PR whose diff touches the hook surface
   additionally requires a recorded fallback-review evidence file keyed to the
   PR's EXACT head sha — the mechanical form of "override needs user
   authorization + a fallback review (local codex / Claude Code adversarial)".

Network-free via the _TEST_GH_* env-injection seams (mirrors the freshness
tests in test_git_push_guard_codex_freshness.py).
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_HOOKS_DIR = _WORKTREE / "scripts" / "hooks"
_spec = importlib.util.spec_from_file_location("git_push_guard", _HOOKS_DIR / "git_push_guard.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

HEAD = "0cd13afeb51025af5dc7bd24df1ffa57cd2babab"
STALE = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def _compare_json(*filenames, status="ahead", previous=None):
    """_TEST_GH_COMPARE shape: the object _classify_post_review_delta fetches."""
    files = []
    for name in filenames:
        entry = {
            "filename": name,
            "additions": 2,
            "deletions": 1,
            "status": "modified",
            "previous_filename": None,
            "has_patch": True,
        }
        files.append(entry)
    if previous:
        files.append(
            {
                "filename": previous[1],
                "additions": 0,
                "deletions": 0,
                "status": "renamed",
                "previous_filename": previous[0],
                "has_patch": False,
            }
        )
    return json.dumps({"status": status, "files": files})


def _files_jsonl(*filenames, previous=None):
    """_TEST_GH_PR_FILES shape: one JSON object per line from pulls/N/files."""
    lines = [json.dumps({"filename": f, "previous_filename": None}) for f in filenames]
    if previous:
        lines.append(json.dumps({"filename": previous[1], "previous_filename": previous[0]}))
    return "\n".join(lines)


class TestHookSurfaceMatcher:
    @pytest.mark.parametrize(
        "path",
        [
            "scripts/hooks/git_push_guard.py",
            "scripts/hooks/shell_parse.py",
            "scripts/hooks/newly_added_guard.py",
            "scripts/bash_safety_hook.sh",
            "scripts/review_scope.py",
            "scripts/review_state.py",
            ".claude/settings.json",
            ".claude/hooks/genesis-hook",
        ],
    )
    def test_hook_surface_paths_match(self, path):
        assert _mod._is_hook_surface_path(path)

    @pytest.mark.parametrize(
        "path",
        [
            "src/genesis/cc/conversation.py",
            "tests/test_hooks/test_git_push_guard_hook_surface.py",
            "docs/architecture/CURRENT.md",
            "scripts/backup.sh",
            "scripts/hooks_readme.md",  # prefix must be a path segment, not a substring
            ".claude/settings.local.json",
            ".claude/skills/genesis-development/SKILL.md",
        ],
    )
    def test_non_hook_surface_paths_do_not_match(self, path):
        assert not _mod._is_hook_surface_path(path)


class TestHookSurfaceDeltaNeverTrivial:
    """Delta A: hook-surface files in the unreviewed compare force 'substantial'."""

    def test_docs_only_delta_still_inline(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_COMPARE", _compare_json("README.md"))
        assert _mod._classify_post_review_delta(STALE, HEAD, None) == "inline"

    def test_hook_file_delta_is_substantial(self, monkeypatch):
        # A tiny 2-line touch-up that review_scope would classify inline —
        # but it touches an enforcement hook, so it is substantial by fiat.
        monkeypatch.setenv("_TEST_GH_COMPARE", _compare_json("scripts/hooks/git_push_guard.py"))
        assert _mod._classify_post_review_delta(STALE, HEAD, None) == "substantial"

    def test_hook_file_mixed_with_docs_is_substantial(self, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_COMPARE", _compare_json("README.md", "scripts/bash_safety_hook.sh")
        )
        assert _mod._classify_post_review_delta(STALE, HEAD, None) == "substantial"

    def test_rename_from_hook_surface_is_substantial(self, monkeypatch):
        # A rename OUT of the hook surface (guard deleted/moved) is a hook change.
        monkeypatch.setenv(
            "_TEST_GH_COMPARE",
            _compare_json(previous=("scripts/hooks/git_discard_guard.py", "attic/old.py")),
        )
        assert _mod._classify_post_review_delta(STALE, HEAD, None) == "substantial"


class TestOverrideEvidenceGate:
    """Delta B: # stale-review-override on a hook-surface PR needs evidence."""

    @pytest.fixture(autouse=True)
    def _evidence_dir(self, tmp_path, monkeypatch):
        self.dir = tmp_path / "override_evidence"
        monkeypatch.setenv("GENESIS_OVERRIDE_REVIEW_EVIDENCE_DIR", str(self.dir))
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", "")

    def test_non_hook_pr_force_passes_without_evidence(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files_jsonl("src/genesis/cc/types.py"))
        should_block, msg, verified = _mod._check_codex_reviewed_head("1", force=True)
        assert not should_block

    def test_hook_pr_force_without_evidence_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files_jsonl("scripts/hooks/git_push_guard.py"))
        should_block, msg, verified = _mod._check_codex_reviewed_head("1", force=True)
        assert should_block
        assert "fallback-review evidence" in msg
        assert HEAD[:12] in msg  # message names the head the evidence must be keyed to

    def test_hook_pr_force_with_evidence_passes(self, monkeypatch, capsys):
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files_jsonl("scripts/hooks/git_push_guard.py"))
        self.dir.mkdir(parents=True)
        (self.dir / f"local__1__{HEAD}.txt").write_text(
            "reviewer: claude-code adversarial (codex quota-dead)\nfindings: none\n"
        )
        should_block, msg, verified = _mod._check_codex_reviewed_head("1", force=True)
        assert not should_block

    def test_hook_pr_force_with_empty_evidence_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files_jsonl("scripts/hooks/git_push_guard.py"))
        self.dir.mkdir(parents=True)
        (self.dir / f"local__1__{HEAD}.txt").write_text("")
        should_block, msg, verified = _mod._check_codex_reviewed_head("1", force=True)
        assert should_block

    def test_hook_pr_force_evidence_for_wrong_sha_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files_jsonl("scripts/hooks/git_push_guard.py"))
        self.dir.mkdir(parents=True)
        (self.dir / f"local__1__{STALE}.txt").write_text("evidence for an older head\n")
        should_block, msg, verified = _mod._check_codex_reviewed_head("1", force=True)
        assert should_block

    def test_unreadable_pr_files_blocks(self, monkeypatch):
        # fail-closed: if the diff can't be read, assume hook surface
        monkeypatch.setenv("_TEST_GH_PR_FILES", "__error__")
        should_block, msg, verified = _mod._check_codex_reviewed_head("1", force=True)
        assert should_block

    def test_unreadable_head_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files_jsonl("scripts/hooks/git_push_guard.py"))
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", "")
        should_block, msg, verified = _mod._check_codex_reviewed_head("1", force=True)
        assert should_block

    def test_rename_out_of_hook_surface_counts(self, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_PR_FILES",
            _files_jsonl(previous=("scripts/hooks/git_discard_guard.py", "attic/x.py")),
        )
        should_block, msg, verified = _mod._check_codex_reviewed_head("1", force=True)
        assert should_block

    def test_force_never_returns_verified_head(self, monkeypatch):
        # the force path must keep its documented "conscious unbound merge"
        # contract: no --match-head-commit binding is derived from it
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files_jsonl("README.md"))
        should_block, msg, verified = _mod._check_codex_reviewed_head("1", force=True)
        assert not should_block
        assert verified is None

    def test_malformed_head_sha_blocks(self, monkeypatch):
        # network-sourced string becomes a path component — garbage blocks
        # explicitly, never a silently unmatchable filename
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files_jsonl("scripts/hooks/git_push_guard.py"))
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", "../../../etc/passwd")
        should_block, msg, verified = _mod._check_codex_reviewed_head("1", force=True)
        assert should_block


class TestChangedFilesFailDirections:
    """The three fail-closed parse paths of _pr_changed_files (architect NOTE 4)."""

    def test_malformed_json_line_returns_none(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_FILES", "not json at all")
        assert _mod._pr_changed_files("1") is None

    def test_non_dict_line_returns_none(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_FILES", json.dumps(["a", "list"]))
        assert _mod._pr_changed_files("1") is None

    def test_pagination_cap_returns_none(self, monkeypatch):
        lines = "\n".join(
            json.dumps({"filename": f"src/f{i}.py", "previous_filename": None}) for i in range(3000)
        )
        monkeypatch.setenv("_TEST_GH_PR_FILES", lines)
        assert _mod._pr_changed_files("1") is None


class TestWiredHooksFenceGuardrail:
    """Every hook wired in .claude/settings.json must be inside the merge-teeth
    fence — a future wiring that falls outside it is a CI failure, keeping
    _HOOK_SURFACE_FILES self-maintaining (architect SHOULD-FIX 2026-08-23: the
    named-list-as-sample trap; review_enforcement_commit.py et al. lived outside
    the original 4-file fence)."""

    def _wired_hook_paths(self):
        cfg = json.loads((_WORKTREE / ".claude" / "settings.json").read_text())
        wired = set()
        for entries in cfg.get("hooks", {}).values():
            for entry in entries:
                for h in entry.get("hooks", []):
                    cmd = h.get("command", "")
                    # ${CLAUDE_PROJECT_DIR}/.claude/hooks/genesis-hook <name>
                    # resolves <name> as scripts/<name> (see genesis-hook:41)
                    for m in re.finditer(r"genesis-hook\s+(\S+)", cmd):
                        wired.add(f"scripts/{m.group(1)}")
                    # direct bash/python wiring of a scripts/ path
                    for m in re.finditer(
                        r"(?:bash|python3?)\s+(?:\$\{?CLAUDE_PROJECT_DIR\}?/)?(scripts/\S+)",
                        cmd,
                    ):
                        wired.add(m.group(1))
        return wired

    def test_wiring_discovery_is_not_vacuous(self):
        wired = self._wired_hook_paths()
        assert "scripts/hooks/git_push_guard.py" in wired
        assert "scripts/review_enforcement_commit.py" in wired
        assert len(wired) >= 20

    def test_every_wired_hook_is_inside_the_fence(self):
        outside = sorted(p for p in self._wired_hook_paths() if not _mod._is_hook_surface_path(p))
        assert not outside, (
            f"hooks wired in .claude/settings.json but OUTSIDE the merge-teeth "
            f"fence — add them to _HOOK_SURFACE_FILES in git_push_guard.py: {outside}"
        )


class TestRound1P2Regressions:
    """Codex round-1 P2s: record validation, row-count cap, config fence."""

    def test_null_filename_record_fails_closed(self, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_PR_FILES", '{"filename": null, "previous_filename": null}'
        )
        assert _mod._pr_changed_files("1") is None

    def test_empty_filename_record_fails_closed(self, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_PR_FILES", '{"filename": "", "previous_filename": null}'
        )
        assert _mod._pr_changed_files("1") is None

    def test_non_string_previous_filename_fails_closed(self, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_PR_FILES", '{"filename": "a.py", "previous_filename": 7}'
        )
        assert _mod._pr_changed_files("1") is None

    def test_cap_counts_rows_not_expanded_paths(self, monkeypatch):
        # 1500 renames = 3000 expanded paths but only 1500 API rows — must NOT
        # trip the 3000-row cap (the old expanded-path count false-closed here).
        lines = "\n".join(
            json.dumps({"filename": f"new{i}.py", "previous_filename": f"old{i}.py"})
            for i in range(1500)
        )
        monkeypatch.setenv("_TEST_GH_PR_FILES", lines)
        files = _mod._pr_changed_files("1")
        assert files is not None
        assert len(files) == 3000

    @pytest.mark.parametrize(
        "path",
        [
            "config/protected_paths.yaml",
            "config/repo_topology.yaml",
            "config/behavioral_rules/no_hide_problems.yaml",
            "config/behavioral_rules/some_future_rule.yaml",
        ],
    )
    def test_hook_owned_config_is_fenced(self, path):
        # Decision-driving config IS enforcement surface: the ordinary
        # substantiality classifier treats YAML as review-trivial, so a
        # rewrite removing blocking patterns could otherwise merge stale.
        assert _mod._is_hook_surface_path(path)

    def test_unrelated_config_not_fenced(self):
        assert not _mod._is_hook_surface_path("config/genesis.yaml.example")

    def test_evidence_identity_includes_repo_and_pr(self, tmp_path, monkeypatch):
        # Codex P2: sha-only evidence would vouch across PRs/repos sharing a
        # head. Evidence written for (repo A, PR 1) must NOT pass (repo B, PR 2).
        d = tmp_path / "ev"
        d.mkdir()
        monkeypatch.setenv("GENESIS_OVERRIDE_REVIEW_EVIDENCE_DIR", str(d))
        monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
        monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", "")
        monkeypatch.setenv(
            "_TEST_GH_PR_FILES", _files_jsonl("scripts/hooks/git_push_guard.py")
        )
        (d / f"ownera_repoa__1__{HEAD}.txt").write_text("evidence for repo A PR 1\n")
        blocked, msg, _ = _mod._check_codex_reviewed_head(
            "2", force=True, repo="ownerb/repob"
        )
        assert blocked
        blocked, msg, _ = _mod._check_codex_reviewed_head(
            "1", force=True, repo="ownera/repoa"
        )
        assert not blocked
