#!/usr/bin/env python3
"""Observe a local Stage-5 v2 window and persist objective evidence."""

from __future__ import annotations

import argparse
import json
import socket
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> tuple[int, str]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.status, response.read().decode("utf-8", "replace")


def _request_json(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    status, body = _request(url, method=method, payload=payload)
    if status >= 400:
        raise RuntimeError(f"{url} returned {status}")
    return json.loads(body or "{}")


def _request_status(url: str) -> int:
    status, _ = _request(url)
    return status


def _port_open(host: str, port: int) -> bool:
    sock = socket.socket()
    sock.settimeout(1)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _parse_host_port(raw_url: str) -> tuple[str, int]:
    stripped = raw_url.replace("http://", "").replace("https://", "").split("/", 1)[0]
    if ":" in stripped:
        host, port = stripped.rsplit(":", 1)
        return host, int(port)
    return stripped, 80


def _seed_task(api_url: str) -> dict[str, Any]:
    suffix = int(time.time())
    title = f"Stage5 观察窗口演练 #{suffix}"
    payload = {
        "title": title,
        "targetDept": "工部",
        "actor": "emperor",
        "source": "stage5-observation",
    }
    created = _request_json(f"{api_url}/api/create-task", method="POST", payload=payload)
    if not created.get("ok") or not created.get("taskId"):
        raise AssertionError(f"failed to create canary task: {created}")
    return {
        "taskId": str(created["taskId"]),
        "title": title,
        "createResult": created,
    }


def _sample_once(
    *,
    backend_url: str,
    frontend_url: str,
    legacy_host: str,
    legacy_port: int,
    require_legacy_down: bool,
    canary_task_id: str,
) -> dict[str, Any]:
    health = _request_json(f"{backend_url}/health")
    deep = _request_json(f"{backend_url}/api/admin/health/deep")
    metrics = _request_json(f"{backend_url}/api/metrics/snapshot")
    queue = _request_json(f"{backend_url}/api/queue-metrics")
    live_status = _request_json(f"{backend_url}/api/live-status")
    frontend_status = _request_status(frontend_url)
    legacy_open = _port_open(legacy_host, legacy_port)

    task_present = any(
        str(item.get("id") or "") == canary_task_id
        for item in (live_status.get("tasks") or [])
        if isinstance(item, dict)
    )
    sample = {
        "ts": _now_iso(),
        "backendHealth": health,
        "deepHealthStatus": str(deep.get("status") or ""),
        "workersOk": bool(((deep.get("checks") or {}).get("workers") or {}).get("ok")),
        "metricsWorkersOk": bool((metrics.get("workers") or {}).get("ok")),
        "queueOk": bool(queue.get("ok")),
        "frontendStatus": int(frontend_status),
        "legacyPortOpen": bool(legacy_open),
        "canaryTaskPresent": bool(task_present),
        "liveStatusTaskCount": len(live_status.get("tasks") or []),
    }
    sample["ok"] = (
        sample["backendHealth"].get("status") == "ok"
        and sample["deepHealthStatus"] == "ok"
        and sample["workersOk"]
        and sample["metricsWorkersOk"]
        and sample["queueOk"]
        and sample["frontendStatus"] < 400
        and sample["canaryTaskPresent"]
        and ((not require_legacy_down) or (not sample["legacyPortOpen"]))
    )
    return sample


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    parser.add_argument("--frontend-url", default="http://127.0.0.1:5173")
    parser.add_argument("--legacy-url", default="http://127.0.0.1:7891/healthz")
    parser.add_argument("--duration-sec", type=int, default=120)
    parser.add_argument("--interval-sec", type=int, default=15)
    parser.add_argument(
        "--output",
        default=str(Path("ops") / "artifacts" / "stage5-observation-window" / "summary.json"),
    )
    parser.add_argument("--allow-legacy-up", action="store_true")
    args = parser.parse_args()

    backend_url = args.backend_url.rstrip("/")
    frontend_url = args.frontend_url.rstrip("/")
    legacy_host, legacy_port = _parse_host_port(args.legacy_url.rstrip("/"))
    require_legacy_down = not args.allow_legacy_up

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    started_at = _now_iso()
    canary = _seed_task(backend_url)
    samples: list[dict[str, Any]] = []
    deadline = time.time() + max(1, args.duration_sec)

    while True:
        sample = _sample_once(
            backend_url=backend_url,
            frontend_url=frontend_url,
            legacy_host=legacy_host,
            legacy_port=legacy_port,
            require_legacy_down=require_legacy_down,
            canary_task_id=canary["taskId"],
        )
        samples.append(sample)
        if time.time() >= deadline:
            break
        time.sleep(max(1, args.interval_sec))

    failed_samples = [sample for sample in samples if not sample["ok"]]
    summary = {
        "startedAt": started_at,
        "finishedAt": _now_iso(),
        "durationSec": int(args.duration_sec),
        "intervalSec": int(args.interval_sec),
        "backendUrl": backend_url,
        "frontendUrl": frontend_url,
        "legacyUrl": args.legacy_url,
        "requireLegacyDown": require_legacy_down,
        "canary": canary,
        "sampleCount": len(samples),
        "ok": not failed_samples,
        "failedSampleCount": len(failed_samples),
        "samples": samples,
    }
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
