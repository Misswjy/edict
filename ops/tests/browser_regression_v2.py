#!/usr/bin/env python3
"""Browser regression for the v2 frontend against a running local stack.

Expected runtime:
- frontend static/proxy server reachable at --frontend-url (default http://127.0.0.1:5173)
- backend reachable at --api-url (default http://127.0.0.1:8000)
- Playwright Chromium installed in the active Python environment
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


def _request_json(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read().decode("utf-8")
        return json.loads(body or "{}")


def _wait_for_url(url: str, *, label: str, timeout_sec: int = 60) -> None:
    deadline = time.time() + timeout_sec
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if 200 <= response.status < 500:
                    return
                last_error = f"unexpected status {response.status}"
        except Exception as exc:  # pragma: no cover - retry loop
            last_error = str(exc)
        time.sleep(1)
    raise AssertionError(f"{label} not ready within {timeout_sec}s: {last_error}")


def _wait_for_any_url(urls: list[str], *, label: str, timeout_sec: int = 60) -> str:
    deadline = time.time() + timeout_sec
    last_error = ""
    while time.time() < deadline:
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    if 200 <= response.status < 500:
                        return url
                    last_error = f"{url} unexpected status {response.status}"
            except Exception as exc:  # pragma: no cover - retry loop
                last_error = f"{url}: {exc}"
        time.sleep(1)
    raise AssertionError(f"{label} not ready within {timeout_sec}s: {last_error}")


def _wait_for_json_predicate(
    url: str,
    *,
    label: str,
    predicate,
    timeout_sec: int = 60,
) -> dict[str, Any]:
    deadline = time.time() + timeout_sec
    last_payload: dict[str, Any] = {}
    last_error = ""
    while time.time() < deadline:
        try:
            payload = _request_json("GET", url)
            last_payload = payload
            if predicate(payload):
                return payload
            last_error = f"predicate not met: {json.dumps(payload, ensure_ascii=False)[:400]}"
        except Exception as exc:  # pragma: no cover - retry loop
            last_error = str(exc)
        time.sleep(1)
    raise AssertionError(f"{label} not ready within {timeout_sec}s: {last_error}")


def _ensure_text(text: str, needle: str, *, context: str) -> None:
    if needle not in text:
        raise AssertionError(f"missing text `{needle}` in {context}")


def _seed_backend(api_url: str) -> dict[str, str]:
    suffix = int(time.time())
    task_title = f"浏览器回归验证：工部提交春耕水利计划 #{suffix}"
    court_topic = f"浏览器回归验证：是否准许春耕水利计划先行试点 #{suffix}"

    created = _request_json(
        "POST",
        f"{api_url}/api/create-task",
        {
            "title": task_title,
            "targetDept": "工部",
            "actor": "emperor",
            "source": "browser-regression",
        },
    )
    if not created.get("ok") or not created.get("taskId"):
        raise AssertionError(f"failed to create seed task: {created}")

    session = _request_json(
        "POST",
        f"{api_url}/api/court-discuss/start",
        {
            "topic": court_topic,
            "officials": ["sili", "zhongshu", "menxia"],
        },
    )
    if not session.get("ok") or not session.get("session_id"):
        raise AssertionError(f"failed to create court session: {session}")

    _wait_for_json_predicate(
        f"{api_url}/api/live-status",
        label="seed task in live-status",
        predicate=lambda payload: any(
            task.get("id") == created["taskId"] for task in (payload.get("tasks") or [])
        ),
    )
    _wait_for_json_predicate(
        f"{api_url}/api/court-discuss/list",
        label="seed session in court list",
        predicate=lambda payload: any(
            item.get("session_id") == session["session_id"] for item in (payload.get("sessions") or [])
        ),
    )

    return {
        "task_id": str(created["taskId"]),
        "task_title": task_title,
        "session_id": str(session["session_id"]),
        "court_topic": court_topic,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run v2 browser regression against a running local stack.")
    parser.add_argument("--frontend-url", default="http://127.0.0.1:5173")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--health-url", default="")
    parser.add_argument(
        "--output-dir",
        default=str(Path("ops") / "artifacts" / "browser-regression-v2"),
    )
    args = parser.parse_args()

    frontend_url = args.frontend_url.rstrip("/")
    api_url = args.api_url.rstrip("/")
    health_url = args.health_url.rstrip("/")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    health_candidates = [health_url] if health_url else [f"{api_url}/health", f"{api_url}/healthz"]
    resolved_health_url = _wait_for_any_url(
        [candidate for candidate in health_candidates if candidate],
        label="backend health",
    )
    _wait_for_url(frontend_url, label="frontend")
    seed = _seed_backend(api_url)

    screenshots: dict[str, str] = {}
    summary = {
        "frontendUrl": frontend_url,
        "apiUrl": api_url,
        "healthUrl": resolved_health_url,
        "seed": seed,
        "screenshots": screenshots,
        "checks": [],
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.add_init_script(
            "localStorage.setItem('openclaw_court_date', new Date().toISOString().slice(0, 10));"
        )
        page.goto(frontend_url, wait_until="networkidle")
        page.wait_for_timeout(1200)

        body = page.locator("body")
        text = body.inner_text()
        _ensure_text(text, "三省六部 · 总控台", context="app shell")
        _ensure_text(text, "同步正常", context="app shell")
        _ensure_text(text, seed["task_title"], context="edict board")
        edict_path = output_dir / "01-edict-board.png"
        page.screenshot(path=str(edict_path), full_page=False)
        screenshots["edictBoard"] = str(edict_path)
        summary["checks"].append("edicts")

        page.locator(".edict-card", has_text=seed["task_title"]).first.click(force=True)
        page.locator(".modal-bg").wait_for(state="visible", timeout=10000)
        modal_text = body.inner_text()
        _ensure_text(modal_text, seed["task_id"], context="task modal")
        _ensure_text(modal_text, "流转日志", context="task modal")
        _ensure_text(modal_text, "司礼监调度", context="task modal")
        detail_path = output_dir / "02-task-detail.png"
        page.screenshot(path=str(detail_path), full_page=False)
        screenshots["taskDetail"] = str(detail_path)
        summary["checks"].append("task-detail")
        page.locator(".modal-close").click()
        page.locator(".modal-bg").wait_for(state="hidden", timeout=10000)

        panel_checks = [
            ("models", 4, ["派发渠道", "变更日志"], "03-model-config.png"),
            ("skills", 5, ["本地技能", "远程技能"], "04-skills-config.png"),
            ("morning", 9, ["天下要闻", "订阅配置"], "05-morning-panel.png"),
            ("officials", 3, ["功绩排行", "在职官员"], "08-officials-panel.png"),
        ]

        for check_name, tab_index, expected, filename in panel_checks:
            page.locator(".tab").nth(tab_index).click()
            page.wait_for_timeout(1200)
            text = body.inner_text()
            for needle in expected:
                _ensure_text(text, needle, context=check_name)
            shot_path = output_dir / filename
            page.screenshot(path=str(shot_path), full_page=False)
            screenshots[check_name] = str(shot_path)
            summary["checks"].append(check_name)

        page.locator(".tab").nth(1).click()
        page.wait_for_timeout(1200)
        text = body.inner_text()
        for needle in ("🏛 朝堂议政", "选择参朝官员", "开始朝议", seed["court_topic"], "恢复会话"):
            _ensure_text(text, needle, context="court setup")
        court_setup_path = output_dir / "06-court-setup.png"
        page.screenshot(path=str(court_setup_path), full_page=False)
        screenshots["courtSetup"] = str(court_setup_path)
        summary["checks"].append("court-setup")

        page.get_by_text("恢复会话", exact=True).first.click()
        page.wait_for_timeout(1500)
        text = body.inner_text()
        for needle in (seed["court_topic"], "▶ 下一轮", "📋 散朝"):
            _ensure_text(text, needle, context="court session")
        court_session_path = output_dir / "07-court-session.png"
        page.screenshot(path=str(court_session_path), full_page=False)
        screenshots["courtSession"] = str(court_session_path)
        summary["checks"].append("court-session")

        browser.close()

    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
