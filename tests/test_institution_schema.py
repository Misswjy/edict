"""Tests for the shared institution schema and generated assets."""

import json
import pathlib

from scripts.generate_institution_assets import (
    DOC_OUT,
    OPENCLAW_OUT,
    PY_OUT,
    TS_OUT,
    build_data,
    load_schema,
    render_docs,
    render_openclaw,
    render_python,
    render_ts,
)


ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_generated_assets_match_institution_schema():
    data = build_data(load_schema())

    assert PY_OUT.read_text(encoding="utf-8") == render_python(data)
    assert TS_OUT.read_text(encoding="utf-8") == render_ts(data)
    assert DOC_OUT.read_text(encoding="utf-8") == render_docs(data)
    assert json.loads(OPENCLAW_OUT.read_text(encoding="utf-8")) == json.loads(render_openclaw(data))


def test_frontend_store_uses_generated_institution_schema():
    store_path = ROOT / "edict" / "frontend" / "src" / "store.ts"
    content = store_path.read_text(encoding="utf-8")
    assert "from './generated/institutionSchema'" in content
    assert "export const DEPTS: ReadonlyArray<DeptDefinition> = GENERATED_DEPTS;" in content


def test_task_contract_uses_generated_institution_schema():
    task_contract_path = ROOT / "edict" / "backend" / "app" / "task_contract.py"
    content = task_contract_path.read_text(encoding="utf-8")
    assert "from .generated.institution_schema import (" in content
