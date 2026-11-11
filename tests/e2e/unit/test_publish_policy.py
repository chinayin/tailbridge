"""控制面本地发布策略：读者保持完整视图，坏输入不替换已有策略。"""

import importlib
import json
from pathlib import Path
import tempfile
import unittest


class PublishPolicyTests(unittest.TestCase):
    def setUp(self):
        self.publish = importlib.import_module('publish_policy').write_policy

    def test_published_policy_is_complete_readable_and_atomic(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'policy.hujson'
            previous = '{"grants": []}\n'
            path.write_text(previous)
            policy = {'grants': [{'src': ['*'], 'dst': ['100.77.0.1'], 'ip': ['tcp:22']}]}
            with path.open() as reader:
                self.publish(path, json.dumps(policy))
                self.assertEqual(reader.read(), previous)
            self.assertEqual(json.loads(path.read_text()), policy)
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_truncated_input_keeps_previous_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'policy.hujson'
            previous = '{"grants": []}\n'
            path.write_text(previous)
            with self.assertRaises(ValueError):
                self.publish(path, '{"grants": [')
            self.assertEqual(path.read_text(), previous)
            self.assertEqual(list(path.parent.iterdir()), [path])
