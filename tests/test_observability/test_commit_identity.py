"""Tests for the prefix-tolerant commit-identity compare (stdlib leaf shared by
the Part A guard and the Part B dashboard badge)."""

from __future__ import annotations

from genesis.observability.commit_identity import is_stale, same_commit

FULL = "0123456789abcdef0123456789abcdef01234567"
SHORT = FULL[:8]


def test_full_superset_of_short():
    assert same_commit(FULL, SHORT) is True
    assert same_commit(SHORT, FULL) is True


def test_equal():
    assert same_commit(FULL, FULL) is True


def test_different():
    assert same_commit(FULL, "fedcba98") is False


def test_none_and_empty_never_match():
    assert same_commit(None, SHORT) is False
    assert same_commit(SHORT, None) is False
    assert same_commit(FULL, "") is False
    assert same_commit("", "") is False


def test_middleware_reexport_is_the_same_callable():
    # Part A's guard imports it under the old name; must remain identical.
    from genesis.observability.mcp_middleware import _same_commit

    assert _same_commit is same_commit


# ── is_stale (identity AND time — the shared Part A / Part B verdict) ─────────


_BEHIND_AT = "2026-07-14T09:00:00+00:00"
_DEPLOY_AT = "2026-07-14T11:00:00+00:00"
_AHEAD_AT = "2026-07-14T13:00:00+00:00"


def test_stale_when_behind_and_differs():
    # commit differs AND deploy completed after spawn → behind → stale.
    assert is_stale(FULL, _BEHIND_AT, _DEPLOY_AT, "fedcba98") is True


def test_not_stale_when_same_commit_even_if_behind():
    assert is_stale(FULL, _BEHIND_AT, _DEPLOY_AT, SHORT) is False


def test_not_stale_when_ahead_of_deploy():
    # spawn started AFTER the deploy completed → ahead → not stale, even though
    # the commit differs (main tree advanced past the last recorded deploy).
    assert is_stale(FULL, _AHEAD_AT, _DEPLOY_AT, "fedcba98") is False


def test_fail_open_on_missing_inputs():
    assert is_stale(None, _BEHIND_AT, _DEPLOY_AT, "fedcba98") is False
    assert is_stale(FULL, None, _DEPLOY_AT, "fedcba98") is False
    assert is_stale(FULL, _BEHIND_AT, None, "fedcba98") is False
    assert is_stale(FULL, _BEHIND_AT, _DEPLOY_AT, None) is False


def test_fail_open_on_unparseable_timestamp():
    assert is_stale(FULL, "not-a-date", _DEPLOY_AT, "fedcba98") is False
    assert is_stale(FULL, _BEHIND_AT, "not-a-date", "fedcba98") is False


def test_mixed_offset_timestamps_compared_by_instant():
    # deploy completed_at is lexically smaller but a LATER instant than spawn_at.
    assert (
        is_stale(FULL, "2026-07-14T11:30:00+00:00", "2026-07-14T09:00:00-04:00", "fedcba98") is True
    )  # =13:00Z > 11:30Z
