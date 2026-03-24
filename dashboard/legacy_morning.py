"""Legacy morning-brief helpers extracted from server.py."""

from __future__ import annotations

import json
import sys


def _srv():
    module = sys.modules.get("server")
    if module is not None and hasattr(module, "DATA"):
        return module
    module = sys.modules.get("__main__")
    if module is not None and str(getattr(module, "__file__", "")).endswith("dashboard/server.py"):
        return module
    import server as module  # type: ignore

    return module


def push_to_feishu():
    srv = _srv()
    cfg = srv.read_json(srv.DATA / "morning_brief_config.json", {})
    webhook = cfg.get("feishu_webhook", "").strip()
    if not webhook:
        return
    if not srv.validate_url(webhook, allowed_schemes=("https",), allowed_domains=("open.feishu.cn", "open.larksuite.com")):
        srv.log.warning(f"飞书 Webhook URL 不合法: {webhook}")
        return
    brief = srv.read_json(srv.DATA / "morning_brief.json", {})
    date_str = brief.get("date", "")
    total = sum(len(items) for items in (brief.get("categories") or {}).values())
    if not total:
        return
    cat_lines = []
    for cat, items in (brief.get("categories") or {}).items():
        if items:
            cat_lines.append(f"  {cat}: {len(items)} 条")
    summary = "\n".join(cat_lines)
    date_fmt = date_str[:4] + "年" + date_str[4:6] + "月" + date_str[6:] + "日" if len(date_str) == 8 else date_str
    payload = json.dumps(
        {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": f"📰 天下要闻 · {date_fmt}"}, "template": "blue"},
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"共 **{total}** 条要闻已更新\n{summary}"}},
                    {"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "🔗 查看完整简报"}, "url": "http://127.0.0.1:7891", "type": "primary"}]},
                    {"tag": "note", "elements": [{"tag": "plain_text", "content": f'采集于 {brief.get("generated_at", "")}'}]},
                ],
            },
        }
    ).encode()
    try:
        req = srv.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
        resp = srv.urlopen(req, timeout=10)
        print(f"[飞书] 推送成功 ({resp.status})")
    except Exception as exc:
        print(f"[飞书] 推送失败: {exc}", file=sys.stderr)
