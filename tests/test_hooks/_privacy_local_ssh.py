#!/usr/bin/python3
"""Owned test transport: never invokes SSH or any network program."""
import json
import os
import shlex
import sys
from pathlib import Path

root = Path(os.environ['R8_LOCAL_WORLD']).resolve()
args = sys.argv[1:]
received = list(args)
if args[:2] == ['-o', 'SendEnv=GIT_PROTOCOL']:
    args = args[2:]
mapping = {
    "fixture/public.git": root / 'public.git',
    "/fixture/public.git": root / 'public.git',
    "fixture/private.git": root / 'private.git',
    "/fixture/private.git": root / 'private.git',
}
target = None
program = None
try:
    command = shlex.split(args[1]) if len(args) == 2 else []
    if args and args[0] == 'git@github.com' and len(command) == 2 and command[0] in {'git-upload-pack', 'git-receive-pack'}:
        target = mapping.get(command[1])
        program = command[0]
except ValueError:
    pass
allowed = target is not None and not os.environ.get('R8_SHIM_FAIL')
execution = ['/usr/bin/' + program, *(['--strict'] if program == 'git-upload-pack' else []), str(target)] if allowed else None
with (root / 'transport.jsonl').open('a') as receipt:
    receipt.write(json.dumps({'argv': received, 'allowed': allowed, 'exec': execution}) + '\n')
if not allowed:
    print('owned transport refused unsupported request', file=sys.stderr)
    sys.exit(97)
env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_') or key in {'GIT_PROTOCOL', 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM'}}
os.execve(execution[0], execution, env)  # noqa: S606 — owned local transport fixture
