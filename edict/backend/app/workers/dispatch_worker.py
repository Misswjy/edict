"""Dispatch Worker — 消费 task.dispatch 事件，执行 OpenClaw agent 调用。

核心解决旧架构痛点：
- 旧: daemon 线程 + subprocess.run → kill -9 丢失一切
- 新: Redis Streams ACK 保证 → 崩溃后自动重新投递

流程:
1. 从 task.dispatch stream 消费事件
2. 调用 OpenClaw CLI: `openclaw agent --agent xxx -m "..."`
3. 解析 agent 输出（kanban_update.py 调用结果）
4. ACK 事件
"""

import asyncio
import logging
import os
import signal
import subprocess
import uuid

from ..config import get_settings
from ..event_contract import (
    TOPIC_AGENT_HEARTBEAT,
    TOPIC_AGENT_THOUGHTS,
    TOPIC_TASK_DISPATCH,
    build_agent_heartbeat_dedupe_key,
    build_agent_output_dedupe_key,
)
from ..services.event_bus import EventBus

log = logging.getLogger("edict.dispatcher")

GROUP = "dispatcher"
CONSUMER = "disp-1"


class DispatchWorker:
    """Agent 派发 Worker — 调用 OpenClaw CLI 执行 agent 任务。"""

    def __init__(self, max_concurrent: int = 3):
        settings = get_settings()
        self.bus = EventBus()
        self._running = False
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active_tasks: dict[str, asyncio.Task] = {}
        self.worker_name = "dispatcher"
        self.instance_id = f"disp-{uuid.uuid4().hex[:8]}"
        heartbeat_interval = max(10, int(getattr(settings, "heartbeat_interval_sec", 30) or 30))
        self.heartbeat_ttl_sec = max(60, heartbeat_interval * 4)

    async def start(self):
        await self.bus.connect()
        await self._heartbeat(status="starting")
        await self.bus.ensure_consumer_group(TOPIC_TASK_DISPATCH, GROUP)
        self._running = True
        log.info("🚀 Dispatch worker started")

        # 恢复崩溃遗留
        await self._recover_pending()
        await self._heartbeat(status="running")

        while self._running:
            try:
                await self._heartbeat(status="running", active_dispatches=len(self._active_tasks))
                await self._poll_cycle()
            except Exception as e:
                log.error(f"Dispatch poll error: {e}", exc_info=True)
                await asyncio.sleep(2)

    async def stop(self):
        self._running = False
        await self._heartbeat(status="stopping", ttl_sec=30, active_dispatches=len(self._active_tasks))
        # 等待进行中的 agent 调用完成
        if self._active_tasks:
            log.info(f"Waiting for {len(self._active_tasks)} active dispatches...")
            await asyncio.gather(*self._active_tasks.values(), return_exceptions=True)
        await self.bus.close()
        log.info("Dispatch worker stopped")

    async def _heartbeat(self, *, status: str, ttl_sec: int | None = None, active_dispatches: int = 0):
        reporter = getattr(self.bus, "report_worker_heartbeat", None)
        if not callable(reporter):
            return
        try:
            await reporter(
                self.worker_name,
                self.instance_id,
                status=status,
                ttl_sec=int(ttl_sec or self.heartbeat_ttl_sec),
                extra={
                    "group": GROUP,
                    "topic": TOPIC_TASK_DISPATCH,
                    "active_dispatches": int(active_dispatches),
                },
            )
        except Exception:
            log.debug("dispatch heartbeat update skipped", exc_info=True)

    async def _recover_pending(self):
        events = await self.bus.claim_stale(
            TOPIC_TASK_DISPATCH, GROUP, CONSUMER, min_idle_ms=60000, count=20
        )
        if events:
            log.info(f"Recovering {len(events)} stale dispatch events")
            for entry_id, event in events:
                await self._dispatch(entry_id, event)

    async def _poll_cycle(self):
        events = await self.bus.consume(
            TOPIC_TASK_DISPATCH, GROUP, CONSUMER, count=3, block_ms=2000
        )
        for entry_id, event in events:
            # 每个派发在独立任务中执行，带并发控制
            task = asyncio.create_task(self._dispatch(entry_id, event))
            task_id = event.get("payload", {}).get("task_id", entry_id)
            self._active_tasks[task_id] = task
            task.add_done_callback(lambda t, tid=task_id: self._active_tasks.pop(tid, None))

    async def _dispatch(self, entry_id: str, event: dict):
        """执行一次 agent 派发。"""
        async with self._semaphore:
            payload = event.get("payload", {})
            meta = event.get("meta", {})
            task_id = payload.get("task_id", "")
            agent = payload.get("agent", "")
            message = payload.get("message", "")
            trace_id = event.get("trace_id", "")
            state = payload.get("state", "")
            version = int(payload.get("version") or meta.get("version") or 1)
            dispatch_key = str(payload.get("dispatch_key") or meta.get("dispatch_key") or "")

            claim_status = ""
            if dispatch_key:
                claim_status = await self.bus.claim_dispatch_execution(
                    dispatch_key,
                    ttl_sec=max(get_settings().dispatch_timeout_sec * 2, 900),
                )
            if claim_status in {"claimed", "completed"}:
                log.info("🛑 Skip duplicate dispatch %s for task %s", dispatch_key, task_id)
                await self.bus.ack(TOPIC_TASK_DISPATCH, GROUP, entry_id)
                return

            log.info(f"🔄 Dispatching task {task_id} → agent '{agent}' state={state}")

            # 发布心跳
            await self.bus.publish(
                topic=TOPIC_AGENT_HEARTBEAT,
                trace_id=trace_id,
                event_type="agent.dispatch.start",
                producer="dispatcher",
                payload={"task_id": task_id, "agent": agent, "dispatch_key": dispatch_key, "version": version},
                meta={"dispatch_key": dispatch_key, "version": version},
                dedupe_key=build_agent_heartbeat_dedupe_key(dispatch_key),
            )

            try:
                result = await self._call_openclaw(agent, message, task_id, trace_id)
                return_code = int(result.get("returncode") or -1)

                # 发布 agent 输出
                await self.bus.publish(
                    topic=TOPIC_AGENT_THOUGHTS,
                    trace_id=trace_id,
                    event_type="agent.output",
                    producer=f"agent.{agent}",
                    payload={
                        "task_id": task_id,
                        "agent": agent,
                        "output": result.get("stdout", ""),
                        "return_code": return_code,
                        "dispatch_key": dispatch_key,
                        "version": version,
                    },
                    meta={"dispatch_key": dispatch_key, "version": version},
                    dedupe_key=build_agent_output_dedupe_key(dispatch_key),
                )
                if dispatch_key:
                    await self.bus.mark_dispatch_execution_complete(
                        dispatch_key,
                        task_id=task_id,
                        agent=agent,
                        state=state,
                        version=version,
                        return_code=return_code,
                    )

                if return_code == 0:
                    log.info(f"✅ Agent '{agent}' completed task {task_id}")
                else:
                    log.warning(
                        f"⚠️ Agent '{agent}' returned non-zero for task {task_id}: "
                        f"rc={return_code}"
                    )

                # ACK — 事件处理完毕
                await self.bus.ack(TOPIC_TASK_DISPATCH, GROUP, entry_id)

            except Exception as e:
                log.error(f"❌ Dispatch failed: task {task_id} → {agent}: {e}", exc_info=True)
                # 不 ACK → Redis 会重新投递给其他消费者

    async def _call_openclaw(
        self,
        agent: str,
        message: str,
        task_id: str,
        trace_id: str,
    ) -> dict:
        """异步调用 OpenClaw CLI — 在线程池中执行。"""
        settings = get_settings()
        cmd = [
            "openclaw", "agent",
            "--agent", agent,
            "-m", message,
        ]

        env = os.environ.copy()
        env["EDICT_TASK_ID"] = task_id
        env["EDICT_TRACE_ID"] = trace_id
        env["EDICT_API_URL"] = f"http://localhost:{settings.port}"

        log.debug(f"Executing: {' '.join(cmd)}")

        def _run():
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    env=env,
                    cwd=settings.openclaw_project_dir or None,
                )
                return {
                    "returncode": proc.returncode,
                    "stdout": proc.stdout[-5000:] if proc.stdout else "",
                    "stderr": proc.stderr[-2000:] if proc.stderr else "",
                }
            except subprocess.TimeoutExpired:
                return {"returncode": -1, "stdout": "", "stderr": "TIMEOUT after 300s"}
            except FileNotFoundError:
                return {"returncode": -1, "stdout": "", "stderr": "openclaw command not found"}

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _run)


async def run_dispatcher():
    """入口函数 — 用于直接运行 worker。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    worker = DispatchWorker()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(worker.stop()))

    await worker.start()
