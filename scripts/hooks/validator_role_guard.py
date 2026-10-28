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
# Allowlists, not verb denylists: an operation outside them refuses (owner, 2026-10-27).
# Git reads that change no work tree, index, ref or config, whatever their arguments.
GIT_READS = frozenset([
    'status', 'log', 'diff', 'show', 'rev-parse', 'ls-files', 'ls-tree', 'ls-remote', 'cat-file',
    'grep', 'blame', 'annotate', 'shortlog', 'describe', 'rev-list', 'merge-base', 'show-ref',
    'for-each-ref', 'name-rev', 'diff-tree', 'diff-files', 'diff-index', 'whatchanged', 'var',
    'version', 'help', 'check-ignore', 'check-attr', 'check-ref-format', 'count-objects',
    'verify-commit', 'verify-tag', 'range-diff', 'cherry', 'show-branch',
])
GIT_PUBLISH = frozenset(['push', 'send-pack', 'svn', 'p4'])
GH_READS = {'pr': {'view', 'list', 'status', 'diff', 'checks'}, 'issue': {'view', 'list', 'status'},
            'repo': {'view', 'list'}, 'run': {'view', 'list', 'watch'}, 'workflow': {'view', 'list'},
            'release': {'view', 'list'}, 'gist': {'view', 'list'}, 'cache': {'list'},
            'auth': {'status'}, 'status': {None}, 'search': None}
GENESIS_READS = {'db': {'query'}, 'eval': {'results', 'datasets'}, 'handoffs': {'list'},
                 'session-address': None, 'lessons': {'list', 'sample'}, 'peers': {'list'},
                 'transcripts': {'status', 'verify'}}  # sql can write files (#3640)
STORE_PORTS = frozenset([5000, 6333])
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
            elif kind not in {'assignment_only', 'appends'}:
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
             ROOT / '.git', ROOT / 'scripts/hooks', ROOT / '.claude/hooks',
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
        _check_output(value, context, git)


def _check_output(value, context, git):
    # One rule for every file the shell or a program writes: a resolvable,
    # unprotected path in a known namespace.
    target = _path(value, context.directory, home_unknown=context.home_unknown, resolve=False)
    if not context.namespace or target is None or _protected(target, git):
        raise Refused('Validator output targets a protected or unresolved path')


_COMMANDS = None


def _git_commands(git):
    # Git ignores an alias that would hide a Git command (git-config, alias.*),
    # so only a name outside this list can carry one, from any config source.
    global _COMMANDS
    if _COMMANDS is None:
        listed = git.run('/', '--list-cmds=builtins,main,others')
        if listed.returncode != 0:
            raise Refused('Unresolved Git command list')
        _COMMANDS = frozenset(listed.stdout.decode().split())
    return _COMMANDS


# Read forms of verbs that also write: listing flags only, no operand that names a target.
_LIST_FLAGS = {
    'branch': {'--show-current', '-a', '--all', '-r', '--remotes', '-l', '--list', '-v', '-vv',
               '--verbose', '--no-color', '--color', '--contains', '--no-contains', '--merged',
               '--no-merged', '--points-at', '--sort', '--format', '--column', '--no-column'},
    'tag': {'-l', '--list', '-n', '--contains', '--no-contains', '--merged', '--no-merged',
            '--points-at', '--sort', '--format', '--column', '--no-column'},
}
# Options that make a read verb run a program or write a file.
_READ_EFFECTS = ('--output', '-O', '--open-files-in-pager', '--ext-diff')


_CONFIG_READS = {'--get', '--get-all', '--get-regexp', '-l', '--list'}
_CONFIG_WRITES = {'--add', '--unset', '--unset-all', '--replace-all', '--rename-section',
                  '--remove-section', '-e', '--edit'}


def _git_read(verb, rest):
    if verb in GIT_READS:
        return True
    flags = [w.split('=', 1)[0] for w in rest if w.startswith('-')]
    positionals = [w for w in rest if not w.startswith('-')]
    if verb in _LIST_FLAGS:
        # Listing only: every flag a listing flag, and an operand only as a
        # --list pattern. Anything else (create, delete, move) is a write.
        pattern = any(f in {'-l', '--list'} for f in flags)
        known = all(f in _LIST_FLAGS[verb] or re.fullmatch(r'-n\d*', f) for f in flags)
        return known and (not positionals or pattern)
    if verb == 'remote':
        return not rest or rest in (['-v'], ['--verbose']) or rest[:1] in (['show'], ['get-url'])
    if verb == 'worktree':
        return rest[:1] == ['list']
    if verb == 'stash':
        return rest[:1] in (['list'], ['show'])
    if verb == 'config':
        return bool(_CONFIG_READS & set(flags)) and not _CONFIG_WRITES & set(flags) and len(positionals) <= 2
    return False


def _git_destination(verb, rest, directory, context):
    # The destination is the last operand (a one-operand clone names its own
    # directory after the source); an option-like one, or a separate git dir,
    # leaves the created repository unproven.
    operands = [w for w in rest if not w.startswith('-')]
    if (rest and rest[-1].startswith('-')) or any(w.startswith('--separate-git-dir') for w in rest):
        raise Refused('Validator Git mutation has no proven operation repository')
    if verb == 'clone' and len(operands) == 1:
        name = re.sub(r'\.git$', '', operands[0].rstrip('/').rsplit('/', 1)[-1].rsplit(':', 1)[-1])
        spelling = name or None
    else:
        spelling = operands[-1] if operands else '.'
    target = _path(spelling, directory, home_unknown=context.home_unknown) if spelling else None
    if target is None or not context.namespace or context.repository_unknown or target.is_relative_to(ROOT):
        raise Refused('Validators cannot create a repository in the main checkout')


def _git(segment, context, git):
    options = []
    index = git_subcommand_index(segment.argv, _options=options)
    verb = segment.argv[index] if index is not None else None
    if verb is not None and verb not in _git_commands(git):
        raise Refused('Validator Git verb is not a Git command; an alias can run any command')
    rest = segment.argv[index + 1:] if index is not None else []
    if verb in GIT_PUBLISH or (verb == 'subtree' and 'push' in rest):
        raise Refused('Validators report findings; publishing code is prohibited')
    if verb is None or _git_read(verb, rest):
        # A read stays a read only without options that run a program or write a
        # file, and with configuration limited to output colour.
        if any(w == e or w.startswith(e + '=') or (e == '-O' and w.startswith('-O'))
               for w in rest for e in _READ_EFFECTS):
            raise Refused('Validator Git read option runs a program or writes a file')
        for name, value in options:
            key = (value or '').split('=', 1)[0].lower()
            if name.startswith('--config-env') or (name == '-c' and not key.startswith('color.')):
                raise Refused('Validator Git read sets configuration beyond output colour')
        for kind, value in (*segment.ancestors, *segment.contexts):
            name, _, assigned = (value or '').partition('=')
            if kind == 'assignment' and (
                    name in {'GIT_CONFIG_PARAMETERS', 'GIT_EXTERNAL_DIFF', 'GIT_PAGER', 'PAGER'}
                    or (name.startswith('GIT_CONFIG_KEY_') and not assigned.lower().startswith('color.'))):
                raise Refused('Validator Git read sets configuration beyond output colour')
        return
    # Every other verb can write: it needs a proven operation repository that is
    # not the main checkout. clone and init create theirs at a destination.
    directory = context.directory
    uncertain = context.repository_unknown or seg_redirects_repo(segment) or not context.namespace
    for name, value in options:
        if name == '-C':
            directory = _path(value, directory, home_unknown=context.home_unknown)
        elif value is not None or name == '--bare':
            uncertain = True
    if verb in {'clone', 'init'}:
        if uncertain:
            raise Refused('Validator Git mutation has no proven operation repository')
        _git_destination(verb, rest, directory, context)
        return
    if directory is None or uncertain:
        raise Refused('Validator Git mutation has no proven operation repository')
    checkout = git.checkout(str(directory))
    if directory.is_relative_to(ROOT) or (checkout is not None and Path(checkout[0]) == ROOT):
        raise Refused('Validators cannot move the main checkout')


def _github(argv):
    invocation = gh_command(argv)
    if invocation is None:
        raise Refused('Unresolved GitHub operation')
    if invocation.group == 'api':
        request = gh_api_request(argv)
        if request is None or request.method not in {'GET', 'HEAD'} or request.fields or request.has_input:
            raise Refused('Validators publish only through the privacy-scanned filer')
        return
    action = invocation.subcommand or (invocation.positionals[0] if invocation.positionals else None)
    if invocation.group not in GH_READS or (
            GH_READS[invocation.group] is not None and action not in GH_READS[invocation.group]):
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


def _genesis_writer(rest):
    # Only the CLI's read subcommands pass; bare help is a read too.
    if not rest or rest[0] in {'-h', '--help'}:
        return
    reads = GENESIS_READS.get(rest[0], ())
    if reads is not None and (len(rest) < 2 or rest[1] not in reads):
        raise Refused('Validator cannot invoke a live Genesis writer')


def _genesis_module(module, rest):
    # `python -m genesis.<module>` is an entry point too. Only the resource
    # governor's reads, and its `run` of a pytest batch, pass.
    rest = list(rest)
    if module != 'genesis.hostmetrics' or not rest:
        raise Refused('Validator cannot invoke a live Genesis writer')
    if rest[0] in {'status', 'preflight', '-h', '--help'}:
        return
    inner = rest[rest.index('--') + 1:] if rest[0] == 'run' and '--' in rest else []
    if inner[:1] == ['timeout']:  # The repo's mandated outer bound: timeout [-k N] [-s SIG] DURATION.
        inner = inner[1:]
        while inner[:1] in (['-k'], ['-s']) and len(inner) > 1:
            inner = inner[2:]
        inner = inner[1:] if inner and re.fullmatch(r'\d+(?:\.\d+)?[smhd]?', inner[0]) else ['timeout']
    pytest = (bool(inner) and Path(inner[0]).name == 'pytest') or (
        bool(inner) and re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', Path(inner[0]).name) is not None
        and inner[1:3] == ['-m', 'pytest'])
    if not pytest:
        raise Refused('Validator resource-governor runs are limited to pytest batches')


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
            if operand == 'genesis':
                _genesis_writer(rest)
            elif operand.startswith('genesis.'):
                _genesis_module(operand, rest)
            return
    elif name == 'genesis':
        _genesis_writer(rest)  # The console script is the same entry point.
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


def _network(segment, context, git):
    curl = segment.exe == 'curl'
    short = 'X:d:H:o:A:u:e:x:F:T:m:sb:IhLvGkqfS' if curl else 'O:qo:U:T:t:SP:'
    long = ('request= data= data-raw= data-binary= data-urlencode= json= form= upload-file= '
            'header= output= user-agent= user= referer= proxy= max-time= connect-timeout= '
            'url= cookie= get head silent show-error fail fail-with-body insecure location compressed '
            'retry= write-out=' if curl else 'method= post-data= post-file= body-data= body-file= '
            'header= output-document= output-file= directory-prefix= quiet timeout= tries= user-agent= spider '
            'no-check-certificate')
    try:
        options, urls = getopt.gnu_getopt(segment.argv[1:], short, long.split())
    except getopt.GetoptError as exc:
        raise Refused('Unclassified HTTP client options') from exc
    # A file the client saves (body, wget log) is an output like a shell redirect.
    saved = {'-o', '--output'} if curl else {'-O', '--output-document', '-o', '--output-file'}
    for value in (v for k, v in options if k in saved and v != '-'):
        _check_output(value, context, git)
    # wget -P saves each URL's file under a directory: check the directory and
    # the file it would create there.
    for directory in (v for k, v in options if not curl and k in {'-P', '--directory-prefix'}):
        _check_output(directory, context, git)
        for url in urls:
            name = urlsplit(url if '://' in url else 'http://' + url).path.rsplit('/', 1)[-1]
            _check_output(directory.rstrip('/') + '/' + (name or 'index.html'), context, git)
    methods = [v.upper() for k, v in options if k in {'-X', '--request', '--method'}]
    data = any(k in {'-d', '--data', '--data-raw', '--data-binary', '--data-urlencode',
                    '--post-data', '--post-file', '--body-data', '--body-file'} for k, _ in options)
    query = curl and any(k in {'-G', '--get'} for k, _ in options)
    body = (data and not query) or (curl and any(k in {'--json', '-F', '--form', '-T', '--upload-file'} for k, _ in options))
    method = methods[-1] if methods else 'POST' if body else 'GET'
    if method in {'GET', 'HEAD'} and not body:
        return
    # The store ports refuse on every host: loopback has many spellings, names
    # resolve to this machine, and a host-side proxy forwards to the stores from
    # an address no local lookup can recognise (owner, 2026-10-27).
    proxies = [v for k, v in options if k in {'-x', '--proxy'}]
    for url in [*urls, *(v for k, v in options if k == '--url'), *proxies]:
        if '$' in url or '`' in url:
            raise Refused('Unresolved mutating HTTP destination')
        parsed = urlsplit(url if '://' in url else 'http://' + url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise Refused('Unresolved mutating HTTP destination') from exc
        if port in STORE_PORTS:
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


GUARDED = frozenset(['git', 'gh', 'kill', 'pkill', 'curl', 'wget', 'sqlite3', 'systemctl',
                     'systemd-run', 'genesis', '.', 'source'])


def _guarded(segment):
    # A program one of the checks above classifies, where appended words could
    # change the operation: xargs-completed forms of these refuse.
    name = segment.exe
    return (name in GUARDED or name in _NESTED or re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', name)
            or (segment.argv and Path(segment.argv[0]).name in DEPLOYS | {'deploy_code_only.sh', 'watchgod'}))


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
        if _guarded(segment) and any(kind == 'appends' for kind, _ in (*segment.ancestors, *segment.contexts)):
            raise Refused('Validator command takes words from input; its operation is unknown')
        if name == 'git':
            _git(segment, program, git)
        elif name == 'gh':
            _github(argv)
        elif name in {'kill', 'pkill'}:
            _processes(segment)
        elif name in {'curl', 'wget'}:
            _network(segment, program, git)
        elif name == 'sqlite3':
            _sqlite(segment, program, command)
        elif name == 'systemd-run':
            raise Refused('Validators cannot launch system services')
        elif name == 'systemctl' and '--user' in argv:
            try:
                _, operands = getopt.gnu_getopt(argv[1:], 'p:n:H:M:t:s:la', [
                    'user', 'system', 'no-pager', 'no-legend', 'quiet', 'all', 'full',
                    'property=', 'lines=', 'host=', 'machine=', 'type=', 'state=', 'value',
                ])
            except getopt.GetoptError as exc:
                raise Refused('Unclassified user-service options') from exc
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
