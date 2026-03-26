"""Edict Backend — FastAPI 应用入口。

Lifespan 管理：
- startup: 连接 Redis Event Bus, 初始化数据库
- shutdown: 关闭连接

路由：
- /api/tasks — 任务 CRUD
- /api/agents — Agent 信息
- /api/events — 事件查询
- /api/admin — 管理操作
- /ws — WebSocket 实时推送
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .services.event_bus import get_event_bus
from .api import tasks, agents, events, admin, websocket, dashboard, models as model_api, skills, morning, officials, admin_actions, court_discuss, metrics

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("edict")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理。"""
    log.info(f"🏛️ Edict Backend starting on port {settings.port}...")

    # 连接 Event Bus
    bus = await get_event_bus()
    log.info("✅ Event Bus connected")

    yield

    # 清理
    await bus.close()
    log.info("Edict Backend shutdown complete")


app = FastAPI(
    title="Edict 三省六部",
    description="事件驱动的 AI Agent 协作平台",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS — 默认仅允许本地看板/前端来源，远程部署需显式配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(tasks.router, prefix="/api/tasks", tags=["tasks"])
app.include_router(agents.router, prefix="/api/agents", tags=["agents"])
app.include_router(events.router, prefix="/api/events", tags=["events"])
app.include_router(admin.router, prefix="/api/admin", tags=["admin"])
app.include_router(metrics.router, prefix="/api/metrics", tags=["metrics"])
app.include_router(dashboard.router, prefix="/api", tags=["dashboard-compat"])
app.include_router(agents.compat_router, prefix="/api", tags=["agents-compat"])
app.include_router(model_api.router, prefix="/api", tags=["models-compat"])
app.include_router(skills.router, prefix="/api", tags=["skills-compat"])
app.include_router(morning.router, prefix="/api", tags=["morning-compat"])
app.include_router(officials.router, prefix="/api", tags=["officials-compat"])
app.include_router(admin_actions.router, prefix="/api", tags=["admin-actions-compat"])
app.include_router(court_discuss.router, prefix="/api", tags=["court-discuss-compat"])
app.include_router(websocket.router, tags=["websocket"])


@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0", "engine": "edict"}


@app.get("/api")
async def api_root():
    return {
        "name": "Edict 三省六部 API",
        "version": "2.0.0",
        "endpoints": {
            "tasks": "/api/tasks",
            "agents": "/api/agents",
            "events": "/api/events",
            "admin": "/api/admin",
            "metrics": "/api/metrics",
            "websocket": "/ws",
            "health": "/health",
        },
    }
