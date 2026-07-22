"""Unit tests for the degraded-banner cause attribution in the proactive hook.

Regression guard for the "genesis-server unreachable" mislabel: the hook used to
print *unreachable* for every server-path failure, including a reachable server
that merely returned a 503 (recall over its 4.5s budget) or was slow. These pin
that ``_server_failure_reason`` distinguishes a truly unreachable server
(connection refused / connect timeout) from a reachable-but-failing one, and that
``_format_degraded`` surfaces that cause in the banner instead of a blanket
"unreachable".
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

_REPO_DIR = Path(__file__).resolve().parent.parent.parent
_SCRIPTS_DIR = _REPO_DIR / "scripts"
_SRC_DIR = _REPO_DIR / "src"
for _p in (_SCRIPTS_DIR, _SRC_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import proactive_memory_hook as hook  # noqa: E402


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectError("connection refused"), httpx.ConnectTimeout("connect timed out")],
)
def test_reason_connect_failures_are_unreachable(exc: Exception):
    """Connection refused / connect-timeout are the ONLY truly-unreachable cases.

    ConnectTimeout is also an httpx.TimeoutException, so this pins that it is
    classified as unreachable (connect branch checked first), not as a slow recall.
    """
    reason = hook._server_failure_reason(exc=exc)
    assert "unreachable" in reason
    assert "connection refused" in reason


def test_reason_read_timeout_is_reachable_not_unreachable():
    reason = hook._server_failure_reason(exc=httpx.ReadTimeout("read timed out"))
    assert "unreachable" not in reason
    assert "timed out" in reason and "reachable" in reason


def test_reason_503_is_reachable_over_budget():
    reason = hook._server_failure_reason(status=503, detail="recall over budget")
    assert "unreachable" not in reason
    assert "503" in reason and "reachable" in reason
    assert "recall over budget" in reason  # server-provided detail is surfaced


def test_reason_500_is_reachable():
    reason = hook._server_failure_reason(status=500)
    assert "unreachable" not in reason
    assert "500" in reason and "reachable" in reason


def test_reason_generic_exception_is_reachable():
    reason = hook._server_failure_reason(exc=ValueError("boom"))
    assert "unreachable" not in reason
    assert "reachable" in reason


def test_banner_503_says_reachable_not_unreachable():
    """The core fix: a reachable-but-503 recall must NOT print 'unreachable'."""
    results = [{"memory_id": "abc12345", "content": "a recalled fact"}]
    reason = hook._server_failure_reason(status=503, detail="recall over budget")
    out = hook._format_degraded(results, reason=reason)
    banner = out.splitlines()[0]
    assert banner.startswith("[Memory recall degraded — ")
    assert "unreachable" not in banner
    assert "503" in banner and "reachable" in banner


def test_banner_unreachable_reason_preserved():
    """A genuinely unreachable server still reads 'unreachable' (unchanged path)."""
    results = [{"memory_id": "abc12345", "content": "a recalled fact"}]
    reason = hook._server_failure_reason(exc=httpx.ConnectError("refused"))
    banner = hook._format_degraded(results, reason=reason).splitlines()[0]
    assert "unreachable" in banner


def test_banner_forced_local_is_unchanged():
    """GENESIS_PROACTIVE_HOOK_MODE=local uses its own banner, ignoring reason."""
    results = [{"memory_id": "abc12345", "content": "a recalled fact"}]
    banner = hook._format_degraded(results, forced_local=True, reason="ignored").splitlines()[0]
    assert "local keyword-only mode" in banner
    assert "degraded" not in banner


def test_banner_none_reason_falls_back_safely():
    """A missing reason (defensive) still yields a coherent, non-crashing banner."""
    results = [{"memory_id": "abc12345", "content": "a recalled fact"}]
    banner = hook._format_degraded(results, reason=None).splitlines()[0]
    assert banner.startswith("[Memory recall degraded — ")
    assert "genesis-server unavailable" in banner
