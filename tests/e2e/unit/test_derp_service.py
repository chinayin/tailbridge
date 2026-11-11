"""Isolated DERP test resources must be cleaned up after Docker CLI failures."""

import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import derp_service


class DerpServiceCleanupTests(unittest.TestCase):
    def test_create_timeout_removes_possible_container_and_preserves_original_error(self):
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
        with patch.object(derp_service, "run", side_effect=run), \
                patch.object(derp_service, "load_config", return_value=config), \
                patch.object(derp_service.sys, "argv", ["derp_service.py"]):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                derp_service.main()
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(attempted), 1)
        self.assertEqual(removed, [(attempted[0], False)])


if __name__ == "__main__":
    unittest.main()
