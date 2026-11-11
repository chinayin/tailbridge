"""从同一份测试结果生成 JSON 和可离线打开的 HTML。"""

from datetime import datetime
from html import escape
import json
from pathlib import Path
from string import Template
from urllib.parse import quote

TEMPLATE = Path(__file__).with_name("report.html")


def escaped(value):
    return escape(str(value), quote=True)


def evidence(value):
    return escaped(json.dumps(value, ensure_ascii=False, indent=2))


def render_html(report, json_name):
    checks = report.get("checks", [])
    config = report.get("configuration", {})
    passed = report.get("passed") is True
    started = datetime.fromisoformat(report["started_at"]) if report.get("started_at") else None
    finished = datetime.fromisoformat(report["finished_at"]) if report.get("finished_at") else None
    duration = f"{(finished - started).total_seconds():.1f} 秒" if started and finished else "未记录"

    def timestamp(value):
        return value.isoformat(sep=" ", timespec="seconds").replace("+00:00", " UTC") if value else "未记录"

    metadata = [
        ("项目", config.get("project", "未记录")),
        ("测试模式", {"full": "完整测试", "basic": "基础测试", "exit": "出口测试"}.get(report.get("mode"), "未记录")),
        ("尾网域", config.get("base_domain", "未记录")),
        ("控制入口", config.get("server_url", "未记录")),
        ("开始时间", timestamp(started)), ("结束时间", timestamp(finished)),
    ]
    rows = []
    for check in checks:
        ok = check.get("passed") is True
        details = {key: value for key, value in check.items() if key not in ("name", "passed")}
        proof = (f'<details><summary>查看证据</summary><pre>{evidence(details)}</pre></details>'
                 if details else '<span class="muted">—</span>')
        rows.append(
            f'<tr><td><span class="badge {"pass" if ok else "fail"}">{"通过" if ok else "失败"}</span></td>'
            f'<th scope="row">{escaped(check.get("name", "未命名检查"))}</th>'
            f'<td class="{"evidence" if details else "no-evidence"}">{proof}</td></tr>')
    error = (f'<section class="error"><h2>失败原因</h2><pre>{escaped(report["error"])}</pre></section>'
             if report.get("error") else "")
    values = {
        "status_class": "pass" if passed else "fail",
        "status": "测试通过" if passed else "测试未通过",
        "count": str(sum(check.get("passed") is True for check in checks)),
        "duration": escaped(duration), "scope": escaped(report.get("scope", "未记录")),
        "metadata": "".join(f"<div><dt>{escaped(label)}</dt><dd>{escaped(value)}</dd></div>"
                            for label, value in metadata),
        "rows": "\n".join(rows) or '<tr><td colspan="3" class="empty">尚无通过的检查项。</td></tr>',
        "error": error, "configuration": evidence(config),
        "versions": evidence(report.get("versions", {})), "json_name": escaped(quote(json_name)),
    }
    return Template(TEMPLATE.read_text(encoding="utf-8")).substitute(values)


def write_report(report, output):
    output = Path(output)
    html = render_html(report, output.name)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    page = output.with_suffix(".html")
    page.write_text(html, encoding="utf-8")
    return page
