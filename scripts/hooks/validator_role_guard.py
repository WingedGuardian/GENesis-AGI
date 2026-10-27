#!/usr/bin/env python3
"""Validator-only named-operation policy; arbitrary programs remain outside it."""
from __future__ import annotations

import argparse
import getopt
import json
import optparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import shell_parse
from codex_validator_adapter import ANCHOR, initial_directory
from codex_validator_guard import RUNTIME_ROOT, Refused, normalize
from deploy_timeout_advisory import _shell_operand
from git_push_guard import _is_sqlite_write
from git_repo_selection import REPO_VARS, seg_redirects_repo, seg_sets_variables, variable_patterns
from main_checkout_guard import _Git
from shell_parse import (
    _NESTED,
    _argv,
    _nested_script,
    analyze_policy,
    gh_api_request,
    gh_command,
    git_subcommand_index,
)

ROOT = RUNTIME_ROOT
PROC = Path('/proc')
GIT_MOVES = frozenset(['commit', 'merge', 'rebase', 'stash', 'checkout', 'switch', 'reset', 'pull'])
DEPLOYS = frozenset(['update.sh', 'bootstrap.sh', 'restore.sh', 'backup.sh', 'disk_hygiene.sh'])
READ_UNITS = frozenset(['status', 'show', 'is-active', 'list-units', 'cat'])
SQL_WRITES = re.compile(r'\b(?:CREATE|VACUUM|ATTACH|DETACH|REINDEX|ANALYZE)\b', re.I)
HOME_PATTERNS = variable_patterns('HOME|CODEX_HOME')
REPO_PATTERNS = variable_patterns('|'.join(REPO_VARS) + '|GIT_CONFIG(?:_[A-Z0-9_]+)?')


def _path(value, directory, *, home_unknown=False, resolve=True):
    if not value or re.search(r'[$`*?\[\]{}\x00]', value):
        return None
    if value.startswith('~'):
        if home_unknown or not (value == '~' or value.startswith('~/')):
            return None
        value = str(Path.home()) + value[1:]
    path = Path(value)
    if not path.is_absolute():
        if directory is None:
            return None
        path = directory / path
    return path.resolve() if resolve else path


@dataclass
class Context:
    directory: Path | None
    namespace: bool = True
    repository_unknown: bool = False
    home_unknown: bool = False

    def applied(self, facts):
        current = replace(self)
        changes = 0
        for kind, value in facts:
            if kind == 'assignment':
                name = value.split('=', 1)[0].rstrip('+')
                current.repository_unknown |= name in REPO_VARS or name.startswith('GIT_CONFIG')
                current.home_unknown |= name in {'HOME', 'CODEX_HOME'}
            elif kind == 'chdir':
                changes += 1
                current.directory = _path(value, current.directory, home_unknown=current.home_unknown)
            elif kind != 'assignment_only':
                current.directory = None
                current.namespace &= kind != 'opaque'
        if changes > 1:
            current.directory = None  # No claim about repeated wrapper option precedence.
        return current


def _protected(target, git):
    canonical = target.resolve()
    target = Path(os.path.abspath(target))
    home = Path.home()
    state = home / '.genesis'
    roots = [ROOT / 'data', state / 'config', home / '.claude', home / '.codex',
             Path(os.environ.get('CODEX_HOME', str(home / '.codex'))),
             ROOT / 'scripts/hooks', ROOT / '.claude/hooks',
             RUNTIME_ROOT / 'scripts/hooks', RUNTIME_ROOT / '.claude/hooks']
    if (any(p.name == 'secrets.env' for p in (target, canonical))
            or any(target.is_relative_to(Path(os.path.abspath(p))) or canonical.is_relative_to(p.resolve()) for p in roots)
            or any(p.parent in {Path(os.path.abspath(state)), state.resolve()} and '.db' in p.name for p in (target, canonical))):
        return True
    return any(p.is_relative_to(ROOT) and git.tracked(str(ROOT), [str(p.relative_to(ROOT))])
               for p in (target, canonical))


def _outputs(segment, context, git):
    for operator, raw in segment.targets:
        if '>' not in operator:
            continue
        words = _argv(raw or '')
        if len(words) != 1:
            raise Refused('Unresolved validator output target')
        value = words[0]
        if operator == '>&' and (value.isdecimal() or value == '-'):
            continue
        # A quoted tilde is literal; leave it relative instead of expanding HOME.
        if raw.startswith(('\"', "'")) and value.startswith('~'):
            value = './' + value
        target = _path(value, context.directory, home_unknown=context.home_unknown, resolve=False)
        if not context.namespace or target is None or _protected(target, git):
            raise Refused('Validator output targets a protected or unresolved path')


def _git(segment, context, git):
    options = []
    index = git_subcommand_index(segment.argv, _options=options)
    verb = segment.argv[index] if index is not None else None
    if verb == 'push':
        raise Refused('Validators report findings; publishing code is prohibited')
    if verb not in GIT_MOVES:
        return
    directory = context.directory
    uncertain = context.repository_unknown or seg_redirects_repo(segment) or not context.namespace
    for name, value in options:
        if name == '-C':
            directory = _path(value, directory, home_unknown=context.home_unknown)
        elif value is not None or name == '--bare':
            uncertain = True
    if directory is None or uncertain:
        raise Refused('Validator Git mutation has no proven operation repository')
    checkout = git.checkout(str(directory))
    if directory.is_relative_to(ROOT) or (checkout is not None and Path(checkout[0]) == ROOT):
        raise Refused('Validators cannot move the main checkout')


def _github(argv):
    invocation = gh_command(argv)
    forbidden = {'pr': {'create', 'merge', 'ready', 'close', 'edit', 'review', 'comment'},
                 'issue': {'create', 'comment', 'close', 'edit'}, 'workflow': {'run'}}
    if invocation is None:
        raise Refused('Unresolved GitHub operation')
    request = gh_api_request(argv)
    if (invocation.group == 'label'
            or invocation.subcommand in forbidden.get(invocation.group, set())
            or (request is not None and (request.method not in {'GET', 'HEAD'}
                                        or request.fields or request.has_input))):
        raise Refused('Validators publish only through the privacy-scanned filer')


class _PythonOptions(optparse.OptionParser):
    def error(self, msg):
        raise Refused('Unclassified Python interpreter options')


def _python_entry(argv):
    parser = _PythonOptions(add_help_option=False)
    parser.disable_interspersed_args()
    parser.set_defaults(selected=None)

    def selected(option, spelling, value, current):
        current.values.selected = (spelling, value, tuple(current.rargs))
        current.rargs[:] = []

    for flag in ('-m', '-c'):
        parser.add_option(flag, action='callback', type='string', callback=selected)
    for letter in 'bBdEhiIOPqRsSuvVx':
        parser.add_option('-' + letter, action='store_true')
    for flag in ('-W', '-X', '--check-hash-based-pycs'):
        parser.add_option(flag, action='append', type='string')
    for flag in ('--help', '--version'):
        parser.add_option(flag, action='store_true')
    options, operands = parser.parse_args(argv[1:])
    return options.selected or ('script', operands[0], tuple(operands[1:])) if operands or options.selected else None


def _scripts(segment):
    argv, name = segment.argv, segment.exe
    operand, rest = argv[0], argv[1:]
    if name in _NESTED:
        if _nested_script(argv, name):
            return  # Canonical child segments contain the actual commands.
        rest = []
        operand, _, noexec = _shell_operand(argv, parser=shell_parse, _tail=rest)
        if operand is None or noexec:
            return
    elif re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', name):
        entry = _python_entry(argv)
        if entry is None or entry[0] == '-c':
            return
        kind, operand, rest = entry
        if kind == '-m':
            if operand == 'genesis' and rest and (rest[0] in {
                'restore', 'trash', 'serve', 'vault', 'peers', 'contribute',
            } or tuple(rest[:2]) == ('handoffs', 'send')):
                raise Refused('Validator cannot invoke a live Genesis writer')
            return
    elif name in {'.', 'source'}:
        operands = argv[2:] if argv[1:2] == ['--'] else argv[1:]
        if not operands:
            return
        operand, rest = operands[0], operands[1:]
    script = Path(operand).name
    if (script in DEPLOYS or (script == 'deploy_code_only.sh' and list(rest) != ['status'])
            or (script == 'watchgod' and rest and rest[0] in {'kill', 'thaw'})):
        raise Refused('Validators cannot deploy, restore, or change live services')


def _genesis_process(pid):
    proc = PROC / str(pid)
    group = (proc / 'cgroup').read_text()
    if re.search(r'(?:^|/)genesis-[^/\n]*\.(?:service|scope)(?:/|$)', group, re.M):
        return True
    argv = (proc / 'cmdline').read_bytes().rstrip(b'\0').decode().split('\0')
    if Path(argv[0]).name in {'genesis', 'genesis-server', 'genesis-bridge'}:
        return True
    if re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', Path(argv[0]).name):
        entry = _python_entry(argv)
        return entry is not None and entry[0] == '-m' and entry[1].split('.')[0] == 'genesis'
    return False


def _matching_pids(args):
    result = subprocess.run(['/usr/bin/pgrep', '-l', *args], capture_output=True, check=False)
    if result.returncode not in {0, 1}:
        raise Refused('Unresolved process selector')
    pids = []
    for line in result.stdout.decode().splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) != 2 or not fields[0].isdecimal():
            raise Refused('Unresolved process selector output')
        pids.append(int(fields[0]))
    return pids


def _processes(segment):
    args = segment.argv[1:]
    if segment.exe == 'pkill':
        pids = _matching_pids(args)
    else:
        if args and args[0] in {'-l', '-L', '--list'}:
            return
        if args and re.fullmatch(r'-(?:\d+|[A-Z][A-Z0-9]*)', args[0]):
            args = args[1:]
        try:
            _, operands = getopt.gnu_getopt(args, 's:n:', ['signal='])
        except getopt.GetoptError as exc:
            raise Refused('Unclassified process operation') from exc
        if not operands or any(not p.isdecimal() or int(p) <= 0 for p in operands):
            raise Refused('Validator process target is not a literal positive PID')
        pids = [int(p) for p in operands]
    if any(_genesis_process(pid) for pid in pids):
        raise Refused('Validators cannot signal Genesis processes')


def _network(segment):
    curl = segment.exe == 'curl'
    short = 'X:d:H:o:A:u:e:x:F:T:m:sb:IhLvGkqfS' if curl else 'O:qo:U:T:t:SP:'
    long = ('request= data= data-raw= data-binary= data-urlencode= json= form= upload-file= '
            'header= output= user-agent= user= referer= proxy= max-time= connect-timeout= '
            'url= cookie= get head silent show-error fail fail-with-body insecure location compressed '
            'retry= write-out=' if curl else 'method= post-data= post-file= body-data= body-file= '
            'header= output-document= quiet timeout= tries= user-agent= spider no-check-certificate')
    try:
        options, urls = getopt.gnu_getopt(segment.argv[1:], short, long.split())
    except getopt.GetoptError as exc:
        raise Refused('Unclassified HTTP client options') from exc
    methods = [v.upper() for k, v in options if k in {'-X', '--request', '--method'}]
    data = any(k in {'-d', '--data', '--data-raw', '--data-binary', '--data-urlencode',
                    '--post-data', '--post-file', '--body-data', '--body-file'} for k, _ in options)
    query = curl and any(k in {'-G', '--get'} for k, _ in options)
    body = (data and not query) or (curl and any(k in {'--json', '-F', '--form', '-T', '--upload-file'} for k, _ in options))
    method = methods[-1] if methods else 'POST' if body else 'GET'
    if method in {'GET', 'HEAD'} and not body:
        return
    for url in [*urls, *(v for k, v in options if k == '--url')]:
        if '$' in url or '`' in url:
            raise Refused('Unresolved mutating HTTP destination')
        parsed = urlsplit(url if '://' in url else 'http://' + url)
        if parsed.hostname in {'localhost', '127.0.0.1', '::1'} and parsed.port in {5000, 6333}:
            raise Refused('Validators cannot write live HTTP stores')


class _SqlOptions(argparse.ArgumentParser):
    def error(self, message):
        raise Refused('Unclassified SQLite client options')


def _sqlite(segment, context, command):
    if _is_sqlite_write(command) or SQL_WRITES.search(command):
        raise Refused('Validator SQLite input contains a write keyword')
    parser = _SqlOptions(add_help=False, allow_abbrev=False)
    for flag in ('-cmd', '-init', '-separator', '-newline', '-nullvalue', '-vfs'):
        parser.add_argument(flag, action='append')
    for flag in ('-readonly', '-batch', '-bail', '-csv', '-json', '-line', '-list', '-header', '-noheader', '-column', '-safe', '-uri'):
        parser.add_argument(flag, action='store_true')
    parser.add_argument('operands', nargs='*')
    args = parser.parse_intermixed_args(segment.argv[1:])
    if not args.operands:
        return
    spelling = args.operands[0]
    uri = urlsplit(spelling) if spelling.startswith('file:') else None
    target = _path(unquote(uri.path) if uri else spelling, context.directory,
                   home_unknown=context.home_unknown, resolve=False)
    if target is None or not context.namespace:
        raise Refused('Validator SQLite target is unresolved')
    canonical = target.resolve()
    target = Path(os.path.abspath(target))
    protected = any(target.is_relative_to(Path(os.path.abspath(root))) or canonical.is_relative_to(root.resolve())
                    for root in (ROOT / 'data', Path.home() / '.genesis'))
    if protected and (uri is None or parse_qs(uri.query).get('mode') != ['ro']):
        raise Refused('Validator protected SQLite access requires file URI mode=ro')


def check_command(command, cwd):
    if initial_directory(command) != Path(cwd).resolve():
        raise Refused('Validator role directory does not match its literal anchor')
    if '<<' in command or ('#' in command and '\\' in command):
        raise Refused('Validator shell syntax requires body files; stop and ask the owner')
    segments, blind = analyze_policy(command)
    if blind is not None:
        raise Refused('Validator command cannot be classified safely')
    scopes = {(): Context(Path(cwd).resolve(), repository_unknown=any(os.environ.get(k) for k in REPO_VARS))}
    parents, kinds = {}, {(): 'root'}
    git = _Git()
    for segment in segments:
        if segment.position[0] < ANCHOR.match(command).end():
            continue
        scope = segment.position[:-1]
        if scope not in scopes:
            parent, before = parents[segment.position[:-2]]
            scopes[scope] = before.applied(segment.ancestors[len(parent.ancestors):])
            kinds[scope] = segment.scope_kind
        context = scopes[scope]
        parents[segment.position] = (segment, replace(context))
        _outputs(segment, context.applied(segment.contexts) if ('assignment_only', None) in segment.contexts else context, git)
        program = context.applied(segment.contexts)
        name, argv = segment.exe, segment.argv
        if name == 'git':
            _git(segment, program, git)
        elif name == 'gh':
            _github(argv)
        elif name in {'kill', 'pkill'}:
            _processes(segment)
        elif name in {'curl', 'wget'}:
            _network(segment)
        elif name == 'sqlite3':
            _sqlite(segment, program, command)
        elif name == 'systemd-run':
            raise Refused('Validators cannot launch system services')
        elif name == 'systemctl' and '--user' in argv:
            _, operands = getopt.gnu_getopt(argv[1:], 'p:n:H:M:t:s:', [
                'user', 'system', 'no-pager', 'no-legend', 'quiet', 'all', 'full',
                'property=', 'lines=', 'host=', 'machine=', 'type=', 'state=', 'value',
            ])
            if not operands or operands[0] not in READ_UNITS:
                raise Refused('Validators cannot change user services')
        if argv:
            _scripts(segment)
        cwd_change = name in {'cd', 'pushd', 'popd'}
        repo_change = seg_sets_variables(segment, REPO_PATTERNS)
        home_change = seg_sets_variables(segment, HOME_PATTERNS)
        home_change |= name == 'unset' and any(a in {'HOME', 'CODEX_HOME'} for a in argv[1:])
        if cwd_change or repo_change or home_change:
            target_scope = scope
            while True:
                updated = replace(scopes[target_scope])
                if cwd_change:
                    updated.directory = None
                updated.repository_unknown |= repo_change
                updated.home_unknown |= home_change
                scopes[target_scope] = updated
                if kinds[target_scope] != 'embedded':
                    break
                target_scope = target_scope[:-2]


def main():
    try:
        payload = json.load(sys.stdin)
        action = normalize(payload)
        if action.kind != 'shell':
            raise Refused('Validator role guard accepts only shell events')
        check_command(action.arguments['command'], Path(payload['cwd']))
    except Refused as error:
        print('BLOCKED: ' + str(error), file=sys.stderr)
    except Exception:
        print('BLOCKED: Validator role evaluation failed', file=sys.stderr)
    else:
        return 0
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
