"""Synthetic gateway/backend/receipt namespaces; no dated-ID heuristics."""
import copy
import json
from decimal import Decimal

import httpx
import pytest

from genesis.eval.qualification import pinned, run
from genesis.eval.qualification.evidence import Incomplete
from tests.test_eval.qualification_transport_fixtures import Gateway, ask, bundle, router
from tests.test_eval.test_qualification_routing_metadata import SPEC, observation


@pytest.mark.parametrize('history', [False, True])
def test_explicit_backend_identity_preserves_gateway_alias(history):
    spec = {**SPEC, 'backend_model': 'synthetic/backend-20260102',
            'receipt_model': 'synthetic/receipt-20260103'}
    obs = observation()
    raw = json.loads(obs['raw_body'])
    meta = raw['openrouter_metadata']
    meta['endpoints']['available'][0]['model'] = spec['backend_model']
    if history:
        meta['attempts'] = [{'model': spec['backend_model'],
                             'provider': spec['receipt_provider'], 'status': 200}]
    obs['raw_body'] = json.dumps(raw)
    pinned.verify_routing(obs, spec)
    assert pinned._observed_identity(obs, spec) == {'model': SPEC['model'], 'upstream': SPEC['upstream']}
    raw['model'] = spec['backend_model']
    obs['raw_body'] = json.dumps(raw)
    with pytest.raises(pinned.LocalFailure):
        pinned.verify_routing(obs, spec)
    assert pinned._observed_identity(obs, spec)['model'] != SPEC['model']


@pytest.mark.parametrize('field', ['backend_model', 'receipt_model'])
@pytest.mark.parametrize('value', [None, '', '  ', 0, False, [], {}])
def test_invalid_optional_identity_is_not_a_legacy_default(field, value):
    with pytest.raises(pinned.LocalFailure):
        pinned.model_identities({**SPEC, field: value})


def test_legacy_identity_never_strips_backend_suffix():
    assert pinned.model_identities(SPEC) == (SPEC['model'],) * 3
    obs = observation()
    raw = json.loads(obs['raw_body'])
    raw['openrouter_metadata']['endpoints']['available'][0]['model'] += '-20260102'
    obs['raw_body'] = json.dumps(raw)
    with pytest.raises(pinned.LocalFailure):
        pinned.verify_routing(obs, SPEC)
    assert pinned._observed_identity(obs, SPEC)['model'] != SPEC['model']


class NamespacedGateway(Gateway):
    backend = 'synthetic/backend-20260102'
    receipt = 'synthetic/receipt-20260103'

    def __call__(self, request):
        response = super().__call__(request)
        if request.method == 'POST' and response.status_code == 200:
            raw = response.json()
            raw['openrouter_metadata']['endpoints']['available'][0]['model'] = self.backend
            self.generations[raw['id']]['model'] = self.receipt
            return httpx.Response(200, json=raw)
        return response


def bind_models(data):
    manifest = data.campaign.expected_manifest
    pins = {'backend_model': NamespacedGateway.backend, 'receipt_model': NamespacedGateway.receipt}
    manifest['binding']['transports'][data.alias].update(pins)
    manifest['binding']['transports'][data.alias].update({
        key: manifest['attempts'][0][key]
        for key in ('receipt_provider', 'provider_identity_evidence')
    })
    for row in manifest['attempts']:
        row.update(pins)


async def test_delegate_receipt_and_offline_replay_preserve_raw_namespaces(tmp_path, monkeypatch):
    monkeypatch.setenv(pinned.KEY_ENV, 'synthetic-only-key')
    secrets = tmp_path / 'secrets.env'
    secrets.write_text('')
    monkeypatch.setenv('SECRETS_PATH', str(secrets))
    for name in pinned.PRODUCTION_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    data, server = bundle(tmp_path), NamespacedGateway()
    bind_models(data)
    with data.campaign:
        result = await ask(router(data, server), data.messages)
        assert result.success
        row = data.campaign.attempt('case-1')
        assert row['observations'][0]['model'] == data.campaign.expected_manifest['attempts'][0]['model']
        raw = json.loads(row['observations'][0]['raw_body'])
        assert raw['openrouter_metadata']['endpoints']['available'][0]['model'] == server.backend
        assert row['receipts'][0]['response_model'] == server.receipt
        assert data.campaign.state.settled == Decimal('0.0001')
    monkeypatch.delenv(pinned.KEY_ENV)
    with data.campaign:
        assert (await ask(router(data, server, dispatch=False), data.messages)).success
    assert len(server.requests) == server.receipt_reads == 1


@pytest.mark.parametrize('field', ['backend_model', 'receipt_model'])
async def test_mismatched_transport_attempt_refuses_before_credential(tmp_path, monkeypatch, field):
    data, server = bundle(tmp_path), NamespacedGateway()
    bind_models(data)
    data.campaign.expected_manifest['attempts'][0][field] = 'wrong/model'
    monkeypatch.setattr(pinned, 'credential', lambda *_: pytest.fail('credential accessed'))
    with data.campaign, pytest.raises(pinned.LocalFailure):
        router(data, server)
    assert not server.requests and server.receipt_reads == server.key_reads == 0


@pytest.mark.parametrize('wrong', ['backend', 'receipt'])
async def test_wrong_namespace_stops_without_resend(tmp_path, monkeypatch, wrong):
    monkeypatch.setenv(pinned.KEY_ENV, 'synthetic-only-key')
    secrets = tmp_path / 'secrets.env'
    secrets.write_text('')
    monkeypatch.setenv('SECRETS_PATH', str(secrets))
    for name in pinned.PRODUCTION_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    data, server = bundle(tmp_path), NamespacedGateway()
    bind_models(data)
    setattr(server, wrong, 'wrong/model')
    with data.campaign:
        with pytest.raises(pinned.LocalFailure):
            await ask(router(data, server), data.messages)
        assert data.campaign.state.blockers
    with data.campaign, pytest.raises(pinned.LocalFailure):
        await ask(router(data, server), data.messages, 'case-2')
    assert len(server.requests) == 1


@pytest.mark.parametrize('field', ['backend_model', 'receipt_model'])
def test_complete_admission_binds_each_attempt_namespace(tmp_path, field):
    data = bundle(tmp_path)
    bind_models(data)
    manifest = data.campaign.expected_manifest
    original = copy.deepcopy(manifest['attempts'])
    manifest['attempts'] = [{**a, 'id': a['id'] + '-' + str(rep), 'repetition': rep}
                            for a in original for rep in (1, 2, 3)]
    manifest['budget'] = '0.9'
    checked = {'models': {data.alias: original[0]['model']},
               'parameters': {data.alias: data.params},
               'versions': {data.contract: original[0]['version']},
               'rubrics': [], 'reference_sources': {}, 'production_sources': {},
               'corpus_hash': 'a' * 64, 'policy_hash': 'b' * 64, 'approval_hash': 'c' * 64,
               'plan': {data.contract: {a['case_id']: {'messages': data.messages,
                                                      'prompt_hash': a['prompt_hash']}
                                        for a in original}}}
    manifest['binding']['admission'] = run.admission_identity(checked)
    run.validate_campaign(manifest, checked, data.alias)
    manifest['attempts'][0][field] = 'wrong/model'
    with pytest.raises(Incomplete):
        run.validate_campaign(manifest, checked, data.alias)


async def test_explicit_namespace_without_transport_refuses_get_before_key(tmp_path, monkeypatch):
    data = bundle(tmp_path)
    bind_models(data)
    data.campaign.expected_manifest['binding'].pop('transports')
    data.campaign.expected_manifest['binding']['source'] = 'synthetic source-only recovery'
    monkeypatch.setattr(pinned, 'credential', lambda *_: pytest.fail('credential accessed'))
    with data.campaign:
        data.campaign.reserve('case-1')
        data.campaign.dispatch('case-1')
        with pytest.raises(pinned.LocalFailure):
            await pinned.reconcile(data.campaign, 'case-1', association={
                'attempt': 'case-1',
                'request_hash': data.campaign.expected_manifest['attempts'][0]['request_hash'],
                'generation_id': 'gen-1', 'evidence': 'synthetic association only'})


async def test_distinct_receipt_only_recovery_never_invents_answer(tmp_path, monkeypatch):
    monkeypatch.setenv(pinned.KEY_ENV, 'synthetic-only-key')
    secrets = tmp_path / 'secrets.env'
    secrets.write_text('')
    monkeypatch.setenv('SECRETS_PATH', str(secrets))
    for name in pinned.PRODUCTION_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    data, server = bundle(tmp_path), NamespacedGateway()
    bind_models(data)
    server.generations['gen-recovery'] = {
        'id': 'gen-recovery', 'model': server.receipt,
        'provider_name': server.provider, 'total_cost': 0.0001,
    }
    with data.campaign:
        data.campaign.reserve('case-1')
        data.campaign.dispatch('case-1')
        await pinned.reconcile(data.campaign, 'case-1', transport=server.transport,
                               association={'attempt': 'case-1',
                                            'request_hash': data.campaign.expected_manifest['attempts'][0]['request_hash'],
                                            'generation_id': 'gen-recovery',
                                            'evidence': 'synthetic verified association'})
        assert data.campaign.state.settled == Decimal('0.0001')
        assert not data.campaign.state.answers and data.campaign.state.blockers
        with pytest.raises(pinned.LocalFailure):
            await ask(router(data, server), data.messages)
    assert not server.requests and server.receipt_reads == 1
