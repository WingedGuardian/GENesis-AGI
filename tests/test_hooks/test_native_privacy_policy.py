"""Owned consumer controls; no native push, remote or installed hook changes."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.hooks import push_privacy_ack as ack
from scripts.hooks import push_privacy_scan as scan


class PolicyChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='r9-policy-')
        self.common = Path(self.tmp.name)
        self.token = 'a' * 64
        self.state = self.common / 'genesis/privacy-ack'
        self.marker = self.state / (self.token + '.json')
        self.environ = patch.dict(os.environ, {}, clear=True)
        self.environ.start()
        self.modules = patch.dict('sys.modules', {'push_privacy_ack': ack})
        self.modules.start()

    def tearDown(self):
        self.modules.stop()
        self.environ.stop()
        self.tmp.cleanup()

    def result(self, verdict, **kwargs):
        return scan.ScanResult(verdict, self.token, common_dir=str(self.common), **kwargs)

    def finish(self, result):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            code = scan.finish(result)
        return code, output.getvalue()

    def test_foreground_first_second_third_for_both_permitted_classes(self):
        for verdict in ('TIER_B', 'COULD_NOT_SCAN'):
            with self.subTest(verdict=verdict):
                self.marker.unlink(missing_ok=True)
                result = self.result(verdict)
                self.assertEqual([self.finish(result)[0] for _ in range(3)], [2, 0, 2])
                self.assertTrue(self.marker.is_file())

    def test_automated_never_creates_or_consumes(self):
        for flag in ('GENESIS_AUTOMATED_PUSH', 'GENESIS_CC_SESSION'):
            with patch.dict(os.environ, {flag: '1'}, clear=True):
                for verdict in ('TIER_B', 'COULD_NOT_SCAN'):
                    with self.subTest(flag=flag, verdict=verdict):
                        self.marker.unlink(missing_ok=True)
                        self.assertEqual(self.finish(self.result(verdict))[0], 2)
                        self.assertFalse(self.marker.exists())
                        self.assertEqual(ack.acknowledge(self.common, self.token), 'first')
                        content = self.marker.read_bytes()
                        self.assertEqual(self.finish(self.result(verdict))[0], 2)
                        self.assertEqual(self.marker.read_bytes(), content)

    def test_tier_a_never_creates_or_consumes(self):
        for automated in ('0', '1'):
            with self.subTest(automated=automated):
                os.environ['GENESIS_AUTOMATED_PUSH'] = automated
                self.marker.unlink(missing_ok=True)
                self.assertEqual(self.finish(self.result('TIER_A'))[0], 2)
                self.assertFalse(self.marker.exists())
                self.assertEqual(ack.acknowledge(self.common, self.token), 'first')
                content = self.marker.read_bytes()
                self.assertEqual(self.finish(self.result('TIER_A'))[0], 2)
                self.assertEqual(self.marker.read_bytes(), content)

    def test_clean_never_touches_acknowledgement(self):
        self.assertEqual(self.finish(self.result('CLEAN'))[0], 0)
        self.assertFalse(self.state.exists())
        ack.acknowledge(self.common, self.token)
        content = self.marker.read_bytes()
        code, output = self.finish(self.result('CLEAN', degraded=('public-base-unproved',)))
        self.assertEqual(code, 0)
        self.assertIn('degraded', output)
        self.assertEqual(self.marker.read_bytes(), content)

    def test_changed_identity_cannot_consume_old_marker(self):
        ack.acknowledge(self.common, self.token)
        content = self.marker.read_bytes()
        changed = scan.ScanResult('TIER_B', 'b' * 64, common_dir=str(self.common))
        self.assertEqual(self.finish(changed)[0], 2)
        self.assertEqual(self.marker.read_bytes(), content)

    def test_unavailable_namespace_or_helper_refuses(self):
        result = scan.ScanResult('COULD_NOT_SCAN', self.token)
        self.assertEqual(self.finish(result)[0], 2)
        with patch.dict('sys.modules', {'push_privacy_ack': None}):
            self.assertEqual(self.finish(self.result('COULD_NOT_SCAN'))[0], 2)
        self.assertFalse(self.state.exists())

    def test_only_consumed_repeat_is_permission(self):
        for state in ('first', 'busy', 'invalid', 'unavailable', 'unexpected'):
            with self.subTest(state=state), patch.object(ack, 'acknowledge', return_value=state):
                self.assertEqual(self.finish(self.result('TIER_B'))[0], 2)
        self.assertFalse(self.state.exists())

    def test_output_has_only_safe_finding_projection(self):
        code, output = self.finish(self.result('TIER_A', findings=(('known-value', 'b' * 64),)))
        self.assertEqual(code, 2)
        self.assertIn('known-value ' + 'b' * 64, output)
        self.assertNotIn(str(self.common), output)


if __name__ == '__main__':
    unittest.main(verbosity=2)
