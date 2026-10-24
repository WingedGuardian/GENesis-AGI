"""Finite carrier facts: decision-only fixtures never run systemd or deploy code."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import _live_fence as fence


@pytest.fixture
def carrier(tmp_path, monkeypatch):
    private, real, live = (tmp_path / name for name in ('private', 'real', 'live'))
    for path in (private, real, live):
        path.mkdir()
    script = private / 'scripts/restore.sh'
    script.parent.mkdir()
    script.write_text('#!/bin/sh\nexit 0\n')
    monkeypatch.setattr(fence._S, 'real_home', str(real))
    monkeypatch.setattr(fence._S, 'live_roots', frozenset({str(live)}))
    monkeypatch.setattr(fence._S, 'allowed_roots', (str(tmp_path),))
    monkeypatch.setattr(fence._S, 'opted_out', True)
    monkeypatch.setattr(fence._S, 'real_tool_names', frozenset())
    env = dict(HOME=str(private), GENESIS_DIR=str(private), PATH=os.defpath)
    return private, real, live, script, env


def decision(carrier, options, payload=None):
    private, _, _, script, env = carrier
    argv = ['systemd-run', '--user', *options, '--', *(payload or ['/bin/bash', str(script)])]
    return fence.judge(argv[0], argv, env, str(private))


@pytest.mark.parametrize('key', ['HOME', 'GENESIS_DIR'])
@pytest.mark.parametrize('form', ['short', 'attached', 'long', 'equals'])
@pytest.mark.parametrize('scope', [False, True])
def test_setenv_live_context_is_not_lost(carrier, key, form, scope):
    _, real, live, _, _ = carrier
    value = key + '=' + str(real if key == 'HOME' else live)
    options = {'short': ['-E', value], 'attached': ['-E' + value],
               'long': ['--setenv', value], 'equals': ['--setenv=' + value]}[form]
    assert decision(carrier, (['--scope'] if scope else []) + options)


def test_scope_retains_private_caller_but_service_does_not(carrier):
    assert decision(carrier, ['--scope']) is None
    assert decision(carrier, [])
    assert decision(carrier, [], ['/usr/bin/true']) is None
    assert decision(carrier, ['--scope'], ['/usr/bin/true']) is None


@pytest.mark.parametrize('properties', [[], ['-p', 'MemoryMax=32M', '-p', 'MemorySwapMax=0']])
def test_explicit_absolute_service_facts_do_not_need_cwd(carrier, properties):
    private = carrier[0]
    options = ['-E', f'HOME={private}', '-E', f'GENESIS_DIR={private}', *properties]
    assert decision(carrier, options) is None


@pytest.mark.parametrize('path_kind', ['absolute', 'empty', 'relative', 'missing'])
@pytest.mark.parametrize('tool_kind', ['owned', 'foreign', 'absent'])
def test_bare_tool_search_only_needs_cwd_for_relative_path(carrier, tmp_path, path_kind, tool_kind):
    private = carrier[0]
    directory = private / 'bin' if tool_kind != 'foreign' else tmp_path / 'foreign-bin'
    directory.mkdir()
    if tool_kind != 'absent':
        tool = directory / 'sudo'
        tool.write_text('#!/bin/sh\nexit 0\n')
        tool.chmod(0o755)
    # A foreign executable is deliberately outside the allowed fixture roots.
    fence._S.allowed_roots = (str(private),)
    options = [] if path_kind == 'missing' else ['-E', 'PATH=' + {
        'absolute': str(directory), 'empty': '', 'relative': 'bin'}[path_kind]]
    result = decision(carrier, options, ['sudo'])
    if path_kind == 'absolute' and tool_kind != 'foreign':
        assert result is None
    else:
        assert result


@pytest.mark.parametrize('manager_optout', [False, True])
@pytest.mark.parametrize('real_optout', [False, True])
@pytest.mark.parametrize('payload_kind', ['live-home', 'live-root', 'foreign-tool', 'benign', 'private'])
def test_real_carrier_optouts_keep_context_and_payload_checks(carrier, monkeypatch, manager_optout, real_optout, payload_kind):
    private, real, live, script, env = carrier
    monkeypatch.setattr(fence._S, 'opted_out', manager_optout)
    monkeypatch.setattr(fence._S, 'real_tool_names', frozenset({'systemd-run'} if real_optout else ()))
    shim_dir = private / 'shim'
    shim_dir.mkdir()
    shim = shim_dir / 'systemd-run'
    shim.write_text('#!/bin/sh\nexit 0\n')
    shim.chmod(0o755)
    env['PATH'] = str(shim_dir)
    monkeypatch.setattr(fence._S, 'base', str(shim_dir))
    monkeypatch.setattr(fence._S, 'allowed_roots', (str(private),))
    options = ['-E', f'HOME={private}', '-E', f'GENESIS_DIR={private}']
    payload = ['/bin/bash', str(script)]
    if payload_kind == 'live-home':
        options += ['-E', f'HOME={real}']
    elif payload_kind == 'live-root':
        options += ['-E', f'GENESIS_DIR={live}']
    elif payload_kind == 'foreign-tool':
        foreign = real / 'sudo'
        foreign.write_text('#!/bin/sh\nexit 0\n')
        foreign.chmod(0o755)
        payload = [str(foreign)]
    elif payload_kind == 'benign':
        payload = ['/usr/bin/true']
    result = decision(carrier, options, payload)
    if manager_optout or real_optout:
        assert bool(result) == (payload_kind in ('live-home', 'live-root', 'foreign-tool'))
    else:
        # The answered shim never launches its payload and retains caller facts.
        assert result is None


@pytest.mark.parametrize('manager_optout,real_optout', [(False, True), (True, False), (True, True)])
def test_real_carrier_optouts_refuse_unknown_payload_boundary(carrier, monkeypatch, manager_optout, real_optout):
    monkeypatch.setattr(fence._S, 'opted_out', manager_optout)
    monkeypatch.setattr(fence._S, 'real_tool_names', frozenset({'systemd-run'} if real_optout else ()))
    assert decision(carrier, ['--unknown-value-option', 'anything'], ['/usr/bin/true'])


@pytest.mark.parametrize('value', ['', 'HOME', 'GENESIS_DIR'])
def test_empty_or_missing_import_does_not_invent_private_service_facts(carrier, value):
    private, _, _, _, env = carrier
    if value:
        env.pop(value)
        options = ['-E', f'{"GENESIS_DIR" if value == "HOME" else "HOME"}={private}', '-E', value]
    else:
        options = ['-E', 'HOME=', '-E', f'GENESIS_DIR={private}']
    assert decision(carrier, options)


def test_repeated_explicit_environment_values_use_last_value(carrier):
    private, real, _, _, _ = carrier
    safe = ['-E', f'GENESIS_DIR={private}']
    assert decision(carrier, [*safe, '-E', f'HOME={real}', '-E', f'HOME={private}']) is None
    assert decision(carrier, [*safe, '-E', f'HOME={private}', '-E', f'HOME={real}'])


@pytest.mark.parametrize('before', [False, True])
@pytest.mark.parametrize('property_value', ['Environment=HOME=/foreign', 'RootDirectory=/foreign', 'WorkingDirectory=/foreign'])
def test_unmodelled_property_taint_survives_later_assignments(carrier, before, property_value):
    private = carrier[0]
    safe = ['-E', f'HOME={private}', '-E', f'GENESIS_DIR={private}']
    prop = ['-p', property_value]
    options = prop + safe if before else safe + prop
    assert decision(carrier, options)
    assert decision(carrier, options, ['/usr/bin/true']) is None
    payload = ['env', f'HOME={private}', f'GENESIS_DIR={private}', '/bin/bash', str(carrier[3])]
    assert decision(carrier, options, payload)


@pytest.mark.parametrize('scope', [False, True])
def test_nested_carriers_preserve_inherited_namespace_uncertainty(carrier, scope):
    private = carrier[0]
    nested = ['systemd-run', '--user', *(['--scope'] if scope else []),
              '-E', f'HOME={private}', '-E', f'GENESIS_DIR={private}',
              '--', '/bin/bash', str(carrier[3])]
    assert decision(carrier, ['--scope', '-p', 'RootDirectory=/unproved'], nested)
    nested[-2:] = ['/usr/bin/true']
    assert decision(carrier, ['--scope', '-p', 'RootDirectory=/unproved'], nested) is None


@pytest.mark.parametrize('selector', [['-d'], ['--same-dir'], ['--working-directory']])
def test_cwd_selectors_consume_the_correct_number_of_arguments(carrier, selector):
    private, _, _, script, _ = carrier
    options = ['-E', f'HOME={private}', '-E', f'GENESIS_DIR={private}']
    if selector == ['--working-directory']:
        selector = [*selector, str(private)]
    assert decision(carrier, options + selector, ['/bin/bash', 'scripts/restore.sh']) is None
    assert fence._systemd_run_payload(['systemd-run', *selector, '--', '/bin/bash', str(script)]) == ['/bin/bash', str(script)]


def test_unknown_and_conflicting_cwd_stays_unknown_for_relative_payload(carrier):
    private = carrier[0]
    safe = ['-E', f'HOME={private}', '-E', f'GENESIS_DIR={private}']
    relative = ['/bin/bash', 'scripts/restore.sh']
    assert decision(carrier, safe, relative)
    assert decision(carrier, safe + ['-d', '--working-directory=' + str(private)], relative)
    assert decision(carrier, safe + ['--working-directory=relative'], relative)


def test_property_values_are_not_script_operands(carrier):
    options = ['--description', '/foreign/scripts/restore.sh', '-E', 'LABEL=/foreign/scripts/restore.sh']
    assert decision(carrier, options, ['/usr/bin/true']) is None


@pytest.mark.parametrize('unset', [['-u', 'HOME'], ['--unset=HOME'], ['-i']])
def test_payload_env_removal_preserves_known_absence(carrier, unset):
    private = carrier[0]
    payload = ['env', *unset, '/bin/bash', str(carrier[3])]
    assert decision(carrier, ['--scope'], payload)
    restored = ['env', '-i', f'HOME={private}', f'GENESIS_DIR={private}', '/bin/bash', str(carrier[3])]
    assert decision(carrier, [], restored) is None


def test_unknown_carrier_arity_cannot_certify_payload(carrier):
    assert decision(carrier, ['--unknown-value-option', 'anything'], ['/usr/bin/true'])


@pytest.mark.parametrize('mode', ['-D', '+D', '--dump-strings', '--dump-po-strings', '--pretty-print', '--help', '--version'])
def test_bash_nonexecution_actions_only_apply_before_script(carrier, mode):
    _, real, _, script, env = carrier
    env = dict(env, HOME=str(real))
    assert fence.judge('/bin/bash', ['/bin/bash', mode, str(script)], env) is None
    assert fence.judge('/bin/bash', ['/bin/bash', str(script), mode], env)


@pytest.mark.parametrize('flags', [['-D', '+n'], ['+D', '+n'], ['-Dn', '+n']])
def test_dump_action_is_sticky_across_noexec_toggles(carrier, flags):
    _, real, _, script, env = carrier
    assert fence.judge('/bin/bash', ['/bin/bash', *flags, str(script)], dict(env, HOME=str(real))) is None


@pytest.mark.parametrize('path', [None, '', '.', 'missing:.'])
@pytest.mark.parametrize('local_tool', [False, True])
def test_startup_discovery_preserves_empty_relative_and_absent_path(tmp_path, path, local_tool):
    root = Path(__file__).resolve().parents[1]
    home = tmp_path / 'home'
    scratch = tmp_path / 'scratch'
    home.mkdir()
    scratch.mkdir()
    if local_tool:
        tool = tmp_path / 'sudo'
        tool.write_text('#!/bin/sh\nexit 0\n')
        tool.chmod(0o755)
    env = dict(HOME=str(home), TMPDIR=str(scratch), PYTHONPATH=str(root))
    if path is not None:
        env['PATH'] = path
    source = '''import json,os
from types import SimpleNamespace
from tests import _live_fence as f
original = dict(os.environ)
expected = sorted(n for n in f._STUB_NAMES if f._resolve(n, original, os.getcwd()))
c = SimpleNamespace(option=SimpleNamespace(basetemp=None), addinivalue_line=lambda *args: None)
f.install(c)
print(json.dumps(dict(original=f._S.orig_path,stubbed=sorted(f._S.stubbed),expected=expected,source=f.__file__)))
'''
    child = subprocess.run([sys.executable, '-c', source], cwd=tmp_path, env=env,
                           capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr
    result = json.loads(child.stdout)
    assert Path(result['source']).resolve() == root / 'tests/_live_fence.py'
    assert result['original'] == (os.defpath if path is None else path)
    assert result['stubbed'] == result['expected']
