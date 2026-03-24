"""Orchestrator Worker — 消费事件总线，驱动任务状态机。

监听 topic:
- task.created → 自动派发给司礼监 agent
- task.planning.complete → 中书审议完成 → 流转门下
- task.review.result → 门下审核 → 通过则 Assigned，退回则 Replan
- task.status → 处理各种状态变更
- task.stalled → 处理停滞任务

这是系统的核心编排器，取代旧架构中 daemon 线程 + 定时扫描的角色。
得益于 Redis Streams ACK 机制：即使 worker 崩溃，未 ACK 的事件
会被其他消费者自动认领，永不丢失。
"""

import asyncio
import logging
import signal

from ..task_contract import TaskState, build_dispatch_key, resolve_dispatch_agent
from ..services.event_bus import (
    EventBus,
    TOPIC_TASK_CREATED,
    TOPIC_TASK_STATUS,
    TOPIC_TASK_DISPATCH,
    TOPIC_TASK_COMPLETED,
    TOPIC_TASK_STALLED,
)

log = logging.getLogger("edict.orchestrator")

GROUP = "orchestrator"
CONSUMER = "orch-1"

# 需要监听的 topics
WATCHED_TOPICS = [
    TOPIC_TASK_CREATED,
    TOPIC_TASK_STATUS,
    TOPIC_TASK_COMPLETED,
    TOPIC_TASK_STALLED,
]


class OrchestratorWorker:
    """事件驱动的编排器 Worker。"""

    def __init__(self):
        self.bus = EventBus()
        self._running = False

    async def start(self):
        """启动 worker 主循环。"""
        await self.bus.connect()

        # 确保所有消费者组
        for topic in WATCHED_TOPICS:
            await self.bus.ensure_consumer_group(topic, GROUP)

        self._running = True
        log.info("🏛️ Orchestrator worker started")

        # 先处理崩溃遗留的 pending 事件
        await self._recover_pending()

        while self._running:
            try:
                await self._poll_cycle()
            except Exception as e:
                log.error(f"Orchestrator poll error: {e}", exc_info=True)
                await asyncio.sleep(2)

    async def stop(self):
        self._running = False
        await self.bus.close()
        log.info("Orchestrator worker stopped")

    async def _recover_pending(self):
        """恢复崩溃前未 ACK 的事件。"""
        for topic in WATCHED_TOPICS:
            events = await self.bus.claim_stale(
                topic, GROUP, CONSUMER, min_idle_ms=30000, count=50
            )
            if events:
                log.info(f"Recovering {len(events)} stale events from {topic}")
                for entry_id, event in events:
                    try:
                        await self._handle_event(topic, entry_id, event)
                        await self.bus.ack(topic, GROUP, entry_id)
                    except Exception as e:
                        log.error(
                            f"Error recovering stale event {entry_id} from {topic}: {e}",
                            exc_info=True,
                        )

    async def _poll_cycle(self):
        """一次轮询周期：从所有 topic 消费事件。"""
        for topic in WATCHED_TOPICS:
            events = await self.bus.consume(
                topic, GROUP, CONSUMER, count=5, block_ms=1000
            )
            for entry_id, event in events:
                try:
                    await self._handle_event(topic, entry_id, event)
                    await self.bus.ack(topic, GROUP, entry_id)
                except Exception as e:
                    log.error(
                        f"Error handling event {entry_id} from {topic}: {e}",
                        exc_info=True,
                    )
                    # 不 ACK → 事件会被重新投递

    async def _handle_event(self, topic: str, entry_id: str, event: dict):
        """根据 topic 和 event_type 分发处理。"""
        event_type = event.get("event_type", "")
        trace_id = event.get("trace_id", "")
        payload = event.get("payload", {})
        meta = event.get("meta", {})

        log.info(f"📨 {topic}/{event_type} trace={trace_id}")

        if topic == TOPIC_TASK_CREATED:
            await self._on_task_created(payload, meta, trace_id)
        elif topic == TOPIC_TASK_STATUS:
            await self._on_task_status(event_type, payload, meta, trace_id)
        elif topic == TOPIC_TASK_COMPLETED:
            await self._on_task_completed(payload, trace_id)
        elif topic == TOPIC_TASK_STALLED:
            await self._on_task_stalled(payload, trace_id)

    async def _on_task_created(self, payload: dict, meta: dict, trace_id: str):
        """任务创建 → 派发给司礼监 agent 起草。"""
        task_id = payload.get("task_id") or payload.get("id")
        state = payload.get("state", TaskState.Sili.value)
        agent = resolve_dispatch_agent(payload, state) or "sili"
        version = int(meta.get("version") or payload.get("_stateVersion") or 1)
        await self._publish_dispatch(
            trace_id=trace_id,
            task_id=task_id,
            state=state,
            version=version,
            agent=agent,
            task_payload=payload,
            message=f"新任务已创建: {payload.get('title', '')}",
            request_id=str(meta.get("request_id") or ""),
        )

    async def _on_task_status(self, event_type: str, payload: dict, meta: dict, trace_id: str):
        """状态变更 → 自动派发下一个 agent。"""
        task_id = payload.get("task_id")
        new_state_str = payload.get("to", "")
        task_payload = dict(payload.get("task") or {})
        if task_payload:
            task_payload.setdefault("id", task_id)
            task_payload.setdefault("state", new_state_str)
            task_payload.setdefault("org", payload.get("org", ""))
            task_payload.setdefault("targetDept", payload.get("targetDept", ""))
            task_payload.setdefault("_stateVersion", payload.get("_stateVersion") or meta.get("version") or 1)
            task_payload.setdefault("lane", payload.get("lane", "standard"))
        else:
            task_payload = {
                "id": task_id,
                "state": new_state_str,
                "org": payload.get("org", ""),
                "targetDept": payload.get("targetDept", ""),
                "_stateVersion": payload.get("_stateVersion") or meta.get("version") or 1,
                "lane": payload.get("lane", "standard"),
            }

        try:
            new_state = TaskState(new_state_str)
        except ValueError:
            log.warning(f"Unknown state: {new_state_str}")
            return

        agent = resolve_dispatch_agent(task_payload or payload, new_state.value)

        if agent:
            version = int(meta.get("version") or task_payload.get("_stateVersion") or 1)
            await self._publish_dispatch(
                trace_id=trace_id,
                task_id=task_id,
                state=new_state_str,
                version=version,
                agent=agent,
                task_payload=task_payload or payload,
                message=f"任务已流转到 {new_state_str}",
                request_id=str(meta.get("request_id") or ""),
            )

    async def _on_task_completed(self, payload: dict, trace_id: str):
        """任务完成 → 记录日志。"""
        task_id = payload.get("task_id")
        log.info(f"🎉 Task {task_id} completed. trace={trace_id}")

    async def _on_task_stalled(self, payload: dict, trace_id: str):
        """任务停滞 → 通知尚书或重新派发。"""
        task_id = payload.get("task_id")
        log.warning(f"⏸️ Task {task_id} stalled! Requesting intervention. trace={trace_id}")
        # TODO: 实现停滞任务的自动恢复策略

    async def _publish_dispatch(
        self,
        *,
        trace_id: str,
        task_id: str,
        state: str,
        version: int,
        agent: str,
        task_payload: dict,
        message: str,
        request_id: str,
    ) -> None:
        dispatch_key = build_dispatch_key(task_id, state, version, agent, manual=False, request_id=request_id)
        await self.bus.publish(
            topic=TOPIC_TASK_DISPATCH,
            trace_id=trace_id,
            event_type="task.dispatch.request",
            producer="orchestrator",
            payload={
                "task_id": task_id,
                "agent": agent,
                "state": state,
                "message": message,
                "dispatch_key": dispatch_key,
                "version": version,
                "task": task_payload,
            },
            meta={"version": version, "dispatch_key": dispatch_key, "request_id": request_id},
            dedupe_key=dispatch_key,
        )


async def run_orchestrator():
    """入口函数 — 用于直接运行 worker。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    worker = OrchestratorWorker()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(worker.stop()))

    await worker.start()
