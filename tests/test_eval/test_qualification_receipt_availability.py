"""Synthetic receipt availability: one completion, durable unresolved funding."""

import asyncio
from copy import deepcopy
from decimal import Decimal

import httpx
import pytest

from genesis.eval.qualification import pinned, run
from genesis.eval.qualification.accounting import reconstruct
from genesis.eval.qualification.evidence import Incomplete
from genesis.eval.qualification.execution import cost_report
from tests.test_eval.qualification_runner_fixtures import runner_data  # noqa: F401
from tests.test_eval.qualification_transport_fixtures import Gateway, ask, bundle, router


@pytest.fixture
def isolated_key(monkeypatch, tmp_path):
    monkeypatch.setenv(pinned.KEY_ENV, 'synthetic-only-key')
    monkeypatch.setenv('SECRETS_PATH', str(tmp_path / 'absent-secrets.env'))
    for name in pinned.PRODUCTION_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)


class DelayedReceipt(Gateway):
    def __init__(self, statuses=(404, 404, 200)):
        super().__init__()
        self.statuses = iter(statuses)
        self.lookups = 0

    def __call__(self, request):
        if request.url.path.endswith('/generation'):
            self.lookups += 1
            status = next(self.statuses, 404)
            if status != 200:
                return httpx.Response(status, json={'error': {'message': 'synthetic pending'}})
        return super().__call__(request)


def policy(data, value='wait_404'):
    manifest = data.campaign.expected_manifest
    manifest['binding']['transports'][data.alias]['receipt_policy'] = value
    for spec in manifest['attempts']:
        spec['receipt_policy'] = value


async def test_single_404_can_reconcile_expense_without_erasing_failure(tmp_path, isolated_key):
    data, gateway = bundle(tmp_path), DelayedReceipt((404, 200))
    with data.campaign:
        with pytest.raises(pinned.LocalFailure):
            await ask(router(data, gateway), data.messages)
        row = data.campaign.attempt('case-1')
        assert row['receipts'] == []
        assert row['lookups'][0]['http_status'] == 404
        assert data.campaign.state.reserved == Decimal('0.1')
    with data.campaign:
        await pinned.reconcile(data.campaign, 'case-1', transport=gateway.transport)
        state = data.campaign.state
        assert state.settled == Decimal('0.0001') and state.reserved == 0
        assert state.blockers == {'failure:case-1'}
        with pytest.raises(pinned.LocalFailure, match='stopped'):
            await ask(router(data, gateway), data.messages, 'case-2')
    assert gateway.lookups == 2 and len(gateway.requests) == 1


async def test_opt_in_wait_404_actual_delegate_settles_once_and_reopens(
    tmp_path, isolated_key, monkeypatch,
):
    data, gateway = bundle(tmp_path), DelayedReceipt()
    policy(data)
    monkeypatch.setattr(pinned, 'RECEIPT_WAIT_INTERVAL', 0, raising=False)
    with data.campaign:
        assert (await ask(router(data, gateway), data.messages)).success
        state = data.campaign.state
        assert not state.blockers and state.settled == Decimal('0.0001')
        assert len(data.campaign.attempt('case-1')['lookups']) == 2
        report = cost_report({data.alias: state})
        assert report['receipt_lookup_failures'] == {'404': 2}
        assert report['observed_receipt_cost'] == '0.0001'
    with data.campaign:
        assert (await ask(router(data, gateway, dispatch=False), data.messages)).success
        # A new coordinate is possible only after the first receipt settled.
        gateway.statuses = iter((200,))
        await ask(router(data, gateway), data.messages, 'case-2')
    assert len(gateway.requests) == 2 and gateway.lookups == 4


@pytest.mark.parametrize('status', [301, 400, 401, 402, 403, 408, 429, 500, 503])
async def test_only_404_can_wait(tmp_path, isolated_key, monkeypatch, status):
    data, gateway = bundle(tmp_path), DelayedReceipt((status, 200))
    policy(data)
    monkeypatch.setattr(pinned, 'RECEIPT_WAIT_INTERVAL', 0, raising=False)
    with data.campaign:
        with pytest.raises(pinned.LocalFailure):
            await ask(router(data, gateway), data.messages)
        assert data.campaign.state.reserved == Decimal('0.1')
        row = data.campaign.attempt('case-1')
        assert row['receipts'] == [] and row['lookups'][0]['http_status'] == status
    assert gateway.lookups == 1 and len(gateway.requests) == 1


@pytest.mark.parametrize('failure', ['timeout', 'cancel', 'deadline', 'read_cap'])
async def test_wait_boundaries_preserve_liability(tmp_path, isolated_key, monkeypatch, failure):
    data, gateway = bundle(tmp_path), DelayedReceipt()
    policy(data)
    monkeypatch.setattr(pinned, 'RECEIPT_WAIT_INTERVAL', 0, raising=False)
    monkeypatch.setattr(pinned, 'RECEIPT_WAIT_TIMEOUT', .02, raising=False)

    async def transport(request):
        if request.url.path.endswith('/generation') and failure in ('timeout', 'cancel'):
            gateway.lookups += 1
            raise asyncio.CancelledError() if failure == 'cancel' else httpx.ReadTimeout('synthetic')
        return gateway(request)

    if failure in ('deadline', 'read_cap'):
        gateway.statuses = iter(())
        if failure == 'deadline':
            monkeypatch.setattr(pinned, 'RECEIPT_WAIT_INTERVAL', 30, raising=False)
        else:
            monkeypatch.setattr(pinned, 'RECEIPT_WAIT_TIMEOUT', 30, raising=False)
    with data.campaign:
        with pytest.raises(asyncio.CancelledError if failure == 'cancel' else pinned.LocalFailure):
            await asyncio.wait_for(
                ask(router(data, gateway, transport=httpx.MockTransport(transport)), data.messages), 5
            )
        row = data.campaign.attempt('case-1')
        assert row['observations'] and not row['receipts'] and row['lookups']
        assert data.campaign.state.reserved == Decimal('0.1')
        assert row['failed']
    assert len(gateway.requests) == 1
    assert gateway.lookups == (21 if failure == 'read_cap' else 1)


@pytest.mark.parametrize('value', [None, True, 0, [], {}, '', 'WAIT_404', 'retry'])
@pytest.mark.parametrize('boundary', ['router', 'reconcile', 'admission'])
async def test_invalid_policy_refuses_before_credentials(
    tmp_path, monkeypatch, data, value, boundary,
):
    def forbidden(*_args, **_kwargs):
        pytest.fail('credential access before policy validation')

    monkeypatch.setattr(pinned, 'credential', forbidden)
    if boundary == 'admission':
        data.manifest['binding']['transports'][data.alias]['receipt_policy'] = value
        for row in data.manifest['attempts']:
            row['receipt_policy'] = value
        with pytest.raises(Incomplete):
            run.validate_campaign(data.manifest, data.checked, data.alias)
        return
    wire = bundle(tmp_path)
    policy(wire, value)
    with wire.campaign:
        if boundary == 'router':
            with pytest.raises(Incomplete):
                router(wire, Gateway())
        else:
            wire.campaign.reserve('case-1')
            wire.campaign.dispatch('case-1')
            wire.campaign.observe('case-1', {'generation_id': 'synthetic-generation'})
            with pytest.raises(Incomplete):
                await pinned.reconcile(wire.campaign, 'case-1')


@pytest.mark.parametrize('boundary', ['router', 'reconcile', 'admission'])
async def test_policy_mismatch_refuses_before_credentials(tmp_path, monkeypatch, data, boundary):
    monkeypatch.setattr(pinned, 'credential', lambda *_: pytest.fail('key read'))
    if boundary == 'admission':
        data.manifest['attempts'][0]['receipt_policy'] = 'wait_404'
        with pytest.raises(Incomplete):
            run.validate_campaign(data.manifest, data.checked, data.alias)
        return
    wire = bundle(tmp_path)
    wire.campaign.expected_manifest['attempts'][0]['receipt_policy'] = 'wait_404'
    with wire.campaign:
        if boundary == 'router':
            with pytest.raises(Incomplete):
                router(wire, Gateway())
        else:
            wire.campaign.reserve('case-1')
            wire.campaign.dispatch('case-1')
            wire.campaign.observe('case-1', {'generation_id': 'synthetic-generation'})
            with pytest.raises(Incomplete):
                await pinned.reconcile(wire.campaign, 'case-1')


@pytest.mark.parametrize('field', ['attempt', 'request_hash', 'generation_id', 'http_status', 'error'])
def test_lookup_validation_precedes_durable_append(tmp_path, field):
    data = bundle(tmp_path)
    with data.campaign:
        data.campaign.reserve('case-1')
        data.campaign.dispatch('case-1')
        lookup = {'attempt': 'case-1', 'request_hash': data.campaign.attempt('case-1')['spec']['request_hash'],
                  'generation_id': 'gen-1', 'http_status': 404, 'error': 'synthetic pending'}
        lookup[field] = None if field != 'http_status' else True
        before = data.campaign.lines
        with pytest.raises(Incomplete):
            data.campaign.append('receipt_lookup', attempt='case-1', evidence=lookup)
        assert data.campaign.lines == before
        lookup[field] = {'attempt': 'case-1', 'request_hash': lookup['request_hash'],
                         'generation_id': 'gen-1', 'http_status': 404,
                         'error': 'pending'}[field]
        if field == 'request_hash':
            lookup[field] = data.campaign.attempt('case-1')['spec']['request_hash']
        # The valid control must be admitted by the same reader authority.
        data.campaign.append('receipt_lookup', attempt='case-1', evidence=lookup)
        assert reconstruct(data.campaign.lines).reserved == Decimal('0.1')


async def test_old_invalid_billing_is_never_reinterpreted(tmp_path, isolated_key):
    data, gateway = bundle(tmp_path), DelayedReceipt((404, 200))
    with data.campaign:
        with pytest.raises(pinned.LocalFailure):
            await ask(router(data, gateway), data.messages)
        row = data.campaign.attempt('case-1')
        data.campaign.settle('case-1', {'http_status': 404, 'error': 'historical invalid receipt',
                                    'generation_id': 'gen-1', 'attempt': 'case-1',
                                    'request_hash': row['spec']['request_hash']})
        before = deepcopy(data.campaign.lines)
        await pinned.reconcile(data.campaign, 'case-1', transport=gateway.transport)
        assert data.campaign.lines[:len(before)] == before
        assert data.campaign.state.reserved == Decimal('0.1')
        assert data.campaign.state.settled == 0
        with pytest.raises(Incomplete):
            data.campaign.acknowledge('failure:case-1', 'cannot erase invalid billing')


def test_lookup_generation_claims_block_cross_campaign_duplicate_costs(tmp_path):
    states = {}
    for index in (0, 1):
        root = tmp_path / str(index)
        root.mkdir()
        data = bundle(root)
        with data.campaign:
            data.campaign.reserve('case-1')
            data.campaign.dispatch('case-1')
            spec = data.campaign.attempt('case-1')['spec']
            shared = {'generation_id': 'shared-gen', 'attempt': 'case-1',
                      'request_hash': spec['request_hash']}
            if index == 0:
                data.campaign.settle('case-1', {**shared, 'model': spec['model'],
                                              'upstream': spec['upstream'], 'charge': '0.01'})
            else:
                data.campaign.append('receipt_lookup', attempt='case-1',
                                     evidence={**shared, 'http_status': 404, 'error': 'pending'})
            states[str(index)] = data.campaign.state
    report = cost_report(states)
    assert report['observed_receipt_cost'] == '0' and report['unresolved_attempts'] == 2


@pytest.mark.parametrize('failure', ['cancel_append', 'deadline_append', 'cancel_cleanup'])
async def test_receipt_cleanup_preserves_initiating_exception(
    tmp_path, isolated_key, monkeypatch, failure,
):
    data = bundle(tmp_path)
    policy(data)
    monkeypatch.setattr(pinned, 'RECEIPT_WAIT_TIMEOUT', .02, raising=False)

    async def transport(_request):
        if failure == 'deadline_append':
            await asyncio.sleep(10)
        raise asyncio.CancelledError()

    class CleanupFault(httpx.MockTransport):
        async def aclose(self):
            if failure == 'cancel_cleanup':
                raise OSError('synthetic cleanup failure')
            await super().aclose()

    with data.campaign:
        data.campaign.reserve('case-1')
        data.campaign.dispatch('case-1')
        data.campaign.observe('case-1', {'generation_id': 'gen-1'})
        original = data.campaign.append

        def append(kind, **kwargs):
            if kind == 'receipt_lookup' and failure.endswith('_append'):
                data.campaign._poisoned = True
                raise OSError('synthetic uncertain lookup append')
            return original(kind, **kwargs)

        monkeypatch.setattr(data.campaign, 'append', append)
        with pytest.raises(TimeoutError if failure == 'deadline_append' else asyncio.CancelledError):
            await pinned.reconcile(data.campaign, 'case-1', transport=CleanupFault(transport))
        if failure.endswith('_append'):
            assert data.campaign._poisoned


async def test_invalid_200_after_pending_lookup_remains_blocked(tmp_path, isolated_key, monkeypatch):
    data, gateway = bundle(tmp_path), DelayedReceipt((404, 200, 200))
    policy(data)
    monkeypatch.setattr(pinned, 'RECEIPT_WAIT_INTERVAL', 0, raising=False)

    def transport(request):
        response = gateway(request)
        if request.url.path.endswith('/generation') and gateway.lookups == 2:
            return httpx.Response(200, json={'data': {'id': 'gen-1', 'model': 'wrong-model',
                                                        'provider_name': gateway.provider,
                                                        'total_cost': .0001}})
        return response

    with data.campaign:
        with pytest.raises(pinned.LocalFailure):
            await ask(router(data, gateway, transport=httpx.MockTransport(transport)), data.messages)
        await pinned.reconcile(data.campaign, 'case-1', transport=gateway.transport)
        assert data.campaign.state.reserved == Decimal('0.1')
        assert data.campaign.state.settled == 0
    assert gateway.lookups == 3 and len(gateway.requests) == 1
