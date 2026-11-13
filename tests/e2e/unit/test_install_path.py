"""E2E 必须在目标容器安装；本机不再渲染 Headscale 配置。"""

from pathlib import Path
import tempfile
import json
import os
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

import e2e
from settings import Settings, ROOT
from config import read_yaml


class InstallPathTests(unittest.TestCase):
    def test_e2e_settings_load_without_legacy_deployment_tree(self):
        with tempfile.TemporaryDirectory() as folder:
            isolated = Path(folder) / 'tests/e2e'
            isolated.mkdir(parents=True)
            for path in ROOT.glob('*.py'):
                shutil.copy2(path, isolated / path.name)
            shutil.copy2(ROOT / 'environment.example.yaml', isolated / 'environment.yaml')
            result = subprocess.run([sys.executable, '-I', '-c',
                                     'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); '
                                     'from settings import load_config; '
                                     'print(load_config(Path(sys.argv[1]) / "environment.yaml").fqdn("target"))',
                                     str(isolated)], cwd=folder, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'client-server.e2e.tb.test')

    def test_install_proxy_is_not_used_by_node_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'bash'
            path.write_text(f'#!{sys.executable}\nimport json, os, sys\n'
                            'from pathlib import Path\n'
                            'keys = ("PROXY", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY")\n'
                            'Path(os.environ["PROXY_TEST_LOG"] + Path(sys.argv[1]).stem).write_text('
                            'json.dumps({key: os.environ.get(key, "") for key in keys}))\n')
            path.chmod(0o755)
            env = {**os.environ, 'PATH': f'{root}:{os.environ["PATH"]}',
                   'PROXY_TEST_LOG': str(root / 'log-'), 'TB_INSTALL_PROXY': 'http://proxy.test:7890',
                   'PROXY': '', 'HTTP_PROXY': '', 'HTTPS_PROXY': '', 'http_proxy': '', 'https_proxy': '',
                   'NO_PROXY': '*'}
            subprocess.run(['/bin/bash', str(ROOT / 'start.sh'), 'node'], env=env, check=True)
            installer = json.loads((root / 'log-install').read_text())
            runtime = json.loads((root / 'log-node').read_text())
            self.assertEqual(installer['PROXY'], 'http://proxy.test:7890')
            self.assertEqual(installer['HTTPS_PROXY'], '')
            self.assertIn('installer-files', installer['NO_PROXY'])
            self.assertNotEqual(installer['NO_PROXY'], '*')
            self.assertEqual(runtime, {key: '*' if key == 'NO_PROXY' else '' for key in runtime})

    def test_node_waits_for_settled_backend_after_socket_appears(self):
        self.assertTrue((ROOT / 'node_status.py').exists(), 'bounded node status wait is required')
        import node_status
        responses = [subprocess.CompletedProcess([], 0, json.dumps({'BackendState': state}), '')
                     for state in ('NoState', 'Starting', 'Running')]
        with patch.object(node_status.subprocess, 'run', side_effect=responses), \
                patch.object(node_status.time, 'sleep'):
            self.assertEqual(node_status.wait_backend(), 'Running')

    def test_granted_policy_is_readable_by_headscale_service_user(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Settings(read_yaml(ROOT / 'environment.example.yaml'), folder)
            policy = config.bundle / 'config/policy.hujson'
            policy.parent.mkdir(parents=True)
            previous = '{"grants": []}\n'
            policy.write_text(previous)
            state = {'Self': {'ID': '1', 'TailscaleIPs': ['100.77.0.1']}}
            response = subprocess.CompletedProcess([], 0, json.dumps(state), '')
            published = []

            def compose(*args, **kwargs):
                if args == ('exec', '-T', 'control', 'python3', '/app/publish_policy.py'):
                    import publish_policy
                    publish_policy.write_policy(policy, kwargs['input'])
                    published.append(True)
                elif args[:5] == ('exec', '-T', 'control', 'headscale', 'policy'):
                    self.assertTrue(published, 'publish in the control filesystem before native validation')
                return response

            with patch.object(e2e, 'CONFIG', config), patch.object(e2e, 'GENERATED', config.generated), \
                    patch.object(e2e, 'compose', side_effect=compose), patch.object(e2e, 'headscale'), \
                    policy.open() as active_reader:
                e2e.configure_exit_node()
                self.assertEqual(active_reader.read(), previous, 'existing readers must see a complete policy')
            self.assertEqual(policy.stat().st_mode & 0o777, 0o644)
            self.assertEqual(len(json.loads(policy.read_text())['grants']), 2)

    def test_compose_uses_repository_source_without_generated_version_file(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Settings(read_yaml(ROOT / 'environment.example.yaml'), folder)
            self.assertNotIn('--env-file', config.compose)
            self.assertIn(str(ROOT / 'compose.yaml'), config.compose)
            self.assertEqual(config.env['TB_INSTALL_SOURCE'], str(ROOT.parents[1]))
            self.assertEqual(config.env.get('SERVER_URL'), 'https://hs.private.test')
            self.assertEqual(config.env.get('BASE_DOMAIN'), 'e2e.tb.test')
            self.assertEqual(config.env.get('DERP_HOSTNAME'), 'derp.private.test')

    def test_prepare_does_not_overwrite_live_control_config_or_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Settings(read_yaml(ROOT / 'environment.example.yaml'), folder)
            config.generated.mkdir(parents=True)
            (config.generated / 'ca.crt').write_text('test CA')
            (config.generated / 'ca.key').write_text('test key')
            for name in ('macbook.sshkey', 'macmini.sshkey', 'workstation.hostkey'):
                (config.generated / name).write_text('key')
                (config.generated / f'{name}.pub').write_text('ssh-ed25519 testkey')
            (config.bundle / 'secrets').mkdir(parents=True)
            (config.bundle / 'secrets/control_tls_key').write_text('key')
            (config.bundle / 'config').mkdir()
            policy = config.bundle / 'config/policy.hujson'
            policy.write_text('existing policy')
            with patch.object(e2e, 'CONFIG', config), patch.object(e2e, 'GENERATED', config.generated), \
                    patch.object(e2e, 'prepare_certificate'):
                e2e.prepare()
            self.assertEqual(policy.read_text(), 'existing policy')
            self.assertFalse((config.bundle / 'config/config.yaml').exists())
            self.assertFalse((config.bundle / '.env').exists())

    def test_prepare_exposes_ca_certificate_but_preserves_private_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Settings(read_yaml(ROOT / 'environment.example.yaml'), folder)
            previous_mask = os.umask(0o077)
            try:
                with patch.object(e2e, 'CONFIG', config), patch.object(e2e, 'GENERATED', config.generated):
                    e2e.prepare()
                    ca = config.generated / 'ca.crt'
                    key = config.generated / 'ca.key'
                    original = (ca.read_bytes(), key.read_bytes())
                    self.assertEqual(ca.stat().st_mode & 0o777, 0o644)
                    self.assertEqual(key.stat().st_mode & 0o777, 0o600)
                    ca.chmod(0o600)
                    e2e.prepare()
                    self.assertEqual(ca.stat().st_mode & 0o777, 0o644)
                    self.assertEqual(key.stat().st_mode & 0o777, 0o600)
                    self.assertEqual((ca.read_bytes(), key.read_bytes()), original)
            finally:
                os.umask(previous_mask)


if __name__ == '__main__':
    unittest.main()
