"""A receipt changed after consent cannot acquire fresh selected authority."""

from contextlib import closing

import pytest

from tests.test_scripts.test_code_intel_selected_supervision import tables
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue
from tests.test_scripts.test_index_marker_execution import _intent, _registration
from tests.test_scripts.test_index_marker_retry_consumption import issued


@pytest.mark.parametrize('provider', ['cbm', 'gitnexus'])
@pytest.mark.parametrize('phase', ['consume', 'register', 'begin'])
@pytest.mark.parametrize('field', ['accepted_terminal_sha256', 'accepted_control_sha256',
                                 'accepted_source_identity', 'accepted_evidence_reference'])
def test_partial_receipt_after_issue_cannot_publish_selected_authority(queue, tmp_path, provider, phase, field):
    retry, grant, request = issued(queue, tmp_path, provider)
    calls = []

    def bind(context, reserved):
        calls.append(context)
        return object()

    if phase != 'consume':
        with queue._decision_gate(native=True) as (db, finalize):
            claimed, _ = retry.consume(db, request, queue._native, queue._legs,
                                       finalize=finalize, bind_runtime=bind)
        if phase == 'begin':
            assert queue.register_scope(claimed['context'], _registration(claimed)) == 'registered'
    with closing(queue._connect()) as db, queue._transaction(db):
        db.execute('UPDATE provider_state SET '+field+'=? WHERE hash=? AND provider=?',
                   ('fixture-partial', grant['hash'], provider))
    before, prior_calls = tables(queue), list(calls)
    with pytest.raises(RuntimeError, match='partial provider safety acceptance'):
        if phase == 'consume':
            with queue._decision_gate(native=True) as (db, finalize):
                retry.consume(db, request, queue._native, queue._legs,
                              finalize=finalize, bind_runtime=bind)
        elif phase == 'register':
            queue.register_scope(claimed['context'], _registration(claimed))
        else:
            queue.begin_native(claimed['context'], _intent(queue, claimed))
    assert tables(queue) == before and calls == prior_calls
