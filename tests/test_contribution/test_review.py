"""Unit tests for genesis.contribution.review."""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from genesis.contribution import review


def _mk_codex_output(agent_text: str) -> str:
    """Compose a JSONL blob that looks like codex --json output."""
    lines = [
        json.dumps({
            "type": "item.completed",
            "item": {"type": "reasoning", "text": "thinking"},
        }),
        json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": agent_text},
        }),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 100}}),
    ]
    return "\n".join(lines)


def test_parse_verdict_pass():
    text = "Looks clean. No issues.\n\nVERDICT: PASS"
    passed, count, summary = review._parse_verdict(text)
    assert passed is True
    assert count == 0


def test_parse_verdict_fail_with_issues():
    text = (
        "Problems found:\n"
        "issue: missing null check\n"
        "concern: timing side channel\n"
        "VERDICT: FAIL"
    )
    passed, count, summary = review._parse_verdict(text)
    assert passed is False
    assert count >= 2


def test_parse_verdict_no_verdict_line():
    text = "just some text"
    passed, _, _ = review._parse_verdict(text)
    assert passed is False


def test_codex_missing_skipped():
    with patch("genesis.contribution.review.shutil.which", return_value=None):
        r = review._try_codex("diff text")
    assert r is None


def test_codex_success_passed():
    output = _mk_codex_output("All good.\n\nVERDICT: PASS")
    mock_proc = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=output, stderr="",
    )
    with (
        patch("genesis.contribution.review.shutil.which", return_value="/fake/codex"),
        patch("genesis.contribution.review.subprocess.run", return_value=mock_proc),
    ):
        r = review._try_codex("diff")
    assert r is not None
    assert r.reviewer == "codex"
    assert r.passed is True
    assert r.raw == "All good.\n\nVERDICT: PASS"


def test_codex_success_failed():
    output = _mk_codex_output(
        "issue: bad import\nconcern: race condition\nVERDICT: FAIL"
    )
    mock_proc = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=output, stderr="",
    )
    with (
        patch("genesis.contribution.review.shutil.which", return_value="/fake/codex"),
        patch("genesis.contribution.review.subprocess.run", return_value=mock_proc),
    ):
        r = review._try_codex("diff")
    assert r is not None
    assert r.passed is False
    assert r.finding_count >= 2


def test_codex_nonzero_returncode_returns_none():
    mock_proc = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="codex error",
    )
    with (
        patch("genesis.contribution.review.shutil.which", return_value="/fake/codex"),
        patch("genesis.contribution.review.subprocess.run", return_value=mock_proc),
    ):
        r = review._try_codex("diff")
    assert r is None


def test_codex_empty_output_returns_none():
    mock_proc = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr="",
    )
    with (
        patch("genesis.contribution.review.shutil.which", return_value="/fake/codex"),
        patch("genesis.contribution.review.subprocess.run", return_value=mock_proc),
    ):
        r = review._try_codex("diff")
    assert r is None


def test_codex_timeout_returns_none():
    def raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="codex", timeout=300)
    with (
        patch("genesis.contribution.review.shutil.which", return_value="/fake/codex"),
        patch("genesis.contribution.review.subprocess.run", side_effect=raise_timeout),
    ):
        r = review._try_codex("diff")
    assert r is None


def test_cc_reviewer_always_none():
    """MVP: cc-reviewer link unreachable from subprocess."""
    assert review._try_cc_reviewer("diff") is None


def test_native_always_none():
    """MVP: genesis-native is a 6.2+ placeholder."""
    assert review._try_genesis_native("diff") is None


def test_chain_first_success_codex():
    """First-success ordering: codex wins, other links not tried."""
    fake_result = review.ReviewResult(
        available=True, reviewer="codex", passed=True,
    )
    with (
        patch("genesis.contribution.review._try_codex", return_value=fake_result),
        patch("genesis.contribution.review._try_cc_reviewer") as cc,
        patch("genesis.contribution.review._try_genesis_native") as native,
    ):
        r = review.run_review_chain("diff")
    assert r.reviewer == "codex"
    cc.assert_not_called()
    native.assert_not_called()


def test_chain_full_failure_unavailable():
    with (
        patch("genesis.contribution.review._try_codex", return_value=None),
        patch("genesis.contribution.review._try_cc_reviewer", return_value=None),
        patch("genesis.contribution.review._try_genesis_native", return_value=None),
    ):
        r = review.run_review_chain("diff")
    assert r.available is False
    assert r.reviewer is None


def test_chain_skip_codex():
    cc_result = review.ReviewResult(available=True, reviewer="cc-reviewer", passed=True)
    with (
        patch("genesis.contribution.review._try_codex") as codex,
        patch("genesis.contribution.review._try_cc_reviewer", return_value=cc_result),
    ):
        r = review.run_review_chain("diff", skip_codex=True)
    codex.assert_not_called()
    assert r.reviewer == "cc-reviewer"


def test_write_review_log(tmp_path):
    result = review.ReviewResult(
        available=True, reviewer="codex", passed=True,
        finding_count=0, summary="clean", raw="full output",
    )
    out = tmp_path / "review.json"
    review.write_review_log(result, out)
    data = json.loads(out.read_text())
    assert data["reviewer"] == "codex"
    assert data["passed"] is True
