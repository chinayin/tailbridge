"""Isolated systemd test resources must be cleaned up after Docker CLI failures."""

from contextlib import redirect_stderr
import io
import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import derp_service
import control_node_service


class SystemdServiceCleanupTests(unittest.TestCase):
    def test_create_timeout_removes_possible_container_and_preserves_original_error(self):
        for module in (derp_service, control_node_service):
            with self.subTest(module=module.__name__):
                self.check_create_timeout(module)

    def check_create_timeout(self, module):
        attempted = []
        removed = []
        failure = subprocess.TimeoutExpired(["docker", "create"], 45)

        def run(args, **kwargs):
            if args[:2] == ["docker", "create"]:
                attempted.append(args[args.index("--name") + 1])
                raise failure
            if args[:2] == ["docker", "rm"]:
                removed.append((args[-1], kwargs.get("check", True)))
                return subprocess.CompletedProcess(args, 1, "", "No such container")
            output = "source-container" if args[:2] == ["docker", "compose"] else "1.104.5-ERR-BuildInfo"
            return subprocess.CompletedProcess(args, 0, output, "")

        config = SimpleNamespace(compose=["docker", "compose"], env={}, project="cleanup-test")
        with patch.object(module, "run", side_effect=run), \
                patch.object(module, "load_config", return_value=config), \
                patch.object(module.sys, "argv", [module.__name__ + ".py"]):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                module.main()
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(attempted), 1)
        self.assertEqual(removed, [(attempted[0], False)])

    def test_original_failure_survives_diagnostic_and_cleanup_failures(self):
        for module, service in ((derp_service, derp_service.ServiceTest),
                                (control_node_service, control_node_service.ControlNodeTest)):
            with self.subTest(module=module.__name__):
                failure = RuntimeError("original service verification failure")
                removed = []
                def run(args, **kwargs):
                    if args[:2] == ["docker", "rm"]:
                        removed.append(args[-1])
                        raise RuntimeError("cleanup failed")
                    if "journalctl" in args or args[:2] == ["docker", "logs"]:
                        raise subprocess.TimeoutExpired(args, 45)
                    return subprocess.CompletedProcess(args, 0, "1.104.5-ERR-BuildInfo", "")
                config = SimpleNamespace(compose=["docker", "compose"], env={}, project="cleanup-test")
                with patch.object(module, "run", side_effect=run), \
                        patch.object(module, "load_config", return_value=config), \
                        patch.object(module.sys, "argv", [module.__name__ + ".py"]), \
                        patch.object(service, "verify", side_effect=failure), redirect_stderr(io.StringIO()):
                    with self.assertRaises(RuntimeError) as caught:
                        module.main()
                self.assertIs(caught.exception, failure)
                self.assertEqual(len(removed), 1)

    def test_proxy_and_registration_credentials_are_passed_only_on_stdin(self):
        service = control_node_service.ControlNodeTest("test-container", "http://" + "user:proxy-secret" + "@proxy.test")
        with patch.object(control_node_service, "run") as execute:
            service.install("node", authkey="registration-secret")
        args = execute.call_args.args[0]
        values = json.loads(execute.call_args.kwargs["input"])
        self.assertNotIn("proxy-secret", " ".join(args))
        self.assertNotIn("registration-secret", " ".join(args))
        self.assertEqual(values["PROXY"], service.proxy)
        self.assertEqual(values["NODE_AUTHKEY"], "registration-secret")

    def test_running_node_must_reconnect_to_control_before_reporting_ready(self):
        service = control_node_service.ControlNodeTest("test-container")
        status = subprocess.CompletedProcess([], 0, json.dumps({"BackendState":"Running", "Self":{"ID":"7"}}), "")
        with patch.object(service, "active"), patch.object(service, "exec", return_value=status), \
                patch.object(service, "headscale", side_effect=[[{"id":7,"online":False}],
                                                                [{"id":7,"online":True}]]) as headscale:
            service.node_ready()
        self.assertEqual(headscale.call_count, 2)


if __name__ == "__main__":
    unittest.main()
