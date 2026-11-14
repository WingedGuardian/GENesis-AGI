"""The metadata prefix must not issue a fresh selected launch acknowledgement."""

from contextlib import closing

import pytest

from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue
from tests.test_scripts.test_index_marker_execution import (
    _claim,
    _intent,
    _registration,
    _reservation,
)


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("transition", ["reserve_launch", "register_scope", "begin_native"])
def test_prior_selected_row_cannot_acquire_a_new_launch_ack(queue, tmp_path, provider, transition):
    claimed = _claim(queue, tmp_path, provider=provider)
    context = claimed["context"]
    if transition != "reserve_launch":
        assert queue.reserve_launch(context, _reservation(claimed)) == "reserved"
    if transition == "begin_native":
        assert queue.register_scope(context, _registration(claimed)) == "registered"
    with closing(queue._connect()) as db, queue._transaction(db):
        db.execute("UPDATE executions SET nonce_origin='selected-recovery' WHERE execution_seq=?",
                   (context["execution_seq"],))
    before = queue.execution_status(context)
    data = {"reserve_launch": _reservation(claimed), "register_scope": _registration(claimed),
            "begin_native": _intent(queue, claimed)}[transition]
    with pytest.raises(ValueError, match="selected execution support"):
        getattr(queue, transition)(context, data)
    assert queue.execution_status(context) == before
    assert queue.get_inflight(context["hash"])["claim_id"] == context["claim_id"]
