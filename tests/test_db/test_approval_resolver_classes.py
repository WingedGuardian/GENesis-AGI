"""Drift guard for the free-text ``resolved_by`` → resolver-class convention.

``approval_requests.resolved_by`` has no CHECK constraint — its values are a
convention spread across several writers. ``classify_resolver`` is the ONE
canonical mapping (J-9 ``approvals`` metrics depend on it); this test pins
every known writer literal and the full live-DB value inventory so the
convention can't drift silently.

**When you add a new ``resolved_by`` writer anywhere in the codebase, add its
literal here** (and, if it's a new class prefix, to the prefix tuples in
``genesis/db/crud/approval_requests.py``). A novel value that matches neither
prefix list classifies as ``unknown`` — visible in the weekly ``approvals``
snapshot as ``unknown_resolver_count``, never silently bucketed.
"""

from __future__ import annotations

import pytest

from genesis.db.crud.approval_requests import (
    HUMAN_RESOLVER_PREFIXES,
    SYSTEM_RESOLVER_PREFIXES,
    classify_resolver,
)

# Every in-tree writer literal, with its source location, plus the values
# observed in the live DB as of 2026-07-09 that have no in-tree writer
# (one-off manual DB fixes) — those must classify as ``unknown``.
_CASES = [
    # ── human: Telegram handlers (channels/telegram/_handler_messages.py) ──
    ("telegram:batch:12345", "human"),
    ("telegram:button:12345", "human"),
    ("telegram:bare_text:12345", "human"),
    # human: approval-channel reply (autonomy/approval_gate.py ~:347)
    ("telegram:reply", "human"),
    # human: dashboard (dashboard/routes/state.py :88/:106)
    ("dashboard", "human"),
    ("dashboard:batch", "human"),
    # human: voice (channels/voice/genesis_bridge.py ~:248)
    ("voice:s2s", "human"),
    # human: operator sessions
    ("manual:cc_session", "human"),
    ("manual:genesis_session", "human"),
    # human: ApprovalManager.resolve() default (autonomy/approval.py ~:95)
    ("user", "human"),
    # ── system: fail-closed cancel (autonomy/approval.py ~:121) ──
    ("system", "system"),
    # system: gate timeout (autonomy/approval_gate.py ~:110)
    ("timeout_auto_expire", "system"),
    # system: sentinel alarm clear (sentinel/dispatcher.py ~:1767)
    ("alarm_cleared", "system"),
    # system: housekeeping jobs
    ("cleanup:self-send-spam", "system"),
    ("cleanup:orphaned-pre-fix-approval", "system"),
    # ── blank: bulk expiry (crud expire_timed_out never writes resolved_by) ──
    (None, "system"),
    ("", "system"),
    ("   ", "system"),
    # ── unknown: live values with NO in-tree writer — never guessed ──
    ("manual_clear_fallow_recovery", "unknown"),
    ("manual_stale_cleanup", "unknown"),
    # unknown: a hypothetical future channel nobody registered
    ("app:button:99", "unknown"),
]


@pytest.mark.parametrize(("resolved_by", "expected"), _CASES)
def test_classify_resolver(resolved_by, expected):
    assert classify_resolver(resolved_by) == expected


def test_manual_colon_vs_manual_underscore_distinction():
    """``manual:`` (operator session) is human; ``manual_*`` (one-off DB fix
    scripts with no in-tree writer) is unknown — the colon is load-bearing."""
    assert classify_resolver("manual:cc_session") == "human"
    assert classify_resolver("manual_stale_cleanup") == "unknown"


def test_prefix_lists_do_not_overlap():
    """No value can match both a human and a system prefix."""
    for h in HUMAN_RESOLVER_PREFIXES:
        for s in SYSTEM_RESOLVER_PREFIXES:
            assert not h.startswith(s) and not s.startswith(h), (h, s)
