"""Rollout config failures and owned source snapshots; no live unit actions."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def rollout():
    spec = importlib.util.spec_from_file_location('rollout_repair_fixture', ROOT/'scripts/lib/mcp_rollout.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('servers,invalid', [
    ([1], True), ('wrong-shape', True), (1, True), (True, True),
    (None, False), (False, False), (0, False), ('', False), ([], False), ({}, False),
])
def test_server_layer_errors_are_contextual_without_tightening_empty_overrides(tmp_path, servers, invalid):
    module = rollout()
    (tmp_path/'config').mkdir()
    (tmp_path/'config/mcp_shared.json').write_text(json.dumps({'servers': servers}))
    if invalid:
        with pytest.raises(module.RolloutError, match='servers.*object'):
            module.load_config(tmp_path, {'GENESIS_HOME': str(tmp_path/'home')})
    else:
        assert module.load_config(tmp_path, {'GENESIS_HOME': str(tmp_path/'home')})['servers'] == {}


def owned_checkout(tmp_path):
    root = tmp_path/'checkout with spaces'
    for name in ['scripts/lib/mcp_rollout.py', 'scripts/lib/shared_mcp_units.sh',
                 'src/genesis/mcp/shared_config.py']:
        source = ROOT/name
        # The extraction is deliberately postponed until it has a src consumer.
        if source.exists():
            target = root/name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    (root/'config').mkdir()
    (root/'config/mcp_shared.json').write_text(json.dumps({
        'servers': {'memory': {'unit': 'genesis-mcp-fixture.service'}}}))
    binary = tmp_path/'bin'
    binary.mkdir()
    calls = tmp_path/'unit-calls'
    shim = binary/'systemctl'
    shim.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$FIXTURE_CALLS"\nprintf "LoadState=not-found\\n"\n')
    shim.chmod(0o755)
    environment = {**os.environ, 'GENESIS_HOME': str(tmp_path/'home'),
                   'PATH': str(binary)+os.pathsep+os.environ['PATH'], 'FIXTURE_CALLS': str(calls)}
    return root, environment, calls


def captured_units(root, environment):
    return subprocess.run(['bash', '-c', '. "$1"; genesis_restart_shared_mcp_units "$2"',
        'fixture', str(root/'scripts/lib/shared_mcp_units.sh'), str(root)],
        env=environment, capture_output=True, text=True, timeout=10)


def test_a_future_import_in_the_complete_cli_survives_captured_execution(tmp_path):
    root, environment, calls = owned_checkout(tmp_path)
    cli = root/'scripts/lib/mcp_rollout.py'
    cli.write_text('from __future__ import annotations\n'+cli.read_text())
    result = captured_units(root, environment)
    assert result.returncode == 0, result.stdout+result.stderr
    assert calls.read_text().splitlines()[-1].endswith('genesis-mcp-fixture.service')


def test_future_cli_names_do_not_silently_diverge_from_normal_imports(tmp_path):
    root, environment, calls = owned_checkout(tmp_path)
    cli = root/'scripts/lib/mcp_rollout.py'
    text = cli.read_text()
    anchor = 'def main(argv=None):'
    assert text.count(anchor) == 1
    cli.write_text(text.replace(anchor, 'scope = None\n\n\n'+anchor))
    ordinary = subprocess.run([sys.executable, '-I', '-S', str(cli), 'units', '--root', str(root)],
        env=environment, capture_output=True, text=True, timeout=10)
    assert ordinary.returncode == 0, ordinary.stderr
    assert ordinary.stdout.splitlines() == ['genesis-mcp-fixture.service']
    result = captured_units(root, environment)
    assert result.returncode == 0, result.stdout+result.stderr
    queried = [line.split()[-1] for line in calls.read_text().splitlines()] if calls.exists() else []
    assert queried == ordinary.stdout.splitlines()


def test_config_import_does_not_depend_on_genesis_package_initialization(tmp_path):
    root, environment, _ = owned_checkout(tmp_path)
    package = root/'src/genesis'
    package.mkdir(parents=True, exist_ok=True)
    (package/'__init__.py').write_text("raise RuntimeError('synthetic package failure')\n")
    code = """
import runpy,sys
scope=runpy.run_path(sys.argv[1],run_name='fixture_import')
assert scope['load_config'](sys.argv[2],{'GENESIS_HOME':sys.argv[3]})['stage']=='off'
assert not any(name=='genesis' or name.startswith(('genesis.','fastmcp','mcp')) for name in sys.modules)
"""
    result = subprocess.run([sys.executable, '-I', '-S', '-c', code,
        str(root/'scripts/lib/mcp_rollout.py'), str(root), environment['GENESIS_HOME']],
        env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_utf8_configuration_preserves_literal_unicode(tmp_path):
    module = rollout()
    (tmp_path / 'config').mkdir()
    argv = ['fixture-é', '東京', 'two words']
    (tmp_path / 'config/mcp_shared.json').write_text(
        json.dumps({'adapter': {'argv': argv}}, ensure_ascii=False), encoding='utf-8')
    assert module.load_config(tmp_path, {'GENESIS_HOME': str(tmp_path / 'home')})['adapter']['argv'] == argv
