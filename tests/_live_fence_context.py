"""Stateless, finite invocation facts for the suite's accidental-launch fence.

Unknown manager facts are different from absent environment keys. Unsupported
unit context is deliberately sticky; this is not a systemd property interpreter.
"""

from __future__ import annotations

import os
import re

PROTECTED_KEYS = frozenset({'HOME', 'PATH', 'GENESIS_DIR', 'GENESIS_UPDATE_ORIG_DIR', 'GENESIS_DEPLOY_ROOT'})
NEUTRAL_PROPERTIES = frozenset({'MemoryMax', 'MemorySwapMax'})
_ENV_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
_FLAGS = frozenset({'--user', '--system', '--scope', '--pipe', '--quiet', '-q',
                    '--wait', '--collect', '--pty', '-t', '--no-block'})
_NEUTRAL_VALUES = frozenset({'-u', '--unit', '--description', '--slice', '--service-type', '--nice', '--job-mode'})


class Environment(dict):
    """Mapping values plus per-carrier unknown facts; missing keys mean absent."""

    def __init__(self, values=(), *, unknown=(), cwd_unknown=False, namespace_unknown=False):
        super().__init__(values)
        self.unknown = frozenset(unknown)
        self.cwd_unknown = cwd_unknown
        self.namespace_unknown = namespace_unknown

    def overlay(self, values):
        result = Environment(self, unknown=self.unknown, cwd_unknown=self.cwd_unknown,
                             namespace_unknown=self.namespace_unknown)
        result.update(values)
        if not self.namespace_unknown:
            result.unknown = self.unknown.difference(values)
        return result


def environment(env):
    if isinstance(env, Environment):
        return env
    values = os.environ if env is None else env
    return Environment({os.fsdecode(key): os.fsdecode(value) for key, value in values.items()})


def unknown(env, key):
    return isinstance(env, Environment) and (env.namespace_unknown or key in env.unknown)


def unknown_cwd(env):
    return isinstance(env, Environment) and (env.cwd_unknown or env.namespace_unknown)


def needs_cwd(program, env):
    """Absolute paths and fully absolute known PATH searches need no CWD."""
    if '/' in program:
        return not os.path.isabs(program)
    if unknown(env, 'PATH'):
        return False  # The separate unknown-PATH rule already refuses this.
    search = environment(env).get('PATH', os.defpath)
    return any(not os.path.isabs(part) for part in search.split(os.pathsep))


def overlay(env, values):
    return environment(env).overlay(values)


def env_options(env, words):
    """Keep known env clearing/unsets distinct from inherited unknown facts."""
    facts = environment(env)
    for start, word in enumerate(words):
        if os.path.basename(word) != 'env':
            continue
        i = start + 1
        while i < len(words) and words[i].startswith('-'):
            option = words[i]
            if option in ('-', '-i', '--ignore-environment'):
                facts = Environment(unknown=PROTECTED_KEYS if facts.namespace_unknown else (),
                                    cwd_unknown=facts.cwd_unknown, namespace_unknown=facts.namespace_unknown)
            elif option in ('-u', '--unset') or option.startswith(('-u', '--unset=')):
                if option in ('-u', '--unset'):
                    i += 1
                    if i == len(words):
                        break
                    name = words[i]
                else:
                    name = option[2:] if option.startswith('-u') else option.partition('=')[2]
                facts = facts.overlay({})
                facts.pop(name, None)
                if not facts.namespace_unknown:
                    facts.unknown = facts.unknown.difference({name})
            elif option == '--':
                break
            else:
                # No arbitrary env option interpreter; unknown context never
                # becomes a private-context proof through later assignments.
                facts = Environment(facts, unknown=PROTECTED_KEYS, cwd_unknown=True, namespace_unknown=True)
                break
            i += 1
        break
    return facts


def systemd_context(argv, env, cwd, value_options):
    """Return payload, environment facts and CWD; reject unknown option arity."""
    caller = environment(env)
    scope, tainted = False, False
    assignments, directories = [], []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == '--':
            i += 1
            break
        if not arg.startswith('-') or arg == '-':
            break
        if arg in ('--help', '--version'):
            return [], caller, cwd
        if arg in ('-d', '--same-dir'):
            directories.append(None if unknown_cwd(caller) else cwd or os.getcwd())
            i += 1
            continue
        key, equals, value = arg.partition('=')
        if arg[:2] in ('-E', '-p', '-u', '-H', '-M') and len(arg) > 2:
            key, equals, value = arg[:2], '=', arg[2:]
        if key in value_options:
            if not equals:
                i += 1
                if i == len(argv):
                    raise ValueError('missing systemd-run option value')
                value = argv[i]
            if key in ('-E', '--setenv'):
                name, assigned, content = value.partition('=')
                if not _ENV_NAME.fullmatch(name):
                    raise ValueError('invalid systemd-run environment name')
                assignments.append((name, assigned, content))
            elif key in ('-p', '--property'):
                tainted |= value.partition('=')[0] not in NEUTRAL_PROPERTIES
            elif key == '--working-directory':
                directories.append(value if os.path.isabs(value) else None)
            elif key not in _NEUTRAL_VALUES:
                tainted = True
        elif arg in _FLAGS:
            scope |= arg == '--scope'
        else:
            raise ValueError('unproved systemd-run option boundary')
        i += 1
    facts = Environment(caller, unknown=caller.unknown, cwd_unknown=caller.cwd_unknown,
                        namespace_unknown=caller.namespace_unknown) if scope else Environment(
                            unknown=PROTECTED_KEYS, cwd_unknown=True,
                            namespace_unknown=caller.namespace_unknown)
    child_cwd = cwd if scope else None
    for name, assigned, value in assignments:
        if assigned:
            facts = facts.overlay({name: value})
        elif name in caller and not unknown(caller, name):
            facts = facts.overlay({name: caller[name]})
        # No caller value means no assignment, not a known-absent manager key.
    if directories:
        child_cwd = directories[0] if len(directories) == 1 else None
        facts.cwd_unknown = child_cwd is None
    if tainted:
        facts.unknown = PROTECTED_KEYS
        facts.cwd_unknown = facts.namespace_unknown = True
        child_cwd = None
    return argv[i:], facts, child_cwd


def script_args(argv, shells):
    """Direct shell script operand, preserving Bash actions versus n state."""
    head = os.path.basename(argv[0])
    if head in ('source', '.'):
        return argv[1:2]
    if head not in shells:
        return []
    noexec, action = False, False
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in ('--', '-'):
            return [] if noexec or action else argv[i + 1:i + 2]
        if arg.startswith('--'):
            if head == 'bash' and arg in ('--dump-strings', '--dump-po-strings', '--pretty-print', '--help', '--version'):
                action = True
            i += 2 if arg in ('--rcfile', '--init-file') else 1
            continue
        if arg.startswith(('-', '+')):
            letters = arg[1:]
            if 'c' in letters:
                return []
            action |= head == 'bash' and 'D' in letters
            values = sum(letter in 'oO' for letter in letters)
            if 'n' in letters or ('o' in letters and 'noexec' in argv[i + 1:i + 1 + values]):
                noexec = arg[0] == '-'
            i += 1 + values
            continue
        return [] if noexec or action else [arg]
    return []
