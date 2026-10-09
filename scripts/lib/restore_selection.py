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


def atomic(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
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


def selection(root: Path, identity: str, snapshot: str | None, refresh: bool, marker: str) -> Path:
    private(root)
    active = root / 'active.json'
    if active.is_symlink():
        raise ValueError('symlink selection pointer')
    old = json.loads(active.read_text()) if active.exists() else None
    if old:
        if not re.fullmatch(r'[a-f0-9]{64}', old['id']):
            raise ValueError('invalid selection ID')
        if (root / old['id']).is_symlink() or (root / old['id'] / 'selection.json').is_symlink():
            raise ValueError('symlink selected workspace')
        state = json.loads((root / old['id'] / 'selection.json').read_text())
        if state['backend'] != identity and not refresh:
            raise ValueError('backend changed; use --refresh-snapshot to select it explicitly')
        work = root / old['id'] if not refresh else None
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
                directory_fd = os.open(root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        elif work.is_symlink() or not (work / 'selection.json').is_file():
            raise ValueError('damaged selection directory')
        atomic(active, {'version': 1, 'id': sid})
    # A killed refresh resumes retirement before any payload download. Only direct,
    # validated tool-owned selection directories are eligible, never targets/backups.
    for item in root.iterdir():
        if re.fullmatch(r'\.selection-[A-Za-z0-9_]+', item.name):
            if item.is_symlink() or not item.is_dir():
                raise ValueError('unsafe interrupted selection staging')
            shutil.rmtree(item)
        if item != work and re.fullmatch(r'[a-f0-9]{64}', item.name):
            if item.is_symlink() or not (item / 'selection.json').is_file():
                raise ValueError('invalid retired workspace')
            retired = json.loads((item / 'selection.json').read_text())
            expected_id = hashlib.sha256(os.fsencode(retired['backend'] + '\0' + retired['snapshot'])).hexdigest()
            if retired.get('version') != 1 or expected_id != item.name:
                raise ValueError('unrecognized retired workspace')
            shutil.rmtree(item)
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
        if (root / 'active.json').is_symlink():
            raise ValueError('symlink selection pointer')
        if (root / 'active.json').exists():
            pointer = json.loads((root / 'active.json').read_text())
            if not re.fullmatch(r'[a-f0-9]{64}', pointer['id']):
                raise ValueError('invalid selection pointer')
            print(json.loads((root / pointer['id'] / 'selection.json').read_text())['snapshot'])
        return
    if args.command == 'open':
        print(selection(root, values[0], values[1] or None, values[2] == 'true', values[3]))
        return
    state_path = root / 'selection.json'
    state = json.loads(state_path.read_text())
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
    component_path = Path(component)
    if (not re.fullmatch(r'[A-Za-z0-9._/-]+', component)
            or component_path.is_absolute() or not component_path.parts
            or str(component_path) != component or '..' in component_path.parts):
        raise ValueError('unsafe component')
    components = state['components']
    if args.command == 'inventory':
        current = components.get(component, {})
        if current.get('state') in ('expected', 'empty'):
            print('\n'.join(current.get('entries', {})))
            return
        raise SystemExit(3)
    if args.command == 'state':
        status = values[1]
        names = values[2:]
        if status not in ('failed', 'empty', 'expected'):
            raise ValueError('invalid inventory state')
        if any(not re.fullmatch(r'[A-Za-z0-9._-]+', name) or name in ('.', '..') for name in names):
            raise ValueError('unsafe payload name')
        components[component] = {'state': status, 'entries': {name: None for name in names}}
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
