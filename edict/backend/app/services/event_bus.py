"""Redis Streams 事件总线，带 Postgres 审计落库与幂等发布。"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

try:
    import redis.asyncio as aioredis
except ModuleNotFoundError:  # pragma: no cover - exercised in lightweight test envs
    aioredis = None

from ..config import get_settings

log = logging.getLogger("edict.event_bus")

# ── 标准 Topic 常量 ──
TOPIC_TASK_CREATED = "task.created"
TOPIC_TASK_PLANNING_REQUEST = "task.planning.request"
TOPIC_TASK_PLANNING_COMPLETE = "task.planning.complete"
TOPIC_TASK_REVIEW_REQUEST = "task.review.request"
TOPIC_TASK_REVIEW_RESULT = "task.review.result"
TOPIC_TASK_DISPATCH = "task.dispatch"
TOPIC_TASK_STATUS = "task.status"
TOPIC_TASK_COMPLETED = "task.completed"
TOPIC_TASK_CLOSED = "task.closed"
TOPIC_TASK_REPLAN = "task.replan"
TOPIC_TASK_STALLED = "task.stalled"
TOPIC_TASK_ESCALATED = "task.escalated"

TOPIC_AGENT_THOUGHTS = "agent.thoughts"
TOPIC_AGENT_TODO_UPDATE = "agent.todo.update"
TOPIC_AGENT_HEARTBEAT = "agent.heartbeat"

# 所有 topic 对应的 Redis Stream key 前缀
STREAM_PREFIX = "edict:stream:"
DISPATCH_EXEC_PREFIX = "edict:dispatch:execution:"


class EventBus:
    """Redis Streams 事件总线。"""

    def __init__(
        self,
        redis_url: str | None = None,
        session_factory: Any | None = None,
    ):
        self._redis_url = redis_url or get_settings().redis_url
        self._redis: aioredis.Redis | None = None
        self._session_factory_override = session_factory

    async def connect(self):
        """建立 Redis 连接。"""
        if self._redis is None:
            if aioredis is None:
                raise RuntimeError("redis package is required to use EventBus.connect()")
            self._redis = aioredis.from_url(
                self._redis_url,
                decode_responses=True,
                max_connections=20,
            )
            log.info("EventBus connected to Redis: %s", self._redis_url)

    async def close(self):
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    @property
    def redis(self):
        assert self._redis is not None, "EventBus not connected. Call connect() first."
        return self._redis

    def _stream_key(self, topic: str) -> str:
        return f"{STREAM_PREFIX}{topic}"

    def _dispatch_state_key(self, dispatch_key: str) -> str:
        return f"{DISPATCH_EXEC_PREFIX}{dispatch_key}"

    async def publish(
        self,
        topic: str,
        trace_id: str,
        event_type: str,
        producer: str,
        payload: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        dedupe_key: str = "",
    ) -> str:
        """发布事件到 Postgres + Redis Stream。"""
        payload_data = dict(payload or {})
        meta_data = dict(meta or {})
        stable_key = (dedupe_key or meta_data.get("dedupe_key") or "").strip()
        if stable_key:
            meta_data.setdefault("dedupe_key", stable_key)

        record, created = await self._get_or_create_event(
            trace_id=trace_id,
            topic=topic,
            event_type=event_type,
            producer=producer,
            payload=payload_data,
            meta=meta_data,
            dedupe_key=stable_key,
        )
        if not created and record.stream_entry_id:
            log.info("📦 Skip duplicate publish %s/%s dedupe=%s", topic, event_type, stable_key)
            return record.stream_entry_id

        event = {
            "event_id": str(record.event_id),
            "trace_id": trace_id,
            "timestamp": record.timestamp.isoformat(),
            "topic": topic,
            "event_type": event_type,
            "producer": producer,
            "payload": json.dumps(payload_data, ensure_ascii=False),
            "meta": json.dumps(meta_data, ensure_ascii=False),
        }
        stream_key = self._stream_key(topic)
        entry_id = await self.redis.xadd(stream_key, event, maxlen=10000)
        await self.redis.publish(f"edict:pubsub:{topic}", json.dumps(event, ensure_ascii=False))
        await self._mark_event_published(record.event_id, entry_id)
        await self._persist_auxiliary_records(topic, trace_id, producer, payload_data, meta_data)
        log.debug("📤 Published %s/%s → %s [%s] trace=%s", topic, event_type, stream_key, entry_id, trace_id)
        return entry_id

    async def ensure_consumer_group(self, topic: str, group: str):
        """确保消费者组存在（幂等）。"""
        stream_key = self._stream_key(topic)
        try:
            await self.redis.xgroup_create(stream_key, group, id="0", mkstream=True)
            log.info("Created consumer group %s on %s", group, stream_key)
        except aioredis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def consume(
        self,
        topic: str,
        group: str,
        consumer: str,
        count: int = 10,
        block_ms: int = 5000,
    ) -> list[tuple[str, dict]]:
        """从消费者组消费事件。"""
        stream_key = self._stream_key(topic)
        results = await self.redis.xreadgroup(
            groupname=group,
            consumername=consumer,
            streams={stream_key: ">"},
            count=count,
            block=block_ms,
        )
        events = []
        if results:
            for _stream, messages in results:
                for entry_id, data in messages:
                    if "payload" in data:
                        data["payload"] = json.loads(data["payload"])
                    if "meta" in data:
                        data["meta"] = json.loads(data["meta"])
                    events.append((entry_id, data))
        return events

    async def ack(self, topic: str, group: str, entry_id: str):
        """确认消费。"""
        stream_key = self._stream_key(topic)
        await self.redis.xack(stream_key, group, entry_id)
        log.debug("✅ ACK %s [%s] group=%s", stream_key, entry_id, group)

    async def get_pending(self, topic: str, group: str, count: int = 10) -> list:
        """查看未 ACK 的 pending 事件。"""
        stream_key = self._stream_key(topic)
        return await self.redis.xpending_range(stream_key, group, min="-", max="+", count=count)

    async def claim_stale(
        self,
        topic: str,
        group: str,
        consumer: str,
        min_idle_ms: int = 60000,
        count: int = 10,
    ) -> list[tuple[str, dict]]:
        """认领超时的 pending 事件。"""
        stream_key = self._stream_key(topic)
        results = await self.redis.xautoclaim(
            stream_key, group, consumer, min_idle_time=min_idle_ms, start_id="0-0", count=count
        )
        if results and len(results) >= 2:
            events = []
            for entry_id, data in results[1]:
                if "payload" in data:
                    data["payload"] = json.loads(data["payload"])
                if "meta" in data:
                    data["meta"] = json.loads(data["meta"])
                events.append((entry_id, data))
            return events
        return []

    async def stream_info(self, topic: str) -> dict:
        """获取 Stream 信息。"""
        stream_key = self._stream_key(topic)
        try:
            return await self.redis.xinfo_stream(stream_key)
        except aioredis.ResponseError:
            return {}

    async def claim_dispatch_execution(self, dispatch_key: str, ttl_sec: int = 900) -> str:
        """对单轮派发做执行侧 claim，避免重复唤醒 Agent。"""
        key = self._dispatch_state_key(dispatch_key)
        existing = await self.redis.get(key)
        if existing:
            try:
                status = json.loads(existing).get("status", "")
            except json.JSONDecodeError:
                status = "claimed"
            if status == "completed":
                return "completed"
            return "claimed"

        payload = json.dumps(
            {"status": "claimed", "claimedAt": datetime.now(timezone.utc).isoformat()},
            ensure_ascii=False,
        )
        claimed = await self.redis.set(key, payload, ex=ttl_sec, nx=True)
        return "acquired" if claimed else "claimed"

    async def mark_dispatch_execution_complete(
        self,
        dispatch_key: str,
        *,
        task_id: str,
        agent: str,
        state: str,
        version: int,
        return_code: int,
        ttl_sec: int = 86400,
    ) -> None:
        key = self._dispatch_state_key(dispatch_key)
        payload = {
            "status": "completed",
            "task_id": task_id,
            "agent": agent,
            "state": state,
            "version": version,
            "return_code": return_code,
            "completedAt": datetime.now(timezone.utc).isoformat(),
        }
        await self.redis.set(key, json.dumps(payload, ensure_ascii=False), ex=ttl_sec)

    async def _get_or_create_event(
        self,
        *,
        trace_id: str,
        topic: str,
        event_type: str,
        producer: str,
        payload: dict[str, Any],
        meta: dict[str, Any],
        dedupe_key: str,
    ) -> tuple[Event, bool]:
        async with self._session_factory() as session:
            from sqlalchemy import select
            from sqlalchemy.exc import IntegrityError
            from ..models.event import Event

            if dedupe_key:
                existing = await self._find_event_by_dedupe(session, topic, dedupe_key)
                if existing is not None:
                    return existing, False

            record = Event(
                event_id=uuid.uuid4(),
                trace_id=trace_id,
                timestamp=datetime.now(timezone.utc),
                topic=topic,
                event_type=event_type,
                producer=producer,
                dedupe_key=dedupe_key or None,
                payload=payload,
                meta=meta,
            )
            session.add(record)
            try:
                await session.commit()
                await session.refresh(record)
                return record, True
            except IntegrityError:
                await session.rollback()
                if dedupe_key:
                    existing = await self._find_event_by_dedupe(session, topic, dedupe_key)
                    if existing is not None:
                        return existing, False
                raise

    async def _find_event_by_dedupe(self, session: Any, topic: str, dedupe_key: str):
        from sqlalchemy import select
        from ..models.event import Event

        stmt = select(Event).where(Event.topic == topic, Event.dedupe_key == dedupe_key)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _mark_event_published(self, event_id: uuid.UUID, stream_entry_id: str) -> None:
        async with self._session_factory() as session:
            from ..models.event import Event

            record = await session.get(Event, event_id)
            if record is None:
                return
            record.stream_entry_id = stream_entry_id
            record.published_at = datetime.now(timezone.utc)
            await session.commit()

    async def _persist_auxiliary_records(
        self,
        topic: str,
        trace_id: str,
        producer: str,
        payload: dict[str, Any],
        meta: dict[str, Any],
    ) -> None:
        if topic != TOPIC_AGENT_THOUGHTS:
            return
        content = str(payload.get("output") or payload.get("content") or "").strip()
        if not content:
            return
        agent = str(payload.get("agent") or producer.replace("agent.", "")).strip()
        async with self._session_factory() as session:
            from ..models.thought import Thought

            session.add(
                Thought(
                    trace_id=trace_id,
                    agent=agent or "unknown",
                    step=int(meta.get("step") or 0),
                    type=str(payload.get("type") or "summary"),
                    source=str(payload.get("source") or "tool"),
                    content=content,
                    tokens=int(payload.get("tokens") or 0),
                    confidence=float(payload.get("confidence") or 0.0),
                    sensitive=bool(payload.get("sensitive") or False),
                )
            )
            await session.commit()

    def _session_factory(self):
        factory = self._session_factory_override
        if factory is not None:
            return factory
        from ..db import async_session

        return async_session


# ── 全局单例 ──
_bus: EventBus | None = None


async def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
        await _bus.connect()
    return _bus
