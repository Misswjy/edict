"""Service package with lazy imports to keep lightweight contexts importable."""

from __future__ import annotations

__all__ = ["EventBus", "get_event_bus", "TaskService"]


def __getattr__(name: str):
    if name in {"EventBus", "get_event_bus"}:
        from .event_bus import EventBus, get_event_bus

        return {"EventBus": EventBus, "get_event_bus": get_event_bus}[name]
    if name == "TaskService":
        from .task_service import TaskService

        return TaskService
    raise AttributeError(name)
