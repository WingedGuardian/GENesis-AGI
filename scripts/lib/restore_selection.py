"""Private, snapshot-bound restore cache. Digests prove cache provenance, not authenticity."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fsync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError('cache payload is not a regular file')
    with path.open('rb') as stream:
        result = hashlib.file_digest(stream, 'sha256').hexdigest()
    return result


def private(root: Path) -> None:
    for part in [root, *root.parents]:
        if part.is_symlink():
            raise ValueError('symlink in restore workspace path')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)


def validate_component(component: str) -> None:
    path = Path(component)
    if (not re.fullmatch(r'[A-Za-z0-9._/-]+', component)
            or path.is_absolute() or not path.parts
            or str(path) != component or '..' in path.parts):
        raise ValueError('unsafe component')


def validate_inventory(record: dict) -> None:
    if (not isinstance(record, dict) or record.get('state') not in ('failed', 'empty', 'expected')
            or not isinstance(record.get('entries'), dict)):
        raise ValueError('invalid component inventory')
    if any(not re.fullmatch(r'[A-Za-z0-9._-]+', name) or name in ('.', '..')
           or value is not None for name, value in record['entries'].items()):
        raise ValueError('unsafe payload name or inventory value')
    if record['state'] == 'empty' and record['entries']:
        raise ValueError('empty inventory contains payloads')


def workspace_state(work: Path) -> dict:
    state_path = work / 'selection.json'
    if work.is_symlink() or state_path.is_symlink() or not state_path.is_file():
        raise ValueError('damaged selection directory')
    state = json.loads(state_path.read_text())
    if (not isinstance(state, dict) or type(state.get('version')) is not int
            or state['version'] != 1 or not isinstance(state.get('backend'), str)
            or not state['backend'] or '\0' in state['backend']
            or not isinstance(state.get('snapshot'), str)
            or not re.fullmatch(r'Genesis/[A-Za-z0-9._-]+/[0-9]{8}T[0-9]{6}Z', state['snapshot'])
            or not isinstance(state.get('format'), str) or not state['format']
            or not isinstance(state.get('marker_sha256'), str)
            or not re.fullmatch(r'[a-f0-9]{64}', state['marker_sha256'])
            or not isinstance(state.get('components'), dict)):
        raise ValueError('invalid selection metadata')
    expected = hashlib.sha256(os.fsencode(state['backend'] + '\0' + state['snapshot'])).hexdigest()
    if expected != work.name:
        raise ValueError('selection identity does not match workspace')
    for component, record in state['components'].items():
        validate_component(component)
        validate_inventory(record)
    return state


def active_workspace(root: Path) -> Path | None:
    active = root / 'active.json'
    if active.is_symlink():
        raise ValueError('symlink selection pointer')
    if not active.exists():
        return None
    pointer = json.loads(active.read_text())
    if (not isinstance(pointer, dict) or type(pointer.get('version')) is not int
            or pointer['version'] != 1 or not isinstance(pointer.get('id'), str)
            or not re.fullmatch(r'[a-f0-9]{64}', pointer['id'])):
        raise ValueError('invalid selection pointer')
    work = root / pointer['id']
    workspace_state(work)
    return work


def selection(root: Path, identity: str, snapshot: str | None, refresh: bool, marker: str) -> Path:
    private(root)
    active = root / 'active.json'
    old = active_workspace(root)
    if old is not None:
        state = workspace_state(old)
        if state['backend'] != identity and not refresh:
            raise ValueError('backend changed; use --refresh-snapshot to select it explicitly')
        work = old if not refresh else None
    else:
        work = None
    if work is None:
        if not snapshot or not re.fullmatch(r'Genesis/[A-Za-z0-9._-]+/[0-9]{8}T[0-9]{6}Z', snapshot):
            raise ValueError('invalid snapshot selection')
        sid = hashlib.sha256(os.fsencode(identity + '\0' + snapshot)).hexdigest()
        work = root / sid
        marker_path = Path(marker)
        with marker_path.open() as stream:
            marker_format = stream.readline(128).rstrip('\r\n') or 'legacy'
        state = {'version': 1, 'backend': identity, 'snapshot': snapshot,
                 'format': marker_format, 'marker_sha256': digest(marker_path), 'components': {}}
        if not work.exists():
            # Own only the unpublished pathname. After rename the context sees
            # it absent and leaves committed work available for pointer retry.
            with tempfile.TemporaryDirectory(prefix='.selection-', dir=root) as staging:
                stage = Path(staging)
                shutil.copyfile(marker_path, stage / 'COMPLETE')
                os.chmod(stage / 'COMPLETE', 0o600)
                with (stage / 'COMPLETE').open('rb') as stream:
                    os.fsync(stream.fileno())
                atomic(stage / 'selection.json', state)
                os.replace(stage, work)
                fsync_directory(root)
        workspace_state(work)
        atomic(active, {'version': 1, 'id': sid})
    # A killed refresh resumes retirement before any payload download. Only direct,
    # validated tool-owned selection directories are eligible, never targets/backups.
    for item in root.iterdir():
        if re.fullmatch(r'\.retired-[a-f0-9]{64}', item.name):
            if item.is_symlink() or not item.is_dir():
                raise ValueError('unsafe retirement tombstone')
            # Retry a rename whose directory fence previously failed before
            # deleting any more children. Metadata may already have been deleted.
            fsync_directory(root)
            shutil.rmtree(item)
        if re.fullmatch(r'\.selection-[A-Za-z0-9_]+', item.name):
            if item.is_symlink() or not item.is_dir():
                raise ValueError('unsafe interrupted selection staging')
            shutil.rmtree(item)
    for item in root.iterdir():
        if item != work and re.fullmatch(r'[a-f0-9]{64}', item.name):
            workspace_state(item)
            tombstone = root / ('.retired-' + item.name)
            if tombstone.exists() or tombstone.is_symlink():
                raise ValueError('retirement tombstone already exists')
            os.replace(item, tombstone)
            fsync_directory(root)
            shutil.rmtree(tombstone)
    for item in work.iterdir():
        if re.fullmatch(r'\.(payload|marker)\.[A-Za-z0-9]+|\.state-[A-Za-z0-9_]+', item.name):
            if item.is_symlink() or not item.is_file():
                raise ValueError('unsafe interrupted cache staging file')
            item.unlink()
    private(work / 'cache')
    if (work / 'view').exists():
        if (work / 'view').is_symlink():
            raise ValueError('symlink restore view')
        shutil.rmtree(work / 'view')
    private(work / 'view')
    return work


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['open', 'pinned', 'inventory', 'cached', 'publish', 'state', 'marker', 'format', 'mode'])
    parser.add_argument('root', type=Path)
    parser.add_argument('args', nargs='*')
    args = parser.parse_args()
    root, values = args.root, args.args
    if args.command == 'pinned':
        for item in [root, *root.parents]:
            if item.is_symlink():
                raise ValueError('symlink in restore selection path')
        work = active_workspace(root)
        if work is not None:
            print(workspace_state(work)['snapshot'])
        return
    if args.command == 'open':
        print(selection(root, values[0], values[1] or None, values[2] == 'true', values[3]))
        return
    state_path = root / 'selection.json'
    state = workspace_state(root)
    if args.command == 'mode':
        if values:
            if values[0] not in ('legacy', 'pooled'):
                raise ValueError('invalid transcript mode')
            state['transcript_mode'] = values[0]
            atomic(state_path, state)
        mode = state.get('transcript_mode')
        if mode not in ('legacy', 'pooled'):
            raise SystemExit(3)
        print(mode)
        return
    if args.command == 'format':
        print(state['format'])
        return
    if args.command == 'marker':
        if digest(Path(values[0])) != state['marker_sha256']:
            raise ValueError('selected COMPLETE marker changed')
        return
    component = values[0]
    validate_component(component)
    components = state['components']
    if args.command == 'inventory':
        current = components.get(component, {})
        if current.get('state') in ('expected', 'empty'):
            print('\n'.join(current['entries']))
            return
        raise SystemExit(3)
    if args.command == 'state':
        status = values[1]
        names = values[2:]
        record = {'state': status, 'entries': {name: None for name in names}}
        validate_inventory(record)
        components[component] = record
        atomic(state_path, state)
        return
    name = values[1]
    expected = components[component]['entries']
    if name not in expected:
        raise ValueError('payload absent from selected inventory')
    cache = root / 'cache' / component / name
    view = root / 'view' / component / name
    private(cache.parent)
    private(view.parent)
    receipts = cache.parent / '.checksums'
    private(receipts)
    receipt_path = receipts / (hashlib.sha256(name.encode()).hexdigest() + '.json')
    binding = {'version': 1, 'selection': root.name, 'component': component, 'name': name}
    if args.command == 'publish':
        stage = Path(values[2])
        sha = digest(stage)
        with stage.open('rb') as stream:
            os.fsync(stream.fileno())
        os.chmod(stage, 0o600)
        os.replace(stage, cache)
        directory_fd = os.open(cache.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        atomic(receipt_path, {**binding, 'sha256': sha})
    else:
        try:
            if receipt_path.is_symlink():
                raise ValueError('symlink checksum receipt')
            receipt = json.loads(receipt_path.read_text())
        except (OSError, ValueError):
            raise SystemExit(3) from None
        if not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in binding.items()):
            raise SystemExit(3)
        if not cache.exists() or digest(cache) != receipt.get('sha256'):
            raise SystemExit(3)
    view.unlink(missing_ok=True)
    os.link(cache, view)
    os.chmod(view, 0o600)


if __name__ == '__main__':
    main()
