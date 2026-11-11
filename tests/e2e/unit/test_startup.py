"""首次镜像拉取与构建可超过两分钟，普通查询仍应有短超时。"""

import subprocess
import unittest
from unittest.mock import call, patch

import e2e
from settings import load_config, ROOT


class StartupTests(unittest.TestCase):
    def test_cold_start_waits_for_runtime_installation_beyond_five_minutes(self):
        elapsed = [0]

        def sleep(seconds):
            elapsed[0] += seconds

        def headscale(*args):
            if elapsed[0] < 360:
                raise RuntimeError('Official package is still downloading')
            return []

        with patch.object(e2e, 'CONFIG', load_config(ROOT / 'environment.example.yaml')), \
                patch.object(e2e, 'prepare'), patch.object(e2e, 'compose'), \
                patch.object(e2e, 'headscale', side_effect=headscale), \
                patch.object(e2e.time, 'sleep', side_effect=sleep), \
                patch.object(e2e.time, 'monotonic', side_effect=lambda: elapsed[0]), \
                patch.object(e2e, 'synchronize_names'), patch.object(e2e, 'verify_initial_policy'), \
                patch.object(e2e, 'configure_exit_node'):
            e2e.up()

    def test_bootstrap_creates_or_reuses_the_fixed_test_user(self):
        user = {"id": 7, "name": "e2e"}
        other = {"id": 1, "name": "poc"}
        for existing in (False, True):
            responses = ([[other, user]] if existing else [[other], None, [other, user]])
            responses += [{"key": "test-only"}] * 3
            with self.subTest(existing=existing), \
                    patch.object(e2e, "CONFIG", load_config(ROOT / "environment.example.yaml")), \
                    patch.object(e2e, "prepare"), patch.object(e2e, "compose"), \
                    patch.object(e2e, "private_file"), patch.object(e2e, "synchronize_names"), \
                    patch.object(e2e, "configure_exit_node"), patch.object(e2e, "verify_initial_policy"), \
                    patch.object(e2e, "headscale", side_effect=responses) as headscale:
                e2e.up(bootstrap=True)
                self.assertEqual(call("users", "create", "e2e") in headscale.call_args_list, not existing)
                keys = [entry for entry in headscale.call_args_list if entry.args[0] == "preauthkeys"]
                self.assertEqual(keys, [call("preauthkeys", "create", "--user", "7", "--expiration", "1h")] * 3)

    def test_cold_start_allows_slow_image_operations(self):
        def compose(*args, timeout=120, **kwargs):
            # 用耗时边界代替真实下载，验证编排给慢速操作留出的时间。
            if args[0] in ("build", "up") and timeout < 180:
                raise subprocess.TimeoutExpired(args, timeout)
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(e2e, "CONFIG", load_config(ROOT / "environment.example.yaml")), \
                patch.object(e2e, "prepare"), patch.object(e2e, "compose", side_effect=compose), \
                patch.object(e2e, "headscale", return_value=[]), patch.object(e2e, "synchronize_names"), \
                patch.object(e2e, "configure_exit_node"), patch.object(e2e, "verify_initial_policy"):
            e2e.up()


if __name__ == "__main__":
    unittest.main()
