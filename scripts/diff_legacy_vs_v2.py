#!/usr/bin/env python3
"""Field-level parity diff tool for legacy and v2 compatibility endpoints.

Supports two comparison modes:
1. Offline: compare captured JSON files.
2. Online: fetch the same endpoint from legacy/v2 environments and diff results.

The tool is intentionally strict by default, but ships with a small per-mode
ignore list for known dynamic metadata such as `checkedAt`.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path
from typing import Any
from urllib import error, request


DEFAULT_IGNORE_PATTERNS: dict[str, list[str]] = {
    "live-status": [
        "syncStatus/controlPlane",
        "syncStatus/engine",
        "syncStatus/queueMetrics*",
        "syncStatus/updatedAt",
    ],
    "queue-metrics": [
        "checkedAt",
    ],
    "scheduler-state": [
        "checkedAt",
    ],
    "action": [],
}


def _read_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_ignore_patterns(paths: list[str], ignore_file: str) -> list[str]:
    patterns = [item.strip() for item in paths if item.strip()]
    if ignore_file:
        for line in Path(ignore_file).read_text(encoding="utf-8").splitlines():
            candidate = line.strip()
            if candidate and not candidate.startswith("#"):
                patterns.append(candidate)
    return patterns


def _canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonicalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    return value


def _sorted_task_list(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((_canonicalize(item) for item in items), key=lambda item: str(item.get("id") or item.get("taskId") or ""))


def normalize_live_status(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload or {})
    tasks = data.get("tasks") or []
    if isinstance(tasks, list):
        data["tasks"] = _sorted_task_list([dict(item) for item in tasks if isinstance(item, dict)])
    sync_status = dict(data.get("syncStatus") or {})
    queue_metrics = sync_status.get("queueMetrics") or {}
    if isinstance(queue_metrics, dict):
        normalized_queues: dict[str, Any] = {}
        for queue_name in sorted(queue_metrics):
            queue_payload = queue_metrics.get(queue_name) or {}
            if isinstance(queue_payload, dict):
                queue_copy = dict(queue_payload)
                tasks_list = queue_copy.get("tasks") or []
                if isinstance(tasks_list, list):
                    queue_copy["tasks"] = sorted(
                        (_canonicalize(item) for item in tasks_list if isinstance(item, dict)),
                        key=lambda item: str(item.get("taskId") or item.get("id") or ""),
                    )
                normalized_queues[queue_name] = _canonicalize(queue_copy)
        sync_status["queueMetrics"] = normalized_queues
    data["syncStatus"] = _canonicalize(sync_status)
    return _canonicalize(data)


def normalize_queue_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload or {})
    queues = data.get("queues") or {}
    if isinstance(queues, dict):
        normalized_queues: dict[str, Any] = {}
        for queue_name in sorted(queues):
            queue_payload = queues.get(queue_name) or {}
            if isinstance(queue_payload, dict):
                queue_copy = dict(queue_payload)
                tasks_list = queue_copy.get("tasks") or []
                if isinstance(tasks_list, list):
                    queue_copy["tasks"] = sorted(
                        (_canonicalize(item) for item in tasks_list if isinstance(item, dict)),
                        key=lambda item: str(item.get("taskId") or item.get("id") or ""),
                    )
                normalized_queues[queue_name] = _canonicalize(queue_copy)
        data["queues"] = normalized_queues
    return _canonicalize(data)


def normalize_scheduler_state(payload: dict[str, Any]) -> dict[str, Any]:
    return _canonicalize(dict(payload or {}))


def normalize_action_result(payload: dict[str, Any]) -> dict[str, Any]:
    return _canonicalize(dict(payload or {}))


def normalize_payload(mode: str, payload: Any) -> Any:
    if not isinstance(payload, dict):
        return _canonicalize(payload)
    if mode == "live-status":
        return normalize_live_status(payload)
    if mode == "queue-metrics":
        return normalize_queue_metrics(payload)
    if mode == "scheduler-state":
        return normalize_scheduler_state(payload)
    if mode == "action":
        return normalize_action_result(payload)
    return _canonicalize(payload)


def _path_join(prefix: str, suffix: str) -> str:
    if not prefix:
        return suffix
    if not suffix:
        return prefix
    return f"{prefix}/{suffix}"


def _value_preview(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    return value


def diff_values(legacy: Any, v2: Any, *, path: str = "") -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    if type(legacy) is not type(v2):
        diffs.append({"path": path or "$", "legacy": _value_preview(legacy), "v2": _value_preview(v2), "kind": "type"})
        return diffs

    if isinstance(legacy, dict):
        keys = sorted(set(legacy) | set(v2))
        for key in keys:
            child_path = _path_join(path, str(key))
            if key not in legacy:
                diffs.append({"path": child_path, "legacy": None, "v2": _value_preview(v2.get(key)), "kind": "missing-in-legacy"})
                continue
            if key not in v2:
                diffs.append({"path": child_path, "legacy": _value_preview(legacy.get(key)), "v2": None, "kind": "missing-in-v2"})
                continue
            diffs.extend(diff_values(legacy[key], v2[key], path=child_path))
        return diffs

    if isinstance(legacy, list):
        max_len = max(len(legacy), len(v2))
        for idx in range(max_len):
            child_path = _path_join(path, str(idx))
            if idx >= len(legacy):
                diffs.append({"path": child_path, "legacy": None, "v2": _value_preview(v2[idx]), "kind": "missing-in-legacy"})
                continue
            if idx >= len(v2):
                diffs.append({"path": child_path, "legacy": _value_preview(legacy[idx]), "v2": None, "kind": "missing-in-v2"})
                continue
            diffs.extend(diff_values(legacy[idx], v2[idx], path=child_path))
        return diffs

    if legacy != v2:
        diffs.append({"path": path or "$", "legacy": _value_preview(legacy), "v2": _value_preview(v2), "kind": "value"})
    return diffs


def partition_diffs(diffs: list[dict[str, Any]], ignore_patterns: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    for item in diffs:
        path = str(item.get("path") or "")
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in ignore_patterns):
            ignored.append(item)
        else:
            kept.append(item)
    return kept, ignored


def compare_payloads(mode: str, legacy: Any, v2: Any, *, ignore_patterns: list[str] | None = None) -> dict[str, Any]:
    normalized_legacy = normalize_payload(mode, legacy)
    normalized_v2 = normalize_payload(mode, v2)
    all_diffs = diff_values(normalized_legacy, normalized_v2)
    ignore_patterns = list(ignore_patterns or [])
    diffs, ignored = partition_diffs(all_diffs, ignore_patterns)
    return {
        "mode": mode,
        "ok": not diffs,
        "diffCount": len(diffs),
        "ignoredDiffCount": len(ignored),
        "ignorePatterns": ignore_patterns,
        "diffs": diffs,
        "ignoredDiffs": ignored,
        "legacy": normalized_legacy,
        "v2": normalized_v2,
    }


def _build_endpoint_path(mode: str, args: argparse.Namespace) -> str:
    if mode == "live-status":
        return "/api/live-status"
    if mode == "queue-metrics":
        return "/api/queue-metrics"
    if mode == "scheduler-state":
        if not args.task_id:
            raise ValueError("--task-id is required for scheduler-state mode")
        return f"/api/scheduler-state/{args.task_id}"
    if mode == "action":
        endpoint = str(args.endpoint or "").strip()
        if not endpoint:
            raise ValueError("--endpoint is required for action mode")
        if not endpoint.startswith("/"):
            endpoint = f"/api/{endpoint}"
        return endpoint
    raise ValueError(f"unsupported mode: {mode}")


def _fetch_json(base_url: str, path: str, *, method: str = "GET", body: dict[str, Any] | None = None, token: str = "") -> Any:
    url = f"{base_url.rstrip('/')}{path}"
    payload = None
    headers = {"Accept": "application/json"}
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Edict-Admin-Token"] = token
    req = request.Request(url, data=payload, method=method.upper(), headers=headers)
    try:
        with request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method.upper()} {url} -> HTTP {exc.code}: {raw}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"{method.upper()} {url} failed: {exc}") from exc
    return json.loads(raw or "{}")


def _load_sources(mode: str, args: argparse.Namespace) -> tuple[Any, Any, dict[str, str]]:
    if args.legacy_file and args.v2_file:
        legacy = _read_json_file(Path(args.legacy_file))
        v2 = _read_json_file(Path(args.v2_file))
        return legacy, v2, {"legacy": args.legacy_file, "v2": args.v2_file}

    if not args.legacy_base_url or not args.v2_base_url:
        raise ValueError("online compare requires both --legacy-base-url and --v2-base-url, or use --legacy-file/--v2-file")

    path = _build_endpoint_path(mode, args)
    method = "POST" if mode == "action" else "GET"
    body = None
    if mode == "action":
        if not args.body_file:
            raise ValueError("--body-file is required for action mode")
        if not args.allow_mutation:
            raise ValueError("action mode mutates environments; rerun with --allow-mutation to continue")
        body = _read_json_file(Path(args.body_file))

    legacy = _fetch_json(args.legacy_base_url, path, method=method, body=body, token=args.legacy_token)
    v2 = _fetch_json(args.v2_base_url, path, method=method, body=body, token=args.v2_token)
    return legacy, v2, {"legacy": f"{args.legacy_base_url.rstrip('/')}{path}", "v2": f"{args.v2_base_url.rstrip('/')}{path}"}


def _print_report(report: dict[str, Any], *, sources: dict[str, str]) -> None:
    print(f"mode={report['mode']}")
    print(f"legacy={sources['legacy']}")
    print(f"v2={sources['v2']}")
    print(f"diffs={report['diffCount']} ignored={report['ignoredDiffCount']} ok={str(report['ok']).lower()}")
    if report["ignorePatterns"]:
        print("ignore_patterns=" + ",".join(report["ignorePatterns"]))
    if report["diffs"]:
        print("field_diffs:")
        for item in report["diffs"]:
            print(
                json.dumps(
                    {
                        "path": item["path"],
                        "kind": item["kind"],
                        "legacy": item["legacy"],
                        "v2": item["v2"],
                    },
                    ensure_ascii=False,
                )
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare legacy and v2 compatibility responses")
    sub = parser.add_subparsers(dest="mode", required=True)

    def add_common(target: argparse.ArgumentParser) -> None:
        target.add_argument("--legacy-file", help="Captured legacy JSON file")
        target.add_argument("--v2-file", help="Captured v2 JSON file")
        target.add_argument("--legacy-base-url", help="Legacy base URL, e.g. http://127.0.0.1:7891")
        target.add_argument("--v2-base-url", help="V2 base URL, e.g. http://127.0.0.1:8000")
        target.add_argument("--legacy-token", default="", help="Optional admin token for legacy endpoint")
        target.add_argument("--v2-token", default="", help="Optional admin token for v2 endpoint")
        target.add_argument("--ignore", action="append", default=[], help="Extra ignore pattern, slash path syntax, supports *")
        target.add_argument("--ignore-file", default="", help="File containing ignore patterns, one per line")
        target.add_argument("--no-default-ignore", action="store_true", help="Disable built-in per-mode ignore patterns")
        target.add_argument("--report-file", default="", help="Write full JSON report to file")

    for mode in ("live-status", "queue-metrics", "scheduler-state"):
        subparser = sub.add_parser(mode)
        add_common(subparser)
        if mode == "scheduler-state":
            subparser.add_argument("--task-id", help="Task id for scheduler-state compare")

    action = sub.add_parser("action")
    add_common(action)
    action.add_argument("--endpoint", help="Action endpoint path or shorthand, e.g. task-action")
    action.add_argument("--body-file", help="JSON body file used for both legacy and v2 action request")
    action.add_argument("--allow-mutation", action="store_true", help="Required for online action compare because it mutates both targets")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        legacy, v2, sources = _load_sources(args.mode, args)
        ignores = [] if args.no_default_ignore else list(DEFAULT_IGNORE_PATTERNS.get(args.mode, []))
        ignores.extend(_load_ignore_patterns(args.ignore, args.ignore_file))
        report = compare_payloads(args.mode, legacy, v2, ignore_patterns=ignores)
        report["sources"] = sources
        _print_report(report, sources=sources)
        if args.report_file:
            Path(args.report_file).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if report["ok"] else 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
