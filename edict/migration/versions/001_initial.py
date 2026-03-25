"""initial schema

Revision ID: 001_initial
Revises:
Create Date: 2026-03-24 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

TASK_STATE_VALUES_SNAPSHOT = (
    "Pending",
    "Sili",
    "Zhongshu",
    "Menxia",
    "Assigned",
    "Next",
    "Doing",
    "Review",
    "Done",
    "Blocked",
    "Cancelled",
)

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    task_state = postgresql.ENUM(*TASK_STATE_VALUES_SNAPSHOT, name="task_state", create_type=False)
    task_state.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("official", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("org", sa.String(length=64), nullable=False, server_default="皇上"),
        sa.Column("state", task_state, nullable=False, server_default="Pending"),
        sa.Column("now", sa.Text(), server_default=""),
        sa.Column("eta", sa.String(length=64), server_default="-"),
        sa.Column("block", sa.Text(), server_default="无"),
        sa.Column("output", sa.Text(), server_default=""),
        sa.Column("ac", sa.Text(), server_default=""),
        sa.Column("priority", sa.String(length=16), server_default="normal"),
        sa.Column("lane", sa.String(length=16), nullable=False, server_default="standard"),
        sa.Column("review_round", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("flow_log", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("progress_log", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("consult_log", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("todos", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("scheduler", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("template_id", sa.String(length=64), server_default=""),
        sa.Column("template_params", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("target_dept", sa.String(length=64), server_default=""),
        sa.Column("prev_state", sa.String(length=32), server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tasks_state_archived", "tasks", ["state", "archived"])
    op.create_index("ix_tasks_updated_at", "tasks", ["updated_at"])
    op.create_index("ix_tasks_org_state", "tasks", ["org", "state"])

    op.create_table(
        "events",
        sa.Column("event_id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("topic", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("producer", sa.String(length=128), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=True),
        sa.Column("stream_entry_id", sa.String(length=64), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("meta", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("topic", "dedupe_key", name="uq_events_topic_dedupe_key"),
    )
    op.create_index("ix_events_trace_id", "events", ["trace_id"])
    op.create_index("ix_events_topic", "events", ["topic"])
    op.create_index("ix_events_timestamp", "events", ["timestamp"])
    op.create_index("ix_events_dedupe_key", "events", ["dedupe_key"])

    op.create_table(
        "thoughts",
        sa.Column("thought_id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("agent", sa.String(length=50), nullable=False),
        sa.Column("step", sa.Integer(), server_default="0"),
        sa.Column("type", sa.String(length=30), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tokens", sa.Integer(), server_default="0"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("meta", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("thought_id"),
    )
    op.create_index("ix_thoughts_trace_id", "thoughts", ["trace_id"])
    op.create_index("ix_thoughts_agent", "thoughts", ["agent"])

    op.create_table(
        "todos",
        sa.Column("todo_id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), server_default=""),
        sa.Column("owner", sa.String(length=64), server_default=""),
        sa.Column("assignee_agent", sa.String(length=32), server_default=""),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("priority", sa.String(length=16), server_default="normal"),
        sa.Column("estimated_cost", sa.Float(), server_default="0"),
        sa.Column("created_by", sa.String(length=64), server_default=""),
        sa.Column("checkpoints", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("todo_id"),
    )
    op.create_index("ix_todos_trace_status", "todos", ["trace_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_todos_trace_status", table_name="todos")
    op.drop_table("todos")
    op.drop_index("ix_thoughts_agent", table_name="thoughts")
    op.drop_index("ix_thoughts_trace_id", table_name="thoughts")
    op.drop_table("thoughts")
    op.drop_index("ix_events_timestamp", table_name="events")
    op.drop_index("ix_events_topic", table_name="events")
    op.drop_index("ix_events_trace_id", table_name="events")
    op.drop_index("ix_events_dedupe_key", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_tasks_org_state", table_name="tasks")
    op.drop_index("ix_tasks_updated_at", table_name="tasks")
    op.drop_index("ix_tasks_state_archived", table_name="tasks")
    op.drop_table("tasks")
    sa.Enum(name="task_state").drop(op.get_bind(), checkfirst=True)
