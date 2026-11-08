"""Source-helper pruning on owned repositories, never the live hygiene entrypoint."""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class PruneCliChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='r9-cli-')
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'source'
        self.other = self.root / 'other'
        self.home = self.root / 'home'
        self.home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('GIT_', 'GENESIS_', 'PYTHON'))}
        self.env.update(HOME=str(self.home), GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
        for repo in (self.repo, self.other):
            subprocess.run(['git', 'init', '-q', str(repo)], check=True, env=self.env)
        target = self.repo / 'scripts/hooks/push_privacy_ack.py'
        target.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / 'scripts/hooks/push_privacy_ack.py', target)
        # Owned source package with the exact shared atomic writer, no decoy PYTHONPATH.
        package = self.repo / 'src/genesis/util'
        package.mkdir(parents=True)
        (package.parent / '__init__.py').write_text('')
        (package / '__init__.py').write_text('')
        shutil.copyfile(ROOT / 'src/genesis/util/atomic.py', package / 'atomic.py')
        self.target = target

    def tearDown(self):
        self.tmp.cleanup()

    def marker(self, repo):
        directory = repo / '.git/genesis/privacy-ack'
        directory.mkdir(parents=True, mode=0o700)
        (repo / '.git/genesis').chmod(0o700)
        marker = directory / ('a' * 64 + '.json')
        old = time.time() - 8 * 86400
        marker.write_text(json.dumps({'version': 1, 'identity': 'a' * 64, 'created_at': old}))
        marker.chmod(0o600)
        os.utime(marker, (old, old))
        return marker

    def run_prune(self, repo, extra=None):
        return subprocess.run(['/usr/bin/python3', str(self.target), '--prune', str(repo)],
            cwd=self.root, env={**self.env, **(extra or {})}, capture_output=True, text=True, timeout=10)

    def test_source_bootstrap_and_explicit_repository(self):
        marker = self.marker(self.repo)
        other = self.marker(self.other)
        result = self.run_prune(self.repo, {'GIT_DIR': str(self.other / '.git'), 'GIT_WORK_TREE': str(self.other)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'Pruned 1 expired privacy acknowledgement files.')
        self.assertFalse(marker.exists())
        self.assertTrue(other.exists())
        self.assertTrue((marker.parent / '.lock').is_file())

    def test_absent_namespace_no_creation(self):
        result = self.run_prune(self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'Pruned 0 expired privacy acknowledgement files.')
        self.assertFalse((self.repo / '.git/genesis').exists())

    def test_invalid_repository_returns_unknown_without_touching_other(self):
        marker = self.marker(self.other)
        result = self.run_prune(self.root / 'not-a-repository')
        self.assertEqual(result.returncode, 2)
        self.assertIn('could not be qualified', result.stderr)
        self.assertTrue(marker.exists())

    def test_hygiene_calls_only_the_owned_repository(self):
        marker = self.marker(self.repo)
        other = self.marker(self.other)
        result = subprocess.run(
            ['bash', '-c', 'source "$1"\nREPO_DIR="$2"\nVENV_PY="$3"\nprune_privacy_acknowledgements',
             'owned-hygiene-step', str(ROOT / 'scripts/disk_hygiene.sh'), str(self.repo), '/usr/bin/python3'],
            cwd=self.root, env=self.env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Pruned 1 expired privacy acknowledgement files.', result.stdout)
        self.assertFalse(marker.exists())
        self.assertTrue(other.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
