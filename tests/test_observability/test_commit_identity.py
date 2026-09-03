"""Tests for the commit-identity leaf: the prefix-tolerant compare, plus the
TWO verdicts it deliberately carries.

`is_behind` (AWARENESS — spawn vs live HEAD) drives the advisory surfaces: the
per-prompt deploy nudge and the dashboard stale-code badge. `is_stale`
(AUTHORIZATION — spawn vs the last RECORDED deploy) drives the MCP middleware
guard, which BLOCKS a tool, and stays deliberately conservative. The
`test_the_two_verdicts_*` cases below pin that divergence so a future change
cannot quietly collapse them into one.
"""

from __future__ import annotations

from genesis.observability.commit_identity import is_behind, is_stale, same_commit

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


# ── is_behind (AWARENESS: spawn vs live HEAD) ───────────────────────────────


def test_behind_when_commits_differ():
    assert is_behind(FULL, "fedcba9876543210fedcba9876543210fedcba98") is True


def test_not_behind_at_head():
    assert is_behind(FULL, FULL) is False


def test_not_behind_when_head_is_the_short_form_of_spawn():
    # A short/full mismatch is the SAME commit, not a deploy.
    assert is_behind(FULL, SHORT) is False
    assert is_behind(SHORT, FULL) is False


def test_behind_fails_open_on_missing_inputs():
    assert is_behind(None, FULL) is False
    assert is_behind(FULL, None) is False
    assert is_behind("", FULL) is False
    assert is_behind(FULL, "") is False


def test_behind_has_no_time_axis():
    """Unlike is_stale, is_behind takes no timestamps: nothing can be ahead of
    live HEAD, so there is no ahead/behind ambiguity to resolve."""
    import inspect

    assert list(inspect.signature(is_behind).parameters) == ["spawn_commit", "head_commit"]


# ── the two verdicts are deliberately different ─────────────────────────────


def test_the_two_verdicts_diverge_on_the_measured_live_case():
    """The defect that motivated `is_behind`, as measured on a live install.

    A session spawned 2026-09-02 was 15 commits behind HEAD, while the newest
    `update_history` row was completed 2026-08-31 — BEFORE the spawn. `is_stale`
    requires the recorded deploy to postdate the spawn, so it is silent; the
    session really was behind, and `is_behind` says so.
    """
    spawn, spawn_at = FULL, "2026-09-02T19:37:13+00:00"
    head = "fedcba9876543210fedcba9876543210fedcba98"
    recorded_deploy_at, recorded_commit = "2026-08-31T21:48:19+00:00", "87590955"

    assert is_stale(spawn, spawn_at, recorded_deploy_at, recorded_commit) is False
    assert is_behind(spawn, head) is True


def test_the_two_verdicts_agree_when_the_record_is_current():
    """When update_history HAS kept up, both verdicts fire — the conservative
    one is a subset of the accurate one, never a contradiction."""
    spawn, spawn_at = FULL, "2026-08-20T00:00:00+00:00"
    head = "fedcba9876543210fedcba9876543210fedcba98"

    assert is_stale(spawn, spawn_at, "2026-08-21T00:00:00+00:00", head) is True
    assert is_behind(spawn, head) is True
