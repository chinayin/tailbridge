"""Execute documented remote installers without networking or elevation."""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
INSTALL_URL = 'https://raw.githubusercontent.com/chinayin/tailbridge/main/install.sh'
SERVER = {'SERVER_URL': 'https://tailbridge.example.net'}
NODE = {**SERVER, 'NODE_AUTHKEY': '<首次注册密钥>'}
EXAMPLES = {
    ('README.md', 0): (['--role', 'node'], {**SERVER, 'NODE_AUTHKEY': '<node-auth-key>'}),
    ('README.zh-CN.md', 0): (['--role', 'node'], {**SERVER, 'NODE_AUTHKEY': '<node-auth-key>'}),
    ('docs/INSTALL.md', 0): (['--help'], {}),
    ('docs/INSTALL.md', 1): (['--role', 'node'], {**NODE, 'PROXY': 'http://127.0.0.1:7890'}),
    ('docs/INSTALL.md', 2): (['--role', 'control', '--dry-run'], {
        **SERVER, 'BASE_DOMAIN': 'tb.example.net', 'ACME_EMAIL': 'ops@example.net',
        'TLS_HOSTNAME': 'tailbridge.example.net', 'DERP_REGION_ID': '910',
        'DERP_REGION_CODE': 'ap-east-1', 'DERP_REGION_NAME': 'Hong Kong',
        'DERP_NODE_NAME': 'tb-derp-hk-01', 'DERP_HOSTNAME': 'tb-derp-hk-01.example.net'}),
    ('docs/INSTALL.md', 3): (['--role', 'derp', '--dry-run'], {
        **SERVER, 'DERP_HOSTNAME': 'tb-derp-hk-01.example.net'}),
    ('docs/INSTALL.md', 4): (['--role', 'node'], NODE),
    ('docs/OPERATIONS.md', 0): (['--role', 'node'], {**SERVER, 'ADVERTISE_EXIT_NODE': 'true'}),
}


class InstallExampleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.env = {'PATH': f'{self.bin}:/usr/bin:/bin', 'TB_TEST_DIR': str(self.base)}
        self.stub('sudo', 'os.execvpe(sys.argv[1], sys.argv[1:], os.environ)')
        self.stub('tailscale', 'raise SystemExit(0)')
        observer = self.stub('observe-installer', '''
Path(os.environ['TB_TEST_DIR'], 'installer.json').write_text(json.dumps({
    'args': sys.argv[1:], 'env': dict(os.environ)}))
raise SystemExit(int(os.environ.get('TB_TEST_INSTALL_STATUS', '0')))
''')
        (self.base / 'download.sh').write_text(
            f'exec {shlex.quote(sys.executable)} {shlex.quote(str(observer))} "$@"\n')
        self.stub('curl', '''
base = Path(os.environ['TB_TEST_DIR'])
(base / 'curl.json').write_text(json.dumps({'args': sys.argv[1:], 'env': dict(os.environ)}))
status = int(os.environ.get('TB_TEST_CURL_STATUS', '0'))
if status:
    raise SystemExit(status)
sys.stdout.write((base / 'download.sh').read_text())
''')

    def stub(self, name, code):
        path = self.bin / name
        path.write_text(f'#!{sys.executable}\nimport os, sys, json\nfrom pathlib import Path\n{code}\n')
        path.chmod(0o755)
        return path

    def examples(self):
        examples = {}
        for name in dict.fromkeys(name for name, _ in EXAMPLES):
            blocks = re.findall(r'^```bash\n(.*?)^```', (ROOT / name).read_text(), re.M | re.S)
            installs = [block for block in blocks if 'curl ' in block and 'install.sh' in block]
            examples.update({(name, index): block for index, block in enumerate(installs)})
        self.assertEqual(examples.keys(), EXAMPLES.keys(), 'every remote install needs an execution case')
        return examples.items()

    def run_example(self, command, **changes):
        for name in ('curl.json', 'installer.json'):
            (self.base / name).unlink(missing_ok=True)
        return subprocess.run(['/bin/bash', '-c', command], cwd=self.base,
                              env={**self.env, **changes}, text=True,
                              capture_output=True, timeout=10)

    def test_download_failure_is_not_reported_as_success(self):
        for name, command in self.examples():
            for status in ('6', '22'):
                with self.subTest(example=name, curl_status=status):
                    result = self.run_example(command, TB_TEST_CURL_STATUS=status)
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertFalse((self.base / 'installer.json').exists())

    def test_installer_failure_is_not_reported_as_success(self):
        for name, command in self.examples():
            with self.subTest(example=name):
                result = self.run_example(command, TB_TEST_INSTALL_STATUS='37')
                self.assertEqual(result.returncode, 37, result.stderr)

    def test_success_preserves_arguments_and_shared_environment(self):
        for name, command in self.examples():
            with self.subTest(example=name):
                result = self.run_example(command)
                self.assertEqual(result.returncode, 0, result.stderr)
                curl = json.loads((self.base / 'curl.json').read_text())
                installer = json.loads((self.base / 'installer.json').read_text())
                args, expected_env = EXAMPLES[name]
                self.assertEqual(installer['args'], args)
                self.assertEqual(curl['args'][-1], INSTALL_URL)
                for key, value in expected_env.items():
                    self.assertEqual(curl['env'].get(key), value, key)
                    self.assertEqual(installer['env'].get(key), value, key)
                if 'PROXY' in expected_env:
                    self.assertEqual(curl['args'][curl['args'].index('-x') + 1], expected_env['PROXY'])


if __name__ == '__main__':
    unittest.main()
