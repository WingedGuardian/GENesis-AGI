"""Crash-state tests for the durable selection protocol used by restore.sh."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / 'scripts/lib/restore_selection.py'
_SPEC = importlib.util.spec_from_file_location('restore_selection', _PATH)
assert _SPEC and _SPEC.loader
selection = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(selection)


def _open(tmp_path, stamp='20261007T120000Z', refresh=False):
    marker = tmp_path / 'marker'
    marker.write_text('genesis-snapshot 1\n')
    root = tmp_path / 'private'
    work = selection.selection(root, 'local:/synthetic/backend', f'Genesis/host/{stamp}', refresh, str(marker))
    return root, work


def test_killed_refresh_after_pointer_resumes_retirement(tmp_path):
    root, old = _open(tmp_path)
    marker = tmp_path / 'marker'
    new_snapshot = 'Genesis/host/20261008T120000Z'
    new_id = selection.hashlib.sha256(('local:/synthetic/backend\0' + new_snapshot).encode()).hexdigest()
    new = root / new_id
    new.mkdir()
    state = json.loads((old / 'selection.json').read_text())
    state['snapshot'] = new_snapshot
    selection.atomic(new / 'selection.json', state)
    selection.atomic(root / 'active.json', {'version': 1, 'id': new_id})
    # Simulate kill after active-pointer publication, before retiring old cache.
    old.joinpath('cache/payload').write_bytes(b'old ciphertext')
    resumed = selection.selection(root, 'local:/synthetic/backend', new_snapshot, False, str(marker))
    assert resumed == new
    assert not old.exists()
    assert (new / 'selection.json').exists()


def test_killed_refresh_before_pointer_preserves_original(tmp_path):
    root, old = _open(tmp_path)
    new_snapshot = 'Genesis/host/20261008T120000Z'
    new_id = selection.hashlib.sha256(('local:/synthetic/backend\0' + new_snapshot).encode()).hexdigest()
    incomplete = root / new_id
    incomplete.mkdir()
    state = json.loads((old / 'selection.json').read_text())
    state['snapshot'] = new_snapshot
    selection.atomic(incomplete / 'selection.json', state)
    staging = root / '.selection-interrupted'
    staging.mkdir()
    (staging / 'partial').write_bytes(b'partial metadata')
    resumed = selection.selection(root, 'local:/synthetic/backend', 'Genesis/host/20261007T120000Z', False, str(tmp_path / 'marker'))
    assert resumed == old
    assert not incomplete.exists()
    assert not staging.exists()
    assert json.loads((root / 'active.json').read_text())['id'] == old.name


def test_backend_change_requires_refresh_and_symlinks_refused(tmp_path):
    root, old = _open(tmp_path)
    with pytest.raises(ValueError, match='backend changed'):
        selection.selection(root, 'local:/different/backend', 'Genesis/host/20261008T120000Z', False, str(tmp_path / 'marker'))
    (old / 'view').rmdir()
    target = tmp_path / 'outside'
    target.mkdir()
    (old / 'view').symlink_to(target)
    with pytest.raises(ValueError, match='symlink restore view'):
        selection.selection(root, 'local:/synthetic/backend', 'Genesis/host/20261007T120000Z', False, str(tmp_path / 'marker'))
    assert target.exists()


def test_payload_receipts_are_bound_and_missing_corrupt_or_killed_updates_refetch(tmp_path):
    root, work = _open(tmp_path)

    def command(*args):
        return subprocess.run([sys.executable, str(_PATH), *args], capture_output=True, text=True)

    assert command('state', str(work), 'data', 'expected', 'payload.gpg').returncode == 0
    initial_state = (work / 'selection.json').read_bytes()
    stage = work / '.payload-staged'
    stage.write_bytes(b'ciphertext')
    assert command('publish', str(work), 'data', 'payload.gpg', str(stage)).returncode == 0
    assert (work / 'selection.json').read_bytes() == initial_state, 'payload publication rewrote immutable inventory'
    assert command('cached', str(work), 'data', 'payload.gpg').returncode == 0
    receipt = next((work / 'cache/data/.checksums').iterdir())
    saved = receipt.read_bytes()
    for invalid in [b'broken json', b'[]', b'{}', saved.replace(work.name.encode(), b'different-selection')]:
        receipt.write_bytes(invalid)
        assert command('cached', str(work), 'data', 'payload.gpg').returncode == 3
    receipt.unlink()
    assert command('cached', str(work), 'data', 'payload.gpg').returncode == 3
    # Kill gap: new ciphertext became visible before checksum publication.
    receipt.write_bytes(saved)
    (work / 'cache/data/payload.gpg').write_bytes(b'changed ciphertext')
    assert command('cached', str(work), 'data', 'payload.gpg').returncode == 3
    # A forged receipt for an uncaptured name cannot authorize that payload.
    assert command('cached', str(work), 'data', 'uncaptured.gpg').returncode != 0


def test_validated_complete_is_durable_before_pointer_publication(tmp_path, monkeypatch):
    original = selection.atomic
    checked = []

    def inspect_pointer(path, value):
        if path.name == 'active.json':
            selected = path.parent / value['id']
            assert (selected / 'COMPLETE').read_text() == 'genesis-snapshot 1\n'
            assert selection.digest(selected / 'COMPLETE') == json.loads((selected / 'selection.json').read_text())['marker_sha256']
            checked.append(True)
        original(path, value)

    monkeypatch.setattr(selection, 'atomic', inspect_pointer)
    _open(tmp_path)
    assert checked


@pytest.mark.parametrize('refresh', [False, True])
@pytest.mark.parametrize('fault', ['copy', 'chmod', 'metadata', 'metadata-published', 'rename'])
def test_initializer_failure_cleans_only_unpublished_stage(tmp_path, monkeypatch, refresh, fault):
    root = tmp_path / 'private'
    old = _open(tmp_path)[1] if refresh else None
    before = (root / 'active.json').read_bytes() if refresh else None
    marker = tmp_path / 'marker'
    marker.write_text('genesis-snapshot 1\n')
    original_copy = selection.shutil.copyfile
    original_chmod = selection.os.chmod
    original_atomic = selection.atomic
    original_replace = selection.os.replace

    def copy(src, dst):
        if fault == 'copy':
            Path(dst).write_bytes(b'partial marker')
            raise OSError('injected copy failure')
        return original_copy(src, dst)

    def chmod(path, mode):
        if fault == 'chmod' and Path(path).name == 'COMPLETE':
            raise OSError('injected chmod failure')
        return original_chmod(path, mode)

    def atomic(path, value):
        if path.name == 'selection.json' and fault.startswith('metadata'):
            if fault == 'metadata-published':
                original_atomic(path, value)
            raise OSError('injected metadata failure')
        return original_atomic(path, value)

    def replace(src, dst):
        if fault == 'rename' and Path(src).name.startswith('.selection-'):
            raise OSError('injected rename failure')
        return original_replace(src, dst)

    monkeypatch.setattr(selection.shutil, 'copyfile', copy)
    monkeypatch.setattr(selection.os, 'chmod', chmod)
    monkeypatch.setattr(selection, 'atomic', atomic)
    monkeypatch.setattr(selection.os, 'replace', replace)
    with pytest.raises(OSError, match='injected'):
        selection.selection(root, 'local:/synthetic/backend', 'Genesis/host/20261008T120000Z', refresh, str(marker))
    assert not list(root.glob('.selection-*'))
    if old:
        assert (root / 'active.json').read_bytes() == before
        assert (old / 'COMPLETE').read_bytes() == marker.read_bytes()
        assert [p for p in root.iterdir() if len(p.name) == 64] == [old]
    else:
        assert not (root / 'active.json').exists()
        assert not list(root.iterdir())


@pytest.mark.parametrize('fault', ['root-fsync', 'pointer-before', 'pointer-after'])
def test_published_workspace_survives_later_failure_and_resumes(tmp_path, monkeypatch, fault):
    root, old = _open(tmp_path)
    before = (root / 'active.json').read_bytes()
    original_atomic = selection.atomic
    original_open = selection.os.open

    def atomic(path, value):
        if path.name == 'active.json' and fault.startswith('pointer'):
            if fault == 'pointer-after':
                original_atomic(path, value)
            raise OSError('injected pointer failure')
        return original_atomic(path, value)

    def open_directory(path, flags, *args, **kwargs):
        if fault == 'root-fsync' and Path(path) == root:
            raise OSError('injected root durability failure')
        return original_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(selection, 'atomic', atomic)
        patch.setattr(selection.os, 'open', open_directory)
        with pytest.raises(OSError, match='injected'):
            _open(tmp_path, stamp='20261008T120000Z', refresh=True)
    new = next(p for p in root.iterdir() if len(p.name) == 64 and p != old)
    assert (new / 'COMPLETE').exists() and (new / 'selection.json').exists()
    assert not list(root.glob('.selection-*'))
    if fault == 'pointer-after':
        assert json.loads((root / 'active.json').read_text())['id'] == new.name
        resumed = _open(tmp_path)[1]
    else:
        assert (root / 'active.json').read_bytes() == before
        resumed = _open(tmp_path, stamp='20261008T120000Z', refresh=True)[1]
    assert resumed == new
    assert not old.exists()


def test_local_backend_filesystem_bytes_survive_selection_and_refresh(tmp_path):
    backend = tmp_path / os.fsdecode(b'backend-\xff')
    backend.mkdir()
    identity = 'local:' + str(backend)
    with pytest.raises(UnicodeEncodeError):
        identity.encode('utf-8')
    marker = tmp_path / 'marker'
    marker.write_text('genesis-snapshot 1\n')
    root = tmp_path / 'private'
    old = selection.selection(root, identity, 'Genesis/host/20261007T120000Z', False, str(marker))
    assert old.name == selection.hashlib.sha256(os.fsencode(identity + '\0Genesis/host/20261007T120000Z')).hexdigest()
    refreshed = selection.selection(root, identity, 'Genesis/host/20261008T120000Z', True, str(marker))
    assert refreshed.exists()
    assert not old.exists()
    assert json.loads((refreshed / 'selection.json').read_text())['backend'] == identity


@pytest.mark.parametrize('command', ['inventory', 'state', 'cached', 'publish'])
@pytest.mark.parametrize('component', ['/absolute', '//absolute', '.', '..', '../data', 'data/../extra', './data', 'data//nested', 'data/', 'data/./nested', 'data\\nested', 'data:other', ''])
def test_component_commands_refuse_noncanonical_or_escaping_paths(tmp_path, command, component):
    _, work = _open(tmp_path)
    state_path = work / 'selection.json'
    state = json.loads(state_path.read_text())
    state['components'][component] = {'state': 'expected', 'entries': {'payload.gpg': None}}
    selection.atomic(state_path, state)
    before = state_path.read_bytes()
    stage = tmp_path / 'stage'
    stage.write_bytes(b'synthetic ciphertext')
    tail = {'inventory': [], 'state': ['empty'], 'cached': ['payload.gpg'],
            'publish': ['payload.gpg', str(stage)]}[command]
    result = subprocess.run([sys.executable, str(_PATH), command, str(work), component, *tail],
                            capture_output=True, text=True)
    assert result.returncode != 0 and 'unsafe component' in result.stderr
    assert stage.read_bytes() == b'synthetic ciphertext'
    assert state_path.read_bytes() == before
    assert not list((work / 'cache').iterdir())
    assert not list((work / 'view').iterdir())


@pytest.mark.parametrize('component', ['data', 'qdrant', 'memory', 'config_overrides', 'secrets',
                                    'eval', 'eval/golden', 'creds', 'creds/ssh', 'analytics',
                                    'transcripts', 'extra'])
def test_supported_component_protocol_roundtrip(tmp_path, component):
    _, work = _open(tmp_path)

    def command(*args):
        return subprocess.run([sys.executable, str(_PATH), *args], capture_output=True, text=True)

    assert command('state', str(work), component, 'expected', 'payload.gpg').returncode == 0
    assert command('inventory', str(work), component).stdout.strip() == 'payload.gpg'
    stage = tmp_path / 'stage'
    stage.write_bytes(b'synthetic ciphertext')
    assert command('publish', str(work), component, 'payload.gpg', str(stage)).returncode == 0
    assert command('cached', str(work), component, 'payload.gpg').returncode == 0
    assert (work / 'cache' / component / 'payload.gpg').read_bytes() == b'synthetic ciphertext'
    assert (work / 'view' / component / 'payload.gpg').read_bytes() == b'synthetic ciphertext'
