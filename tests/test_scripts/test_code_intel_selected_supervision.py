"""Selected supervision preserves original ownership and commit-before-ACK."""

import sqlite3
from contextlib import closing, contextmanager

import pytest

from tests.test_scripts.test_code_intel_gitnexus_metadata import selection as selection
from tests.test_scripts.test_code_intel_grant_supervision import cbm_configuration, configured
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules


def supervised_case(selection, tmp_path, monkeypatch, provider):
    supervisor, request, _, activity, modules = configured(selection, tmp_path, monkeypatch, 5)
    lease = cbm_configuration(supervisor, request, modules, monkeypatch) if provider == 'cbm' else None
    grant = supervisor.request_retry(request)
    data = dict(repo_path=request['repo_path'], provider=provider, nonce=grant['nonce'],
                managed_config=request['managed_config'])
    return supervisor, modules, grant, data, activity, lease


def tables(queue):
    with closing(queue._connect()) as db:
        return {name: [dict(row) for row in db.execute('SELECT * FROM '+name)]
                for name in ('pending', 'failed', 'provider_state', 'executions', 'inflight')}


def inert_owner(modules, monkeypatch):
    calls = []

    class Owner:
        def __init__(self, used, context, reserved, probe, manager):
            assert used is modules
            assert reserved['owner'] == modules['queue']._recovery.owner_identity()
            calls.append(context)

        def run(self, bound):
            assert bound.runtime_value['context'] == calls[-1]
            return None

    monkeypatch.setattr(modules['owner'], 'PhysicalOwner', Owner)
    return calls


@pytest.mark.parametrize('provider', ['cbm', 'gitnexus'])
@pytest.mark.parametrize('phase', ['policy', 'census', 'capacity', 'preparation', 'bind', 'final-drain'])
def test_selected_owner_drift_rolls_back_without_publishing_a_trial(selection, tmp_path, monkeypatch, provider, phase):
    supervisor, modules, _, data, activity, lease = supervised_case(selection, tmp_path, monkeypatch, provider)
    queue, admission = modules['queue'], modules['owner_admission']
    before = tables(queue)
    original = queue._recovery.owner_identity()
    current = [original]
    changed = []

    def drift():
        current[0] = {**original, 'start_ticks': original['start_ticks'] + 1}
        changed.append(phase)

    calls = inert_owner(modules, monkeypatch)
    with monkeypatch.context() as fault:
        fault.setattr(queue._recovery, 'owner_identity', lambda: dict(current[0]))
        if phase == 'policy':
            policy = admission.policy

            def changed_policy(*args, **kwargs):
                result = policy(*args, **kwargs)
                drift()
                return result

            fault.setattr(admission, 'policy', changed_policy)
        elif phase in ('census', 'capacity'):
            manager = modules['scope'].ScopeManager
            method = 'require_no_batch' if phase == 'census' else 'require_slice_capacity'
            observe = getattr(manager, method)

            def changed_observation(self, *args, **kwargs):
                result = observe(self, *args, **kwargs)
                drift()
                return result

            fault.setattr(manager, method, changed_observation)
        elif phase == 'preparation':
            prepare = admission.prepared_inputs

            @contextmanager
            def changed_preparation(*args, **kwargs):
                with prepare(*args, **kwargs) as inputs:
                    drift()
                    yield inputs

            fault.setattr(admission, 'prepared_inputs', changed_preparation)
        elif phase == 'bind':
            decode = modules['queued'].runtime_inputs

            def changed_binding(*args, **kwargs):
                result = decode(*args, **kwargs)
                drift()
                return result

            fault.setattr(modules['queued'], 'runtime_inputs', changed_binding)
        else:
            decision = queue._decision_gate

            @contextmanager
            def changed_decision(*args, **kwargs):
                with decision(*args, **kwargs) as (db, finalize):
                    def last():
                        result = finalize()
                        drift()
                        return result
                    yield db, last

            fault.setitem(queue.claim_native.__globals__, '_decision_gate', changed_decision)
        with pytest.raises(ValueError, match='owner'):
            supervisor.claim_selected(data)
    assert changed == [phase]
    assert not calls and not activity.exists()
    assert tables(queue) == before
    if lease:
        held, streams, _ = lease
        assert not held and all(stream.closed for stream in streams)


@pytest.mark.parametrize('provider', ['cbm', 'gitnexus'])
def test_denied_real_commit_cannot_publish_or_execute_selected_input(selection, tmp_path, monkeypatch, provider):
    supervisor, modules, grant, data, activity, lease = supervised_case(selection, tmp_path, monkeypatch, provider)
    queue = modules['queue']
    before = tables(queue)
    calls = inert_owner(modules, monkeypatch)
    transaction = queue._transaction
    denied = []

    def authorize(action, first, second, database, source):
        if action == sqlite3.SQLITE_TRANSACTION and first == 'COMMIT':
            denied.append(first)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @contextmanager
    def denied_commit(db):
        try:
            with transaction(db):
                yield db
                db.set_authorizer(authorize)
        finally:
            db.set_authorizer(None)

    with monkeypatch.context() as fault:
        fault.setitem(queue.claim_native.__globals__, '_transaction', denied_commit)
        with pytest.raises(sqlite3.DatabaseError, match='not authorized'):
            supervisor.claim_selected(data)
    assert denied == ['COMMIT'] and not calls and not activity.exists()
    assert tables(queue) == before
    reply = supervisor.claim_selected(data)  # Failed COMMIT left this consent unused.
    assert reply['state'] == 'open' and reply['launch_authority'] is False and len(calls) == 1
    with closing(queue._connect()) as db:
        assert queue._retry.load(db, grant['hash'], provider, queue._native)['status'] == 'spent'
        assert db.execute('SELECT count(*) FROM executions').fetchone()[0] == 1
    if lease:
        held, streams, _ = lease
        assert not held and all(stream.closed for stream in streams)


@pytest.mark.parametrize('gate', ['enabled', 'disabled', 'sentinel'])
def test_cbm_selected_claim_rechecks_enablement_and_lease_before_consuming(selection, tmp_path, monkeypatch, gate):
    supervisor, modules, grant, data, activity, lease = supervised_case(selection, tmp_path, monkeypatch, 'cbm')
    queue, managed = modules['queue'], modules['managed']
    held, streams, _ = lease
    before = tables(queue)
    calls = inert_owner(modules, monkeypatch)
    consumed = []
    consume = queue._retry.consume

    def leased(*args, **kwargs):
        assert held == [True] and streams and all(stream.closed for stream in streams)
        consumed.append(True)
        return consume(*args, **kwargs)

    monkeypatch.setattr(queue._retry, 'consume', leased)
    if gate == 'disabled':
        monkeypatch.setattr(managed, 'show', lambda *args, **kwargs: {'UnitFileState': 'disabled'})
    elif gate == 'sentinel':
        (supervisor.snapshot.root/'disabled').touch()
    if gate == 'enabled':
        reply = supervisor.claim_selected(data)
        assert reply['state'] == 'open' and reply['launch_authority'] is False
        assert consumed == [True] and len(calls) == 1
        row = queue.execution_status(reply['trial'])
        assert row['launch_permission'] == 'reserved' and row['native_intent'] is None
        with closing(queue._connect()) as db:
            assert queue._retry.load(db, grant['hash'], 'cbm', queue._native)['status'] == 'spent'
    else:
        with pytest.raises(ValueError, match='sentinel|persistently enabled'):
            supervisor.claim_selected(data)
        assert not calls and not consumed and tables(queue) == before
    assert not held and all(stream.closed for stream in streams) and not activity.exists()
