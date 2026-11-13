"""Provider selection freezes files only; executable checks happen after ACK."""
import hashlib
import json
import runpy
import shlex
from pathlib import Path

import pytest

from tests.test_scripts.test_code_intel_source import case
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules


def adapter():
    path = Path(__file__).resolve().parents[2] / 'scripts/lib/code_intel_gitnexus.py'
    assert path.is_file(), 'typed GN adapter must bind metadata without executing providers'
    return runpy.run_path(str(path))


def package(path, version='1.6.12'):
    script = path / 'dist/cli/index.js'
    script.parent.mkdir(parents=True)
    script.write_text('#!/usr/bin/env node\n// harmless metadata fixture\n')
    script.chmod(0o700)
    (path / 'package.json').write_text(json.dumps(dict(name='gitnexus', version=version, type='module',
        bin=dict(gitnexus='dist/cli/index.js'))))
    return script


@pytest.fixture
def selection(tmp_path, monkeypatch):
    _, _, snapshot = case(tmp_path, monkeypatch, 'runner')
    modules = snapshot.assemble()
    is_file = Path.is_file
    monkeypatch.setattr(Path, 'is_file', lambda path: False if str(path) in (
        '/usr/local/bin/gitnexus', '/usr/bin/gitnexus', '/bin/gitnexus') else is_file(path))
    home, state, cwd = [tmp_path / p for p in ('home', 'state', 'caller')]
    for path in (home, state, cwd):
        path.mkdir()
    activity = tmp_path / 'unexpected-execution'
    node = home / 'selected node/bin/node'
    node.parent.mkdir(parents=True)
    node.write_text('#!/bin/sh\nprintf executed > ' + shlex.quote(str(activity)) + '\nprintf v22.23.2\n')
    node.chmod(0o700)
    entry = package(home / 'provider')
    env = dict(HOME=str(home), GENESIS_HOME=str(state), PATH='/usr/bin:/bin',
        GITNEXUS_NODE_BIN=str(node), GITNEXUS_BIN=str(entry), INVOCATION_ID='1'*32)
    return snapshot, modules, env, cwd, activity, entry, node


def test_selection_is_metadata_only_with_one_native_path_prefix(selection):
    snapshot, modules, env, cwd, activity, entry, node = selection
    options, frozen = adapter()['capture'](snapshot, modules, env, cwd=cwd)
    assert not activity.exists(), 'no version/provider subprocess before reservation'
    assert options['node']['path'] == str(node.resolve())
    assert options['entry']['path'] == str(entry.resolve())
    assert options['node']['sha256'] == hashlib.sha256(node.read_bytes()).hexdigest()
    assert options['entry']['sha256'] == hashlib.sha256(entry.read_bytes()).hexdigest()
    assert options['package']['path'] == str(entry.parents[2] / 'package.json')
    assert options['cwd'] == str(cwd) and options['path'] == env['PATH']
    assert frozen['PATH'] == str(node.parent) + ':' + env['PATH']
    assert 'INVOCATION_ID' not in frozen and env['INVOCATION_ID'] == '1'*32
    assert json.loads(json.dumps(options)) == options


@pytest.mark.parametrize('bad', ['relative/node', '/missing/node', '/bin/sh', '/bad:path/node', '/bad\npath/node'])
def test_invalid_selected_node_refuses_without_running_anything(selection, bad):
    snapshot, modules, env, cwd, activity, _, _ = selection
    env['GITNEXUS_NODE_BIN'] = bad
    with pytest.raises((OSError, ValueError)):
        adapter()['capture'](snapshot, modules, env, cwd=cwd)
    assert not activity.exists()


def test_machine_node_selection_uses_same_trailing_newline_semantics(selection):
    snapshot, modules, env, cwd, activity, _, node = selection
    env.pop('GITNEXUS_NODE_BIN')
    (Path(env['GENESIS_HOME']) / 'gitnexus-node').write_text(str(node)+'\n\n')
    options, frozen = adapter()['capture'](snapshot, modules, env, cwd=cwd)
    assert options['node']['path'] == str(node)
    assert frozen['PATH'] == str(node.parent)+':'+env['PATH'] and not activity.exists()


def test_metadata_pin_refuses_another_package_version(selection):
    snapshot, modules, env, cwd, activity, entry, _ = selection
    manifest = entry.parents[2] / 'package.json'
    data = json.loads(manifest.read_text())
    data['version'] = '99.0.0'
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        adapter()['capture'](snapshot, modules, env, cwd=cwd)
    assert not activity.exists()


def test_aliases_are_one_install_but_distinct_implicit_installs_refuse(selection):
    snapshot, modules, env, cwd, activity, entry, _ = selection
    env.pop('GITNEXUS_BIN')
    home = Path(env['HOME'])
    first = home / '.npm-global/bin/gitnexus'
    first.parent.mkdir(parents=True)
    first.symlink_to(entry)
    second = home / '.local/bin/gitnexus'
    second.parent.mkdir(parents=True)
    second.symlink_to(entry)
    options, _ = adapter()['capture'](snapshot, modules, env, cwd=cwd)
    assert options['entry']['path'] == str(entry)
    second.unlink()
    different = package(home / 'other-provider')
    second.symlink_to(different)
    with pytest.raises(ValueError):
        adapter()['capture'](snapshot, modules, env, cwd=cwd)
    env['GITNEXUS_BIN'] = str(different)
    options, _ = adapter()['capture'](snapshot, modules, env, cwd=cwd)
    assert options['entry']['path'] == str(different) and not activity.exists()


def test_relative_explicit_binary_resolves_in_original_caller_directory(selection):
    snapshot, modules, env, cwd, activity, entry, _ = selection
    link = cwd / 'literal $entry'
    link.symlink_to(entry)
    env['GITNEXUS_BIN'] = './literal $entry'
    options, _ = adapter()['capture'](snapshot, modules, env, cwd=cwd)
    assert options['entry']['path'] == str(entry) and not activity.exists()


def test_unpackaged_executable_is_not_executed_to_discover_its_version(selection):
    snapshot, modules, env, cwd, activity, _, node = selection
    env['GITNEXUS_BIN'] = str(node)
    with pytest.raises(ValueError):
        adapter()['capture'](snapshot, modules, env, cwd=cwd)
    assert not activity.exists()


def test_absent_path_candidate_does_not_invent_an_executable_filename(selection):
    snapshot, modules, env, cwd, activity, _, _ = selection
    env.pop('GITNEXUS_BIN')
    entry = package(cwd)
    (cwd / '.no-gitnexus-candidate').symlink_to(entry)
    with pytest.raises(ValueError):
        adapter()['capture'](snapshot, modules, env, cwd=cwd)
    assert not activity.exists()


def test_explicit_provider_override_ends_the_discovery_search(selection, monkeypatch):
    snapshot, modules, env, cwd, activity, entry, _ = selection
    namespace = adapter()
    def forbidden(*args):
        raise AssertionError('explicit provider selection must not search PATH')
    monkeypatch.setitem(namespace['capture'].__globals__, '_which', forbidden)
    options, _ = namespace['capture'](snapshot, modules, env, cwd=cwd)
    assert options['entry']['path'] == str(entry) and not activity.exists()


@pytest.mark.parametrize('fault', ['type', 'bin', 'wrapper'])
def test_only_the_pinned_package_esm_entry_is_a_representable_native_input(selection, fault):
    snapshot, modules, env, cwd, activity, entry, _ = selection
    path = entry.parents[2]/'package.json'
    data = json.loads(path.read_text())
    if fault == 'type':
        data['type'] = 'commonjs'
    elif fault == 'bin':
        data['bin']['gitnexus'] = 'wrapper.js'
    else:
        wrapper = entry.parents[2]/'wrapper.js'
        wrapper.write_bytes(entry.read_bytes())
        wrapper.chmod(0o700)
        env['GITNEXUS_BIN'] = str(wrapper)
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        adapter()['capture'](snapshot, modules, env, cwd=cwd)
    assert not activity.exists()
