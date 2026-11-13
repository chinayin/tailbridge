"""出口授权及失败后的选择恢复；真实转发另由 Compose E2E 验证。"""

import json
import subprocess
import unittest
from unittest.mock import patch

import e2e
import verify
from settings import load_config, ROOT


class ExitPolicyTests(unittest.TestCase):
    def test_policy_uses_actual_node_addresses_and_only_authorizes_client_a(self):
        config = load_config(ROOT / "environment.example.yaml")
        states = {
            "client-macbook": {"ips": ["100.77.0.3", "fd7a:115c:a1e0:77::3"]},
            "client-macmini": {"ips": ["100.77.0.2"]},
            "client-server": {"ips": ["100.77.0.1"]},
        }
        policy = e2e.exit_policy(config, states)
        self.assertEqual(policy["grants"], [
            {"src": ["*"], "dst": ["100.77.0.3", "fd7a:115c:a1e0:77::3", "100.77.0.2", "100.77.0.1"],
             "ip": ["*"]},
            {"src": ["100.77.0.3", "fd7a:115c:a1e0:77::3"],
             "dst": ["autogroup:internet"], "ip": ["*"]},
        ])


class SelectionCleanupTests(unittest.TestCase):
    def exercise(self, failure, *, lan=True):
        state = {"ExitNodeID": "", "ExitNodeIP": "", "ExitNodeAllowLANAccess": lan}

        def node(service, *args, **kwargs):
            self.assertEqual(service, "client-macbook")
            if args == ("tailscale", "debug", "prefs"):
                return subprocess.CompletedProcess(args, 0, json.dumps(state), "")
            self.assertEqual(args[:2], ("tailscale", "set"))
            selection = next(value.split("=", 1)[1] for value in args if value.startswith("--exit-node="))
            state["ExitNodeID"] = "1" if selection else ""
            state["ExitNodeAllowLANAccess"] = "--exit-node-allow-lan-access=true" in args
            if selection and failure == "selection_timeout":
                raise subprocess.TimeoutExpired(args, 10)
            if not selection and failure == "restore_failure":
                raise RuntimeError("restore failed")
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(verify, "node", side_effect=node):
            try:
                with verify.selected_exit("client-macbook", "100.77.0.1"):
                    self.assertTrue(state["ExitNodeAllowLANAccess"])
                    if failure == "body_failure":
                        raise AssertionError("HTTP failed")
            except (AssertionError, RuntimeError, subprocess.TimeoutExpired):
                if failure is None:
                    raise
                if failure == "restore_failure":
                    raise
            else:
                self.assertIsNone(failure)
        self.assertEqual(state, {"ExitNodeID": "", "ExitNodeIP": "", "ExitNodeAllowLANAccess": lan})

    def test_selection_is_restored_after_success(self):
        self.exercise(None)

    def test_selection_enables_lan_and_restores_disabled_preference(self):
        self.exercise(None, lan=False)

    def test_selection_timeout_after_mutation_still_restores_preferences(self):
        self.exercise("selection_timeout")

    def test_http_failure_still_restores_preferences(self):
        self.exercise("body_failure")

    def test_restore_failure_is_reported(self):
        with self.assertRaisesRegex(RuntimeError, "restore failed"):
            self.exercise("restore_failure")

    def test_preexisting_exit_selection_is_not_overwritten(self):
        result = subprocess.CompletedProcess([], 0, '{"ExitNodeID":"other"}', "")
        with patch.object(verify, "node", return_value=result) as node:
            with self.assertRaisesRegex(AssertionError, "already selected"):
                with verify.selected_exit("client-macbook", "100.77.0.1"):
                    self.fail("Should not enter the selection context")
            self.assertEqual(node.call_count, 1)


if __name__ == "__main__":
    unittest.main()
