"""只模拟 Docker 命令的超时 / 错误，验证真实故障编排代码的恢复行为。"""

import contextlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import verify

verify.configure(verify.ROOT / "environment.example.yaml")


class Network:
    def __init__(self, failure):
        self.failure = failure
        self.relay = True
        self.blocked = set()
        self.removal_attempts = []

    def compose(self, action, *args, **kwargs):
        if action in ("start", "stop"):
            assert args == ("derp-main",), "Fault injection must leave control running"
        if action == "stop":
            self.relay = False
            if self.failure == "stop_timeout":
                raise subprocess.TimeoutExpired("stop", 30)
        elif action == "start":
            if self.failure == "start_timeout":
                raise subprocess.TimeoutExpired("start", 30)
            self.relay = True
        elif action == "ps":
            return subprocess.CompletedProcess([], 0, "control\n" + ("derp-main\n" if self.relay else ""), "")
        else:
            raise AssertionError(f"Unexpected compose operation: {action}")
        return subprocess.CompletedProcess([], 0, "", "")

    def firewall(self, service, action):
        code = 0
        if action == "-I":
            self.blocked.add(service)
            if self.failure == "insert_timeout":
                raise subprocess.TimeoutExpired("insert", 30)
        elif action == "-D":
            self.removal_attempts.append(service)
            if self.failure == "delete_error" and service == verify.CLIENTS[0]:
                code = 2
            else:
                self.blocked.discard(service)
        elif action == "-C":
            code = 0 if service in self.blocked else 1
        return subprocess.CompletedProcess([], code, "", "injected error" if code == 2 else "")

    @contextlib.contextmanager
    def attached(self):
        ok = subprocess.CompletedProcess([], 0, "", "")
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(verify, "compose", self.compose))
            stack.enter_context(patch.object(verify, "firewall", self.firewall))
            stack.enter_context(patch.object(verify, "node", return_value=ok))
            stack.enter_context(patch.object(verify, "relay_ping", return_value="DERP(ap-east-1)"))
            stack.enter_context(patch.object(verify, "assert_ssh", return_value="tailnet"))
            # 本组只验证 DERP 故障清理，出口 HTTP 的选择恢复另有独立测试。
            stack.enter_context(patch.object(verify, "container_network", return_value={
                "IPAddress": "198.18.0.2", "GlobalIPv6Address": "2001:db8:77::2"}))
            stack.enter_context(patch.object(verify, "selected_exit", return_value=contextlib.nullcontext()))
            stack.enter_context(patch.object(verify, "assert_exit_origin", return_value={}))
            stack.enter_context(patch.object(verify, "ssh", side_effect=lambda *a, **k:
                subprocess.CompletedProcess([], 0 if self.relay else 255, "", "")))
            stack.enter_context(patch.object(verify, "passed"))
            yield


class FaultCleanupTests(unittest.TestCase):
    states = {name: {"ips": [f"100.77.0.{i}"]}
              for i, name in enumerate(verify.NODES, 1)}

    def exercise(self, network):
        with network.attached():
            with self.assertRaises((RuntimeError, subprocess.TimeoutExpired)):
                verify.relayed(self.states)

    def test_successful_fault_cycle_only_stops_relays(self):
        network = Network(None)
        with network.attached():
            verify.relayed(self.states)
        self.assertTrue(network.relay)
        self.assertFalse(network.blocked)

    def test_stop_timeout_after_mutation_still_restores_relay(self):
        network = Network("stop_timeout")
        self.exercise(network)
        self.assertTrue(network.relay)
        self.assertFalse(network.blocked)

    def test_insert_timeout_after_mutation_still_removes_rule(self):
        network = Network("insert_timeout")
        self.exercise(network)
        self.assertFalse(network.blocked)

    def test_relay_cleanup_failure_does_not_skip_firewall_cleanup(self):
        network = Network("start_timeout")
        self.exercise(network)
        self.assertEqual(set(network.removal_attempts), set(verify.CLIENTS))
        self.assertFalse(network.blocked)

    def test_rule_removal_error_is_reported_and_other_client_is_restored(self):
        network = Network("delete_error")
        self.exercise(network)
        self.assertEqual(set(network.removal_attempts), set(verify.CLIENTS))
        self.assertEqual(network.blocked, {verify.CLIENTS[0]})

    def test_restart_failure_resumes_the_temporarily_stopped_node(self):
        for failed_action, auto_resumed in (('down', False), ('restart', False), ('restart', True)):
            with self.subTest(failed_action=failed_action, auto_resumed=auto_resumed):
                stopped = False
                started = False

                def node(service, *args, **kwargs):
                    nonlocal stopped
                    if args == ('tailscale', 'down'):
                        stopped = True
                        if failed_action == 'down':
                            raise subprocess.TimeoutExpired('down', 45)
                    elif args == ('timeout', '45', 'tailscale', 'up'):
                        self.assertTrue(started)
                        self.assertTrue(stopped, 'do not reconfigure an already Running node')
                        stopped = False
                    elif args == ('python3', '/app/node_status.py'):
                        return subprocess.CompletedProcess([], 0, 'Stopped' if stopped else 'Running', '')
                    return subprocess.CompletedProcess([], 0, '', '')

                def compose(action, *args, **kwargs):
                    nonlocal started, stopped
                    if action == 'restart':
                        raise subprocess.TimeoutExpired('restart', 60)
                    if action == 'start':
                        started = True
                        if auto_resumed:
                            stopped = False

                with patch.object(verify, 'node', node), patch.object(verify, 'compose', compose):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        verify.restart(self.states)
                self.assertFalse(stopped)

    def test_derp_map_restores_from_host_when_control_cannot_exec(self):
        for restart_fails in (False, True):
            with self.subTest(restart_fails=restart_fails), tempfile.TemporaryDirectory() as directory:
                bundle = Path(directory)
                path = bundle / 'config/derp.yaml'
                path.parent.mkdir()
                original = 'regions:\n  910:\n    regionname: Hong Kong\n'
                path.write_text(original)
                path.chmod(0o640)
                before = path.stat()
                stopped = False
                execute = verify.command

                def command(args, **kwargs):
                    if args[0] == 'docker':
                        return subprocess.CompletedProcess(args, 0, 'started-at', '')
                    return execute(args, **kwargs)

                def node(service, *args, **kwargs):
                    nonlocal stopped
                    if stopped:
                        raise RuntimeError('control is stopped')
                    if args[0] == 'cat':
                        output = path.read_text()
                    elif args[0] == 'python3':
                        execute([verify.sys.executable, '-c', args[2], str(path), args[4]])
                        stopped = True
                        raise RuntimeError('control stopped after Map write')
                    else:
                        output = json.dumps({'Regions': {'910': {'RegionName': 'Hong Kong'}}})
                    return subprocess.CompletedProcess(args, 0, output, '')

                def compose(action, *args, **kwargs):
                    nonlocal stopped
                    if action == 'start':
                        if restart_fails:
                            raise RuntimeError('restart failed')
                        stopped = False
                    return subprocess.CompletedProcess(args, 0, 'control-container', '')

                expected = 'control stopped after Map write'
                if restart_fails:
                    expected += '; DERP map cleanup failed: restart failed'
                with patch.object(verify.CONFIG, 'bundle', bundle), \
                        patch.object(verify, 'node', node), patch.object(verify, 'compose', compose), \
                        patch.object(verify, 'command', command):
                    with self.assertRaisesRegex(RuntimeError, expected):
                        verify.control_reload(self.states)
                self.assertEqual(path.read_text(), original)
                after = path.stat()
                self.assertEqual((after.st_uid, after.st_gid, after.st_mode),
                                 (before.st_uid, before.st_gid, before.st_mode))
                self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
