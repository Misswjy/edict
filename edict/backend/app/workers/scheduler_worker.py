"""Scheduler Worker — 周期性执行停滞扫描与恢复动作。"""

from __future__ import annotations

import asyncio
import logging
import signal
import uuid

from ..config import get_settings
from ..services.event_bus import EventBus

log = logging.getLogger("edict.scheduler")

LOCK_KEY = "edict:scheduler:leader"


class SchedulerWorker:
    """后台调度扫描 worker，取代外部 curl /api/scheduler-scan 轮询。"""

    def __init__(
        self,
        *,
        scan_interval_sec: int | None = None,
        threshold_sec: int | None = None,
        lock_ttl_sec: int | None = None,
    ):
        settings = get_settings()
        self.bus = EventBus()
        self._running = False
        self.instance_id = f"scheduler-{uuid.uuid4().hex[:8]}"
        self.scan_interval_sec = max(5, int(scan_interval_sec or settings.scheduler_scan_interval_seconds or 60))
        self.threshold_sec = max(60, int(threshold_sec or settings.stall_threshold_sec or 180))
        self.lock_ttl_sec = max(self.scan_interval_sec * 3, int(lock_ttl_sec or 0), 30)

    async def start(self):
        await self.bus.connect()
        self._running = True
        log.info(
            "🧭 Scheduler worker started instance=%s interval=%ss threshold=%ss lock_ttl=%ss",
            self.instance_id,
            self.scan_interval_sec,
            self.threshold_sec,
            self.lock_ttl_sec,
        )
        while self._running:
            try:
                await self._run_cycle()
            except Exception as exc:  # pragma: no cover - defensive runtime guard
                log.error("Scheduler cycle failed: %s", exc, exc_info=True)
                await asyncio.sleep(min(self.scan_interval_sec, 5))

    async def stop(self):
        self._running = False
        try:
            current_owner = await self.bus.redis.get(LOCK_KEY)
            if current_owner == self.instance_id:
                await self.bus.redis.delete(LOCK_KEY)
        except Exception:  # pragma: no cover - best effort cleanup
            log.debug("scheduler lock cleanup skipped", exc_info=True)
        await self.bus.close()
        log.info("Scheduler worker stopped instance=%s", self.instance_id)

    async def _run_cycle(self):
        if not await self._acquire_or_renew_leader_lock():
            log.debug("scheduler leader lock held by another instance; skipping this cycle")
            await asyncio.sleep(self.scan_interval_sec)
            return
        result = await self._scan_tasks()
        log.info(
            "🧭 Scheduler scan complete instance=%s actions=%s threshold=%s",
            self.instance_id,
            int(result.get("count") or 0),
            int(result.get("thresholdSec") or self.threshold_sec),
        )
        await asyncio.sleep(self.scan_interval_sec)

    async def _acquire_or_renew_leader_lock(self) -> bool:
        current_owner = await self.bus.redis.get(LOCK_KEY)
        if current_owner == self.instance_id:
            await self.bus.redis.expire(LOCK_KEY, self.lock_ttl_sec)
            return True
        claimed = await self.bus.redis.set(LOCK_KEY, self.instance_id, ex=self.lock_ttl_sec, nx=True)
        return bool(claimed)

    async def _scan_tasks(self) -> dict:
        from ..db import async_session
        from ..services.task_service import TaskService
        from ..task_contract import make_actor_context

        async with async_session() as db:
            svc = TaskService(db, self.bus)
            return await svc.scheduler_scan(
                self.threshold_sec,
                actor=make_actor_context("sili", source="scheduler-worker"),
            )


async def run_scheduler():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    worker = SchedulerWorker()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(worker.stop()))

    await worker.start()
