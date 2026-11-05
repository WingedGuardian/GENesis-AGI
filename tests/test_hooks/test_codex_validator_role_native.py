"""Real pinned clients, actual role source, and harmless owned execution sinks."""
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import private_module
from tests.test_hooks.native_codex import (
    configure,
    fixture_provider,
    run_native,
    trust_fixture_hook,
)
from tests.test_hooks.test_codex_validator_adapter_native import source_copy

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.environ.get('GENESIS_CODEX_NATIVE_TESTS') != '1',
                              reason='opt-in native role qualification')
RULES = ['git', 'gh', 'deploy', 'service', 'http', 'python', 'sqlite', 'file', 'symlink', 'heredoc', 'hash_escape', 'assignment_redirect']
MATRIX = [(rule, mode, 'role') for rule in RULES for mode in ('allow', 'deny', 'noop')]
MATRIX += [(rule, mode, 'adapter') for rule in ('gh', 'heredoc', 'hash_escape', 'assignment_redirect') for mode in ('allow', 'deny', 'noop')]


@pytest.mark.parametrize('client', ['cli', 'app'])
@pytest.mark.parametrize('rule,mode,boundary', MATRIX)
def test_native_role_controls_only_owned_sinks(tmp_path, monkeypatch, client, rule, mode, boundary):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts/hooks'))
    definition = private_module('role_native_definition', ROOT / 'scripts/hooks/codex_validator_adapter.py')
    cli = os.environ.get('GENESIS_CODEX_CLI_BIN') or shutil.which('codex')
    app = os.environ.get('GENESIS_CODEX_APP_SERVER_BIN') or cli
    binary = cli if client == 'cli' else app
    assert binary and subprocess.check_output([binary, '--version'], text=True).strip() == 'codex-cli 0.163.0'
    runtime = source_copy(tmp_path)
    source_hashes = {str(p.relative_to(runtime)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted(runtime.rglob('*')) if p.is_file()}
    scratch = tmp_path / 'tmp/validator'
    scratch.mkdir(parents=True)
    body_file = scratch / 'body.txt'
    body_file.write_text('owned data body\n')
    receipt = scratch / 'action-receipt'
    protected = runtime / 'data/fixture.db'
    store = tmp_path / 'owned-store'
    store.mkdir()
    protected.parent.symlink_to(store, target_is_directory=True)
    protected.write_text('retained fixture baseline')
    guarded_alias = runtime / 'scripts/hooks/fixture-policy.txt'
    guarded_file = store / 'guarded-file'
    guarded_file.write_text('retained fixture baseline')
    guarded_alias.symlink_to(guarded_file)
    ordinary = tmp_path / 'ordinary-file'
    ordinary.write_text('retained fixture baseline')
    ordinary_alias = scratch / 'ordinary-alias'
    ordinary_alias.symlink_to(ordinary)
    fakes = tmp_path / 'fakes'
    fakes.mkdir()
    for name in ('git', 'gh', 'deploy_code_only.sh', 'update.sh', 'watchgod', 'systemctl', 'curl', 'python3', 'sqlite3'):
        script = fakes / name
        if name in {'deploy_code_only.sh', 'update.sh', 'watchgod'}:
            script.write_text('#!/bin/sh\nprintf fixture > ' + shlex.quote(str(receipt)) + '\n')
        else:
            script.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nPath({str(receipt)!r}).write_text("fixture")\n')
        script.chmod(0o700)
    def fake(name):
        return shlex.quote(str(fakes / name))
    allow = {
        'git': fake('git') + ' status',
        'gh': fake('gh') + ' pr view 1',
        'deploy': 'bash --rcfile ' + fake('deploy_code_only.sh') + ' ' + fake('deploy_code_only.sh') + ' status',
        'service': fake('systemctl') + ' --user status genesis-fixture',
        'http': fake('curl') + ' -sSf http://localhost:5000/health',
        'python': fake('python3') + ' -m genesis db query "select 1"',
        'sqlite': fake('sqlite3') + ' ' + shlex.quote(f'file:{protected}?mode=ro') + ' "select 1"',
        'file': 'printf fixture > ' + shlex.quote(str(runtime / '../tmp/validator/action-receipt')),
        'symlink': 'printf fixture > ' + shlex.quote(str(ordinary_alias)),
        'heredoc': '/bin/cat ' + shlex.quote(str(body_file)) + '; ' + fake('git') + ' status',
        'hash_escape': "printf '%s' '# note'; " + fake('git') + ' status',
        'assignment_redirect': 'HOME=' + shlex.quote(str(runtime)) + " printf '' > ~/tmp/validator/action-receipt",
    }
    deny = {
        'git': fake('git') + ' push',
        'gh': fake('gh') + ' pr comment 1 --body fixture',
        'deploy': 'bash --rcfile ' + fake('watchgod') + ' ' + fake('watchgod') + ' kill 4101',
        'service': fake('systemctl') + ' --user restart genesis-fixture',
        'http': fake('curl') + ' -b -G -d fixture=one http://localhost:6333/fixture',
        'python': fake('python3') + ' -Imgenesis serve',
        'sqlite': 'export HOME=' + shlex.quote(str(runtime)) + '; ' + fake('sqlite3') + ' ~/data/fixture.db "select 1"',
        'file': 'printf -v HOME ' + shlex.quote(str(runtime)) + '; printf fixture > ~/data/fixture.db',
        'symlink': 'printf fixture > ' + shlex.quote(str(guarded_alias)),
        'heredoc': 'echo "$( (cat <<\'EOF\'\n)\nEOF\n)\n' + fake('git') + ' push\n)"',
        'hash_escape': 'echo before # ' + '\\' + '"x"\n' + fake('git') + ' push\necho after # "',
        'assignment_redirect': 'HOME=' + shlex.quote(str(runtime)) + ' > ~/data/fixture.db',
    }
    if rule == 'hash_escape' and boundary == 'adapter':
        # Other guards may refuse a push first. This harmless quoted mention
        # isolates whether the composed adapter actually includes the role rule.
        deny['hash_escape'] = "printf '%s' '#\\'; " + fake('git') + ' status'
    if rule in {'file', 'assignment_redirect'} and mode != 'allow':
        receipt = protected
    if rule == 'symlink':
        receipt = ordinary_alias if mode == 'allow' else guarded_alias
    command = 'cd ' + shlex.quote(str(scratch)) + ' || exit\n' + (allow if mode == 'allow' else deny)[rule]
    args = {'cmd': command, 'workdir': str(tmp_path)}
    if boundary == 'adapter':
        item = {'type': 'custom_tool_call', 'id': 'ct_fixture', 'call_id': 'call_fixture',
                'namespace': 'functions', 'name': 'exec',
                'input': 'text(await tools.exec_command(' + json.dumps(args) + '));'}
    else:
        item = {'type': 'function_call', 'id': 'fc_fixture', 'call_id': 'call_fixture',
                'name': 'exec_command', 'arguments': json.dumps(args)}
    evidence = {'client': client, 'rule': rule, 'mode': mode, 'boundary': boundary,
                'command': command, 'runtime': str(runtime), 'receipt': str(receipt),
                'protected': str(protected), 'guarded_alias': str(guarded_alias),
                'ordinary_alias': str(ordinary_alias), 'hashes': source_hashes}
    try:
        with fixture_provider(item) as (url, requests):
            env = configure(tmp_path, url, 'allow')
            env.update(HOME=str(tmp_path), GENESIS_HOME=str(tmp_path / '.genesis'),
                       GENESIS_CC_SESSION='1', _TEST_HOOK_ASK_POLICY='')
            config = tmp_path / 'codex/config.toml'
            config.write_text(config.read_text().replace('timeout = 5', 'timeout = 15'))
            if mode == 'noop':
                invocation = ['true']
            elif boundary == 'adapter':
                invocation = ['bash', '-c', definition.hook_command(runtime)]
            else:
                invocation = [sys.executable, str(runtime / 'scripts/hooks/validator_role_guard.py')]
            # Native Codex omits workdir; the role gets the independently validated
            # literal anchor, as it does from the real adapter in the bridge arm.
            hook = tmp_path / 'hook.py'
            hook.write_text('import json,pathlib,subprocess,sys\nraw=sys.stdin.buffer.read()\n'
                f'with pathlib.Path({str(tmp_path / "hooks.jsonl")!r}).open("a") as out: out.write(raw.decode()+"\\n")\n'
                'data=json.loads(raw)\n'
                + (f'data["cwd"]={str(scratch)!r}\n' if boundary == 'role' else '')
                + f'run=subprocess.run({invocation!r},input=json.dumps(data).encode())\n'
                'raise SystemExit(run.returncode)\n')
            evidence['trust'] = trust_fixture_hook(app, tmp_path, env)
            evidence['events'], evidence['stderr'] = run_native(binary, client, tmp_path, env, evidence)
            evidence['requests'] = requests
    finally:
        (tmp_path / 'native-evidence.json').write_text(json.dumps(evidence, indent=2))
    assert len(requests) == 2
    hooks = [json.loads(line) for line in (tmp_path / 'hooks.jsonl').read_text().splitlines()]
    assert len(hooks) == 1 and hooks[0]['tool_name'] == 'Bash'
    assert hooks[0]['tool_input']['command'] == command
    if rule == 'assignment_redirect':
        assert receipt.read_text() == ('retained fixture baseline' if mode == 'deny' else '')
        if mode != 'noop':
            assert protected.read_text() == 'retained fixture baseline'
    elif rule in {'file', 'symlink'} and mode != 'allow':
        assert receipt.read_text() == ('fixture' if mode == 'noop' else 'retained fixture baseline')
    else:
        assert receipt.exists() is (mode != 'deny')
        if receipt.exists():
            assert receipt.read_text() == 'fixture'
        assert protected.read_text() == 'retained fixture baseline'
    if mode == 'deny':
        markers = {'git': 'publishing code is prohibited', 'gh': 'privacy-scanned filer',
                   'deploy': 'cannot deploy', 'service': 'cannot change user services',
                   'http': 'cannot write live HTTP stores', 'python': 'live Genesis writer',
                   'sqlite': 'SQLite target is unresolved', 'file': 'protected or unresolved path',
                   'symlink': 'protected or unresolved path', 'heredoc': 'body files', 'hash_escape': 'body files',
                   'assignment_redirect': 'protected or unresolved path'}
        assert markers[rule] in json.dumps(requests[-1]), evidence
