"""Tests for the dashboard observation lifecycle-status derivation."""

from genesis.dashboard.routes.observations import _observation_status


def test_status_new_when_unread_and_active():
    assert _observation_status({}) == "new"
    assert _observation_status({"retrieved_count": 0, "surfaced_at": None}) == "new"


def test_status_read_via_retrieved_count():
    assert _observation_status({"retrieved_count": 1}) == "read"


def test_status_read_via_surfaced_at():
    assert _observation_status({"surfaced_at": "2026-01-01T00:00:00"}) == "read"


def test_status_acted_takes_precedence_over_read():
    assert (
        _observation_status({"influenced_action": 1, "retrieved_count": 3}) == "acted"
    )


def test_status_resolved_takes_top_precedence():
    assert (
        _observation_status(
            {"resolved": 1, "influenced_action": 1, "retrieved_count": 3}
        )
        == "resolved"
    )
