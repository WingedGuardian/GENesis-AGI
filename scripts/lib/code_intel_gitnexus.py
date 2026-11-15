"""Pinned GN inputs; provider discovery processes belong after scoped startup."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


def version(snapshot):
    pins = re.findall(rb'^GENESIS_GITNEXUS_VERSION="([0-9]+\.[0-9]+\.[0-9]+)"$',
                      snapshot.buffers['scripts/lib/gitnexus_version.sh'], re.M)
    if len(pins) != 1:
        raise ValueError('captured GN version pin is unavailable')
    return pins[0].decode('ascii')


def _pin(path, modules):
    path = path.resolve(strict=True)
    with path.open('rb') as stream:
        identity = modules['queued'].executable_identity(stream)
    return dict(path=str(path), **identity)


def _which(name, path, cwd):
    search = os.pathsep.join(str(cwd / part) if not Path(part).is_absolute() else part
                             for part in path.split(os.pathsep))
    return shutil.which(name, path=search)


def _package(entry, snapshot, modules):
    for directory in entry.parents[:5]:
        path = directory / 'package.json'
        if not path.is_file():
            continue
        pin = _pin(path, modules)
        with path.open('rb') as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != pin['sha256']:
            raise ValueError('GN package metadata changed or exceeds its supported bound')
        data = json.loads(raw, object_pairs_hook=modules['proof']._unique)
        if isinstance(data, dict) and data.get('name') == 'gitnexus':
            if (data.get('version') != version(snapshot) or data.get('type') != 'module'
                    or data.get('bin') != {'gitnexus': 'dist/cli/index.js'}
                    or (directory/'dist/cli/index.js').resolve(strict=True) != entry):
                raise ValueError('GN package differs from its supported pinned ESM entry')
            return pin
    raise ValueError('typed GN requires a packaged provider; version execution is not metadata')


def capture(snapshot, modules, environment, *, cwd):
    """Read one representable provisional choice; never execute Node/npm/provider.

    The unchanged canonical resolver must confirm it after registration. An
    implicit selection outside this known population or multiple installations
    needs an existing explicit operator override, not an inferred npm parser.
    """
    cwd = Path(cwd).resolve(strict=True)
    path = environment.get('PATH')
    if not isinstance(path, str):
        raise ValueError('typed GN requires its explicit resolution PATH')
    home = Path(environment.get('HOME') or Path.home())
    state = Path(environment.get('GENESIS_HOME') or home / '.genesis')
    selected = environment.get('GITNEXUS_NODE_BIN') or ''
    if not selected:
        config = state / 'gitnexus-node'
        if config.exists() or config.is_symlink():
            selected = config.read_text().rstrip('\n')
            if not selected:
                raise ValueError('empty GN Node selection')
    explicit_node = bool(selected)
    if explicit_node and (not Path(selected).is_absolute() or Path(selected).name != 'node'
                          or '\n' in selected or ':' in selected):
        raise ValueError('invalid GN Node selection')
    node = Path(selected or _which('node', path, cwd) or '')
    if not node.is_file() or not os.access(node, os.X_OK):
        raise ValueError('selected GN Node is unavailable')
    frozen = dict(environment)
    frozen.pop('INVOCATION_ID', None)
    if explicit_node:
        frozen['PATH'] = str(node.parent) + ':' + path
    binary = environment.get('GITNEXUS_BIN') or ''
    explicit = Path(binary) if binary else None
    if explicit is not None and not explicit.is_absolute():
        explicit = cwd / explicit
    if explicit is not None and explicit.is_file() and os.access(explicit, os.X_OK):
        candidates = [explicit]
    else:
        path_candidate = _which('gitnexus', path, cwd)
        candidates = [home / '.npm-global/bin/gitnexus', home / '.local/bin/gitnexus',
                      Path('/usr/local/bin/gitnexus'), Path('/usr/bin/gitnexus')]
        if path_candidate:
            candidates.append(Path(path_candidate))
    unique = {}
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            resolved = candidate.resolve(strict=True)
            info = resolved.stat()
            unique.setdefault((info.st_dev, info.st_ino), resolved)
    if len(unique) != 1:
        raise ValueError('typed GN requires one provisional installation or an explicit override')
    entry, = unique.values()
    return dict(node=_pin(node, modules), entry=_pin(entry, modules),
                package=_package(entry, snapshot, modules), cwd=str(cwd), path=path), frozen


def _source(snapshot, modules, context, reserved, policy, options, recipe):
    configuration = dict(repo_path=context['repo_path'], run_mode=reserved['run_mode'],
        cap_bytes=reserved['cap_bytes'], policy=policy, provider_options=options,
        environment_recipe=recipe, arguments=options['arguments'])
    return dict(commit=snapshot.commit, files=dict(snapshot.files),
        provider_digest=options['node']['sha256'], provider_version='gitnexus@'+version(snapshot),
        configuration_sha256=modules['queue']._native.digest(configuration))


def freeze(snapshot, modules, context, reserved, policy, environment, *, cwd=None, recovery=False):
    """Bind actual files and the complete invocation recipe without admission."""
    if context['provider'] != 'gitnexus':
        raise ValueError('GN adapter requires its own provider context')
    options, frozen = capture(snapshot, modules, environment, cwd=Path.cwd() if cwd is None else cwd)
    if type(recovery) is not bool:
        raise ValueError('invalid GN recovery argument selection')
    options['arguments'] = ['analyze', '--force', '--no-parse-cache'] if recovery else ['analyze']
    recipe = dict(frozen=frozen, registration_slots={'INVOCATION_ID': 'scope.invocation_id'})
    modules['queued'].NativeEnvironment(recipe, modules['queue']._native)
    return options, recipe, _source(snapshot, modules, context, reserved, policy, options, recipe)


def validate(snapshot, modules, value):
    """Closed capsule decoding is not discovery, registration or provider I/O."""
    native = modules['queue']._native
    options = native._object(value['provider_options'], ('node', 'entry', 'package', 'cwd', 'path', 'arguments'),
                             'GN runtime options')
    if options['arguments'] not in (['analyze'], ['analyze', '--force', '--no-parse-cache']):
        raise ValueError('unsupported GN runtime arguments')
    for key in ('node', 'entry', 'package'):
        pin = native._object(options[key], ('path', 'device', 'inode', 'sha256'), 'GN file pin')
        if not Path(native._text(pin['path'], 'GN file path')).is_absolute():
            raise ValueError('GN file pin requires an absolute path')
        native._integer(pin['device'], 'GN file device')
        native._integer(pin['inode'], 'GN file inode', positive=True)
        native._hex(pin['sha256'], 64, 'GN file digest')
    if (not Path(native._text(options['cwd'], 'GN resolver directory')).is_absolute()
            or not isinstance(options['path'], str) or '\0' in options['path']):
        raise ValueError('invalid GN resolver inputs')
    expected = _source(snapshot, modules, value['context'], value['reservation'], value['policy'],
                       options, value['environment_recipe'])
    if expected != value['reservation']['source']:
        raise ValueError('GN runtime differs from its complete accepted source and recipe')


def require_origin(value, modules):
    """Refuse an argv mismatch against the actual retained execution origin.

    This observation is a wiring fence, never a launch acknowledgement. The
    admitted native transitions independently enforce grant/source/evidence.
    """
    origin = modules['queue'].execution_status(value['context']).get('nonce_origin')
    if origin not in ('generated', 'selected-recovery'):
        raise ValueError('GN requires its actual supported execution origin')
    recovery = origin == 'selected-recovery'
    expected = ['analyze', '--force', '--no-parse-cache'] if recovery else ['analyze']
    if value['provider_options']['arguments'] != expected:
        raise ValueError('GN arguments differ from the actual execution origin')
    return recovery


def current_source(snapshot, modules, value):
    """Re-read accepted inputs without running selection/version processes."""
    if not snapshot.current_matches():
        raise ValueError('captured GN startup source changed')
    recovery = require_origin(value, modules)
    original = dict(value['environment_recipe']['frozen'])
    original['PATH'] = value['provider_options']['path']
    options, recipe, source = freeze(snapshot, modules, value['context'], value['reservation'],
        value['policy'], original, cwd=value['provider_options']['cwd'],
        recovery=recovery)
    if options != value['provider_options'] or recipe != value['environment_recipe']:
        raise ValueError('accepted GN files or native environment changed')
    return source


def confirm(snapshot, modules, value, environment):
    """The scoped registered reporter confirms the unchanged canonical resolver."""
    program = snapshot.buffers['scripts/lib/gitnexus_version.sh'].decode('utf-8') + r'''
genesis_gitnexus_select_node && genesis_gitnexus_node_supported && genesis_gitnexus_installed_is_pinned || exit 3
resolved="$(genesis_gitnexus_resolve_binary)" || exit 3
printf '%s\0%s\0' "$(command -v node)" "$resolved"
'''
    options = value['provider_options']
    result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-p', '-c', program],
        cwd=options['cwd'], env=dict(environment), capture_output=True, check=True)
    fields = result.stdout.split(b'\0')
    if len(fields) != 3 or fields[-1] or not all(fields[:2]):
        raise ValueError('invalid canonical GN resolver transport')
    for key, field in zip(('node', 'entry'), fields[:2], strict=True):
        path = Path(field.decode('utf-8'))
        path = path if path.is_absolute() else Path(options['cwd']) / path
        if _pin(path, modules) != options[key]:
            raise ValueError('canonical GN resolver differs from the reserved installation')


def run(snapshot, modules, value, environment, *, startup, observe, verify):
    """Prepare only after registration; native results use the shared durable seam."""
    queued = None
    try:
        context, reserved, policy = value['context'], value['reservation'], value['policy']

        def admit():
            modules['admission'].assess(reserved['cap_bytes'], policy['sibling_reserve_bytes'],
                reserved['unit'], provider='gitnexus', cache_reserve=policy['cache_reserve_bytes'])

        probe = modules['proof'].ScopeProbe(reserved['unit'], reserved['cap_bytes'],
                                          route=(policy['route'], policy['slice']))
        manager = modules['scope'].ScopeManager(modules['unit'].bus_call, modules['unit'].typed_property,
                                               deadline=time.monotonic()+policy['wall_seconds'])
        queued = modules['queued'].QueuedRun(modules['queue'], modules['proof'], probe, manager,
            context, source=lambda: current_source(snapshot, modules, value), admit=admit)
        queued.bind_startup(startup, observe, verify=verify)
        registered = queued.register()
        admit()
        native_environment = environment.resolve(registered['invocation_id'], dict(os.environ))
        confirm(snapshot, modules, value, native_environment)
        with Path(value['provider_options']['node']['path']).open('rb') as executable:
            report = queued.native(
                [f'/proc/self/fd/{executable.fileno()}', value['provider_options']['entry']['path'],
                 *value['provider_options']['arguments']],
                executable, after_spawn=executable.close,
                pass_fds=(executable.fileno(),), env=native_environment, cwd=context['repo_path'])
        provider_ok = report['outcome'] == 'complete' and report['payload_returncode'] == 0
        return 0 if provider_ok else 111
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
        print('code-intel: captured GN refused/failed: '+type(error).__name__, file=sys.stderr)
        return 125
    finally:
        if queued is not None:
            if queued.report is not None:
                queued.terminal(provider_ok=(queued.report['outcome'] == 'complete'
                                            and queued.report['payload_returncode'] == 0))
            elif queued.registration is not None and queued.launch is None:
                queued.refuse_preparation()
