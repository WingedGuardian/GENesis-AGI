"""Unit tests for genesis.contribution.review."""
from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

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


def _popen_mock(stdout: str = "", returncode: int = 0, pid: int = 54321) -> MagicMock:
    """A Popen stand-in. pid is EXPLICIT — never rely on a mock default
    (int(MagicMock().pid) coerces to 1 → the killpg(1) trap)."""
    m = MagicMock()
    m.pid = pid
    m.returncode = returncode
    m.communicate.return_value = (stdout, "")
    return m


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
    with (
        patch("genesis.contribution.review.shutil.which", return_value="/fake/codex"),
        patch("genesis.contribution.review.subprocess.Popen", return_value=_popen_mock(output)),
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
    with (
        patch("genesis.contribution.review.shutil.which", return_value="/fake/codex"),
        patch("genesis.contribution.review.subprocess.Popen", return_value=_popen_mock(output)),
    ):
        r = review._try_codex("diff")
    assert r is not None
    assert r.passed is False
    assert r.finding_count >= 2


def test_codex_nonzero_returncode_returns_none():
    with (
        patch("genesis.contribution.review.shutil.which", return_value="/fake/codex"),
        patch(
            "genesis.contribution.review.subprocess.Popen",
            return_value=_popen_mock("", returncode=1),
        ),
    ):
        r = review._try_codex("diff")
    assert r is None


def test_codex_empty_output_returns_none():
    with (
        patch("genesis.contribution.review.shutil.which", return_value="/fake/codex"),
        patch("genesis.contribution.review.subprocess.Popen", return_value=_popen_mock("")),
    ):
        r = review._try_codex("diff")
    assert r is None


def test_codex_spawn_oserror_returns_none():
    with (
        patch("genesis.contribution.review.shutil.which", return_value="/fake/codex"),
        patch(
            "genesis.contribution.review.subprocess.Popen",
            side_effect=OSError("spawn failed"),
        ),
    ):
        r = review._try_codex("diff")
    assert r is None


class TestCodexTimeoutTreeKill:
    """Timeout must SIGKILL codex's whole process GROUP (codex is a Node
    launcher — killing only the direct child orphans its workers), signalling
    proc.pid AS the pgid (never os.getpgid — it raises once the leader is
    reaped, leaking the tree), then drain with a BOUNDED communicate."""

    def _run_timeout(self, proc: MagicMock):
        with (
            patch("genesis.contribution.review.shutil.which", return_value="/fake/codex"),
            patch("genesis.contribution.review.subprocess.Popen", return_value=proc),
            patch("genesis.util.proc_kill.os.killpg") as killpg,
        ):
            r = review._try_codex("diff")
        return r, killpg

    def test_timeout_group_kills_by_pid(self):
        proc = _popen_mock(pid=54321)
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="codex", timeout=300),
            ("", ""),  # bounded drain
        ]
        r, killpg = self._run_timeout(proc)
        assert r is None
        killpg.assert_called_once()
        assert killpg.call_args.args[0] == 54321
        proc.kill.assert_not_called()

    def test_timeout_pgid_guard_refuses_group_kill(self):
        proc = _popen_mock(pid=1)
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="codex", timeout=300),
            ("", ""),
        ]
        r, killpg = self._run_timeout(proc)
        assert r is None
        killpg.assert_not_called()
        proc.kill.assert_called_once()

    def test_timeout_drain_is_bounded_and_survives_second_timeout(self):
        proc = _popen_mock(pid=54321)
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="codex", timeout=300),
            subprocess.TimeoutExpired(cmd="codex", timeout=30),  # drain times out too
        ]
        r, killpg = self._run_timeout(proc)
        assert r is None  # still degrades, never raises
        killpg.assert_called_once()
        # the drain was attempted with a timeout kwarg (bounded, not blocking)
        drain_call = proc.communicate.call_args_list[1]
        assert drain_call.kwargs.get("timeout") is not None

    def test_keyboard_interrupt_kills_group_and_propagates(self):
        """start_new_session detaches codex from the CLI's terminal group, so
        a human's Ctrl+C never reaches it — the handler must group-kill before
        propagating, or the interrupt itself orphans the tree."""
        proc = _popen_mock(pid=54321)
        proc.communicate.side_effect = [KeyboardInterrupt(), ("", "")]
        with (
            patch("genesis.contribution.review.shutil.which", return_value="/fake/codex"),
            patch("genesis.contribution.review.subprocess.Popen", return_value=proc),
            patch("genesis.util.proc_kill.os.killpg") as killpg,
            pytest.raises(KeyboardInterrupt),
        ):
            review._try_codex("diff")
        killpg.assert_called_once()
        assert killpg.call_args.args[0] == 54321


def test_codex_spawned_in_new_session():
    """codex must be spawned with start_new_session=True (own group for the
    tree-kill) and NOT via preexec_fn (post-fork Python deadlock risk)."""
    captured: dict = {}

    def _fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return _popen_mock(_mk_codex_output("ok\nVERDICT: PASS"))

    with (
        patch("genesis.contribution.review.shutil.which", return_value="/fake/codex"),
        patch("genesis.contribution.review.subprocess.Popen", side_effect=_fake_popen),
    ):
        r = review._try_codex("diff")
    assert r is not None
    assert captured.get("start_new_session") is True
    assert "preexec_fn" not in captured


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
