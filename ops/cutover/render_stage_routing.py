#!/usr/bin/env python3
"""Render staged cutover routing config and manifest files."""

import argparse
import json
import re
import shlex
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

V2_UPSTREAM_DEFAULT = "http://backend:8000"
LEGACY_UPSTREAM_DEFAULT = "http://host.docker.internal:7891"
FRONTEND_API_BASE = "/api"

READ_EXACT_PATHS = [
    "/api/live-status",
    "/api/queue-metrics",
    "/api/agent-config",
    "/api/model-change-log",
    "/api/officials-stats",
    "/api/morning-brief",
    "/api/morning-config",
    "/api/agents-status",
    "/api/remote-skills-list",
    "/api/court-discuss/list",
    "/api/court-discuss/officials",
    "/api/court-discuss/fate",
]

READ_PREFIX_PATHS = [
    "/api/morning-brief/",
    "/api/task-activity/",
    "/api/scheduler-state/",
    "/api/skill-content/",
    "/api/court-discuss/session/",
]

WRITE_EXACT_PATHS = [
    "/api/set-model",
    "/api/set-dispatch-channel",
    "/api/agent-wake",
    "/api/task-action",
    "/api/review-action",
    "/api/advance-state",
    "/api/archive-task",
    "/api/scheduler-scan",
    "/api/scheduler-retry",
    "/api/scheduler-escalate",
    "/api/scheduler-rollback",
    "/api/morning-brief/refresh",
    "/api/morning-config",
    "/api/add-skill",
    "/api/add-remote-skill",
    "/api/update-remote-skill",
    "/api/remove-remote-skill",
    "/api/create-task",
    "/api/court-discuss/start",
    "/api/court-discuss/advance",
    "/api/court-discuss/conclude",
    "/api/court-discuss/destroy",
]


@dataclass(frozen=True)
class RouteRule:
    method: str
    path: str
    target: str
    match_type: str = "exact"
    semantic: str = ""

    def matches(self, method: str, path: str) -> bool:
        if method.upper() != self.method.upper():
            return False
        if self.match_type == "exact":
            return path == self.path
        return path.startswith(self.path)


@dataclass(frozen=True)
class StagePlan:
    stage: int
    stage_name: str
    description: str
    default_api_target: str
    read_to_v2: bool
    write_to_v2: bool
    worker_to_v2: bool
    scheduler_to_v2: bool
    legacy_observation: bool
    freeze_legacy_writes: bool
    stop_legacy_loop: bool
    compose_up_services: tuple[str, ...]
    compose_stop_services: tuple[str, ...]
    ws_mode: str
    route_rules: tuple[RouteRule, ...]


def _normalize_url(value: str) -> str:
    return value.strip().rstrip("/")


def _default_route_rules(stage: int, v2_upstream: str) -> tuple[RouteRule, ...]:
    rules: list[RouteRule] = []
    if stage >= 1:
        rules.extend(RouteRule("GET", path, v2_upstream, semantic="read") for path in READ_EXACT_PATHS)
        rules.extend(RouteRule("GET", path, v2_upstream, match_type="prefix", semantic="read") for path in READ_PREFIX_PATHS)
    if stage >= 2:
        rules.extend(RouteRule("POST", path, v2_upstream, semantic="write") for path in WRITE_EXACT_PATHS)
    return tuple(rules)


def build_stage_plan(
    stage: int,
    *,
    legacy_upstream: str = LEGACY_UPSTREAM_DEFAULT,
    v2_upstream: str = V2_UPSTREAM_DEFAULT,
) -> StagePlan:
    legacy_upstream = _normalize_url(legacy_upstream)
    v2_upstream = _normalize_url(v2_upstream)
    base_services = ("postgres", "redis", "backend", "frontend")

    if stage == 0:
        return StagePlan(
            stage=0,
            stage_name="legacy_rollback",
            description="Frontend traffic returns to legacy; WebSocket falls back to polling.",
            default_api_target=legacy_upstream,
            read_to_v2=False,
            write_to_v2=False,
            worker_to_v2=False,
            scheduler_to_v2=False,
            legacy_observation=False,
            freeze_legacy_writes=False,
            stop_legacy_loop=False,
            compose_up_services=("frontend",),
            compose_stop_services=("scheduler", "dispatcher", "orchestrator", "backend"),
            ws_mode="disabled",
            route_rules=(),
        )
    if stage not in {1, 2, 3, 4, 5}:
        raise ValueError(f"unsupported stage: {stage}")

    config = {
        1: {
            "stage_name": "frontend_reads_v2",
            "description": "Frontend read traffic moves to v2; manual writes stay on legacy.",
            "read_to_v2": True,
            "write_to_v2": False,
            "worker_to_v2": False,
            "scheduler_to_v2": False,
            "legacy_observation": False,
            "freeze_legacy_writes": False,
            "stop_legacy_loop": False,
            "compose_up_services": base_services,
            "compose_stop_services": ("scheduler", "dispatcher", "orchestrator"),
            "ws_mode": "v2",
            "default_api_target": legacy_upstream,
        },
        2: {
            "stage_name": "manual_writes_v2",
            "description": "Frontend reads and manual control writes move to v2; legacy runtime remains standby.",
            "read_to_v2": True,
            "write_to_v2": True,
            "worker_to_v2": False,
            "scheduler_to_v2": False,
            "legacy_observation": False,
            "freeze_legacy_writes": True,
            "stop_legacy_loop": False,
            "compose_up_services": base_services,
            "compose_stop_services": ("scheduler", "dispatcher", "orchestrator"),
            "ws_mode": "v2",
            "default_api_target": legacy_upstream,
        },
        3: {
            "stage_name": "dispatch_and_events_v2",
            "description": "Reads, writes, orchestrator, and dispatcher move to v2; legacy scheduler stays active.",
            "read_to_v2": True,
            "write_to_v2": True,
            "worker_to_v2": True,
            "scheduler_to_v2": False,
            "legacy_observation": False,
            "freeze_legacy_writes": True,
            "stop_legacy_loop": False,
            "compose_up_services": base_services + ("orchestrator", "dispatcher"),
            "compose_stop_services": ("scheduler",),
            "ws_mode": "v2",
            "default_api_target": legacy_upstream,
        },
        4: {
            "stage_name": "scheduler_v2",
            "description": "Scheduling moves to v2; legacy loop should be stopped.",
            "read_to_v2": True,
            "write_to_v2": True,
            "worker_to_v2": True,
            "scheduler_to_v2": True,
            "legacy_observation": False,
            "freeze_legacy_writes": True,
            "stop_legacy_loop": True,
            "compose_up_services": base_services + ("orchestrator", "dispatcher", "scheduler"),
            "compose_stop_services": (),
            "ws_mode": "v2",
            "default_api_target": legacy_upstream,
        },
        5: {
            "stage_name": "legacy_observation_window",
            "description": "All production traffic stays on v2 while legacy enters read-only observation.",
            "read_to_v2": True,
            "write_to_v2": True,
            "worker_to_v2": True,
            "scheduler_to_v2": True,
            "legacy_observation": True,
            "freeze_legacy_writes": True,
            "stop_legacy_loop": True,
            "compose_up_services": base_services + ("orchestrator", "dispatcher", "scheduler"),
            "compose_stop_services": (),
            "ws_mode": "v2",
            "default_api_target": v2_upstream,
        },
    }[stage]

    return StagePlan(
        stage=stage,
        stage_name=config["stage_name"],
        description=config["description"],
        default_api_target=config["default_api_target"],
        read_to_v2=config["read_to_v2"],
        write_to_v2=config["write_to_v2"],
        worker_to_v2=config["worker_to_v2"],
        scheduler_to_v2=config["scheduler_to_v2"],
        legacy_observation=config["legacy_observation"],
        freeze_legacy_writes=config["freeze_legacy_writes"],
        stop_legacy_loop=config["stop_legacy_loop"],
        compose_up_services=config["compose_up_services"],
        compose_stop_services=config["compose_stop_services"],
        ws_mode=config["ws_mode"],
        route_rules=_default_route_rules(stage, v2_upstream),
    )


def route_target_for(plan: StagePlan, method: str, path: str) -> str:
    method = method.upper()
    for rule in plan.route_rules:
        if rule.matches(method, path):
            return rule.target
    return plan.default_api_target


def _render_rule(rule: RouteRule) -> str:
    if rule.match_type == "exact":
        key = f'"{rule.method.upper()}:{rule.path}"'
    else:
        key = f'~^{re.escape(rule.method.upper())}:{re.escape(rule.path)}'
    return f"    {key} {rule.target};"


def render_nginx_conf(plan: StagePlan) -> str:
    lines = [
        'map "$request_method:$uri" $edict_api_upstream {',
        f"    default {plan.default_api_target};",
    ]
    lines.extend(_render_rule(rule) for rule in plan.route_rules)
    lines.extend(
        [
            "}",
            "",
            "server {",
            "    listen 3000;",
            "    root /usr/share/nginx/html;",
            "    index index.html;",
            "",
            f"    # Generated by render_stage_routing.py for stage {plan.stage}: {plan.stage_name}",
            "",
            "    location / {",
            "        try_files $uri $uri/ /index.html;",
            "    }",
            "",
            "    location /api/ {",
            "        proxy_pass $edict_api_upstream;",
            "        proxy_http_version 1.1;",
            "        proxy_set_header Host $host;",
            "        proxy_set_header X-Real-IP $remote_addr;",
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
            "        proxy_set_header X-Forwarded-Proto $scheme;",
            "    }",
            "",
        ]
    )
    if plan.ws_mode == "v2":
        lines.extend(
            [
                "    location /ws {",
                "        proxy_pass http://backend:8000;",
                "        proxy_http_version 1.1;",
                "        proxy_set_header Upgrade $http_upgrade;",
                '        proxy_set_header Connection "upgrade";',
                "        proxy_set_header Host $host;",
                "        proxy_set_header X-Real-IP $remote_addr;",
                "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
                "        proxy_set_header X-Forwarded-Proto $scheme;",
                "        proxy_read_timeout 86400;",
                "    }",
            ]
        )
    else:
        lines.extend(
            [
                "    location /ws {",
                "        return 410;",
                "    }",
            ]
        )
    lines.extend(["}", ""])
    return "\n".join(lines)


def manifest_for(plan: StagePlan, routing_config_file: str, manifest_file: str) -> dict[str, object]:
    return {
        "stage": plan.stage,
        "stageName": plan.stage_name,
        "description": plan.description,
        "frontendApiBase": FRONTEND_API_BASE,
        "defaultApiTarget": plan.default_api_target,
        "readToV2": plan.read_to_v2,
        "writeToV2": plan.write_to_v2,
        "workerToV2": plan.worker_to_v2,
        "schedulerToV2": plan.scheduler_to_v2,
        "legacyObservation": plan.legacy_observation,
        "freezeLegacyWrites": plan.freeze_legacy_writes,
        "stopLegacyLoop": plan.stop_legacy_loop,
        "composeUpServices": list(plan.compose_up_services),
        "composeStopServices": list(plan.compose_stop_services),
        "wsMode": plan.ws_mode,
        "routingConfigFile": routing_config_file,
        "manifestFile": manifest_file,
        "routeRules": [asdict(rule) for rule in plan.route_rules],
    }


def shell_exports(plan: StagePlan, routing_config_file: str, manifest_file: str) -> str:
    values = {
        "STAGE": str(plan.stage),
        "STAGE_NAME": plan.stage_name,
        "STAGE_DESCRIPTION": plan.description,
        "COMPOSE_UP_SERVICES": " ".join(plan.compose_up_services),
        "COMPOSE_STOP_SERVICES": " ".join(plan.compose_stop_services),
        "FREEZE_LEGACY_WRITES": "1" if plan.freeze_legacy_writes else "0",
        "STOP_LEGACY_LOOP": "1" if plan.stop_legacy_loop else "0",
        "LEGACY_OBSERVATION": "1" if plan.legacy_observation else "0",
        "FRONTEND_API_BASE": FRONTEND_API_BASE,
        "ROUTING_CONFIG_FILE": routing_config_file,
        "MANIFEST_FILE": manifest_file,
    }
    return "\n".join(f"{key}={shlex.quote(value)}" for key, value in values.items())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=int, required=True, help="Cutover stage: 1-5, or 0 for full legacy rollback.")
    parser.add_argument("--legacy-upstream-url", default=LEGACY_UPSTREAM_DEFAULT)
    parser.add_argument("--v2-upstream-url", default=V2_UPSTREAM_DEFAULT)
    parser.add_argument("--output", default="ops/cutover/generated/frontend-default.conf")
    parser.add_argument("--manifest-file", default="ops/cutover/generated/stage-manifest.json")
    parser.add_argument("--emit-env", action="store_true", help="Print shell exports to stdout.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write files; only compute the plan.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    plan = build_stage_plan(
        args.stage,
        legacy_upstream=args.legacy_upstream_url,
        v2_upstream=args.v2_upstream_url,
    )
    output = Path(args.output)
    manifest_path = Path(args.manifest_file)
    manifest = manifest_for(plan, str(output), str(manifest_path))
    rendered = render_nginx_conf(plan)

    if not args.dry_run:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        print(
            f"[render-stage-routing] dry-run stage={plan.stage} output={output} manifest={manifest_path}",
            file=sys.stderr,
        )

    if args.emit_env:
        print(shell_exports(plan, str(output), str(manifest_path)))
    else:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
