"""Tests for genesis.observability.deploy_record.row_facts."""

import pytest

from genesis.observability.deploy_record import (
    NOT_RESTARTED_MARKER,
    degraded_markers,
    row_facts,
)


@pytest.mark.parametrize(
    ("status", "degraded", "expected"),
    [
        ("success", None, (True, True, True)),
        ("success", "", (True, True, True)),
        ("success", NOT_RESTARTED_MARKER, (True, True, False)),
        (
            "success",
            "check:no-baseline, genesis-server-not-restarted",
            (True, True, False),
        ),
        # Exact-token semantics: a longer token must NOT match.
        ("success", "genesis-server-not-restarted-extra", (True, True, True)),
        ("failed", None, (False, False, None)),
        ("failed", NOT_RESTARTED_MARKER, (False, False, None)),
        ("rolled_back", None, (False, False, None)),
        ("conflicts_pending", None, (False, False, None)),
        ("crashed_recovered", None, (False, False, None)),
        (None, None, (False, False, None)),
    ],
)
def test_row_facts(status, degraded, expected) -> None:
    facts = row_facts(status, degraded)
    assert (facts.code_applied, facts.activation_applied, facts.server_restarted) == expected
    assert facts.as_dict() == {
        "code_applied": expected[0],
        "activation_applied": expected[1],
        "server_restarted": expected[2],
    }


def test_degraded_markers_strips_and_drops_empties() -> None:
    assert degraded_markers(None) == ()
    assert degraded_markers("") == ()
    assert degraded_markers("  a , ,b ") == ("a", "b")
