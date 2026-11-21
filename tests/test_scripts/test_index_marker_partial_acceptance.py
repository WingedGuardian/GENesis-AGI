"""Malformed acceptance receipts cannot disappear from explicit retry admission."""

from contextlib import closing

import pytest

from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue
from tests.test_scripts.test_index_marker_retry_grant import NOW, candidate, helper

FIELDS = ('accepted_terminal_sha256', 'accepted_control_sha256',
          'accepted_source_identity', 'accepted_evidence_reference')


def state(queue, h, provider):
    with closing(queue._connect()) as db:
        return dict(db.execute('SELECT * FROM provider_state WHERE hash=? AND provider=?',
                               (h, provider)).fetchone())


def work(queue):
    with closing(queue._connect()) as db:
        return {table: [dict(row) for row in db.execute('SELECT * FROM '+table)]
                for table in ('pending', 'failed', 'executions', 'inflight')}


@pytest.mark.parametrize('provider', ['cbm', 'gitnexus'])
@pytest.mark.parametrize('mask', range(1, 16))
def test_partial_zero_watermark_refuses_recovery_without_repairing_other_state(queue, tmp_path, provider, mask):
    retry = helper()
    data = candidate(queue, tmp_path, provider)
    h = data['hash']
    with queue._decision_gate(native=True) as (db, finalize):
        grant = retry.issue(db, data, queue._native, queue._legs, now=NOW, finalize=finalize)
    components = tuple('fixture-partial' if mask & (1 << i) else None for i in range(4))
    with closing(queue._connect()) as db, queue._transaction(db):
        db.execute('UPDATE provider_state SET '+','.join(field+'=?' for field in FIELDS)+
                   ' WHERE hash=? AND provider=?', (*components, h, provider))
    original, before = state(queue, h, provider), work(queue)
    with closing(queue._connect()) as db:
        with pytest.raises(RuntimeError, match='partial provider safety acceptance'):
            queue._recovery.provider_blocked(db, h, provider, queue._native)
        with pytest.raises(RuntimeError, match='partial provider safety acceptance'):
            retry.view(db, h, provider, queue._native, queue._legs, ())
    with (
        pytest.raises(RuntimeError, match='partial provider safety acceptance'),
        queue._decision_gate(native=True) as (db, finalize),
    ):
        retry.issue(db, {**data, 'nonce': 'c'*32}, queue._native, queue._legs,
                    now=NOW, finalize=finalize)
    assert state(queue, h, provider) == original and work(queue) == before
    sibling = 'gitnexus' if provider == 'cbm' else 'cbm'
    with queue._decision_gate(native=True) as (db, finalize):
        other = retry.issue(db, {**data, 'provider': sibling, 'nonce': 'c'*32},
                            queue._native, queue._legs, now=NOW, finalize=finalize)
    assert other['status'] == 'issued' and state(queue, h, provider) == original
    assert work(queue) == before
    with queue._decision_gate(native=True) as (db, _):
        retry.revoke(db, h, provider, grant['nonce'], queue._native)
    retained = state(queue, h, provider)
    with closing(queue._connect()) as db:
        assert retry.load(db, h, provider, queue._native) == {**grant, 'status': 'revoked'}
    assert retained['accepted_sequence'] == 0
    assert tuple(retained[field] for field in FIELDS) == components
    assert work(queue) == before


@pytest.mark.parametrize('provider', ['cbm', 'gitnexus'])
@pytest.mark.parametrize('accepted', [0, 42])
def test_empty_zero_receipt_and_invalid_positive_receipt_keep_distinct_domains(queue, tmp_path, provider, accepted):
    retry = helper()
    data = candidate(queue, tmp_path, provider)
    with queue._decision_gate(native=True) as (db, finalize):
        retry.issue(db, data, queue._native, queue._legs, now=NOW, finalize=finalize)
    with closing(queue._connect()) as db, queue._transaction(db):
        db.execute('UPDATE provider_state SET accepted_sequence=? WHERE hash=? AND provider=?',
                   (accepted, data['hash'], provider))
        observed = retry.view(db, data['hash'], provider, queue._native, queue._legs, ())
        assert bool(observed['evidence']) == bool(accepted)
        assert queue._recovery.provider_blocked(db, data['hash'], provider, queue._native) == bool(accepted)
        if accepted:
            db.execute('UPDATE provider_state SET accepted_evidence_reference=? WHERE hash=? AND provider=?',
                       ('fixture://changed-invalid-receipt', data['hash'], provider))
            changed = retry.view(db, data['hash'], provider, queue._native, queue._legs, ())
            assert changed['evidence'] != observed['evidence']
