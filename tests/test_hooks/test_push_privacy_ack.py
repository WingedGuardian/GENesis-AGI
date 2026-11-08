"""Owned controls for one-use privacy acknowledgement state."""

import fcntl
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.hooks import push_privacy_ack as ack


class AcknowledgementChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="r9-ack-")
        self.common = Path(self.tmp.name)
        self.state = self.common / "genesis/privacy-ack"
        self.token = "a" * 64
        self.marker = self.state / (self.token + ".json")
        self.now = 1_000_000.0

    def tearDown(self):
        self.tmp.cleanup()

    def seed(self):
        self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now), "first")
        self.assertTrue(self.marker.is_file())

    def test_first_second_third_and_private_payload(self):
        self.seed()
        data = json.loads(self.marker.read_text())
        self.assertEqual(data, {"version": 1, "identity": self.token, "created_at": self.now})
        self.assertEqual(self.marker.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now + 1), "repeat")
        self.assertFalse(self.marker.exists())
        self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now + 2), "first")

    def test_exact_expiry_boundary_then_expired_refresh(self):
        self.seed()
        self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now + 86400), "repeat")
        self.seed()
        self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now + 86401), "first")
        self.assertEqual(json.loads(self.marker.read_text())["created_at"], self.now + 86401)

    def test_future_and_changed_identity_do_not_consume(self):
        self.seed()
        original = self.marker.read_bytes()
        self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now - 1), "invalid")
        self.assertEqual(ack.acknowledge(self.common, "b" * 64, now=self.now), "first")
        self.assertEqual(self.marker.read_bytes(), original)

    def test_malformed_and_oversized_preserved(self):
        self.seed()
        for content in (
            b"unrelated text",
            b"x" * 4097,
            b"{}",
            b'{"version":true,"identity":"a","created_at":0}',
        ):
            with self.subTest(content_length=len(content)):
                self.marker.write_bytes(content)
                self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now), "invalid")
                self.assertEqual(self.marker.read_bytes(), content)

    def test_lock_contention_preserves_marker_and_inode(self):
        self.seed()
        lock = self.state / ".lock"
        inode = lock.stat().st_ino
        with lock.open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now), "busy")
            self.assertIsNone(ack.prune(self.common, now=self.now + 800000))
            self.assertTrue(self.marker.exists())
        self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now), "repeat")
        self.assertEqual(lock.stat().st_ino, inode)

    def test_symlink_fifo_and_directory_marker_refused_without_follow(self):
        self.seed()
        outside = self.common / "outside"
        original = self.marker.read_bytes()
        outside.write_bytes(original)
        outside.chmod(0o600)
        for kind in ("symlink", "fifo", "directory"):
            with self.subTest(kind=kind):
                self.marker.unlink()
                if kind == "symlink":
                    self.marker.symlink_to(outside)
                elif kind == "fifo":
                    os.mkfifo(self.marker)
                else:
                    self.marker.mkdir()
                self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now), "invalid")
                self.assertEqual(outside.read_bytes(), original)
                if kind == "directory":
                    self.marker.rmdir()
                else:
                    self.marker.unlink()
                self.marker.write_text("temporary owned placeholder")

    def test_symlink_fifo_and_nonprivate_fixed_lock_refused(self):
        self.seed()
        lock = self.state / ".lock"
        outside = self.common / "outside"
        outside.write_text("untouched")
        outside.chmod(0o600)
        lock.unlink()
        lock.symlink_to(outside)
        self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now), "invalid")
        lock.unlink()
        os.mkfifo(lock)
        self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now), "invalid")
        lock.unlink()
        lock.write_text("")
        lock.chmod(0o644)
        self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now), "invalid")
        self.assertEqual(outside.read_text(), "untouched")
        self.assertTrue(self.marker.exists())

    def test_gc_valid_old_marker_and_partial_temp_only(self):
        self.seed()
        bad = self.state / ("b" * 64 + ".json")
        bad.write_text("unknown user content")
        bad.chmod(0o600)
        temp = self.state / "tmpabcd1234.tmp"
        temp.write_text("partial")
        temp.chmod(0o600)
        unrelated = self.state / "notes.txt"
        unrelated.write_text("preserved")
        for path in (self.marker, bad, temp, unrelated):
            os.utime(path, (self.now, self.now))
        self.assertEqual(ack.prune(self.common, now=self.now + 7 * 86400), 0)
        self.assertEqual(ack.prune(self.common, now=self.now + 7 * 86400 + 1), 2)
        self.assertFalse(self.marker.exists())
        self.assertFalse(temp.exists())
        self.assertEqual(bad.read_text(), "unknown user content")
        self.assertTrue(unrelated.exists())
        self.assertTrue((self.state / ".lock").exists())

    def test_linked_temp_is_not_corruption(self):
        self.seed()
        temp = self.state / "tmpabcd1234.tmp"
        os.link(self.marker, temp)
        self.assertEqual(self.marker.stat().st_nlink, 2)
        self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now), "repeat")
        self.assertTrue(temp.exists())

    def test_no_state_on_invalid_token_or_absent_prune(self):
        self.assertEqual(ack.acknowledge(self.common, "../wrong", now=self.now), "invalid")
        self.assertEqual(ack.prune(self.common, now=self.now), 0)
        self.assertFalse(self.state.exists())

    def test_invalid_numeric_state_and_clock_are_bounded(self):
        self.seed()
        for created in (True, "yesterday", float("nan"), float("inf"), -1, 10**400):
            with self.subTest(created_type=type(created).__name__):
                content = json.dumps({"version": 1, "identity": self.token, "created_at": created})
                self.marker.write_text(content)
                self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now), "invalid")
                self.assertEqual(self.marker.read_text(), content)
        for now in (None, True, "yesterday", float("nan"), float("inf"), -1, 10**400):
            if now is None:
                continue  # None intentionally selects the actual clock.
            with self.subTest(clock_type=type(now).__name__):
                self.assertEqual(ack.acknowledge(self.common, self.token, now=now), "invalid")
                self.assertIsNone(ack.prune(self.common, now=now))

    def test_nested_unknown_json_is_preserved_and_never_a_permit(self):
        self.seed()
        content = b"[" * 1500 + b"0" + b"]" * 1500
        self.marker.write_bytes(content)
        os.utime(self.marker, (self.now, self.now))
        self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now), "invalid")
        self.assertEqual(ack.prune(self.common, now=self.now + 800000), 0)
        self.assertEqual(self.marker.read_bytes(), content)

    def test_publication_and_consumption_errors_never_allow(self):
        with patch.object(
            ack, "atomic_write_text", side_effect=OSError("owned publication failure")
        ):
            self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now), "unavailable")
        self.assertFalse(self.marker.exists())
        self.seed()
        original = self.marker.read_bytes()
        with patch.object(Path, "unlink", side_effect=OSError("owned unlink failure")):
            self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now), "unavailable")
        self.assertEqual(self.marker.read_bytes(), original)
        self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now), "repeat")

    def test_namespace_symlink_or_unsafe_modes_refuse_without_outside_write(self):
        outside = self.common / "outside"
        outside.mkdir()
        genesis = self.common / "genesis"
        genesis.symlink_to(outside, target_is_directory=True)
        self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now), "invalid")
        self.assertEqual(list(outside.iterdir()), [])
        genesis.unlink()
        genesis.mkdir(mode=0o777)
        genesis.chmod(0o777)
        self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now), "invalid")
        self.assertFalse(self.state.exists())
        genesis.chmod(0o700)
        self.state.mkdir(mode=0o755)
        self.assertEqual(ack.acknowledge(self.common, self.token, now=self.now), "invalid")
        self.assertEqual(list(self.state.iterdir()), [])

    def child(self, code, *args):
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                str(Path(__file__).resolve().parents[2] / part) for part in ("scripts/hooks", "src")
            ),
        }
        return subprocess.Popen(
            ["/usr/bin/python3", "-c", code, str(self.common), self.token, str(self.now), *args],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_two_native_contenders_consume_one_marker_generation(self):
        self.seed()
        code = """import sys
import push_privacy_ack as ack
print('ready', flush=True)
sys.stdin.readline()
print(ack.acknowledge(sys.argv[1],sys.argv[2],now=float(sys.argv[3])),flush=True)
"""
        children = [self.child(code), self.child(code)]
        try:
            for child in children:
                self.assertEqual(child.stdout.readline().strip(), "ready")
            for child in children:
                child.stdin.write("go\n")
                child.stdin.flush()
            results = [child.communicate(timeout=5)[0].strip() for child in children]
            self.assertEqual(results.count("repeat"), 1)
            self.assertTrue(set(results) <= {"repeat", "busy", "first"})
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)

    def test_native_process_death_at_publication_and_consumption_boundaries(self):
        code = """import sys,os,time,tempfile
from pathlib import Path
import push_privacy_ack as ack
phase=sys.argv[4]
real=ack.atomic_write_text
def parked(*args,**kwargs):
    if phase=='before':
        fd,name=tempfile.mkstemp(dir=Path(args[0]).parent,suffix='.tmp')
        os.write(fd,b'partial');os.close(fd)
    else: real(*args,**kwargs)
    print('ready',flush=True);time.sleep(30)
if phase=='consume':
    unlink=Path.unlink
    def interrupted(path,*args,**kwargs):
        unlink(path,*args,**kwargs)
        if path.name==sys.argv[2]+'.json':
            print('ready',flush=True);time.sleep(30)
    Path.unlink=interrupted
else: ack.atomic_write_text=parked
ack.acknowledge(sys.argv[1],sys.argv[2],now=float(sys.argv[3]))
"""
        for phase in ("before", "after", "consume"):
            with self.subTest(phase=phase):
                self.marker.unlink(missing_ok=True)
                if phase == "consume":
                    self.seed()
                child = self.child(code, phase)
                try:
                    self.assertEqual(child.stdout.readline().strip(), "ready")
                    child.kill()
                    child.communicate(timeout=5)
                    expected = "repeat" if phase == "after" else "first"
                    self.assertEqual(
                        ack.acknowledge(self.common, self.token, now=self.now), expected
                    )
                    self.assertTrue((self.state / ".lock").is_file())
                finally:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
