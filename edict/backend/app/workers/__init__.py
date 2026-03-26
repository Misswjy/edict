"""Worker package exports without eager submodule imports.

Keeping this package side-effect free avoids `python -m app.workers.*`
preloading the target module through package import, which triggers
`runpy` warnings and can mask real startup failures.
"""

from __future__ import annotations

__all__ = [
    "OrchestratorWorker",
    "run_orchestrator",
    "DispatchWorker",
    "run_dispatcher",
    "SchedulerWorker",
    "run_scheduler",
]


def __getattr__(name: str):
    if name in {"OrchestratorWorker", "run_orchestrator"}:
        from .orchestrator_worker import OrchestratorWorker, run_orchestrator

        exports = {
            "OrchestratorWorker": OrchestratorWorker,
            "run_orchestrator": run_orchestrator,
        }
        return exports[name]
    if name in {"DispatchWorker", "run_dispatcher"}:
        from .dispatch_worker import DispatchWorker, run_dispatcher

        exports = {
            "DispatchWorker": DispatchWorker,
            "run_dispatcher": run_dispatcher,
        }
        return exports[name]
    if name in {"SchedulerWorker", "run_scheduler"}:
        from .scheduler_worker import SchedulerWorker, run_scheduler

        exports = {
            "SchedulerWorker": SchedulerWorker,
            "run_scheduler": run_scheduler,
        }
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
