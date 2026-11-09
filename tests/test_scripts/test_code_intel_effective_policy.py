"""Owner and reporter bind the same full policy, including nested watchdog values."""
import runpy
from copy import deepcopy
from pathlib import Path

import pytest

from tests.test_scripts.test_index_marker_legs import queue as queue


def policy():
    return dict(route='workload', slice='genesis-workload.slice', wall_seconds=3600,
                sibling_reserve_bytes=2 * 1024**3, cache_reserve_bytes=2 * 1024**3,
                io_weight='20', cpu_quota='200%',
                watchdog=dict(interval='15', warmup_interval='5', warmup_seconds='30',
                              load_max='4', iowait_max='25', bad_samples='2', continuous_pause_max='600'))


def normalize(value, queue, **kwargs):
    module = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/lib/code_intel_queued_worker.py'))
    operation = module.get('effective_policy')
    assert callable(operation), 'owner and native factory require one shared complete policy binding'
    return operation(value, queue._native, **kwargs)


def test_full_policy_is_preserved_and_does_not_share_nested_inputs(queue):
    value = policy()
    result = normalize(value, queue, full=True)
    expected = deepcopy(value)
    value['watchdog']['interval'] = '999'
    assert result == expected and queue._native.digest(result) != queue._native.digest(value)


def test_foundation_five_fields_remain_supported_but_cannot_be_an_owner_policy(queue):
    value = policy()
    for key in ('io_weight', 'cpu_quota', 'watchdog'):
        value.pop(key)
    assert normalize(value, queue) == value
    with pytest.raises(ValueError):
        normalize(value, queue, full=True)


@pytest.mark.parametrize('key', ['io_weight', 'cpu_quota', 'watchdog'])
def test_partial_full_policy_cannot_silently_project_to_five_fields(queue, key):
    value = policy()
    value.pop(key)
    with pytest.raises(ValueError):
        normalize(value, queue)


@pytest.mark.parametrize('path,value', [
    (('extra',), 1), (('wall_seconds',), True), (('cache_reserve_bytes',), -1),
    (('sibling_reserve_bytes',), 1.5), (('io_weight',), '0'), (('io_weight',), '10001'),
    (('io_weight',), '20\nStopUnit=foreign'), (('cpu_quota',), '0%'),
    (('cpu_quota',), 'Infinity%'), (('cpu_quota',), '200%; another-command'),
    (('watchdog',), []), (('watchdog', 'unknown'), '1'), (('watchdog', 'interval'), '0'),
    (('watchdog', 'warmup_interval'), '-1'), (('watchdog', 'bad_samples'), '1.5'),
    (('watchdog', 'continuous_pause_max'), '0'), (('watchdog', 'warmup_seconds'), True),
    (('watchdog', 'load_max'), 'NaN'), (('watchdog', 'iowait_max'), '25\nstop'),
])
def test_unsupported_or_ambiguous_policy_refuses(queue, path, value):
    selected = policy()
    target = selected
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        normalize(selected, queue, full=True)


def test_fractional_sampling_and_zero_warmup_keep_existing_supported_semantics(queue):
    selected = policy()
    selected['watchdog'].update(interval='0.05', warmup_interval='.25', warmup_seconds='0', load_max='0')
    selected['cpu_quota'] = '12.5%'
    assert normalize(selected, queue, full=True) == selected
