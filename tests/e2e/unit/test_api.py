"""Management smoke credentials must be revoked after success and failure."""

import json
import subprocess
import unittest
from unittest.mock import patch

import verify
import api_smoke


class ApiKeyCleanupTests(unittest.TestCase):
    def test_temporary_key_is_sent_over_stdin_and_expired_even_after_failure(self):
        verify.configure(verify.ROOT / "environment.example.yaml")
        for failed in (False, True):
            with self.subTest(failed=failed):
                active = set()
                key = "hskey-api-abcde-gh_123-" + "s" * 64

                def node(service, *args, **kwargs):
                    self.assertNotIn(key, args)
                    if args[:3] == ("headscale", "apikeys", "create"):
                        active.add("abcde-gh_123")
                        return subprocess.CompletedProcess(args, 0, json.dumps(key), "")
                    if args[:3] == ("headscale", "apikeys", "expire"):
                        self.assertEqual(args[3:], ("--prefix", "abcde-gh_123"))
                        active.remove("abcde-gh_123")
                        return subprocess.CompletedProcess(args, 0, "", "")
                    self.assertEqual(service, verify.CLIENTS[0])
                    self.assertEqual(kwargs["input"], key + "\n")
                    if failed:
                        raise RuntimeError("API smoke failed")
                    return subprocess.CompletedProcess(args, 0, '{"checks":["REST lifecycle"]}', "")

                with patch.object(verify, "node", side_effect=node), patch.object(verify, "passed"):
                    if failed:
                        with self.assertRaisesRegex(RuntimeError, "API smoke failed"):
                            verify.management_api()
                    else:
                        verify.management_api()
                self.assertFalse(active)


class ResourceCleanupTests(unittest.TestCase):
    def exercise(self, failure=None):
        resources = {
            "/node": {"10": {"id": "10", "user": {"id": "1"}},
                      "20": {"id": "20", "user": {"id": "2"}}},
            "/preauthkey": {"11": {"id": "11", "user": {"id": "1"}},
                            "21": {"id": "21", "user": {"id": "2"}}},
            "/user": {"1": {"id": "1"}, "2": {"id": "2"}},
        }

        def api(method, path):
            if method == "GET":
                field = {"/node": "nodes", "/preauthkey": "preAuthKeys", "/user": "users"}[path.split("?")[0]]
                if path == "/user?id=2":
                    return {field: [resources["/user"]["2"]] if "2" in resources["/user"] else []}
                return {field: list(resources[path].values())}
            self.assertEqual(method, "DELETE")
            if "?id=" in path:
                group, identifier = path.split("?id=")
            else:
                group, identifier = path.rsplit("/", 1)
            self.assertEqual(identifier, "21" if group == "/preauthkey" else "20" if group == "/node" else "2")
            if group == "/node" and failure == "error":
                raise RuntimeError("Delete failed")
            if group != "/node" or failure != "ignored":
                resources[group].pop(identifier)
            return {}

        if failure:
            with self.assertRaisesRegex(AssertionError, "REST cleanup failed"):
                api_smoke.cleanup(api, "2")
        else:
            api_smoke.cleanup(api, "2")
        self.assertEqual(resources["/user"], {"1": {"id": "1"}})
        self.assertEqual(resources["/preauthkey"], {"11": {"id": "11", "user": {"id": "1"}}})
        self.assertIn("10", resources["/node"])
        if failure is None:
            self.assertEqual(set(resources["/node"]), {"10"})

    def test_cleanup_only_removes_resources_owned_by_the_temporary_user(self):
        self.exercise()

    def test_failed_node_delete_still_attempts_key_and_user_cleanup(self):
        self.exercise("error")

    def test_success_response_without_deletion_is_reported(self):
        self.exercise("ignored")


if __name__ == "__main__":
    unittest.main()
