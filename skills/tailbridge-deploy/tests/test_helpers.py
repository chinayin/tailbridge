import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

import yaml


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class HelpersTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def run_helper(self, name, *args, data=None):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / name), *map(str, args)],
            input=data, text=True, capture_output=True,
        )

    def test_state_round_trip_and_private_permissions(self):
        target = self.root / "state.yaml"
        target.write_text("old: true\n")
        target.chmod(0o644)
        data = {"status": "ready", "id": "001", "checks": {"tls": True}, "note": "已验证"}
        result = self.run_helper("write_state.py", target, data=json.dumps(data))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(yaml.safe_load(target.read_text()), data)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result.stdout, "")

    def test_invalid_state_keeps_previous_result(self):
        target = self.root / "state.yaml"
        target.write_text("status: previous\n")
        result = self.run_helper("write_state.py", target, data='{"credential":"do-not-echo"')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(), "status: previous\n")
        self.assertNotIn("do-not-echo", result.stdout + result.stderr)
        self.assertEqual(list(self.root.iterdir()), [target])

    def test_state_rejects_non_yaml_destination(self):
        target = self.root / "state.json"
        result = self.run_helper("write_state.py", target, data='{"status":"ready"}')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())

    def config(self, hostname="relay", cert_path=None, roles=("control", "derp")):
        cert_dir = self.root / "control with ' quote"
        relay_dir = self.root / "relay with space"
        cert_dir.mkdir(exist_ok=True)
        relay_dir.mkdir(exist_ok=True)
        config = {
            "roles": list(roles),
            "acme": {"cert_name": "tb-test-01"},
            "control": {
                "TLS_CERT_PATH": str(cert_path or cert_dir / "fullchain.pem"),
                "TLS_KEY_PATH": str(cert_dir / "privkey.pem"),
            },
            "derp": {"DERP_HOSTNAME": hostname, "DERP_CERT_DIR": str(relay_dir)},
        }
        path = self.root / "deploy.yaml"
        path.write_text(yaml.safe_dump(config))
        return path, config

    def hook(self, roles=("control", "derp")):
        config_path, config = self.config(roles=roles)
        lineage = self.root / "lineage with ' quote"
        lineage.mkdir()
        (lineage / "fullchain.pem").write_text("certificate fixture")
        (lineage / "privkey.pem").write_text("private fixture")
        result = self.run_helper("render_cert_hook.py", config_path, "--lineage", lineage)
        self.assertEqual(result.returncode, 0, result.stderr)
        hook = self.root / "hook.sh"
        hook.write_text(result.stdout)
        return hook, config, lineage

    def test_hook_ignores_other_lineages(self):
        hook, config, lineage = self.hook()
        env = dict(os.environ, RENEWED_LINEAGE=str(lineage) + "-other")
        result = subprocess.run(["sh", str(hook)], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(Path(config["control"]["TLS_CERT_PATH"]).exists())

    def execute_hook(self, hook, lineage, fail_service=""):
        commands = self.root / "commands"
        commands.mkdir()
        install = commands / "install"
        install.write_text(
            "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -c " + shlex.quote(
                "import os, shutil, sys; shutil.copyfile(sys.argv[-2], sys.argv[-1]); "
                "os.chmod(sys.argv[-1], int(sys.argv[sys.argv.index('-m') + 1], 8))"
            ) + ' "$@"\n'
        )
        systemctl = commands / "systemctl"
        systemctl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALL_LOG"\n[ "$2" != "$FAIL_SERVICE" ]\n')
        install.chmod(0o755)
        systemctl.chmod(0o755)
        calls = self.root / "calls.txt"
        env = dict(os.environ, RENEWED_LINEAGE=str(lineage), CALL_LOG=str(calls), FAIL_SERVICE=fail_service,
                   PATH=str(commands) + os.pathsep + os.environ["PATH"])
        result = subprocess.run(["sh", str(hook)], env=env, capture_output=True, text=True)
        return result, calls

    def test_hook_publishes_all_files_and_restarts_services(self):
        hook, config, lineage = self.hook()
        result, calls = self.execute_hook(hook, lineage)
        self.assertEqual(result.returncode, 0, result.stderr)
        outputs = [
            (Path(config["control"]["TLS_CERT_PATH"]), "certificate fixture", 0o644),
            (Path(config["control"]["TLS_KEY_PATH"]), "private fixture", 0o640),
            (Path(config["derp"]["DERP_CERT_DIR"]) / "relay.crt", "certificate fixture", 0o644),
            (Path(config["derp"]["DERP_CERT_DIR"]) / "relay.key", "private fixture", 0o640),
        ]
        for path, content, mode in outputs:
            self.assertEqual(path.read_text(), content)
            self.assertEqual(path.stat().st_mode & 0o777, mode)
        self.assertEqual(calls.read_text().splitlines(), [
            "try-restart headscale.service", "try-restart derper.service",
        ])

    def test_hook_attempts_both_restarts_and_reports_failure(self):
        hook, _, lineage = self.hook()
        result, calls = self.execute_hook(hook, lineage, fail_service="headscale.service")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls.read_text().splitlines(), [
            "try-restart headscale.service", "try-restart derper.service",
        ])

    def test_relay_only_host_publishes_and_restarts_only_derper(self):
        hook, config, lineage = self.hook(roles=("derp",))
        result, calls = self.execute_hook(hook, lineage)
        self.assertEqual(result.returncode, 0, result.stderr)
        relay = Path(config["derp"]["DERP_CERT_DIR"])
        self.assertEqual(sorted(path.name for path in relay.iterdir()), ["relay.crt", "relay.key"])
        self.assertFalse(Path(config["control"]["TLS_CERT_PATH"]).exists())
        self.assertEqual(calls.read_text().splitlines(), ["try-restart derper.service"])

    def test_lineage_defaults_to_the_configured_certificate_name(self):
        path, _ = self.config()
        result = self.run_helper("render_cert_hook.py", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/etc/letsencrypt/live/tb-test-01 ]", result.stdout)
        for name in ("../escape", "", None):
            with self.subTest(name=name):
                config = yaml.safe_load(path.read_text())
                config["acme"]["cert_name"] = name
                path.write_text(yaml.safe_dump(config))
                result = self.run_helper("render_cert_hook.py", path)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_standalone_host_renders_the_same_hook_as_control_and_derp(self):
        lineage = str(self.root / "lineage")
        hooks = []
        for roles in (("control", "derp"), ("control", "derp", "node")):
            path, _ = self.config(roles=roles)
            result = self.run_helper("render_cert_hook.py", path, "--lineage", lineage)
            self.assertEqual(result.returncode, 0, result.stderr)
            hooks.append(result.stdout)
        self.assertEqual(hooks[0], hooks[1])

    def test_hook_rejects_missing_or_unknown_roles(self):
        for roles in ((), ("node",), ("derp", "derp"), ("control", "webui")):
            with self.subTest(roles=roles):
                path, _ = self.config(roles=roles)
                result = self.run_helper("render_cert_hook.py", path, "--lineage", self.root)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_hook_rejects_hostname_path_traversal(self):
        path, _ = self.config(hostname="../escape")
        result = self.run_helper("render_cert_hook.py", path)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_hook_rejects_relative_destination(self):
        path, _ = self.config(cert_path="relative.pem")
        result = self.run_helper("render_cert_hook.py", path)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_hook_rejects_overlapping_cert_and_key(self):
        path, config = self.config()
        config["control"]["TLS_KEY_PATH"] = config["control"]["TLS_CERT_PATH"]
        path.write_text(yaml.safe_dump(config))
        result = self.run_helper("render_cert_hook.py", path)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_hook_rejects_parent_components_that_alias_destinations(self):
        path, config = self.config()
        certificate = Path(config["control"]["TLS_CERT_PATH"])
        config["control"]["TLS_KEY_PATH"] = str(certificate.parent / "child" / ".." / certificate.name)
        path.write_text(yaml.safe_dump(config))
        result = self.run_helper("render_cert_hook.py", path)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
