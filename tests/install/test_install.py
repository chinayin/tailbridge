"""安装器的文件行为；外部系统只在命令边界替换，不连接真实服务。"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / 'install.sh').is_file(), 'root install.sh must exist')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.prefix = self.base / 'root'
        self.templates = self.base / 'files'
        shutil.copytree(ROOT / 'files', self.templates)
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.stub('uname', 'print("Linux")')
        self.stub('dpkg', 'print("arm64")')
        self.stub('apt-get', 'raise SystemExit("unexpected apt-get")')
        self.stub('timeout', 'import subprocess; raise SystemExit(subprocess.call(sys.argv[2:]))')
        self.env = {**os.environ, 'PATH': f'{self.bin}:{os.environ["PATH"]}',
                    'TB_PREFIX': str(self.prefix), 'TB_OFFLINE': '1',
                    'TB_TEMPLATE_DIR': str(self.templates), 'INSTALL_BASE_URL': 'https://install.example.test',
                    'SERVER_URL': 'https://control.example.test',
                    'BASE_DOMAIN': 'tail.example.test', 'ACME_EMAIL': '',
                    'TLS_HOSTNAME': '', 'TLS_CERT_PATH': '/test/cert.pem',
                    'TLS_KEY_PATH': '/test/key.pem', 'DERP_HOSTNAME': 'derp.example.test',
                    'DERP_IPV4': '', 'DERP_PORT': '8443', 'ADVERTISE_EXIT_NODE': '',
                    'DERP_REGION_ID': '910', 'DERP_REGION_CODE': 'ap-east-1',
                    'DERP_REGION_NAME': 'Hong Kong', 'DERP_NODE_NAME': 'tb-derp-hk-01'}

    def stub(self, name, code):
        path = self.bin / name
        path.write_text(f'#!{sys.executable}\nimport os, sys, json\nfrom pathlib import Path\n{code}\n')
        path.chmod(0o755)

    def run_install(self, *args, role='control', changes=None, expected=0):
        env = {**self.env, **(changes or {})}
        result = subprocess.run(['sh', str(ROOT / 'install.sh'), '--role', role, *args],
                                env=env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stderr)
        self.assertEqual(result.stdout, '')
        return result

    def config(self, name='config.yaml'):
        return self.prefix / 'etc/headscale' / name

    def test_templates_and_deny_policy(self):
        self.run_install()
        self.assertIn('server_url: "https://control.example.test"', self.config().read_text())
        self.assertIn('base_domain: "tail.example.test"', self.config().read_text())
        self.assertNotIn('${', self.config().read_text())
        self.assertEqual(json.loads(self.config('policy.hujson').read_text()), {'grants': []})
        self.assertIn('urls: []', self.config().read_text())

    def test_environment_named_placeholders_render_required_and_optional_values(self):
        (self.templates / 'config.yaml').write_text('server_url: "${SERVER_URL}"\ndns:\n  base_domain: "${BASE_DOMAIN}"\nacme_email: "${ACME_EMAIL}"\n')
        (self.templates / 'derp.yaml').write_text('hostname: "${DERP_HOSTNAME}"\nipv4: "${DERP_IPV4}"\nderpport: ${DERP_PORT}\n')
        self.run_install()
        self.assertEqual(self.config().read_text(), 'server_url: "https://control.example.test"\ndns:\n  base_domain: "tail.example.test"\nacme_email: ""\n')
        self.assertEqual(self.config('derp.yaml').read_text(), 'hostname: "derp.example.test"\nipv4: ""\nderpport: 8443\n')

    def test_overwrite_requires_force_and_same_content_is_idempotent(self):
        self.run_install()
        self.run_install()
        self.config('policy.hujson').write_text('{"grants":[{"src":["*"],"dst":["*"],"ip":["*"]}]}')
        before = {p.name: p.read_bytes() for p in self.config().parent.iterdir()}
        result = self.run_install(expected=1)
        self.assertIn('--force', result.stderr)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.config().parent.iterdir()})
        result = self.run_install('--force')
        self.assertIn('policy.hujson', result.stderr)
        self.assertEqual(json.loads(self.config('policy.hujson').read_text()), {'grants': []})

    def test_derp_region_and_node_render_from_environment(self):
        for identifier, code, label, name in (
                ('900', 'home', 'Home', 'tb-derp-home'),
                ('901', 'work', 'Work', 'tb-derp-work'),
                ('910', 'ap-east-1', '香港 Hong Kong', 'tb-derp-hk-01')):
            with self.subTest(region=code):
                self.run_install('--force', changes={'DERP_REGION_ID': identifier,
                    'DERP_REGION_CODE': code, 'DERP_REGION_NAME': label, 'DERP_NODE_NAME': name})
                rendered = self.config('derp.yaml').read_text()
                self.assertIn(f'  {identifier}:\n    regionid: {identifier}\n', rendered)
                self.assertEqual(rendered.count(f'regionid: {identifier}\n'), 2)
                self.assertIn(f'regioncode: "{code}"', rendered)
                self.assertIn(f'regionname: "{label}"', rendered)
                self.assertIn(f'name: "{name}"', rendered)
                self.assertNotIn('${', rendered)

    def test_derp_port_defaults_to_https_and_accepts_override(self):
        self.env.pop('DERP_PORT')
        for value, expected in ((None, 443), ('', 443), ('8443', 8443)):
            with self.subTest(value=value):
                self.run_install('--force', changes={} if value is None else {'DERP_PORT': value})
                self.assertIn(f'derpport: {expected}\n', self.config('derp.yaml').read_text())
        for value in ('0', '65536', 'https'):
            with self.subTest(value=value):
                self.run_install('--force', changes={'DERP_PORT': value}, expected=1)
                self.assertIn('derpport: 8443\n', self.config('derp.yaml').read_text())

    def test_default_derp_port_does_not_overwrite_existing_map(self):
        self.run_install()
        before = self.config('derp.yaml').read_bytes()
        result = self.run_install(changes={'DERP_PORT': ''}, expected=1)
        self.assertIn('--force', result.stderr)
        self.assertEqual(self.config('derp.yaml').read_bytes(), before)

    def test_derp_region_inputs_are_required_before_writing(self):
        for field in ('DERP_REGION_ID', 'DERP_REGION_CODE', 'DERP_REGION_NAME', 'DERP_NODE_NAME'):
            with self.subTest(field=field):
                result = self.run_install(changes={field: ''}, expected=2)
                self.assertIn(field, result.stderr)
                self.assertFalse(self.prefix.exists())

    def test_derp_region_invalid_inputs_are_rejected_before_writing(self):
        cases = [('DERP_REGION_ID', value) for value in ('0', '-1', '0910', '65536', '1e3')]
        cases += [('DERP_REGION_CODE', value) for value in ('HK', 'ap_east_1', '9hk', 'a'*33)]
        cases += [('DERP_NODE_NAME', value) for value in ('../relay', 'UPPER', 'a'*49)]
        cases += [('DERP_REGION_NAME', value) for value in ('   ', 'bad"name', '${SERVER_URL}', 'bad\nname')]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.run_install(changes={field: value}, expected=1)
                self.assertFalse(self.prefix.exists())

    def test_derp_region_change_preserves_existing_map_without_force(self):
        self.run_install()
        self.config('derp.yaml').write_text(self.config('derp.yaml').read_text() +
            '  900:\n    regionid: 900\n    regioncode: home\n    regionname: Home\n'
            '    nodes:\n      - name: tb-derp-home\n        regionid: 900\n'
            '        hostname: home.example.test\n        derpport: 443\n'
            '        stunport: 3478\n        canport80: false\n')
        before = {p.name: p.read_bytes() for p in self.config().parent.iterdir()}
        result = self.run_install(expected=1)
        self.assertIn('--force', result.stderr)
        self.assertIn('existing relay regions', result.stderr)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.config().parent.iterdir()})

    def test_force_preserves_all_identity_files(self):
        states = ['var/lib/headscale/db.sqlite', 'var/lib/headscale/noise_private.key',
                  'var/lib/tailscale/tailscaled.state']
        for name in states:
            path = self.prefix / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'existing identity\x00')
            path.chmod(0o600)
        self.run_install('--force')
        for name in states:
            path = self.prefix / name
            self.assertEqual(path.read_bytes(), b'existing identity\x00')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_last_template_failure_keeps_every_target(self):
        self.run_install()
        before = {p.name: p.read_bytes() for p in self.config().parent.iterdir()}
        (self.templates / 'policy.hujson').write_text('{"grants": [], "${LEFTOVER}": true}')
        self.run_install('--force', changes={'BASE_DOMAIN': 'other.example.test'}, expected=1)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.config().parent.iterdir()})

    def test_invalid_replacements_and_required_inputs(self):
        for value in ('bad|value', 'bad&value', 'bad\\value', 'bad"value', "bad'value", 'bad\nvalue', 'bad\rvalue', '${SERVER_URL}'):
            with self.subTest(value=value):
                self.run_install(changes={'BASE_DOMAIN': value}, expected=1)
                self.assertFalse(self.config().exists())
        self.run_install(changes={'SERVER_URL': ''}, expected=2)

    def test_tls_modes_are_mutually_exclusive(self):
        self.run_install(changes={'TLS_HOSTNAME': 'control.example.test',
                                  'ACME_EMAIL': 'ops@example.test'}, expected=1)
        self.run_install(changes={'TLS_HOSTNAME': 'control.example.test',
                                  'ACME_EMAIL': 'ops@example.test',
                                  'TLS_CERT_PATH': '', 'TLS_KEY_PATH': ''})
        self.assertIn('control.example.test', self.config().read_text())

    def test_dry_run_resolves_version_overrides_without_side_effects(self):
        result = self.run_install('--dry-run', '--headscale-version', '0.29.8')
        self.assertIn('headscale_0.29.8_linux_arm64.deb', result.stderr)
        self.assertFalse(self.prefix.exists())
        self.assertNotIn('versions.env', result.stderr)
        result = self.run_install('--dry-run', '--tailscale-version', '1.104.6', role='node',
                                  changes={'NODE_HOSTNAME': 'test-node', 'NODE_AUTHKEY': 'secret-value'})
        self.assertIn('tailscale_1.104.6_arm64.tgz', result.stderr)
        self.assertNotIn('secret-value', result.stderr)

    def test_go_toolchain_is_managed_by_installer(self):
        result = self.run_install('--dry-run', role='derp', changes={'GO_VERSION': '9.9.9'})
        self.assertIn('go1.27.4.linux-arm64.tar.gz', result.stderr)
        self.run_install('--dry-run', '--go-version', '1.27.3', role='derp', expected=1)

    def test_installer_version_requires_no_role_platform_or_configuration(self):
        self.stub('uname', 'raise SystemExit("version must not inspect the platform")')
        result = subprocess.run(['sh', str(ROOT / 'install.sh'), '--version'],
                                env={**self.env, 'INSTALL_BASE_URL': '', 'SERVER_URL': ''},
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r'^tailbridge-install \d+\.\d+\.\d+\n$')
        self.assertEqual(result.stderr, '')
        self.assertFalse(self.prefix.exists())

    def test_cli_rejects_unknown_and_malformed_versions(self):
        for args in (('--unknown',), ('--headscale-version', ''), ('--tailscale-version', 'latest'),
                     ('--tailscale-version',), ('--', 'unexpected')):
            with self.subTest(args=args):
                self.run_install(*args, expected=1)

    def test_node_without_systemd_does_not_require_authkey(self):
        result = self.run_install(role='node', changes={'DERP_REGION_ID': '', 'DERP_REGION_CODE': '',
            'DERP_REGION_NAME': '', 'DERP_NODE_NAME': ''})
        self.assertIn('registration', result.stderr)
        self.assertFalse((self.prefix / 'var/lib/tailscale/tailscaled.state').exists())

    def test_derp_needs_no_template_url_or_runtime_parameters(self):
        self.run_install(role='derp', changes={'INSTALL_BASE_URL': '', 'SERVER_URL': '',
            'DERP_REGION_ID': '', 'DERP_REGION_CODE': '', 'DERP_REGION_NAME': '', 'DERP_NODE_NAME': ''})
        self.assertFalse(self.config('derp.yaml').exists())

    def test_empty_existing_identity_is_rejected_before_daemon_start(self):
        state = self.prefix / 'var/lib/tailscale/tailscaled.state'
        state.parent.mkdir(parents=True)
        state.touch()
        self.run_install(role='node', changes={'NODE_HOSTNAME': 'test-node',
                                              'NODE_AUTHKEY': 'unused'}, expected=2)
        self.assertEqual(state.read_bytes(), b'')

    def test_failed_download_does_not_publish_configuration(self):
        self.stub('id', 'print("0")')
        self.stub('curl', 'raise SystemExit(22)')
        self.run_install(changes={'TB_OFFLINE': '0'}, expected=1)
        self.assertFalse(self.config().exists())

    def test_first_registration_uses_private_key_file_after_service_start(self):
        for name in ('lib/systemd/system/tailscaled.service', 'etc/default/tailscaled'):
            target = self.prefix / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
        (self.prefix / 'run/systemd/system').mkdir(parents=True)
        self.stub('id', 'print("0")')
        self.stub('tailscaled', 'print("1.104.5")')
        calls = self.base / 'registration.json'
        self.stub('systemctl', 'Path(os.environ["TEST_STARTED"]).touch()')
        self.stub('tailscale', '''
if sys.argv[1] == 'version': print('1.104.5')
elif sys.argv[1] == 'status': print(json.dumps({'BackendState': 'NeedsLogin'}))
elif sys.argv[1] == 'up':
    assert Path(os.environ['TEST_STARTED']).exists()
    arg = next(x for x in sys.argv if x.startswith('--auth-key=file:'))
    key = Path(arg.removeprefix('--auth-key=file:'))
    Path(os.environ['TEST_CALLS']).write_text(json.dumps({'key': key.read_text(),
        'permissions': key.stat().st_mode & 0o777, 'args': sys.argv[1:]}))
''')
        for hostname in ('', 'fresh-node'):
            with self.subTest(hostname=hostname):
                result = self.run_install(role='node', changes={'TB_OFFLINE': '0', 'NODE_HOSTNAME': hostname,
                                         'NODE_AUTHKEY': 'test-secret', 'TEST_STARTED': str(self.base / 'started'),
                                         'TEST_CALLS': str(calls)})
                data = json.loads(calls.read_text())
                self.assertEqual(data['key'], 'test-secret')
                self.assertEqual(data['permissions'], 0o600)
                self.assertIn('--timeout=45s', data['args'])
                self.assertIn('--login-server=https://control.example.test', data['args'])
                hostname_args = [arg for arg in data['args'] if arg.startswith('--hostname=')]
                self.assertEqual(hostname_args, [f'--hostname={hostname}'] if hostname else [])
                self.assertNotIn('test-secret', result.stderr)

    def test_installed_binaries_without_official_unit_are_repaired(self):
        self.stub('id', 'print("0")')
        self.stub('tailscaled', 'print("1.104.5")')
        self.stub('tailscale', 'print("1.104.5")')
        self.stub('curl', 'raise SystemExit(22)')
        # 缺少单元时必须进入包安装路径；下载失败不能误报已安装成功。
        self.run_install(role='node', changes={'TB_OFFLINE': '0', 'NODE_HOSTNAME': 'test-node'}, expected=1)

    def test_missing_first_authkey_is_rejected_before_service_start(self):
        (self.prefix / 'run/systemd/system').mkdir(parents=True)
        for name in ('lib/systemd/system/tailscaled.service', 'etc/default/tailscaled'):
            target = self.prefix / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
        self.stub('id', 'print("0")')
        self.stub('tailscaled', 'print("1.104.5")')
        self.stub('tailscale', 'print("1.104.5") if sys.argv[1]=="version" else print(json.dumps({"BackendState":"NeedsLogin"}))')
        started = self.base / 'started'
        self.stub('systemctl', 'Path(os.environ["TEST_STARTED"]).touch()')
        self.run_install(role='node', changes={'TB_OFFLINE': '0', 'NODE_HOSTNAME': 'test-node',
                                              'TEST_STARTED': str(started)}, expected=2)
        self.assertFalse(started.exists())

    def test_registration_existing_identity_is_never_replaced(self):
        for name in ('lib/systemd/system/tailscaled.service', 'etc/default/tailscaled'):
            target = self.prefix / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
        state = self.prefix / 'var/lib/tailscale/tailscaled.state'
        state.parent.mkdir(parents=True)
        state.write_text('old identity')
        (self.prefix / 'run/systemd/system').mkdir(parents=True)
        self.stub('id', 'print("0")')
        self.stub('tailscaled', 'print("1.104.5")')
        self.stub('systemctl', 'pass')
        calls = self.base / 'calls'
        self.stub('tailscale', '''
if sys.argv[1] == 'version': print('1.104.5')
elif sys.argv[1] == 'status': print(json.dumps({'BackendState': os.environ['TEST_STATE']}))
elif sys.argv[1:] == ['debug', 'prefs']: print(json.dumps({'ControlURL': 'https://control.example.test/'}))
elif sys.argv[1:] == ['up'] and os.environ['TEST_STATE'] == 'Stopped':
    Path(os.environ['TEST_CALLS']).write_text('resumed existing preferences')
else:
    Path(os.environ['TEST_CALLS']).write_text('unexpected authentication')
    raise SystemExit(1)
''')
        for status, expected in (('Running', 0), ('Stopped', 0), ('NeedsLogin', 2)):
            with self.subTest(status=status):
                calls.unlink(missing_ok=True)
                self.run_install('--force', role='node', expected=expected,
                                 changes={'TB_OFFLINE': '0', 'NODE_HOSTNAME': 'test-node',
                                          'NODE_AUTHKEY': 'must-not-be-used', 'TEST_STATE': status,
                                          'TEST_CALLS': str(calls)})
                if status == 'Stopped':
                    self.assertEqual(calls.read_text(), 'resumed existing preferences')
                else:
                    self.assertFalse(calls.exists())
                self.assertEqual(state.read_text(), 'old identity')

    def exit_runtime(self, backend='Running', systemd=True):
        for name in ('lib/systemd/system/tailscaled.service', 'etc/default/tailscaled'):
            path = self.prefix / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        state = self.prefix / 'var/lib/tailscale/tailscaled.state'
        if backend != 'NeedsLogin':
            state.parent.mkdir(parents=True)
            state.write_text('preserved identity')
        if systemd:
            (self.prefix / 'run/systemd/system').mkdir(parents=True)
        self.stub('id', 'print("0")')
        self.stub('tailscaled', 'print("1.104.5")')
        self.stub('systemctl', 'pass')
        self.stub('sysctl', '''
p=Path(os.environ['TEST_KERNEL']); values=json.loads(p.read_text()) if p.exists() else {}
if sys.argv[1]=='-n': print(values.get(sys.argv[2],0))
elif sys.argv[1]=='-w':
    key,value=sys.argv[2].split('=')
    if os.environ.get('TEST_SYSCTL_FAIL')==key: raise SystemExit(1)
    values[key]=int(value); p.write_text(json.dumps(values))
else: raise SystemExit('unexpected sysctl command')
''')
        self.stub('tailscale', '''
if sys.argv[1]=='version': print('1.104.5')
elif sys.argv[1]=='status': print(json.dumps({'BackendState':os.environ['TEST_STATE']}))
elif sys.argv[1:]==['debug','prefs']:
    if os.environ.get('TEST_PREFS_FAIL'): raise SystemExit(1)
    print(json.dumps({'ControlURL':os.environ.get('TEST_CONTROL_URL','https://control.example.test/')}))
else:
    if sys.argv[1]=='set':
        assert sys.argv[2:]==['--advertise-exit-node']
        assert json.loads(Path(os.environ['TEST_KERNEL']).read_text())=={'net.ipv4.ip_forward':1,'net.ipv6.conf.all.forwarding':1}
    with open(os.environ['TEST_CALLS'],'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')
''')
        self.stub('iptables', 'raise SystemExit("installer must not rewrite firewall rules")')
        return {'TB_OFFLINE':'0', 'ADVERTISE_EXIT_NODE':'true', 'TEST_STATE':backend,
                'TEST_KERNEL':str(self.base/'kernel.json'), 'TEST_CALLS':str(self.base/'calls'),
                'NODE_AUTHKEY':'first-key' if backend=='NeedsLogin' else 'must-not-be-used'}

    def test_exit_enables_forwarding_before_advertising_and_keeps_identity(self):
        changes = self.exit_runtime()
        self.run_install(role='node', changes=changes)
        config = self.prefix / 'etc/sysctl.d/99-tailbridge-exit-node.conf'
        self.assertEqual(config.read_text(), 'net.ipv4.ip_forward=1\nnet.ipv6.conf.all.forwarding=1\n')
        self.assertEqual(config.stat().st_mode & 0o777, 0o644)
        self.assertEqual(json.loads((self.base/'kernel.json').read_text()),
                         {'net.ipv4.ip_forward':1,'net.ipv6.conf.all.forwarding':1})
        self.assertEqual(json.loads((self.base/'calls').read_text()), ['set','--advertise-exit-node'])
        self.assertEqual((self.prefix/'var/lib/tailscale/tailscaled.state').read_text(), 'preserved identity')
        self.run_install(role='node', changes=changes)
        self.assertEqual(config.read_text().count('net.ipv4.ip_forward'), 1)

    def test_existing_node_rejects_wrong_or_unknown_control_before_tailnet_changes(self):
        changes = self.exit_runtime()
        for backend in ('Running', 'Stopped'):
            for prefs in ({'TEST_CONTROL_URL': 'https://other.example.test'},
                          {'TEST_CONTROL_URL': ''}, {'TEST_PREFS_FAIL': '1'}):
                with self.subTest(backend=backend, prefs=prefs):
                    result = self.run_install('--force', role='node', expected=2,
                                              changes={**changes, 'TEST_STATE': backend, **prefs})
                    self.assertFalse((self.base / 'calls').exists())
                    self.assertIn('state preserved', result.stderr)
                    self.assertEqual((self.prefix / 'var/lib/tailscale/tailscaled.state').read_text(),
                                     'preserved identity')

    def test_stopped_node_with_custom_state_path_preserves_preferences(self):
        changes = self.exit_runtime(backend='Stopped')
        (self.prefix / 'var/lib/tailscale/tailscaled.state').unlink()
        self.run_install(role='node', expected=2,
                         changes={**changes, 'TEST_CONTROL_URL': 'https://other.example.test'})
        self.assertFalse((self.base / 'calls').exists())
        self.run_install(role='node', changes=changes)
        calls = [json.loads(line) for line in (self.base / 'calls').read_text().splitlines()]
        self.assertEqual(calls, [['up'], ['set', '--advertise-exit-node']])

    def test_control_rejects_directory_before_publishing_any_file(self):
        self.config('derp.yaml').mkdir(parents=True)
        self.config().write_bytes(b'original configuration')
        self.run_install('--force', expected=1)
        self.assertEqual(self.config().read_bytes(), b'original configuration')
        self.assertEqual(list(self.config('derp.yaml').iterdir()), [])
        self.assertFalse(self.config('policy.hujson').exists())

    def test_exit_first_registration_precedes_advertisement(self):
        changes = self.exit_runtime(backend='NeedsLogin')
        self.run_install(role='node', changes=changes)
        calls = [json.loads(line) for line in (self.base/'calls').read_text().splitlines()]
        self.assertEqual([call[0] for call in calls], ['up','set'])
        self.assertTrue(any(arg.startswith('--auth-key=file:') for arg in calls[0]))

    def test_exit_unset_or_false_preserves_existing_configuration(self):
        changes = self.exit_runtime()
        config = self.prefix/'etc/sysctl.d/99-tailbridge-exit-node.conf'
        config.parent.mkdir(parents=True)
        config.write_text('existing sysctl configuration\n')
        for value in ('', 'false'):
            with self.subTest(value=value):
                self.run_install(role='node', changes={**changes,'ADVERTISE_EXIT_NODE':value})
                self.assertEqual(config.read_text(), 'existing sysctl configuration\n')
                self.assertFalse((self.base/'kernel.json').exists())
                self.assertFalse((self.base/'calls').exists())

    def test_exit_dry_run_and_invalid_inputs_have_no_side_effects(self):
        result = self.run_install('--dry-run', role='node', changes={'ADVERTISE_EXIT_NODE':'true'})
        self.assertIn('forwarding', result.stderr)
        self.assertIn('advertise', result.stderr)
        self.assertFalse(self.prefix.exists())
        self.run_install(role='node', changes={'ADVERTISE_EXIT_NODE':'yes'}, expected=1)
        self.run_install(role='control', changes={'ADVERTISE_EXIT_NODE':'true'}, expected=1)
        self.assertFalse(self.prefix.exists())

    def test_exit_conflicting_file_requires_force_and_rejects_nonfiles(self):
        changes = self.exit_runtime()
        config = self.prefix/'etc/sysctl.d/99-tailbridge-exit-node.conf'
        config.parent.mkdir(parents=True)
        config.write_text('net.ipv4.ip_forward=0\n')
        self.run_install(role='node', changes=changes, expected=1)
        self.assertEqual(config.read_text(), 'net.ipv4.ip_forward=0\n')
        self.assertFalse((self.base/'kernel.json').exists())
        self.assertFalse((self.base/'calls').exists())
        self.run_install('--force', role='node', changes=changes)
        config.unlink()
        other=self.base/'other'; other.write_text('do not modify')
        config.symlink_to(other)
        self.run_install('--force', role='node', changes=changes, expected=1)
        self.assertEqual(other.read_text(), 'do not modify')
        config.unlink()
        config.mkdir()
        calls = (self.base/'calls').read_bytes()
        kernel = (self.base/'kernel.json').read_bytes()
        self.run_install('--force', role='node', changes=changes, expected=1)
        self.assertEqual(list(config.iterdir()), [])
        self.assertEqual((self.base/'calls').read_bytes(), calls)
        self.assertEqual((self.base/'kernel.json').read_bytes(), kernel)

    def test_exit_forwarding_failure_does_not_advertise(self):
        changes = self.exit_runtime()
        self.run_install(role='node', changes={**changes,'TEST_SYSCTL_FAIL':'net.ipv6.conf.all.forwarding'}, expected=1)
        self.assertFalse((self.base/'calls').exists())

    def test_exit_without_systemd_prepares_forwarding_and_defers_advertisement(self):
        changes = self.exit_runtime(backend='NeedsLogin', systemd=False)
        self.run_install(role='node', changes=changes)
        self.assertTrue((self.prefix/'etc/sysctl.d/99-tailbridge-exit-node.conf').exists())
        self.assertTrue((self.base/'kernel.json').exists())
        self.assertFalse((self.base/'calls').exists())

    def control_runtime(self, multiline=True):
        self.stub('id', 'print("0")')
        output = 'headscale version v0.29.7\ncommit: abc\nbuild time: today\nbuild info: release' if multiline else 'headscale version v0.29.7'
        self.stub('headscale', f'print({output!r}) if sys.argv[1] == "version" else None')
        self.stub('curl', '''
import shutil
args=sys.argv[1:]; url=args[-1]
if '/files/' not in url: raise SystemExit(22)
shutil.copyfile(Path(os.environ['TB_TEMPLATE_DIR']) / url.rsplit('/',1)[1], args[args.index('--output')+1])
''')
        self.stub('runuser', 'os.execvp(sys.argv[4], sys.argv[4:])')
        self.stub('install', '''
import subprocess
args = sys.argv[1:]
with open(os.environ['TEST_INSTALL_CALLS'], 'a') as f: f.write(json.dumps(args)+'\\n')
clean=[]
while args:
    arg=args.pop(0)
    if arg in ('-o','-g'): args.pop(0)
    else:
        if arg.startswith(('/var/lib/headscale','/run/headscale')): arg=os.environ['TB_PREFIX']+arg
        clean.append(arg)
raise SystemExit(subprocess.call(['/usr/bin/install', *clean]))
''')
        return {'TB_OFFLINE': '0', 'TLS_HOSTNAME': 'control.example.test',
                'ACME_EMAIL': 'ops@example.test', 'TLS_CERT_PATH': '',
                'TLS_KEY_PATH': '', 'TEST_INSTALL_CALLS': str(self.base / 'install-calls')}

    def test_installed_headscale_multiline_version_is_accepted(self):
        self.run_install(changes=self.control_runtime())
        self.assertTrue(self.config().is_file())

    def test_failed_control_validation_restores_all_files_and_permissions(self):
        changes = self.control_runtime()
        originals = {'config.yaml': (b'original configuration', 0o600),
                     'derp.yaml': (b'original regions', 0o640),
                     'policy.hujson': (b'original grants', 0o644)}
        self.config().parent.mkdir(parents=True)
        for name, (content, mode) in originals.items():
            self.config(name).write_bytes(content)
            self.config(name).chmod(mode)
        (self.prefix / 'run/systemd/system').mkdir(parents=True)
        self.stub('systemctl', 'Path(os.environ["TEST_STARTED"]).touch()')
        self.stub('headscale', '''
if sys.argv[1] == 'version': print('headscale version v0.29.7')
elif sys.argv[1] == os.environ['TEST_FAIL_CHECK']: raise SystemExit(1)
''')
        for check in ('configtest', 'policy'):
            with self.subTest(check=check):
                self.run_install('--force', expected=1, changes={**changes, 'TEST_FAIL_CHECK': check,
                                 'TEST_STARTED': str(self.base / 'started')})
                for name, (content, mode) in originals.items():
                    self.assertEqual(self.config(name).read_bytes(), content)
                    self.assertEqual(self.config(name).stat().st_mode & 0o777, mode)
                self.assertFalse((self.base / 'started').exists())

    def test_failed_first_control_validation_removes_new_files(self):
        changes = self.control_runtime()
        self.stub('headscale', '''
if sys.argv[1] == 'version': print('headscale version v0.29.7')
elif sys.argv[1] == 'configtest': raise SystemExit(1)
''')
        self.run_install(expected=1, changes=changes)
        for name in ('config.yaml', 'derp.yaml', 'policy.hujson'):
            self.assertFalse(self.config(name).exists())

    def test_partial_control_write_failure_restores_files(self):
        self.run_install()
        originals = {p.name: p.read_bytes() for p in self.config().parent.iterdir()}
        self.stub('mv', '''
import subprocess
target = Path(sys.argv[-1])
marker = Path(os.environ['TEST_FAILED_WRITE'])
if target.name == 'derp.yaml' and not marker.exists():
    marker.touch()
    raise SystemExit(1)
raise SystemExit(subprocess.call(['/bin/mv', *sys.argv[1:]]))
''')
        self.run_install('--force', expected=1, changes={'SERVER_URL': 'https://new.example.test',
                         'DERP_REGION_NAME': 'New Region', 'TEST_FAILED_WRITE': str(self.base / 'failed')})
        for name, content in originals.items():
            self.assertEqual(self.config(name).read_bytes(), content)

    def test_initial_policy_validation_does_not_require_running_headscale(self):
        changes = self.control_runtime()
        self.stub('headscale', '''
if sys.argv[1] == 'version': print('headscale version v0.29.7')
elif sys.argv[1:3] == ['policy', 'check']:
    if '--bypass-grpc-and-access-database-directly' not in sys.argv:
        raise SystemExit('Headscale is not running; gRPC socket is unavailable')
    if '--force' not in sys.argv:
        raise SystemExit('Offline validation would prompt for confirmation')
''')
        self.run_install(changes=changes)

    def test_policy_validation_does_not_bypass_existing_server_endpoint(self):
        path = self.prefix / 'run/headscale/headscale.sock'
        path.parent.mkdir(parents=True)
        path.touch()
        changes = self.control_runtime()
        self.stub('headscale', '''
if sys.argv[1] == 'version': print('headscale version v0.29.7')
elif sys.argv[1:3] == ['policy', 'check'] and '--bypass-grpc-and-access-database-directly' in sys.argv:
    raise SystemExit('Do not bypass a running server to access its database')
''')
        self.run_install(changes=changes)

    def test_control_prepares_parent_state_directory_for_service_user(self):
        self.run_install(changes=self.control_runtime(multiline=False))
        calls = [json.loads(line) for line in (self.base / 'install-calls').read_text().splitlines()]
        owned = [arg for call in calls if '-o' in call and 'headscale' in call for arg in call]
        self.assertIn(str(self.prefix / 'var/lib/headscale'), owned)

    def test_existing_control_identity_can_be_read_by_service_without_replacement(self):
        changes = self.control_runtime(multiline=False)
        identity = self.prefix / 'var/lib/headscale/noise_private.key'
        identity.parent.mkdir(parents=True)
        identity.write_bytes(b'preserved noise identity')
        identity.chmod(0o600)
        ownership = self.base / 'ownership'
        self.stub('chown', 'Path(os.environ["TEST_OWNERSHIP"]).write_text(json.dumps(sys.argv[1:]))')
        self.run_install(changes={**changes, 'TEST_OWNERSHIP': str(ownership)})
        self.assertTrue(ownership.exists(), 'existing identity must be accessible to the service account')
        self.assertIn(str(identity), json.loads(ownership.read_text()))
        self.assertEqual(identity.read_bytes(), b'preserved noise identity')
        self.assertEqual(identity.stat().st_mode & 0o777, 0o600)

    def test_derper_uses_official_go_download_metadata(self):
        self.stub('id', 'print("0")')
        self.stub('go', 'print("go version go0.0.0 linux/arm64")')
        self.stub('curl', '''
import hashlib
args=sys.argv[1:]; out=Path(args[args.index('--output')+1]); url=args[-1]
if url == 'https://go.dev/dl/?mode=json&include=all':
    out.write_text(json.dumps([{'version':'go1.27.4','files':[
        {'filename':'go1.27.4.linux-amd64.tar.gz','sha256':'a'*64},
        {'filename':'go1.27.4.linux-arm64.tar.gz','sha256':hashlib.sha256(b'archive').hexdigest()}]}]))
elif url == 'https://go.dev/dl/go1.27.4.linux-arm64.tar.gz': out.write_bytes(b'archive')
else: raise SystemExit(22)
''')
        self.stub('sha256sum', '''
import hashlib
digest, path = Path(sys.argv[2]).read_text().strip().split('  ',1)
raise SystemExit(0 if hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest else 1)
''')
        self.stub('tar', '''
target=Path(sys.argv[sys.argv.index('-C')+1])/'go/bin/go'
target.parent.mkdir(parents=True)
code='import os,sys\\nfrom pathlib import Path\\nif sys.argv[1]=="version": print("go version go1.27.4 linux/arm64")\\nelse:\\n p=Path(os.environ["GOBIN"]); p.mkdir(); (p/"derper").write_text("built derper")\\n'
target.write_text('#!'+sys.executable+'\\n'+code); target.chmod(0o755)
''')
        self.run_install(role='derp', changes={'TB_OFFLINE': '0'})
        self.assertEqual((self.prefix / 'usr/local/bin/derper').read_text(), 'built derper')


if __name__ == '__main__':
    unittest.main()
