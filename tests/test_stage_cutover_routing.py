"""Regression tests for staged cutover routing helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[1] / "ops" / "cutover" / "render_stage_routing.py"
    spec = importlib.util.spec_from_file_location("render_stage_routing", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_stage1_routes_reads_to_v2_but_keeps_writes_on_legacy():
    mod = _load_module()
    plan = mod.build_stage_plan(1)

    assert mod.route_target_for(plan, "GET", "/api/live-status") == mod.V2_UPSTREAM_DEFAULT
    assert mod.route_target_for(plan, "GET", "/api/morning-config") == mod.V2_UPSTREAM_DEFAULT
    assert mod.route_target_for(plan, "POST", "/api/morning-config") == mod.LEGACY_UPSTREAM_DEFAULT
    assert mod.route_target_for(plan, "POST", "/api/task-action") == mod.LEGACY_UPSTREAM_DEFAULT
    assert plan.compose_up_services == ("postgres", "redis", "backend", "frontend")
    assert plan.compose_stop_services == ("scheduler", "dispatcher", "orchestrator")
    assert plan.freeze_legacy_writes is False


def test_stage2_routes_manual_writes_to_v2_but_keeps_unknown_paths_on_legacy():
    mod = _load_module()
    plan = mod.build_stage_plan(2)

    assert mod.route_target_for(plan, "POST", "/api/task-action") == mod.V2_UPSTREAM_DEFAULT
    assert mod.route_target_for(plan, "POST", "/api/create-task") == mod.V2_UPSTREAM_DEFAULT
    assert mod.route_target_for(plan, "POST", "/api/court-discuss/start") == mod.V2_UPSTREAM_DEFAULT
    assert mod.route_target_for(plan, "POST", "/api/repair-flow-order") == mod.LEGACY_UPSTREAM_DEFAULT
    assert plan.freeze_legacy_writes is True
    assert plan.stop_legacy_loop is False


def test_stage4_and_stage5_service_gates_match_checklist_semantics():
    mod = _load_module()
    stage4 = mod.build_stage_plan(4)
    stage5 = mod.build_stage_plan(5)

    assert stage4.compose_up_services == ("postgres", "redis", "backend", "frontend", "orchestrator", "dispatcher", "scheduler")
    assert stage4.stop_legacy_loop is True
    assert stage4.legacy_observation is False
    assert stage5.default_api_target == mod.V2_UPSTREAM_DEFAULT
    assert stage5.legacy_observation is True
    assert mod.route_target_for(stage5, "POST", "/api/repair-flow-order") == mod.V2_UPSTREAM_DEFAULT


def test_legacy_rollback_plan_disables_websocket_and_defaults_to_legacy():
    mod = _load_module()
    plan = mod.build_stage_plan(0)
    rendered = mod.render_nginx_conf(plan)

    assert mod.route_target_for(plan, "GET", "/api/live-status") == mod.LEGACY_UPSTREAM_DEFAULT
    assert 'return 410;' in rendered
    assert '"GET:/api/live-status"' not in rendered


def test_rendered_stage2_config_contains_method_aware_map_rules():
    mod = _load_module()
    plan = mod.build_stage_plan(2)
    rendered = mod.render_nginx_conf(plan)

    assert 'map "$request_method:$uri" $edict_api_upstream {' in rendered
    assert '"GET:/api/live-status" http://backend:8000;' in rendered
    assert '"POST:/api/task-action" http://backend:8000;' in rendered
    assert '~^GET:/api/task\\-activity/' in rendered
    assert 'proxy_pass $edict_api_upstream;' in rendered
    assert 'proxy_pass http://backend:8000;' in rendered
