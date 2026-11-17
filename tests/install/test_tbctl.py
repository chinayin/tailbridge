"""控制面运维工具：校验失败不替换文件，读者只看到完整版本，备份可恢复且按期清理。"""

from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
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


if __name__ == '__main__':
    unittest.main()
