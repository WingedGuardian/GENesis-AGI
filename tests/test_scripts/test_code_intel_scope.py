"""Synthetic original-subtree proof; this is not native cgroup qualification."""
import os
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_execution import scope as scope

ROOT = Path(__file__).resolve().parents[2]


def control():
    source = ROOT / 'scripts/lib/code_intel_scope.py'
    assert source.is_file(), 'Q3b needs an original-subtree collector, not foreground inference'
    namespace = runpy.run_path(str(source))
    assert callable(namespace.get('cleanup_observation'))
    return namespace['cleanup_observation']


@pytest.fixture
def original_scope(scope, evidence):
    probe, leaf, proc = scope
    facts = probe.capture()
    registered = {'unit': probe.unit, 'invocation_id': '1' * 32,
                  'facts': facts, 'stop_usec': 90_000_000}
    root = Path(facts['root']['path'])
    observer = root / 'observer.scope'
    observer.mkdir()
    (proc / 'self/cgroup').write_text('0::/observer.scope\n')
    (proc / 'self/task' / str(os.getpid()) / 'cgroup').write_text('0::/observer.scope\n')
    (leaf / 'cgroup.events').write_text('populated 0\nfrozen 0\n')
    return registered, leaf, proc, SimpleNamespace(**evidence)


def read(operation, value, *, waited=True):
    registration, leaf, proc, proof = value
    return operation(registration, 'a' * 64, proof, owned_wait=waited, proc=proc)


def test_empty_original_subtree_requires_owned_wait_and_current_counters(original_scope):
    operation = control()
    registration, leaf, proc, proof = original_scope
    (leaf / 'memory.events').write_text('low 0\nhigh 12\nmax 8\noom 1\noom_kill 1\n')
    data = read(operation, original_scope)
    assert data['state'] == 'quiescent' and data['populated'] == 0 and data['owned_wait'] is True
    assert data['registration_sha256'] == 'a' * 64 and type(data['observed_ns']) is int
    assert data['facts']['reporter'] == registration['facts']['reporter']
    assert data['facts']['events']['oom_kill'] == 1
    assert registration['facts']['events']['oom_kill'] == 0


def _last_descendant_oom(monkeypatch, leaf, *, before='cgroup.events'):
    """The last owned descendant dies at one precise observation boundary."""
    original = Path.read_text
    seen = []

    def exiting(path, *args, **kwargs):
        if path == leaf / before and not seen:
            seen.append('last-owned-exit')
            (leaf / 'memory.events').write_text('low 0\nhigh 0\nmax 1\noom 1\noom_kill 1\n')
            (leaf / 'cgroup.events').write_text('populated 0\nfrozen 0\n')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', exiting)
    return seen


@pytest.mark.parametrize('sample,state,populated', [
    ('cgroup.events', 'quiescent', 0),
    ('memory.events', 'unresolved', 1),
])
def test_last_descendant_oom_is_sampled_after_observed_emptiness(
    original_scope, monkeypatch, sample, state, populated
):
    operation = control()
    _, leaf, _, _ = original_scope
    (leaf / 'cgroup.events').write_text('populated 1\nfrozen 0\n')
    seen = _last_descendant_oom(monkeypatch, leaf, before=sample)
    data = read(operation, original_scope)
    assert seen == ['last-owned-exit']
    assert data['state'] == state and data['populated'] == populated
    assert data['facts']['events']['oom_kill'] == 1


@pytest.mark.parametrize('populated,waited', [(1, True), (0, False), (1, False)])
def test_nonempty_or_unowned_wait_cannot_qualify_quiescence(original_scope, populated, waited):
    operation = control()
    registration, leaf, proc, proof = original_scope
    (leaf / 'cgroup.events').write_text(f'populated {populated}\nfrozen 0\n')
    data = read(operation, original_scope, waited=waited)
    assert data['state'] == 'unresolved' and data['populated'] == populated
    assert data['owned_wait'] is waited


def test_real_original_path_absence_under_same_root_is_separate_proof(original_scope):
    operation = control()
    registration, leaf, proc, proof = original_scope
    for path in leaf.iterdir():
        path.unlink()
    leaf.rmdir()
    data = read(operation, original_scope, waited=False)
    assert data['state'] == 'removed' and data['absent'] is True
    assert data['root'] == registration['facts']['root']
    assert data['mounts_sha256'] == registration['facts']['mounts_sha256']
    assert data['boot_id'] == registration['facts']['reporter']['boot_id']


def test_changed_actual_boot_closes_old_tasks_without_old_root_visibility(original_scope):
    operation = control()
    registration, leaf, proc, proof = original_scope
    (proc / 'sys/kernel/random/boot_id').write_text('00000000-0000-0000-0000-000000000002')
    root = Path(registration['facts']['root']['path'])
    root.rename(root.with_name('retained-old-boot-root'))
    data = read(operation, original_scope, waited=False)
    assert data['state'] == 'rebooted'
    assert data['old_boot_id'] == registration['facts']['reporter']['boot_id']
    assert data['new_boot_id'] == '00000000-0000-0000-0000-000000000002'


@pytest.mark.parametrize('fault', ['root', 'leaf', 'mounts', 'missing-counter', 'counter-reset',
                                  'local-reset', 'duplicate-counter', 'cap', 'swap'])
def test_changed_or_unreadable_original_proof_refuses_instead_of_inventing_zero(original_scope, fault):
    operation = control()
    registration, leaf, proc, proof = original_scope
    if fault == 'root':
        root = Path(registration['facts']['root']['path'])
        root.rename(root.with_name('retained-original-root'))
        (root / 'observer.scope').mkdir(parents=True)
    elif fault == 'leaf':
        leaf.rename(leaf.with_name('retained-original-leaf'))
        leaf.mkdir()
    elif fault == 'mounts':
        path = proc / 'self/mountinfo'
        path.write_text(path.read_text() + path.read_text().replace('1 0', '2 0'))
    elif fault == 'missing-counter':
        (leaf / 'memory.events').unlink()
    elif fault == 'counter-reset':
        registration['facts']['events']['high'] = 1
    elif fault == 'local-reset':
        registration['facts']['local_events']['high'] = 1
    elif fault == 'duplicate-counter':
        path = leaf / 'memory.events'
        path.write_text(path.read_text() + 'oom_kill 0\n')
    elif fault == 'cap':
        (leaf / 'memory.max').write_text(str(7 * 1024**3))
    else:
        (leaf / 'memory.swap.max').write_text('1')
    with pytest.raises((OSError, ValueError)):
        read(operation, original_scope)


@pytest.mark.parametrize('raw', ['frozen 0\n', 'populated 0\npopulated 0\n',
                                'populated 2\n', 'populated true\n'])
def test_population_must_be_explicit_single_and_boolean_domain(original_scope, raw):
    operation = control()
    registration, leaf, proc, proof = original_scope
    (leaf / 'cgroup.events').write_text(raw)
    with pytest.raises(ValueError):
        read(operation, original_scope)


@pytest.mark.parametrize('waited', [None, 0, 1, 'yes'])
def test_owned_wait_is_an_actual_boolean_contract(original_scope, waited):
    operation = control()
    with pytest.raises(ValueError):
        read(operation, original_scope, waited=waited)


def test_identity_is_rechecked_after_counter_reads(original_scope, monkeypatch):
    operation = control()
    registration, leaf, proc, proof = original_scope
    original = proof._events

    def replaced(path):
        result = original(path)
        if path == leaf / 'memory.events.local':
            retained = leaf.with_name('retained-racing-original-leaf')
            leaf.rename(retained)
            leaf.mkdir()
            for old in retained.iterdir():
                (leaf / old.name).write_bytes(old.read_bytes())
        return result

    monkeypatch.setattr(proof, '_events', replaced)
    with pytest.raises((OSError, ValueError)):
        read(operation, original_scope)
