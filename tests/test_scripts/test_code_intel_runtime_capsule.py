"""Private runtime transport is closed and never becomes launch authority."""

import fcntl
import hashlib
import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from tests.test_scripts.test_code_intel_source import case
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules

FIELDS = ('context', 'reservation', 'policy', 'provider_options', 'environment_recipe')


def runtime(captured, modules):
    repo = str(captured.root)
    context = dict(hash=modules['queue'].marker_hash(repo), repo_path=repo, provider='cbm',
                   protocol=3, execution_seq=1, claim_id='1' * 32)
    source = dict(commit=captured.commit, files=dict(captured.files),
                  provider_digest=modules['worker'].BUILD,
                  provider_version='v0.11.0-stock-worker', configuration_sha256='a' * 64)
    owner = dict(pid=os.getpid(), start_ticks=int(Path('/proc/self/stat').read_text().rpartition(') ')[2].split()[19]),
                 boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
    reserved = dict(unit=modules['queue']._native.expected_unit(context), run_mode='full',
                    source=source, owner=owner, cap_bytes=8 * 1024**3,
                    route='workload', slice='genesis-workload.slice', wall_seconds=14400)
    policy = dict(route=reserved['route'], slice=reserved['slice'], wall_seconds=14400,
                  sibling_reserve_bytes=2 * 1024**3, cache_reserve_bytes=2 * 1024**3,
                  io_weight='50', cpu_quota='200%',
                  watchdog=dict(interval='15', warmup_interval='5', warmup_seconds='30',
                                load_max='4', iowait_max='25', bad_samples='2',
                                continuous_pause_max='600'))
    return dict(context=context, reservation=reserved, policy=policy,
                provider_options=dict(managed_config=str(captured.root / 'settings.json'), persistence='true'),
                environment_recipe=dict(frozen={'PATH': '/usr/bin:/bin',
                                                 'SYNTHETIC_RUNTIME_VALUE': 'capsule-fixture-private-value'},
                                        registration_slots={'INVOCATION_ID': 'scope.invocation_id'}))


def bind(captured, value):
    operation = getattr(captured, 'with_runtime', None)
    assert callable(operation), 'runtime needs a versioned private capsule binding'
    return operation(value)


def test_private_runtime_roundtrip_and_actual_describe_survive_source_removal(tmp_path, monkeypatch):
    root, module, captured = case(tmp_path, monkeypatch, 'runner')
    value = runtime(captured, captured.assemble())
    secret = value['environment_recipe']['frozen']['SYNTHETIC_RUNTIME_VALUE']
    bound = bind(captured, value)
    assert json.loads(captured._payload())['version'] == 1
    assert json.loads(bound._payload())['version'] == 2
    assert bound.runtime_value == value
    assert secret not in repr(bound) and secret not in repr(bound.resume_command())
    for name in captured.buffers:
        (root / name).unlink()
    with bound.sealed() as fd:
        restored = module.restore(fd, captured.buffers[module.SELF], bound.transport_sha256)
        assert restored.runtime_value == value and restored.files == captured.files
        result = subprocess.run(bound.resume_command(), stdin=fd, capture_output=True,
                                text=True, check=True, timeout=30)
    described = json.loads(result.stdout)
    assert described == dict(commit=captured.commit, files=dict(captured.files), automatic_authority=False)
    assert secret not in result.stdout + result.stderr
    assert not (tmp_path / 'state').exists()


def test_runtime_binding_and_accessors_do_not_share_mutable_input(tmp_path, monkeypatch):
    _, _, captured = case(tmp_path, monkeypatch, 'runner')
    value = runtime(captured, captured.assemble())
    original = deepcopy(value)
    bound = bind(captured, value)
    digest = bound.transport_sha256
    value['environment_recipe']['frozen']['PATH'] = '/changed'
    exposed = bound.runtime_value
    exposed['policy']['watchdog']['interval'] = '999'
    assert bound.runtime_value == original and bound.transport_sha256 == digest


@pytest.mark.parametrize('action', ['queue', 'available', 'shell'])
def test_bound_runtime_refuses_source_only_command_roles(tmp_path, monkeypatch, action):
    _, _, captured = case(tmp_path, monkeypatch, 'runner')
    bound = bind(captured, runtime(captured, captured.assemble()))
    with pytest.raises(ValueError, match='runtime'):
        bound.resume_command(action)


def test_runtime_role_is_rechecked_before_a_real_scratch_queue_operation(tmp_path, monkeypatch):
    _, _, captured = case(tmp_path, monkeypatch, 'runner')
    bound = bind(captured, runtime(captured, captured.assemble()))
    command = bound.resume_command()
    assert command[-2:] == ['describe', 'runner']
    command[-2] = 'queue'  # Bypass the command builder, not the receiving role.
    command.extend(['list-legs', '--nul'])
    with bound.sealed() as fd:
        result = subprocess.run(command, stdin=fd, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and 'runtime' in result.stderr
    assert not (tmp_path / 'state').exists()


@pytest.mark.parametrize('fault,key', [('missing', key) for key in FIELDS]
                         + [('not-object', key) for key in FIELDS] + [('extra', None)])
def test_runtime_transport_rejects_partial_or_unknown_objects(tmp_path, monkeypatch, fault, key):
    _, _, captured = case(tmp_path, monkeypatch, 'runner')
    value = runtime(captured, captured.assemble())
    if fault == 'missing':
        value.pop(key)
    elif fault == 'not-object':
        value[key] = []
    else:
        value['extra'] = {}
    with pytest.raises(ValueError, match='runtime'):
        bind(captured, value)


def test_runtime_binding_requires_runner_profile(tmp_path, monkeypatch):
    _, _, captured = case(tmp_path, monkeypatch, 'entry')
    value = runtime(captured, captured.assemble())
    with pytest.raises(ValueError, match='runner|runtime'):
        bind(captured, value)


def test_runtime_size_bound_includes_private_values(tmp_path, monkeypatch):
    _, module, captured = case(tmp_path, monkeypatch, 'runner')
    value = runtime(captured, captured.assemble())
    value['environment_recipe']['frozen']['SYNTHETIC_RUNTIME_VALUE'] = 'x' * module.CAPSULE_LIMIT
    with pytest.raises(ValueError, match='oversized|runtime'):
        bind(captured, value)


def test_restore_revalidates_runtime_after_valid_seals_and_transport_digest(tmp_path, monkeypatch):
    _, module, captured = case(tmp_path, monkeypatch, 'runner')
    bound = bind(captured, runtime(captured, captured.assemble()))
    value = json.loads(bound._payload())
    value['runtime']['unknown'] = {}
    raw = json.dumps(value).encode()
    fd = os.memfd_create('invalid-runtime-fixture', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        assert os.write(fd, raw) == len(raw)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, module.SEALS)
        with pytest.raises(ValueError, match='runtime'):
            module.restore(fd, captured.buffers[module.SELF], hashlib.sha256(raw).hexdigest())
    finally:
        os.close(fd)
