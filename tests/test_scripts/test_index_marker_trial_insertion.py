"""Selected insertion stages a real context without coalescing or publishing work."""

from contextlib import closing

import pytest

from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue


class AbortTrial(Exception):
    pass


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("debt", [0, 4, 5, 7])
def test_issued_nonce_and_real_debt_are_staged_without_touching_independent_pending(
    queue, tmp_path, provider, debt
):
    insert = getattr(queue._legs, "_insert_claim", None)
    assert callable(insert), (
        "selected trial must reuse the real claim insertion with its issued nonce"
    )
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "both", "fast")
    h = queue.marker_hash(repo)
    pending = [{k: v for k, v in row.items() if k != "age_s"} for row in queue.list_markers()]
    with pytest.raises(AbortTrial), closing(queue._connect()) as db, queue._transaction(db):
        row = dict(
            db.execute(
                "SELECT * FROM pending WHERE hash=? AND provider=?", (h, provider)
            ).fetchone()
        )
        row.update(mode="full", attempts=debt)
        claimed = insert(
            db, h, provider, row, protocol=3, nonce="b" * 32, origin="selected-recovery"
        )
        assert claimed["context"]["claim_id"] == "b" * 32 and claimed["attempts"] == debt
        assert claimed["context"]["protocol"] == 3 and claimed["mode"] == "full"
        retained = dict(db.execute("SELECT * FROM executions").fetchone())
        assert retained["nonce_origin"] == "selected-recovery" and retained["attempts"] == debt
        assert db.execute("SELECT count(*) FROM pending").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM inflight").fetchone()[0] == 1
        # A helper cannot publish an open/unreserved selected trial: its caller
        # still owes atomic actual-owner reservation and the final decision.
        raise AbortTrial
    assert [
        {k: v for k, v in row.items() if k != "age_s"} for row in queue.list_markers()
    ] == pending
    with closing(queue._connect()) as db:
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM inflight").fetchone()[0] == 0


@pytest.mark.parametrize(
    "nonce,origin,protocol",
    [
        ("bad", "selected-recovery", 3),
        ("b" * 32, "unknown", 3),
        ("b" * 32, "selected-recovery", 2),
        ("b" * 32, "generated", True),
    ],
)
def test_insertion_does_not_turn_an_invalid_identity_into_a_published_claim(
    queue, tmp_path, nonce, origin, protocol
):
    insert = getattr(queue._legs, "_insert_claim", None)
    assert callable(insert), "issued trial identity must be validated by the shared insertion"
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "cbm", "fast")
    h = queue.marker_hash(repo)
    with closing(queue._connect()) as db, queue._transaction(db):
        row = dict(db.execute("SELECT * FROM pending").fetchone())
        with pytest.raises(ValueError):
            insert(db, h, "cbm", row, protocol=protocol, nonce=nonce, origin=origin)
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
