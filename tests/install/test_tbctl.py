"""控制面运维工具：校验失败不替换文件，读者只看到完整版本，备份可恢复且按期清理。"""

from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
DERP = '''regions:
  910:
    regionid: 910
    regioncode: "ap-east-1"
    regionname: "Hong Kong"
    nodes:
      - name: "tb-derp-hk-01"
        regionid: 910
        hostname: "derp.example.test"
        ipv4: ""
        derpport: 443
        stunport: 3478
        canport80: false
'''


def load_tool():
    loader = SourceFileLoader('tbctl', str(ROOT / 'files/tbctl'))
    module = module_from_spec(spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


class ControlToolTests(unittest.TestCase):
    def setUp(self):
        self.tool = load_tool()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for name in ('CONFIG', 'STATE', 'BACKUPS'):
            path = self.root / name.lower()
            path.mkdir()
            setattr(self.tool, name, path)
        self.policy = self.tool.CONFIG / 'policy.hujson'
        self.policy.write_text('{"grants": []}\n')
        self.derp = self.tool.CONFIG / 'derp.yaml'
        self.derp.write_text(DERP)

    def test_template_seed_is_a_valid_derp_map(self):
        rendered = (ROOT / 'files/derp.yaml').read_text()
        for name, value in (('DERP_REGION_ID', '910'), ('DERP_PORT', '443')):
            rendered = rendered.replace('${' + name + '}', value)
        path = self.root / 'seed.yaml'
        path.write_text(rendered)
        self.tool.check_derp(path)

    def test_derp_map_is_published_atomically(self):
        updated = DERP.replace('Hong Kong', 'Hong Kong reload')
        with self.derp.open() as reader:
            self.tool.publish('derp', updated)
            self.assertEqual(reader.read(), DERP)
        self.assertEqual(self.derp.read_text(), updated)
        self.assertEqual(self.derp.stat().st_mode & 0o777, 0o644)
        self.assertEqual(sorted(p.name for p in self.tool.CONFIG.iterdir()), ['derp.yaml', 'policy.hujson'])

    def test_invalid_derp_map_keeps_previous_file(self):
        for content in ('regions: [', 'regions: {}\n', DERP.replace('regionid: 910\n        hostname', 'regionid: 911\n        hostname'),
                        DERP.replace('        hostname: "derp.example.test"\n', '')):
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.tool.publish('derp', content)
            self.assertEqual(self.derp.read_text(), DERP)
            self.assertEqual(sorted(p.name for p in self.tool.CONFIG.iterdir()), ['derp.yaml', 'policy.hujson'])

    def test_policy_is_checked_before_publication_and_then_reloaded(self):
        granted = '{"grants": [{"src": ["*"], "dst": ["*"], "ip": ["*"]}]}\n'
        with patch.object(self.tool, 'check_policy', side_effect=RuntimeError('rejected')), \
                patch.object(self.tool, 'reload_policy') as reload:
            with self.assertRaisesRegex(RuntimeError, 'rejected'):
                self.tool.publish('policy', granted)
            reload.assert_not_called()
        self.assertEqual(self.policy.read_text(), '{"grants": []}\n')
        checked = []
        with patch.object(self.tool, 'check_policy', side_effect=lambda path: checked.append(path.read_text())), \
                patch.object(self.tool, 'reload_policy', return_value=True) as reload:
            self.tool.publish('policy', granted)
            reload.assert_called_once()
        self.assertEqual(checked, [granted])
        self.assertEqual(self.policy.read_text(), granted)

    def test_unchanged_content_neither_checks_nor_reloads(self):
        with patch.object(self.tool, 'check_policy') as check, patch.object(self.tool, 'reload_policy') as reload:
            self.tool.publish('policy', self.policy.read_text())
        check.assert_not_called()
        reload.assert_not_called()

    def test_backup_snapshots_state_and_prunes_expired_copies(self):
        database = sqlite3.connect(self.tool.STATE / 'db.sqlite')
        database.execute('PRAGMA journal_mode=WAL')
        database.execute('CREATE TABLE nodes (name TEXT)')
        database.execute("INSERT INTO nodes VALUES ('kept')")
        database.commit()
        (self.tool.STATE / 'noise_private.key').write_text('noise')
        (self.tool.CONFIG / 'config.yaml').write_text('server_url: x\n')
        now = datetime(2026, 11, 17, tzinfo=timezone.utc)
        old = self.tool.BACKUPS / (now - timedelta(days=15)).strftime(self.tool.STAMP)
        recent = self.tool.BACKUPS / (now - timedelta(days=13)).strftime(self.tool.STAMP)
        leftover = self.tool.BACKUPS / '.interrupted'
        for path in (old, recent, leftover):
            path.mkdir()
        previous = os.umask(0o022)
        try:
            self.tool.backup(now)
        finally:
            os.umask(previous)
            database.close()
        snapshot = self.tool.BACKUPS / now.strftime(self.tool.STAMP)
        self.assertEqual(sorted(p.name for p in self.tool.BACKUPS.iterdir()), sorted([recent.name, snapshot.name]))
        copied = sqlite3.connect(snapshot / 'db.sqlite')
        try:
            self.assertEqual(copied.execute('SELECT name FROM nodes').fetchall(), [('kept',)])
        finally:
            copied.close()
        self.assertEqual(sorted(p.name for p in snapshot.iterdir()),
                         ['config.yaml', 'db.sqlite', 'derp.yaml', 'noise_private.key', 'policy.hujson'])
        self.assertEqual((snapshot / 'db.sqlite').stat().st_mode & 0o777, 0o600)

    def status_fixture(self, now, nodes):
        (self.tool.CONFIG / 'config.yaml').write_text('server_url: https://control.example.test:8443\n')
        self.derp.write_text(DERP.replace('stunport: 3478', 'stunport: 0') + DERP.replace('910', '911')
                             .replace('regions:\n', '').replace('derp.example.test', 'stunless.example.test')
                             .replace('stunport: 3478', 'stunport: -1'))
        (self.tool.BACKUPS / (now - timedelta(hours=2)).strftime(self.tool.STAMP)).mkdir()
        listed = json.dumps(nodes)
        probes = []

        def https(host, port, path, when):
            probes.append(('https', host, port, path))
            return 200, timedelta(days=60 if host != 'stunless.example.test' else 10)

        return probes, [
            patch.object(self.tool, 'serve_pid', return_value=42),
            patch.object(self.tool, 'check_policy'),
            patch.object(self.tool, 'headscale', return_value=listed),
            patch.object(self.tool, 'probe_https', side_effect=https),
            patch.object(self.tool, 'probe_stun', side_effect=lambda host, port: probes.append(('stun', host, port))),
        ]

    def test_status_reports_topology_checks_and_redacts_node_keys(self):
        now = datetime(2026, 11, 18, tzinfo=timezone.utc)
        seen = {'seconds': int((now - timedelta(hours=30)).timestamp())}
        nodes = [
            {'id': 1, 'given_name': 'tb-node-a', 'user': {'name': 'ops'}, 'online': True, 'node_key': 'nodekey:secret',
             'pre_auth_key': {'key': 'secret'}, 'ip_addresses': ['100.77.0.1'], 'last_seen': seen},
            {'id': 2, 'given_name': 'tb-node-b', 'user': {'name': 'ops'}, 'online': False, 'last_seen': seen,
             'expiry': {'seconds': -62135596800}},
        ]
        probes, patches = self.status_fixture(now, nodes)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            report = self.tool.status(now)
        checks = {c['name']: c['status'] for c in report['checks']}
        self.assertEqual(checks, {
            'control.service': 'ok', 'control.health': 'ok', 'control.policy': 'ok', 'control.backup': 'ok',
            'control.disk': checks['control.disk'], 'derp.910.tb-derp-hk-01.https': 'ok',
            'derp.910.tb-derp-hk-01.stun': 'ok', 'derp.911.tb-derp-hk-01.https': 'warn', 'nodes': 'ok',
            'node.tb-node-a': 'ok', 'node.tb-node-b': 'warn'})
        self.assertEqual(report['status'], 'warn')
        self.assertIn(('https', 'control.example.test', 8443, '/health'), probes)
        self.assertIn(('stun', 'derp.example.test', 3478), probes)
        self.assertNotIn('stunless.example.test', [p[1] for p in probes if p[0] == 'stun'])
        self.assertEqual([r['id'] for r in report['regions']], [910, 911])
        self.assertNotIn('secret', json.dumps(report))
        self.assertEqual(report['nodes'][1]['expiry'], None)

    def test_status_fails_on_unreachable_relay_or_expiring_certificate(self):
        now = datetime(2026, 11, 18, tzinfo=timezone.utc)
        probes, patches = self.status_fixture(now, [])
        patches[3] = patch.object(self.tool, 'probe_https', side_effect=[(200, timedelta(days=5)), OSError('timed out'),
                                                                           (200, timedelta(days=60))])
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            report = self.tool.status(now)
        failed = [c['name'] for c in report['checks'] if c['status'] == 'fail']
        self.assertEqual(failed, ['control.health', 'derp.910.tb-derp-hk-01.https'])
        self.assertEqual(report['status'], 'fail')
        with patch.object(self.tool, 'status', return_value=report), patch.object(self.tool.os, 'geteuid', return_value=0), \
                patch('sys.stdout'):
            self.assertEqual(self.tool.main(['status', '--json']), 2)


if __name__ == '__main__':
    unittest.main()
