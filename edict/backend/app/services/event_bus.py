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
from ..event_contract import (
    TOPIC_AGENT_HEARTBEAT,
    TOPIC_AGENT_THOUGHTS,
    TOPIC_AGENT_TODO_UPDATE,
    TOPIC_TASK_CLOSED,
    TOPIC_TASK_COMPLETED,
    TOPIC_TASK_CREATED,
    TOPIC_TASK_DISPATCH,
    TOPIC_TASK_ESCALATED,
    TOPIC_TASK_PLANNING_COMPLETE,
    TOPIC_TASK_PLANNING_REQUEST,
    TOPIC_TASK_REPLAN,
    TOPIC_TASK_REVIEW_REQUEST,
    TOPIC_TASK_REVIEW_RESULT,
    TOPIC_TASK_STALLED,
    TOPIC_TASK_STATUS,
)

log = logging.getLogger("edict.event_bus")

# 所有 topic 对应的 Redis Stream key 前缀
STREAM_PREFIX = "edict:stream:"
DISPATCH_EXEC_PREFIX = "edict:dispatch:execution:"
WORKER_HEARTBEAT_PREFIX = "edict:worker:heartbeat:"
WORKER_HEARTBEAT_REGISTRY_KEY = "edict:worker:registry"


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

    def _worker_heartbeat_key(self, worker_name: str, instance_id: str) -> str:
        return f"{WORKER_HEARTBEAT_PREFIX}{worker_name}:{instance_id}"

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

    async def stream_groups(self, topic: str) -> list[dict[str, Any]]:
        """获取 Stream 的 consumer groups 信息。"""
        stream_key = self._stream_key(topic)
        try:
            groups = await self.redis.xinfo_groups(stream_key)
            return [dict(group or {}) for group in (groups or [])]
        except aioredis.ResponseError:
            return []

    async def stream_consumers(self, topic: str, group: str) -> list[dict[str, Any]]:
        """获取某个 consumer group 的 consumers 信息。"""
        stream_key = self._stream_key(topic)
        try:
            consumers = await self.redis.xinfo_consumers(stream_key, group)
            return [dict(item or {}) for item in (consumers or [])]
        except aioredis.ResponseError:
            return []

    async def pending_summary(self, topic: str, group: str) -> dict[str, Any]:
        """获取 pending 摘要（总数、最旧/最新 entry、按 consumer 分布）。"""
        stream_key = self._stream_key(topic)
        try:
            raw = await self.redis.execute_command("XPENDING", stream_key, group)
        except aioredis.ResponseError:
            return {"count": 0, "min_id": "", "max_id": "", "consumers": []}

        if not isinstance(raw, (list, tuple)) or len(raw) < 4:
            return {"count": 0, "min_id": "", "max_id": "", "consumers": []}

        consumers: list[dict[str, Any]] = []
        for item in raw[3] or []:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            consumers.append(
                {
                    "name": str(item[0]),
                    "pending": int(item[1] or 0),
                }
            )

        return {
            "count": int(raw[0] or 0),
            "min_id": str(raw[1] or ""),
            "max_id": str(raw[2] or ""),
            "consumers": consumers,
        }

    async def report_worker_heartbeat(
        self,
        worker_name: str,
        instance_id: str,
        *,
        status: str = "running",
        extra: dict[str, Any] | None = None,
        ttl_sec: int = 120,
    ) -> dict[str, Any]:
        """写入 worker 心跳（Redis key + registry set）。"""
        key = self._worker_heartbeat_key(worker_name, instance_id)
        payload = {
            "worker": worker_name,
            "instance_id": instance_id,
            "status": status,
            "ts": datetime.now(timezone.utc).isoformat(),
            "extra": dict(extra or {}),
        }
        await self.redis.set(key, json.dumps(payload, ensure_ascii=False), ex=max(10, int(ttl_sec or 120)))
        await self.redis.sadd(WORKER_HEARTBEAT_REGISTRY_KEY, key)
        await self.redis.expire(WORKER_HEARTBEAT_REGISTRY_KEY, max(300, int(ttl_sec or 120) * 4))
        return payload

    async def list_worker_heartbeats(self) -> list[dict[str, Any]]:
        """读取当前已注册 worker 心跳。"""
        keys = await self.redis.smembers(WORKER_HEARTBEAT_REGISTRY_KEY)
        key_list = sorted(str(key) for key in (keys or []) if key)
        if not key_list:
            return []

        payloads = await self.redis.mget(key_list)
        rows: list[dict[str, Any]] = []
        stale_keys: list[str] = []
        for key, raw in zip(key_list, payloads):
            if not raw:
                stale_keys.append(key)
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                stale_keys.append(key)
                continue
            if not isinstance(item, dict):
                stale_keys.append(key)
                continue
            row = dict(item)
            row["key"] = key
            rows.append(row)

        if stale_keys:
            await self.redis.srem(WORKER_HEARTBEAT_REGISTRY_KEY, *stale_keys)

        rows.sort(key=lambda r: (str(r.get("worker") or ""), str(r.get("instance_id") or "")))
        return rows

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
        async with self._session_context() as session:
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
        async with self._session_context() as session:
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
        if topic == TOPIC_AGENT_THOUGHTS:
            content = str(payload.get("output") or payload.get("content") or "").strip()
            if not content:
                return
            agent = str(payload.get("agent") or producer.replace("agent.", "")).strip()
            async with self._session_context() as session:
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
            return

        if topic == TOPIC_AGENT_TODO_UPDATE:
            from .activity_service import project_todos_snapshot

            async with self._session_context() as session:
                await project_todos_snapshot(
                    session,
                    trace_id=trace_id,
                    items=list(payload.get("items") or []),
                    producer=producer,
                    source_of_truth=str(payload.get("source_of_truth") or "tasks.todos"),
                )

    def _session_factory(self):
        factory = self._session_factory_override
        if factory is not None:
            return factory
        from ..db import async_session

        return async_session

    def _session_context(self):
        factory = self._session_factory()
        return factory if hasattr(factory, "__aenter__") else factory()


# ── 全局单例 ──
_bus: EventBus | None = None


async def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
        await _bus.connect()
    return _bus
