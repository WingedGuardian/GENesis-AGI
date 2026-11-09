"""Reusable local-only Git world for the eventual R8 adapter's public-base API."""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

PUBLIC = 'git@github.com:fixture/public.git'
PRIVATE = 'git@github.com:fixture/private.git'


class LocalPublicBaseWorld:
    def __init__(self, tmp_path):
        self.root = Path(tmp_path)
        self.commands = []
        self.env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        self.env.update(HOME=str(self.root), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0', GIT_SSH_VARIANT='ssh', R8_LOCAL_WORLD=str(self.root))
        # The eventual adapter inherits this boundary even when it calls Git
        # directly instead of using this fixture's command recorder.
        self.env.update(GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_0='protocol.allow', GIT_CONFIG_VALUE_0='never', GIT_CONFIG_KEY_1='protocol.ssh.allow', GIT_CONFIG_VALUE_1='always')
        self.shim = self.root / 'owned-local-ssh'
        shutil.copy2(Path(__file__).with_name('_privacy_local_ssh.py'), self.shim)
        self.shim.chmod(0o700)
        self.env['GIT_SSH'] = str(self.shim)
        self.seed = self.root / 'seed'
        self.seed.mkdir()
        self.git(self.seed, '-c', 'init.templateDir=', 'init', '-q', '-b', 'main')
        self.tree = self.git(self.seed, 'hash-object', '-t', 'tree', os.devnull).stdout.decode().strip()
        self.main = self.commit(self.seed, 'public main')
        self.master = self.commit(self.seed, 'public master', self.main)
        self.git(self.seed, 'update-ref', 'refs/heads/main', self.main)
        self.git(self.seed, 'update-ref', 'refs/heads/master', self.master)
        self.git(self.root, '-c', 'protocol.file.allow=always', 'clone', '--bare', '--no-hardlinks', str(self.seed), str(self.root / 'public.git'))
        self.private = self.commit(self.seed, 'synthetic private origin content', self.main)
        self.outgoing = self.commit(self.seed, 'outgoing change', self.private)
        self.git(self.seed, 'update-ref', 'refs/heads/main', self.private)
        self.git(self.seed, 'update-ref', 'refs/heads/topic', self.outgoing)
        self.git(self.root, '-c', 'protocol.file.allow=always', 'clone', '--bare', '--no-hardlinks', str(self.seed), str(self.root / 'private.git'))
        self.client = self.root / 'client'
        self.git(self.root, '-c', 'protocol.file.allow=always', 'clone', '--no-hardlinks', str(self.root / 'private.git'), str(self.client))
        self.git(self.client, 'remote', 'set-url', 'origin', PRIVATE)
        self.initial_snapshot = self.snapshot()

    def git(self, cwd, *args, extra_env=None, check=True):
        command = ['/usr/bin/git', '--no-replace-objects', '-c', 'protocol.allow=never', '-c', 'protocol.ssh.allow=always', '-C', str(cwd), *args]
        result = subprocess.run(command, env={**self.env, **(extra_env or {})}, capture_output=True, timeout=5)
        self.commands.append({'argv': command, 'exit': result.returncode, 'stdout': result.stdout.decode('utf-8', 'surrogateescape'), 'stderr': result.stderr.decode('utf-8', 'surrogateescape')})
        if check:
            result.check_returncode()
        return result

    def commit(self, repo, message, parent=None):
        return self.git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit-tree', self.tree, *(['-p', parent] if parent else []), '-m', message).stdout.decode().strip()

    def advertise(self, main, master):
        for name, oid in [('main', main), ('master', master)]:
            self.git(self.root / 'public.git', 'update-ref', *(['-d', 'refs/heads/' + name] if oid is None else ['refs/heads/' + name, oid]))

    def snapshot(self):
        return {str(path.relative_to(self.client)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted((self.client / '.git').rglob('*')) if path.is_file()}

    def transport(self):
        path = self.root / 'transport.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
