"""报告内容来自实际结果；覆盖失败展示和 HTML 转义。"""

import contextlib
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from report import write_report
import verify


class Elements(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.data = {
            "passed": True, "mode": "full", "scope": "local Docker simulation; SSH only",
            "started_at": "2026-11-10T12:00:00+00:00", "finished_at": "2026-11-10T12:00:30+00:00",
            "configuration": {"project": "tailbridge", "base_domain": "e2e.tb.test",
                              "server_url": "https://hs.private.test"},
            "versions": {"tailscale": "1.104.4"},
            "checks": [{"name": "MagicDNS SSH", "passed": True, "path": "DERP(tb)"}],
        }

    def test_writes_matching_json_and_offline_html(self):
        output = self.folder / "results.json"
        html = write_report(self.data, output)
        self.assertEqual(html, self.folder / "results.html")
        self.assertEqual(json.loads(output.read_text()), self.data)
        page = html.read_text()
        for text in ("测试通过", "已通过 1 项", "30.0 秒", "MagicDNS SSH", "DERP(tb)", "e2e.tb.test", "https://hs.private.test"):
            self.assertIn(text, page)
        self.assertNotIn("script", Elements(page).tags)
        self.assertNotIn("<link", page)
        self.assertIn('href="results.json"', page)

    def test_failure_without_configuration_is_visible_and_escaped(self):
        attack = '</pre><script>alert("x")</script>'
        self.data.update(passed=False, error=attack)
        self.data.pop("configuration")
        self.data.pop("versions")
        self.data["checks"][0]["name"] = attack
        self.data["checks"][0]["path"] = attack
        page = write_report(self.data, self.folder / "results.json").read_text()
        self.assertIn("测试未通过", page)
        self.assertIn("失败原因", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("script", Elements(page).tags)

    def test_verifier_failure_also_writes_basic_html(self):
        with patch.object(verify, "CONFIG", SimpleNamespace(generated=self.folder)), \
                patch.object(verify, "REPORT", {"scope": "local Docker simulation; SSH only", "checks": []}), \
                patch.object(verify, "configure", side_effect=ValueError("invalid configuration")), \
                patch("sys.argv", ["verify.py", "--basic"]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(verify.main(), 1)
        self.assertFalse(json.loads((self.folder / "basic-results.json").read_text())["passed"])
        page = (self.folder / "basic-results.html").read_text()
        self.assertIn("测试未通过", page)
        self.assertIn("invalid configuration", page)
        self.assertIn("尚无通过的检查项", page)


if __name__ == "__main__":
    unittest.main()
