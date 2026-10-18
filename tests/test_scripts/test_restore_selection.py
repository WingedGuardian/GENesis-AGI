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


@pytest.mark.parametrize('record', [
    {'state': 'expected'}, {'state': 'empty'}, None, [],
    {'state': 'expected', 'entries': []},
    {'state': 'unknown', 'entries': {}},
    {'state': 'expected', 'entries': {'../escaped': None}},
    {'state': 'expected', 'entries': {'.': None}},
    {'state': 'expected', 'entries': {'payload.gpg': 'wrong value'}},
    {'state': 'empty', 'entries': {'payload.gpg': None}},
])
@pytest.mark.parametrize('entry', ['inventory', 'pinned', 'publish'])
def test_malformed_nested_inventory_never_authorizes_payloads(tmp_path, record, entry):
    root, work = _open(tmp_path)
    state = json.loads((work / 'selection.json').read_text())
    state['components']['memory'] = record
    selection.atomic(work / 'selection.json', state)
    before = (work / 'selection.json').read_bytes()
    stage = tmp_path / 'stage'
    stage.write_bytes(b'preserved staging payload')
    args = ['pinned', str(root)] if entry == 'pinned' else [entry, str(work), 'memory']
    if entry == 'publish':
        args.extend(['payload.gpg', str(stage)])
    result = subprocess.run([sys.executable, str(_PATH), *args], capture_output=True, text=True)
    assert result.returncode != 0 and not result.stdout
    assert (work / 'selection.json').read_bytes() == before
    assert stage.read_bytes() == b'preserved staging payload'
    assert not list((work / 'cache').iterdir())
    assert not list((work / 'view').iterdir())


def test_native_shell_rejects_missing_memory_entries_instead_of_silent_omission(tmp_path):
    _, work = _open(tmp_path)
    state = json.loads((work / 'selection.json').read_text())
    state['components']['memory'] = {'state': 'expected'}
    selection.atomic(work / 'selection.json', state)
    shell = (_PATH.parents[1] / 'restore.sh').read_text()
    start = shell.index('    _selected_pull_component() {')
    end = shell.index('    marker="$_RESTORE_WORKSPACE/COMPLETE"', start)
    # Exercise the actual shell consumer and helper, without unrelated backend setup.
    driver = '''die() { printf '%s\\n' "$*" >&2; exit 1; }
warn() { printf '%s\\n' "$*" >&2; }
_SCRIPT_DIR="$1"
_RESTORE_WORKSPACE="$2"
''' + shell[start:end] + '\n_selected_pull_component memory memory "\\.gpg$"\n'
    result = subprocess.run(['bash', '-c', driver, 'restore-test', str(_PATH.parents[1]), str(work)],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert 'damaged selected inventory: memory' in result.stderr


@pytest.mark.parametrize('status', ['failed', 'expected'])
def test_retryable_and_empty_expected_inventories_remain_supported(tmp_path, status):
    _, work = _open(tmp_path)
    result = subprocess.run([sys.executable, str(_PATH), 'state', str(work), 'extra', status], capture_output=True)
    assert result.returncode == 0
    assert selection.workspace_state(work)['components']['extra'] == {'state': status, 'entries': {}}


@pytest.mark.parametrize('field,value', [
    ('snapshot', 'Genesis/host/20261009T120000Z'),
    ('backend', 'local:/different/backend'), ('version', 2), ('version', True),
])
@pytest.mark.parametrize('entry', ['open', 'pinned', 'inventory', 'cached', 'publish'])
def test_corrupt_workspace_identity_never_authorizes_reuse(tmp_path, field, value, entry):
    root, work = _open(tmp_path)
    state = json.loads((work / 'selection.json').read_text())
    state['components']['data'] = {'state': 'expected', 'entries': {'payload.gpg': None}}
    selection.atomic(work / 'selection.json', state)
    stage = work / '.payload-test'
    stage.write_bytes(b'old ciphertext')
    if entry == 'cached':
        populated = subprocess.run([sys.executable, str(_PATH), 'publish', str(work), 'data', 'payload.gpg', str(stage)], capture_output=True)
        assert populated.returncode == 0
    state[field] = value
    selection.atomic(work / 'selection.json', state)
    before = (work / 'selection.json').read_bytes()
    if entry == 'open':
        with pytest.raises(ValueError):
            selection.selection(root, 'local:/synthetic/backend', None, False, '')
    else:
        if entry == 'pinned':
            args = ['pinned', str(root)]
        elif entry == 'inventory':
            args = ['inventory', str(work), 'data']
        else:
            args = [entry, str(work), 'data', 'payload.gpg']
            if entry == 'publish':
                args.append(str(stage))
        result = subprocess.run([sys.executable, str(_PATH), *args], capture_output=True, text=True)
        assert result.returncode != 0
        assert not result.stdout
    assert (work / 'selection.json').read_bytes() == before
    if entry == 'publish':
        assert stage.read_bytes() == b'old ciphertext'
        assert not (work / 'cache/data/payload.gpg').exists()


@pytest.mark.parametrize('field,value', [('snapshot', 'Genesis/host/20261009T120000Z'), ('version', True)])
def test_orphan_workspace_validated_before_pointer_publication(tmp_path, field, value):
    root, work = _open(tmp_path)
    (root / 'active.json').unlink()
    state = json.loads((work / 'selection.json').read_text())
    state[field] = value
    selection.atomic(work / 'selection.json', state)
    with pytest.raises(ValueError):
        selection.selection(root, 'local:/synthetic/backend', 'Genesis/host/20261007T120000Z', False, str(tmp_path / 'marker'))
    assert not (root / 'active.json').exists()


@pytest.mark.parametrize('version', [2, True])
@pytest.mark.parametrize('entry', ['open', 'pinned'])
def test_pointer_version_is_not_ignored(tmp_path, version, entry):
    root, work = _open(tmp_path)
    selection.atomic(root / 'active.json', {'version': version, 'id': work.name})
    if entry == 'open':
        with pytest.raises(ValueError):
            selection.selection(root, 'local:/synthetic/backend', None, False, '')
    else:
        result = subprocess.run([sys.executable, str(_PATH), 'pinned', str(root)], capture_output=True, text=True)
        assert result.returncode != 0 and not result.stdout


@pytest.mark.parametrize('pointer', [{}, [], None])
@pytest.mark.parametrize('entry', ['open', 'pinned'])
def test_existing_falsey_pointer_is_not_absent(tmp_path, pointer, entry):
    root, work = _open(tmp_path)
    selection.atomic(root / 'active.json', pointer)
    before = (root / 'active.json').read_bytes()
    if entry == 'open':
        with pytest.raises(ValueError):
            selection.selection(root, 'local:/synthetic/backend', 'Genesis/host/20261007T120000Z', False, str(tmp_path / 'marker'))
    else:
        result = subprocess.run([sys.executable, str(_PATH), 'pinned', str(root)], capture_output=True, text=True)
        assert result.returncode != 0 and not result.stdout
    assert (root / 'active.json').read_bytes() == before and (work / 'selection.json').exists()


@pytest.mark.parametrize('delete_metadata', [False, True])
def test_interrupted_retirement_resumes_without_metadata(tmp_path, monkeypatch, delete_metadata):
    root, old = _open(tmp_path)
    (old / 'cache/retained').write_bytes(b'old owned ciphertext')
    original = selection.shutil.rmtree
    def interrupted(path, *args, **kwargs):
        if Path(path).name in (old.name, '.retired-' + old.name):
            if delete_metadata:
                (Path(path) / 'selection.json').unlink()
            raise OSError('injected retirement interruption')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(selection.shutil, 'rmtree', interrupted)
    with pytest.raises(OSError):
        selection.selection(root, 'local:/synthetic/backend', 'Genesis/host/20261008T120000Z', True, str(tmp_path / 'marker'))
    new_id = json.loads((root / 'active.json').read_text())['id']
    assert new_id != old.name
    assert (root / new_id / 'selection.json').exists()
    monkeypatch.setattr(selection.shutil, 'rmtree', original)
    resumed = selection.selection(root, 'local:/synthetic/backend', None, False, '')
    assert resumed.name == new_id
    assert not old.exists() and not list(root.glob('.retired-*'))


def test_retirement_tombstone_symlink_never_deletes_target(tmp_path):
    root, work = _open(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'sentinel').write_bytes(b'preserved')
    (root / ('.retired-' + 'a' * 64)).symlink_to(outside)
    with pytest.raises(ValueError):
        selection.selection(root, 'local:/synthetic/backend', None, False, '')
    assert (outside / 'sentinel').read_bytes() == b'preserved'
    assert (work / 'selection.json').exists()


def test_retirement_rename_fence_failure_preserves_payload_until_retry(tmp_path, monkeypatch):
    root, old = _open(tmp_path)
    (old / 'cache/retained').write_bytes(b'old owned ciphertext')
    original_replace = selection.os.replace
    original_fsync = selection.os.fsync
    renamed = False
    def replace(source, target):
        nonlocal renamed
        result = original_replace(source, target)
        if str(target).endswith('.retired-' + old.name):
            renamed = True
        return result
    def fence(fd):
        if renamed:
            raise OSError('injected retirement directory fence failure')
        return original_fsync(fd)
    monkeypatch.setattr(selection.os, 'replace', replace)
    monkeypatch.setattr(selection.os, 'fsync', fence)
    with pytest.raises(OSError):
        selection.selection(root, 'local:/synthetic/backend', 'Genesis/host/20261008T120000Z', True, str(tmp_path / 'marker'))
    tombstone = root / ('.retired-' + old.name)
    assert (tombstone / 'cache/retained').read_bytes() == b'old owned ciphertext'
    active_id = json.loads((root / 'active.json').read_text())['id']
    assert active_id != old.name and (root / active_id / 'selection.json').exists()
    monkeypatch.setattr(selection.os, 'fsync', original_fsync)
    resumed = selection.selection(root, 'local:/synthetic/backend', None, False, '')
    assert resumed.name == active_id and not tombstone.exists()


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
