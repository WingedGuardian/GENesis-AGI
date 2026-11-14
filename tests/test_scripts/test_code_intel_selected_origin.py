"""Internally consistent capsules cannot choose their own execution provenance."""

import time
from contextlib import closing
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_code_intel_gitnexus_metadata import selection as selection
from tests.test_scripts.test_code_intel_gitnexus_runtime import frozen
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules


def origin_case(selection, *, origin, arguments):
    """Use real typed queue writers for the origin and a valid frozen capsule.

    Deliberately allow an opposite supported argv when constructing these
    internal inputs: the decoder's self-consistency is not provenance authority.
    No owner, scope manager or provider is run by this fixture.
    """
    snapshot, modules, value, _ = frozen(selection)
    queue, gn = modules['queue'], modules['gitnexus']
    queue._prepare()
    with closing(queue._connect()) as db, queue._transaction(db):
        queue._recovery.upgrade(db, queue._legs, queue._native)
    repo, provider = str(snapshot.root), 'gitnexus'
    h = queue.marker_hash(repo)
    queue.write_marker(repo, provider, 'full')
    if origin == 'generated':
        claimed = queue.claim_native(h, provider=provider)
        value['context'] = claimed['context']
        value['reservation']['unit'] = queue._native.expected_unit(value['context'])
    else:
        assert origin == 'selected-recovery'
        nonce = 'b' * 32
        value['reservation']['unit'] = queue._native.expected_unit(dict(hash=h, provider=provider, claim_id=nonce))
    options, recipe, source = gn.freeze(snapshot, modules, value['context'],
        value['reservation'], value['policy'], selection[2], cwd=selection[3],
        recovery=arguments == 'selected-recovery')
    value['reservation']['source'] = source
    value.update(provider_options=options, environment_recipe=recipe)
    if origin == 'generated':
        assert queue.reserve_launch(value['context'], value['reservation']) == 'reserved'
        bound = snapshot.with_runtime(value)
    else:
        policy_sha256 = queue._native.digest(value['policy'])
        data = dict(hash=h, repo_path=repo, provider=provider, origin='pending',
            run_mode='full', nonce=nonce, source=source, policy_sha256=policy_sha256,
            evidence_reference='fixture://actual-selected-origin',
            not_after_wall_ns=time.time_ns() + 3600 * 10**9)
        with queue._decision_gate(native=True) as (db, finalize):
            queue._retry.issue(db, data, queue._native, queue._legs, finalize=finalize)

        def bind(context, reserved):
            value.update(context=context, reservation=reserved)
            result = snapshot.with_runtime(value)
            modules['queued'].runtime_inputs(result, modules)
            return result

        request = dict(hash=h, provider=provider, nonce=nonce,
                       reservation=value['reservation'], policy_sha256=policy_sha256)
        with queue._decision_gate(native=True) as (db, finalize):
            claimed, bound = queue._retry.consume(db, request, queue._native, queue._legs,
                finalize=finalize, bind_runtime=bind)
        assert bound.runtime_value['context'] == claimed['context']
    assert queue.execution_status(bound.runtime_value['context'])['nonce_origin'] == origin
    decoded, _ = modules['queued'].runtime_inputs(bound, modules)
    assert decoded == value  # Both supported arrays are structurally consistent.
    assert not selection[4].exists()
    return snapshot, modules, bound


def inert_owner(modules, monkeypatch):
    """Observe shared-entry progress without any manager query or child."""
    calls = []

    class Manager:
        def __init__(self, *args, **kwargs):
            calls.append('manager')

    def probe(*args, **kwargs):
        calls.append('probe')
        return SimpleNamespace()

    class Owner:
        def __init__(self, *args, **kwargs):
            calls.append('owner')

        def run(self, bound):
            calls.append('run')
            return None

    monkeypatch.setattr(modules['scope'], 'ScopeManager', Manager)
    monkeypatch.setattr(modules['proof'], 'ScopeProbe', probe)
    monkeypatch.setattr(modules['owner'], 'PhysicalOwner', Owner)
    return calls


@pytest.mark.parametrize('origin', ['generated', 'selected-recovery'])
@pytest.mark.parametrize('matching', [False, True])
def test_shared_execution_checks_durable_origin_before_constructing_owner(selection, monkeypatch, origin, matching):
    arguments = origin if matching else ('selected-recovery' if origin == 'generated' else 'generated')
    _, modules, bound = origin_case(selection, origin=origin, arguments=arguments)
    queue = modules['queue']
    before = queue.execution_status(bound.runtime_value['context'])
    calls = inert_owner(modules, monkeypatch)
    operation = modules['owner_admission'].execute_runtime
    if matching:
        assert operation(bound, modules, actual_owner=queue._recovery.owner_identity()) is None
        assert calls == ['manager', 'probe', 'owner', 'run']
    else:
        with pytest.raises(ValueError, match='arguments.*execution origin'):
            operation(bound, modules, actual_owner=queue._recovery.owner_identity())
        assert not calls
    assert queue.execution_status(bound.runtime_value['context']) == before
    assert not selection[4].exists()


@pytest.mark.parametrize('origin', ['generated', 'selected-recovery'])
@pytest.mark.parametrize('matching', [False, True])
def test_source_callback_rechecks_origin_instead_of_trusting_argv(selection, origin, matching):
    arguments = origin if matching else ('selected-recovery' if origin == 'generated' else 'generated')
    snapshot, modules, bound = origin_case(selection, origin=origin, arguments=arguments)
    value = bound.runtime_value
    before = modules['queue'].execution_status(value['context'])
    if matching:
        assert modules['gitnexus'].current_source(snapshot, modules, value) == value['reservation']['source']
    else:
        with pytest.raises(ValueError, match='arguments.*execution origin'):
            modules['gitnexus'].current_source(snapshot, modules, value)
    assert modules['queue'].execution_status(value['context']) == before
    assert not selection[4].exists()


@pytest.mark.parametrize('observed', [None, 'unqualified', False])
def test_origin_observation_cannot_default_to_ordinary_permission(selection, monkeypatch, observed):
    _, modules, bound = origin_case(selection, origin='generated', arguments='generated')
    monkeypatch.setattr(modules['queue'], 'execution_status', lambda context: {'nonce_origin': observed})
    calls = inert_owner(modules, monkeypatch)
    with pytest.raises(ValueError, match='supported execution origin'):
        modules['owner_admission'].execute_runtime(bound, modules,
            actual_owner=modules['queue']._recovery.owner_identity())
    assert not calls and not selection[4].exists()


def test_shared_execution_cannot_replace_the_original_owner_with_a_current_token(selection, monkeypatch):
    _, modules, bound = origin_case(selection, origin='selected-recovery', arguments='selected-recovery')
    current = modules['queue']._recovery.owner_identity()
    original = {**current, 'start_ticks': current['start_ticks'] + 1}
    assert bound.runtime_value['reservation']['owner'] == current
    calls = inert_owner(modules, monkeypatch)
    with pytest.raises(ValueError, match='another actual owner'):
        modules['owner_admission'].execute_runtime(bound, modules, actual_owner=original)
    assert not calls and not selection[4].exists()
