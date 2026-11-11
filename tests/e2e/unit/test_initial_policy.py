"""控制面重启后先等待拒绝策略传播，再验证真实 SSH 拒绝。"""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import e2e
from settings import load_config


class InitialPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = load_config(e2e.ROOT / 'environment.example.yaml')
        config.bundle = Path(self.tmp.name) / 'bundle'
        config.generated = Path(self.tmp.name) / 'generated'
        (config.bundle / 'config').mkdir(parents=True)
        (config.bundle / 'config/policy.hujson').write_text('{"grants": []}')
        for name, value in (('CONFIG', config), ('GENERATED', config.generated)):
            replacement = patch.object(e2e, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.report = config.generated / 'install-results.json'
        self.filter_reads = 0
        self.receiver_ready = False
        self.ssh_attempts = 0

    def run_check(self, *, delayed=True, stuck=False, bypass=False):
        def compose(action, option, service, *command, **kwargs):
            self.assertEqual((action, option), ('exec', '-T'))
            stdout, code = '', 0
            if command[:2] == ('tailscale', 'status'):
                stdout = json.dumps({'Self': {'TailscaleIPs': ['100.77.0.3']}})
            elif command == ('tailscale', 'debug', 'netmap'):
                if service == 'client-server':
                    self.filter_reads += 1
                    self.receiver_ready = not stuck and (not delayed or self.filter_reads > 1)
                ready = service != 'client-server' or self.receiver_ready
                stdout = json.dumps({'PacketFilter': [] if ready else [{'IPProto': [6, 17]}]})
            elif command[0] == 'ssh':
                self.ssh_attempts += 1
                code = 0 if bypass or not self.receiver_ready else 255
            else:
                self.assertIn(command[0], ('python3', 'curl'))
            return subprocess.CompletedProcess([], code, stdout, '')

        ticks = iter(range(1000))
        with patch.object(e2e, 'compose', compose), \
                patch.object(e2e.time, 'monotonic', side_effect=lambda: next(ticks)), \
                patch.object(e2e.time, 'sleep'):
            e2e.verify_initial_policy()

    def test_waits_for_receiver_policy_before_ssh(self):
        self.run_check()
        self.assertGreater(self.filter_reads, 1)
        self.assertEqual(self.ssh_attempts, 1)
        self.assertTrue(json.loads(self.report.read_text())['ssh_denied_before_grant'])

    def test_policy_propagation_timeout_does_not_publish_success(self):
        with self.assertRaisesRegex(RuntimeError, 'policy propagation timed out'):
            self.run_check(stuck=True)
        self.assertEqual(self.ssh_attempts, 0)
        self.assertFalse(self.report.exists())

    def test_ssh_bypass_still_fails_after_policy_arrives(self):
        with self.assertRaisesRegex(RuntimeError, 'bypassed the initial deny policy'):
            self.run_check(delayed=False, bypass=True)
        self.assertFalse(self.report.exists())


if __name__ == '__main__':
    unittest.main()
