"""Codex round-escalation gate on `gh pr comment … @codex review`.

Once a PR already carries ESCALATION_ROUND_CAP Codex reviews, requesting
another round must BLOCK with the step-back advisory (triage / mechanism /
state-space) until a trailing '# escalation-ack'. Companion to the commit
gate's Rule 3: that counter tracks LOCAL review rounds and stays asleep when
every local review is clean while the loop churns through CODEX rounds — the
2026-08-12 MW-3 whack-a-mole blind spot this gate closes.

Network-free via the existing _TEST_GH_CODEX_REVIEWS seam (JSONL, one
{login, commit_id} per line — same seam the reviewed-head gate uses).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "hooks"
_spec = importlib.util.spec_from_file_location(
    "git_push_guard_escalation_under_test", _HOOKS_DIR / "git_push_guard.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

CODEX = "chatgpt-codex-connector[bot]"
CAP = _mod.ESCALATION_ROUND_CAP


def _reviews_jsonl(*commit_ids: str, login: str = CODEX) -> str:
    return "\n".join(json.dumps({"login": login, "commit_id": cid}) for cid in commit_ids)


def _shas(n: int) -> list[str]:
    return [f"{i:x}" * 40 for i in range(1, n + 1)]


def _segs(cmd: str):
    return _mod.analyze(cmd)


def _check(cmd: str):
    return _mod._check_codex_round_escalation(_segs(cmd))


TRIGGER = 'gh pr comment 1372 --body "@codex review"'


class TestCountHelper:
    def test_counts_only_codex_reviews(self, monkeypatch):
        mixed = _reviews_jsonl(*_shas(2)) + "\n" + _reviews_jsonl("f" * 40, login="some-human")
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", mixed)
        ids = _mod._codex_review_commit_ids("1372")
        assert ids is not None and len(ids) == 2

    def test_empty_seam_is_zero_not_none(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "")
        assert _mod._codex_review_commit_ids("1372") == []

    def test_latest_sha_still_last_entry(self, monkeypatch):
        # Refactor regression: the #1366 reviewed-head gate consumes the SAME
        # fetch — the latest sha must remain the LAST codex entry.
        shas = _shas(3)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*shas))
        assert _mod._latest_codex_reviewed_sha("1372") == shas[-1]

    def test_api_error_returns_none(self, monkeypatch):
        monkeypatch.delenv("_TEST_GH_CODEX_REVIEWS", raising=False)

        def boom(*a, **k):
            raise OSError("no network in tests")

        monkeypatch.setattr(_mod.subprocess, "run", boom)
        assert _mod._codex_review_commit_ids("1372") is None


class TestEscalationGate:
    def test_at_cap_blocks_with_step_back_order(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        block, msg = _check(TRIGGER)
        assert block is True
        low = msg.lower()
        # The advisory must carry all three disciplines + the continue path.
        assert "triage" in low
        assert "mechanism" in low
        assert "state" in low and "enumerate" in low
        assert "revert" in low
        assert "escalation-ack" in msg

    def test_above_cap_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP + 2)))
        block, _ = _check(TRIGGER)
        assert block is True

    def test_below_cap_allows_silently(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP - 1)))
        assert _check(TRIGGER) == (False, "")

    def test_ack_sigil_allows(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP + 1)))
        block, _ = _check(TRIGGER + "  # escalation-ack")
        assert block is False

    def test_non_codex_comment_untouched(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        assert _check('gh pr comment 1372 --body "great work"') == (False, "")

    def test_non_comment_commands_untouched(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        for cmd in ("git status", "gh pr view 1372", "echo '@codex review'"):
            assert _check(cmd) == (False, "")

    def test_numberless_comment_fails_open(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        assert _check('gh pr comment --body "@codex review"') == (False, "")

    def test_api_error_fails_open(self, monkeypatch):
        monkeypatch.delenv("_TEST_GH_CODEX_REVIEWS", raising=False)

        def boom(*a, **k):
            raise OSError("no network in tests")

        monkeypatch.setattr(_mod.subprocess, "run", boom)
        assert _check(TRIGGER) == (False, "")

    def test_url_and_hash_pr_forms_resolve(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        for form in (
            'gh pr comment "#1372" --body "@codex review"',
            'gh pr comment https://github.com/o/r/pull/1372 --body "@codex review"',
        ):
            block, _ = _check(form)
            assert block is True, form

    def test_explicit_repo_flag_parsed(self):
        argv = ["gh", "pr", "comment", "7", "--repo", "o/r", "--body", "x"]
        assert _mod._comment_repo(argv) == "o/r"
        assert _mod._comment_pr_number(argv) == "7"


class TestSoftDependency:
    def test_module_loads_without_review_state(self, monkeypatch):
        """CRITICAL (reviewer, round 1): an unguarded top-level import of
        review_state would crash the WHOLE guard at module load (exit 1 → CC
        treats as non-blocking → every fail-closed gate silently gone). The
        import must be soft: review_state unimportable → module still loads,
        cap falls back to its documented default."""
        import sys as _sys

        monkeypatch.setitem(_sys.modules, "review_state", None)  # forces ImportError
        spec = importlib.util.spec_from_file_location(
            "git_push_guard_no_review_state", _HOOKS_DIR / "git_push_guard.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # must NOT raise
        assert mod.ESCALATION_ROUND_CAP == 3  # documented fallback = the prose cap


class TestMainWiring:
    def _run_main(self, monkeypatch, cmd: str) -> int:
        monkeypatch.setattr(_mod, "read_payload", lambda: {"tool_input": {"command": cmd}})
        return _mod.main()

    def test_main_blocks_at_cap(self, monkeypatch, capsys):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        rc = self._run_main(monkeypatch, TRIGGER)
        assert rc == 2
        assert "escalation-ack" in capsys.readouterr().err

    def test_main_allows_below_cap(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP - 1)))
        assert self._run_main(monkeypatch, TRIGGER) == 0

    def test_main_allows_with_ack(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        assert self._run_main(monkeypatch, TRIGGER + "  # escalation-ack") == 0
