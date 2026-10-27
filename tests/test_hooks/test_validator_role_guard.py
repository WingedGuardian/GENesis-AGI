"""Pure role-policy checks use owned repositories; command bodies never execute."""
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import private_module

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def role(monkeypatch, tmp_path):
    home, main, scratch = [tmp_path / name for name in ('home', 'main', 'scratch')]
    for path in (home, main, scratch):
        path.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('CODEX_HOME', str(home / 'isolated-codex'))
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', '/dev/null')
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    subprocess.run(['git', 'init', '-q', str(main)], check=True)
    (main / 'README').write_text('fixture\n')
    subprocess.run(['git', '-C', str(main), 'add', 'README'], check=True)
    path = ROOT / 'scripts/hooks/validator_role_guard.py'
    if path.exists():
        monkeypatch.syspath_prepend(str(path.parent))
        module = private_module('validator_role', path)
        assert Path(module.__file__).resolve() == path.resolve()
    else:
        # Deliberately unfenced baseline for the new component, not an import error.
        module = SimpleNamespace(Refused=ValueError, check_command=lambda *_: None)
    monkeypatch.setattr(module, 'ROOT', main, raising=False)
    monkeypatch.setattr(module, '_genesis_process', lambda pid: pid == 4101, raising=False)
    monkeypatch.setattr(module, '_matching_pids', lambda args: [4101] if 'genesis-fixture' in args else [4102], raising=False)
    return module, {'main': main, 'scratch': scratch, 'home': home}


def check(role, template, anchor='main'):
    module, paths = role
    body = template.format_map({key: shlex.quote(str(value)) for key, value in paths.items()})
    directory = paths[anchor]
    command = 'cd ' + shlex.quote(str(directory)) + ' || exit\n' + body
    return module.check_command(command, directory)


DENIED = [
    'git push', 'git -C {scratch} push', 'env -C {scratch} git push',
    'bash -c "git push"', 'echo item | xargs git push',
    *['git ' + verb for verb in ('commit', 'merge', 'rebase', 'stash', 'checkout', 'switch', 'reset', 'pull')],
    'git -C {main} commit', 'env -C {main} git commit',
    'cd {scratch}; git commit', 'false && cd {scratch}; git commit',
    'true || cd {scratch}; git commit',
    *['gh pr ' + verb + ' 1' for verb in ('create', 'merge', 'ready', 'close', 'edit', 'review', 'comment')],
    *['gh issue ' + verb + ' 1' for verb in ('create', 'comment', 'close', 'edit')],
    'gh workflow run fixture.yml', 'gh label list',
    'gh api repos/example/project -XPOST', 'gh -X PATCH api repos/example/project',
    'gh api repos/example/project --method=PUT', 'gh api repos/example/project -X DELETE',
    'gh api repos/example/project -fkey=value', 'gh api repos/example/project -Fkey=true -XGET',
    'gh api repos/example/project --input=fixture.json',
    *['scripts/' + name for name in ('update.sh', 'bootstrap.sh', 'restore.sh', 'backup.sh', 'disk_hygiene.sh')],
    'scripts/deploy_code_only.sh', 'scripts/deploy_code_only.sh restart status',
    'bash -euo pipefail scripts/update.sh', 'source scripts/update.sh',
    'scripts/watchgod kill fixture', 'scripts/watchgod thaw fixture',
    'bash scripts/watchgod kill fixture',
    *['systemctl --user ' + verb + ' genesis-fixture' for verb in ('start', 'stop', 'restart', 'enable', 'daemon-reload')],
    'systemd-run --user true', 'kill -TERM 4101', 'pkill -f genesis-fixture',
    'curl -XPOST http://localhost:5000/fixture', 'curl --url=http://127.0.0.1:6333/fixture -dkey=value',
    'wget --post-data=key=value http://localhost:5000/fixture',
    *['python -m genesis ' + action for action in ('restore', 'trash', 'handoffs send', 'serve', 'vault', 'peers', 'contribute')],
    'python -Imgenesis restore', 'python -X dev -m genesis serve',
    'sqlite3 {main}/data/fixture.db "select 1"',
    'sqlite3 {home}/.genesis/nested/fixture.db "select 1"',
    'sqlite3 file:{main}/data/fixture.db?mode=ro "CREATE TABLE fixture(value)"',
    'sqlite3 file:{main}/data/fixture.db?mode=ro -cmd "DELETE FROM fixture" "select 1"',
    'echo value > {main}/README', '> {main}/data/fixture.db',
    'echo value >> {home}/.genesis/config/fixture.yaml',
    'echo value > {home}/.genesis/fixture.db-wal',
    'echo value > {home}/.claude/fixture', 'echo value > {home}/.codex/config.toml',
    'echo value > {home}/isolated-codex/config.toml',
    'echo value > {main}/scripts/hooks/fixture.py', 'echo value > {scratch}/secrets.env',
    'env -C {scratch} true > {main}/README',
    'env -C {scratch} true > README',
]


@pytest.mark.parametrize('command', DENIED)
def test_denied_role_operations(role, command):
    with pytest.raises(role[0].Refused):
        check(role, command)


@pytest.mark.parametrize('operator', ['>', '>>', '>|', '&>', '<>'])
@pytest.mark.parametrize('wrapper', ['{}', '({})', 'time {}', 'if {}; then :; fi'])
def test_assignment_only_home_precedes_output_expansion(role, operator, wrapper):
    with pytest.raises(role[0].Refused):
        check(role, wrapper.format('HOME={main} ' + operator + ' ~/README'))


@pytest.mark.parametrize('body', [
    'HOME={main} printf fixture > ~/fixture',
    'env -C {main} > README',
    'HOME={main} env > ~/fixture',
    'HOME={main} > {scratch}/fixture',
])
def test_output_context_preserves_command_and_wrapper_order(role, body):
    check(role, body, anchor='scratch')


@pytest.mark.parametrize(('command', 'anchor'), [
    ('git status', 'main'), ('git log -1', 'main'), ('git commit -m fixture', 'scratch'),
    ('git -C {scratch} commit -m fixture', 'main'),
    ('env -C {scratch} git commit -m fixture', 'main'),
    ('gh pr view 1', 'main'), ('gh issue list', 'main'), ('gh api repos/example/project -XGET', 'main'),
    ('gh api repos/example/project --jq ' + shlex.quote('"-X POST -fkey=value"'), 'main'),
    ('scripts/deploy_code_only.sh status', 'main'), ('bash scripts/deploy_code_only.sh status', 'main'),
    ('scripts/watchgod status', 'main'),
    *[('systemctl --user ' + verb + ' genesis-fixture', 'main')
      for verb in ('status', 'show', 'is-active', 'list-units', 'cat')],
    ('journalctl --user -u genesis-fixture -n 1', 'main'), ('kill -TERM 4102', 'main'),
    ('pkill -f fixture-worker', 'main'),
    ('curl -sSf http://localhost:5000/health', 'main'),
    ('curl -I http://127.0.0.1:6333/fixture', 'main'),
    ('wget -qO- http://localhost:5000/health', 'main'),
    ('curl -XPOST -dkey=value https://example.test/fixture', 'main'),
    ('python -m genesis health', 'main'),
    ('python -c "print(\"-m genesis restore\")"', 'main'),
    ('python fixture.py -m genesis restore', 'main'),
    ('sqlite3 file:{main}/data/fixture.db?mode=ro "select 1"', 'main'),
    ('echo value > {scratch}/fixture', 'main'), ('true > README', 'scratch'),
    ('env -C {main} true > fixture', 'scratch'),
    ('echo value > {home}/.genesis/output/prv-fixture/fixture.db', 'main'),
    ('echo "git push > secrets.env"', 'main'),
    ('git clone --shared {main} {scratch}/clone', 'main'),
])
def test_read_and_owned_scratch_controls(role, command, anchor):
    check(role, command, anchor)


@pytest.mark.parametrize(('command', 'anchor'), [
    ('env -C {main} git commit', 'scratch'),
    ('env -C {scratch} git -C {main} commit', 'main'),
    ('git -C {scratch} -C ../main commit', 'scratch'),
    ('git --git-dir={main}/.git commit', 'scratch'),
    ('GIT_DIR={main}/.git bash -c "git commit"', 'scratch'),
    ('env GIT_DIR={main}/.git bash -c "git commit"', 'scratch'),
    ('export GIT_DIR={main}/.git; git commit', 'scratch'),
    ('case fixture in fixture) cd {main};; esac; git commit', 'scratch'),
    ('case fixture in fixture) export GIT_DIR={main}/.git;; esac; git commit', 'scratch'),
    ('bash -c "cd {scratch}; git commit"', 'main'),
    ('env -C {main} bash -c "true > README"', 'scratch'),
    ('git status; > {main}/README; git status', 'scratch'),
    ('HOME={main} bash -c "true > ~/README"', 'scratch'),
    ('export HOME={main}; true > ~/README', 'scratch'),
    ('curl -XGET -d fixture=one http://localhost:5000/fixture', 'main'),
    ('curl -d -G http://localhost:5000/fixture', 'main'),
    ('curl -H "X-Fixture: -G" -d fixture=one http://localhost:5000/fixture', 'main'),
    ('curl -G -XPOST -d fixture=one http://localhost:5000/fixture', 'main'),
    ('curl --json "{{}}" http://localhost:6333/fixture', 'main'),
    ('wget --method=GET --body-data=fixture=one http://localhost:5000/fixture', 'main'),
    ('sqlite3 file:{main}/data/fixture.db?mode=ro\\&mode=rw "select 1"', 'main'),
    ('echo "CREATE TABLE fixture(value)" | sqlite3 file:{main}/data/fixture.db?mode=ro', 'main'),
    ('kill -- 0', 'main'), ('kill -- -4102', 'main'),
])
def test_context_and_option_boundary_denials(role, command, anchor):
    with pytest.raises(role[0].Refused):
        check(role, command, anchor)


@pytest.mark.parametrize(('command', 'anchor'), [
    ('bash -c "git commit -m fixture"', 'scratch'),
    ('bash -c "cd {main}"; git commit -m fixture', 'scratch'),
    ('echo "$(cd {main})"; git commit -m fixture', 'scratch'),
    ('GIT_DIR={main}/.git echo "$(git commit -m fixture)"', 'scratch'),
    ('env GIT_DIR={main}/.git true; git commit -m fixture', 'scratch'),
    ('cd {main}; git -C {scratch} commit -m fixture', 'scratch'),
    ('env -C {scratch} bash -c "git commit -m fixture"', 'main'),
    ('env -C {scratch} bash -c "true > fixture"', 'main'),
    ('env -C {main} true > fixture', 'scratch'),
    ('case fixture in fixture) cd {main};; esac; git status', 'scratch'),
    ('curl -G -d fixture=one http://localhost:5000/fixture', 'main'),
    ('curl -d fixture=one -G http://localhost:5000/fixture', 'main'),
    ('curl -XPOST -XGET http://localhost:5000/fixture', 'main'),
    ('curl -H "X-Fixture: -XPOST" http://localhost:5000/fixture', 'main'),
    ('sqlite3 -readonly file:{main}/data/fixture.db?mode=ro -cmd "select 1" "select 2"', 'main'),
    ('python -W -m fixture.py -m genesis restore', 'main'),
    ('python -c "print(1)" -m genesis restore', 'main'),
    ('bash -n scripts/update.sh', 'main'),
    ('echo scripts/update.sh', 'main'),
    ('systemctl --user --property restart show genesis-fixture', 'main'),
])
def test_context_and_option_boundary_allow_controls(role, command, anchor):
    check(role, command, anchor)


@pytest.fixture
def process_module(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts/hooks'))
    module = private_module('validator_role_process', ROOT / 'scripts/hooks/validator_role_guard.py')
    monkeypatch.setattr(module, 'PROC', tmp_path)
    return module


@pytest.mark.parametrize(('argv', 'group', 'expected'), [
    (['/usr/bin/python3', '-m', 'genesis', 'serve'], '0::/owned\n', True),
    (['/usr/bin/python3', '-Imgenesis', 'health'], '0::/owned\n', True),
    (['/usr/bin/sleep', '1'], '0::/user.slice/genesis-server.service\n', True),
    (['/usr/bin/echo', 'genesis-server', '-m', 'genesis'], '0::/owned\n', False),
    (['/usr/bin/python3', 'fixture.py', '-m', 'genesis'], '0::/owned\n', False),
    (['/usr/bin/sleep', '1'], '0::/owned\n', False),
])
def test_process_identity_uses_real_operand_positions(process_module, tmp_path, argv, group, expected):
    process = tmp_path / '4101'
    process.mkdir()
    (process / 'cgroup').write_text(group)
    (process / 'cmdline').write_bytes(b'\0'.join(s.encode() for s in argv) + b'\0')
    assert process_module._genesis_process(4101) is expected


@pytest.mark.parametrize('output', [b'2\n', b'not-a-pid fixture\n', b''])
def test_process_selector_output_cannot_treat_count_or_error_as_pid(process_module, monkeypatch, output):
    result = subprocess.CompletedProcess([], 2 if not output else 0, output, b'')
    monkeypatch.setattr(process_module.subprocess, 'run', lambda *a, **kw: result)
    with pytest.raises(process_module.Refused):
        process_module._matching_pids(['fixture'])


def test_process_selector_uses_actual_procps_for_owned_child(process_module):
    import os

    child = subprocess.Popen(['/bin/sleep', '60'])
    try:
        assert child.pid in process_module._matching_pids(['-P', str(os.getpid()), '-x', 'sleep'])
    finally:
        child.terminate()
        child.wait()


@pytest.mark.parametrize(('body', 'allowed', 'reason'), [
    ('printf fixture', True, ''),
    ('bash -c "printf fixture"', True, ''),
    ('printf fixture > {scratch}/receipt', True, ''),
    ('git push', False, 'publishing code is prohibited'),
    ('gh pr comment 1 --body fixture', False, 'privacy-scanned filer'),
    ('echo fixture > {runtime}/data/fixture.db', False, 'protected or unresolved path'),
    ('sqlite3 {runtime}/data/fixture.db "select 1"', False, 'mode=ro'),
    ('sqlite3 file:{runtime}/data/fixture.db?mode=ro "select 1"', True, ''),
])
def test_real_role_cli_uses_copied_source_and_never_executes_payload(tmp_path, body, allowed, reason):
    import json
    import os
    import sys

    from tests.test_hooks.test_codex_validator_adapter_native import source_copy

    runtime = source_copy(tmp_path)
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    command = 'cd ' + shlex.quote(str(scratch)) + ' || exit\n' + body.format(runtime=runtime, scratch=scratch)
    payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash',
               'tool_input': {'command': command}, 'cwd': str(scratch)}
    env = dict(os.environ, HOME=str(tmp_path), GENESIS_HOME=str(tmp_path / '.genesis'),
               PYTHONDONTWRITEBYTECODE='1', GENESIS_CC_SESSION='')
    run = subprocess.run([sys.executable, str(runtime / 'scripts/hooks/validator_role_guard.py')],
                         input=json.dumps(payload), capture_output=True, text=True, env=env)
    assert run.returncode == (0 if allowed else 2), run.stderr
    assert not run.stdout and (not run.stderr if allowed else reason in run.stderr)
    assert not (scratch / 'receipt').exists()
    assert not (runtime / 'data').exists()


@pytest.mark.parametrize('producer', [
    'HOME={main}', 'export HOME={main}', 'printf -v HOME {main}', 'printf -vHOME {main}',
    'read HOME', 'mapfile HOME', 'readarray HOME', 'getopts x HOME -x', 'eval "HOME={main}"',
    'declare -n alias=HOME; printf -v alias {main}',
])
@pytest.mark.parametrize('consumer', ['true > ~/README', 'git -C ~ commit',
                                     'sqlite3 ~/data/fixture.db "select 1"'])
def test_all_persistent_home_producers_invalidate_every_home_sensitive_consumer(role, producer, consumer):
    with pytest.raises(role[0].Refused):
        check(role, producer + '; ' + consumer, 'scratch')


@pytest.mark.parametrize('flag', ['--rcfile', '--init-file'])
def test_shell_script_tail_retains_duplicate_option_value_spelling(role, flag):
    with pytest.raises(role[0].Refused):
        check(role, 'bash ' + flag + ' scripts/watchgod scripts/watchgod kill 4101')


@pytest.mark.parametrize('flag', ['--rcfile', '--init-file'])
def test_shell_script_status_uses_the_bound_duplicate_operand(role, flag):
    check(role, 'bash ' + flag + ' scripts/deploy_code_only.sh scripts/deploy_code_only.sh status')


@pytest.mark.parametrize('port', [5000, 6333])
def test_cookie_argument_that_looks_like_get_cannot_hide_a_body(role, port):
    with pytest.raises(role[0].Refused):
        check(role, 'curl -b -G -d fixture=one http://localhost:' + str(port) + '/fixture')


def test_wget_directory_option_value_cannot_supply_a_method(role):
    check(role, 'wget -P --method=POST http://localhost:5000/fixture')


@pytest.mark.parametrize('arguments', ['-T5', '-T 5 --method=GET'])
def test_wget_timeout_is_not_curl_upload(role, arguments):
    check(role, 'wget ' + arguments + ' http://localhost:5000/fixture')


@pytest.mark.parametrize('root_name', ['data', 'state'])
@pytest.mark.parametrize('spelling', ['alias', 'canonical'])
@pytest.mark.parametrize('readonly', [False, True])
def test_static_store_root_symlinks_retain_the_sqlite_boundary(role, tmp_path, root_name, spelling, readonly):
    module, paths = role
    store = tmp_path / 'owned-store'
    store.mkdir()
    alias = paths['main'] / 'data' if root_name == 'data' else paths['home'] / '.genesis'
    alias.symlink_to(store, target_is_directory=True)
    target = (alias if spelling == 'alias' else store) / 'fixture.db'
    database = 'file:' + str(target) + '?mode=ro' if readonly else str(target)
    command = 'sqlite3 ' + shlex.quote(database) + ' "select 1"'
    if readonly:
        check(role, command)
    else:
        with pytest.raises(module.Refused):
            check(role, command)


@pytest.mark.parametrize(('relative', 'denied'), [
    ('fixture.db-wal', True), ('config/fixture.yaml', True),
    ('output/prv-fixture/fixture.db', False),
])
def test_symlinked_state_preserves_sidecar_protection_and_nested_scratch(role, tmp_path, relative, denied):
    module, paths = role
    store = tmp_path / 'owned-state'
    store.mkdir()
    (paths['home'] / '.genesis').symlink_to(store, target_is_directory=True)
    command = 'true > ' + shlex.quote(str(paths['home'] / '.genesis' / relative))
    if denied:
        with pytest.raises(module.Refused):
            check(role, command)
    else:
        check(role, command)


@pytest.mark.parametrize('kind', ['data', 'sidecar', 'codex', 'secret', 'tracked'])
def test_protected_file_symlink_cannot_lose_its_logical_identity(role, tmp_path, kind):
    module, paths = role
    external = tmp_path / 'ordinary.txt'
    external.write_text('retained owned fixture')
    targets = {'data': paths['main'] / 'data/fixture.db',
               'sidecar': paths['home'] / '.genesis/fixture.db-wal',
               'codex': paths['home'] / '.codex/config.toml',
               'secret': paths['scratch'] / 'secrets.env',
               'tracked': paths['main'] / 'README'}
    target = targets[kind]
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()  # This fixture's own README, not a user file.
    target.symlink_to(external)
    command = 'true > ' + shlex.quote(str(target))
    with pytest.raises(module.Refused):
        check(role, command)
    if kind == 'data':
        with pytest.raises(module.Refused):
            check(role, 'sqlite3 ' + shlex.quote(str(target)) + ' "select 1"')
    assert external.read_text() == 'retained owned fixture'


def test_ordinary_scratch_symlink_and_protected_readonly_database_remain_allowed(role, tmp_path):
    _, paths = role
    external = tmp_path / 'ordinary.txt'
    external.write_text('retained owned fixture')
    alias = paths['scratch'] / 'alias'
    alias.symlink_to(external)
    check(role, 'true > ' + shlex.quote(str(alias)))
    target = paths['main'] / 'data/fixture.db'
    target.parent.mkdir()
    target.symlink_to(external)
    check(role, 'sqlite3 ' + shlex.quote('file:' + str(target) + '?mode=ro') + ' "select 1"')
    assert external.read_text() == 'retained owned fixture'


def test_sqlite_file_alias_keeps_the_protected_database_identity(role, tmp_path):
    module, paths = role
    external = tmp_path / 'ordinary.txt'
    external.write_text('retained owned fixture')
    target = paths['main'] / 'data/fixture.db'
    target.parent.mkdir()
    target.symlink_to(external)
    with pytest.raises(module.Refused):
        check(role, 'sqlite3 ' + shlex.quote(str(target)) + ' "select 1"')


@pytest.mark.parametrize('spelling', ['../scratch/fixture', '{home}/.codex/../tmp/validator/fixture'])
def test_parent_components_preserve_ordinary_scratch_outputs(role, spelling):
    _, paths = role
    (paths['home'] / '.codex').mkdir()
    (paths['home'] / 'tmp/validator').mkdir(parents=True)
    check(role, 'true > ' + spelling)


@pytest.mark.parametrize('root_name', ['data', 'state'])
def test_sqlite_parent_components_can_leave_an_ordinary_protected_directory(role, root_name):
    _, paths = role
    root = paths['main'] / 'data' if root_name == 'data' else paths['home'] / '.genesis'
    root.mkdir()
    check(role, 'sqlite3 ' + shlex.quote(str(root / '../ordinary.db')) + ' "select 1"')


@pytest.mark.parametrize('client', ['output', 'sqlite'])
def test_symlink_parent_components_keep_the_original_canonical_destination(role, client):
    module, paths = role
    nested = paths['main'] / 'data/nested'
    nested.mkdir(parents=True)
    alias = paths['scratch'] / 'alias'
    alias.symlink_to(nested, target_is_directory=True)
    target = shlex.quote(str(alias / '../fixture.db'))
    command = 'true > ' + target if client == 'output' else 'sqlite3 ' + target + ' "select 1"'
    with pytest.raises(module.Refused):
        check(role, command)


def _bare_role_dependencies():
    import ast

    hooks = ROOT / 'scripts/hooks'
    siblings = {p.stem for p in hooks.glob('*.py')}
    names = set()
    for node in ast.parse((hooks / 'validator_role_guard.py').read_text()).body:
        if isinstance(node, ast.ImportFrom) and node.module in siblings:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names if alias.name in siblings)
    assert names
    return sorted(names)


@pytest.mark.parametrize('dependency,fault', [(None, None)] + [
    (name, fault) for name in _bare_role_dependencies() for fault in ('missing', 'crash')
])
def test_actual_adapter_denies_every_role_import_failure(tmp_path, dependency, fault):
    import json
    import os

    from tests.test_hooks.test_codex_validator_adapter_native import source_copy

    runtime = source_copy(tmp_path)
    if dependency:
        target = runtime / 'scripts/hooks' / (dependency + '.py')
        if fault == 'missing':
            target.unlink()
        else:
            target.write_text("raise RuntimeError('fixture-import-failure')\n")
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    command = 'cd ' + shlex.quote(str(scratch)) + ' || exit\nprintf fixture'
    data = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash',
            'tool_input': {'command': command}, 'cwd': str(scratch)}
    signal = tmp_path / 'fd3'
    result = subprocess.run(['bash', '-c', 'bash "$1" 3>"$2"', 'fixture',
                             str(runtime / 'scripts/hooks/codex-validator-adapter'), str(signal)],
                            input=json.dumps(data), capture_output=True, text=True, timeout=15,
                            env={'PATH': os.defpath, 'HOME': str(tmp_path),
                                 'GENESIS_HOME': str(tmp_path / '.genesis')})
    assert result.returncode == (2 if dependency else 0), result.stderr
    assert signal.read_text() == ('deny\n' if dependency else 'allow\n')
    assert not result.stdout


_LITERAL_HEREDOC_BODIES = [
    "echo \"$( (cat <<" + delimiter + "\n)\nEOF\n)\ngit push scratch\n)\""
    for delimiter in ('EOF', "'EOF'")
]
_LITERAL_HEREDOC_BODIES += ['bash -c ' + shlex.quote(body) for body in _LITERAL_HEREDOC_BODIES[:]]
_LITERAL_HEREDOC_BODIES += ['cat <<EOF\nfixture\nEOF', "printf '%s' '<<'", 'echo $((1<<2))']


@pytest.mark.parametrize('body', _LITERAL_HEREDOC_BODIES)
def test_literal_heredoc_policy_requires_body_files(role, body):
    # Owner-approved literal policy also rejects quoted mentions/arithmetic. No
    # submitted body runs, including when verifying the previously unfenced path.
    with pytest.raises(role[0].Refused, match='body files'):
        check(role, body)


@pytest.mark.parametrize('body', ['cat {scratch}/body.txt', "printf '%s' body-file"])
def test_literal_heredoc_body_file_controls_remain_allowed(role, body):
    check(role, body)


@pytest.mark.parametrize('body', [
    r'echo \#', r'echo a\#b', r"echo It\'s # note", r"printf '%s' '#\'",
    'bash -c ' + shlex.quote(r'echo \#'),
    'echo before # ' + '\\' + '"x"\ngit push\necho after # "',
])
def test_mixed_hash_escape_refuses_before_parser(role, monkeypatch, body):
    parsed = []

    def analyze(command):
        parsed.append(command)
        return [], None

    monkeypatch.setattr(role[0], 'analyze_policy', analyze)
    with pytest.raises(role[0].Refused, match='body files'):
        check(role, body)
    assert parsed == []


@pytest.mark.parametrize('body', ["echo '# note'", r"echo It\'s"])
def test_single_hash_or_escape_controls_remain_allowed(role, body):
    check(role, body)
