# V2 全量迁移改造清单

> 目标：将当前三省六部系统从 legacy `dashboard/server.py + data/*.json` 架构，完整迁移到 v2 `FastAPI + Redis + Postgres` 事件驱动架构，并最终彻底下线 legacy 运行时。

## 1. 文档用途

本清单用于指导一次完整的架构迁移，不是概念性 roadmap，而是面向落地执行的改造总表。它覆盖：

- 数据模型统一
- API 全量替换
- 任务流转一致性
- JSON 到 PostgreSQL 数据迁移
- 前端无缝切换
- 权限与审计一致性
- Redis 事件总线替代直接调用
- 调度系统替换 legacy 定时轮询
- 监控告警补齐
- 切流与回滚

## 2. 迁移目标与完成定义

### 2.1 最终目标

迁移完成后，系统应满足以下条件：

- 所有任务、状态、流转、调度、审计数据均以 Postgres 为唯一持久化真相源
- 所有实时协作、派发、心跳、调度信号均以 Redis Streams / PubSub 为唯一运行时事件总线
- 所有前端读写接口均由 FastAPI 提供
- 所有 Agent 与后台任务不再直接读写 `data/tasks_source.json`
- `dashboard/server.py`、`scripts/run_loop.sh`、`scripts/kanban_update.py` 不再参与生产主链路
- legacy 兼容接口、JSON 轮询逻辑、兼容脚本全部下线或只保留离线归档用途

### 2.2 Done 定义

以下条件全部成立，方可视为迁移完成：

1. 前端默认只连接 v2 API，不再依赖 legacy base URL。
2. 新建任务、状态推进、审批、叫停、恢复、咨询、调度、派发、归档在 v2 上行为与 legacy 一致。
3. 历史 JSON 任务可以完整迁移到 Postgres，且关键字段对账一致。
4. Worker 崩溃、重复派发、事件积压、任务停滞等场景在 v2 上可观测、可恢复。
5. README 与启动方式默认指向 v2，不再以 legacy 为主路径。
6. 可以完成一次演练：切到 v2，验证通过，再回滚到 legacy，并保证数据可恢复。

## 3. 当前现状与关键差距

### 3.1 现状判断

当前仓库是双轨状态：

- legacy 主链路：`dashboard/`、`scripts/`、`data/`
- v2 主链路：`edict/backend/app/`、`edict/frontend/src/`、`edict/migration/`

### 3.2 已存在的 v2 基础

- FastAPI 主入口：`edict/backend/app/main.py`
- 任务服务：`edict/backend/app/services/task_service.py`
- 事件总线：`edict/backend/app/services/event_bus.py`
- Orchestrator / Dispatcher Worker：
  - `edict/backend/app/workers/orchestrator_worker.py`
  - `edict/backend/app/workers/dispatch_worker.py`
- React 前端：
  - `edict/frontend/src/App.tsx`
  - `edict/frontend/src/api.ts`
- Alembic 初始迁移：
  - `edict/migration/versions/001_initial.py`

### 3.3 当前硬缺口

- 前端仍依赖多组 legacy-only 端点，特别是：
  - `/api/agent-config`
  - `/api/model-change-log`
  - `/api/officials-stats`
  - `/api/morning-*`
  - `/api/set-model`
  - `/api/set-dispatch-channel`
  - `/api/skill-content/*`
  - `/api/add-skill`
  - `/api/remote-skills-*`
  - `/api/court-discuss/*`
- v2 中仍保留 legacy 兼容路由：
  - `edict/backend/app/api/legacy.py`
- 现有 JSON 迁移脚本与当前 ORM 已失配：
  - `edict/migration/migrate_json_to_pg.py`
- v2 审计日志仍主要以应用日志形式输出，尚未形成可查询审计表
- v2 部分 API 仍直接读取 `data/*.json`
- 调度扫描仍依赖 legacy 风格的外部触发，而不是独立 worker / scheduler

## 4. 迁移原则

### 4.1 单一真相源

- 制度与契约真相源：`config/institution_schema.json`
- 任务快照真相源：Postgres `tasks`
- 事件审计真相源：Postgres `events`
- 运行时事件总线：Redis Streams / PubSub

### 4.2 兼容期策略

- 允许短期双跑与影子对账
- 不允许长期双写双读成为常态
- 兼容层只作为切流过渡，不作为最终架构组成

### 4.3 迁移顺序原则

必须按下面顺序推进：

1. 先冻结契约和字段语义
2. 再统一数据模型
3. 再补全 API 和 Worker
4. 再做数据迁移
5. 再做前端切换
6. 最后下线 legacy

## 5. 优先级总览

| 优先级 | 目标 | 结果 |
|---|---|---|
| P0 | 冻结契约、补齐设计、建立迁移闸门 | 可以开始编码改造，但还未切流 |
| P1 | 打通 v2 主链路 | v2 可以承接所有核心业务动作 |
| P2 | 数据迁移、影子双跑、分阶段切流 | v2 成为默认运行链路 |
| P3 | 下线 legacy、清理文档和脚本 | 仓库默认只剩 v2 |

## 6. 详细改造清单

---

## P0. 迁移基线、范围冻结、回滚闸门

### P0-1. 冻结制度契约与字段语义

- [x] 已完成✅ 明确 `config/institution_schema.json` 为状态机、权限矩阵、快车道、中央队列 SLA 的唯一真相源
- [x] 已完成✅ 明确 v2 对外任务快照格式以 `Task.to_dict()` 为基准
- [x] 已完成✅ 明确 legacy 字段与 v2 字段的一一映射关系
- [x] 已完成✅ 明确 `todos` 的最终真相源策略
- [x] 已完成✅ 明确 audit 的最终存储模型

涉及文件：

- `config/institution_schema.json`
- `edict/backend/app/task_contract.py`
- `edict/backend/app/models/task.py`
- `edict/frontend/src/api.ts`
- `docs/task-dispatch-architecture.md`

验收标准：

- 形成字段映射表并冻结，不再临时改字段名
- 所有相关方对 `state/org/targetDept/lane/review_round/todos/consultLog/_scheduler/_stateVersion` 语义达成一致

风险点：

- 在迁移中途修改状态名或字段语义，会导致前后端、迁移脚本、事件回放同时失效

落地结果：

- 字段冻结与映射说明已写入 `docs/task-dispatch-architecture.md`
- `edict/backend/app/task_contract.py` 已统一 snake_case 存储别名与 camelCase API 快照映射
- `edict/backend/app/models/todo.py` 已标明当前以 `tasks.todos` 为真相源
- `edict/backend/app/models/task_audit.py` + `edict/migration/versions/002_task_audits.py` 已落地 durable audit

### P0-2. 建立完整 API inventory 与 parity 表

- [x] 已完成✅ 盘点前端实际调用的全部 `/api/*`
- [x] 已完成✅ 标记每个端点是“已在 v2”“待迁移”“废弃”“可合并”
- [x] 已完成✅ 输出 legacy -> v2 路由映射表

涉及文件：

- `dashboard/server.py`
- `edict/frontend/src/api.ts`
- `edict/backend/app/api/*.py`

验收标准：

- 前端调用的每个端点都有明确去向
- 不存在“切流时才发现缺端点”的情况

风险点：

- 长尾管理类接口漏迁，会导致 UI 进入“主流程可用、边角全坏”的状态

当前 inventory / parity：

| 前端调用 | 现状 | v2 去向 |
|---|---|---|
| `/api/live-status` | 已在 v2 | `edict/backend/app/api/dashboard.py` |
| `/api/task-activity/{id}` | 已在 v2 | `edict/backend/app/api/dashboard.py` |
| `/api/scheduler-state/{id}` | 已在 v2 | `edict/backend/app/api/dashboard.py` |
| `/api/queue-metrics` | 已在 v2 | `edict/backend/app/api/dashboard.py` |
| `/api/create-task` | 已在 v2 | `edict/backend/app/api/dashboard.py` |
| `/api/task-action` | 已在 v2 | `edict/backend/app/api/dashboard.py` |
| `/api/review-action` | 已在 v2 | `edict/backend/app/api/dashboard.py` |
| `/api/advance-state` | 已在 v2 | `edict/backend/app/api/dashboard.py` |
| `/api/task-consult` | 已在 v2 | `edict/backend/app/api/dashboard.py` |
| `/api/archive-task` | 已在 v2 | `edict/backend/app/api/dashboard.py` |
| `/api/task-todos` | 已在 v2 | `edict/backend/app/api/dashboard.py` |
| `/api/scheduler-scan` | 已在 v2 | `edict/backend/app/api/dashboard.py` |
| `/api/scheduler-retry` | 已在 v2 | `edict/backend/app/api/dashboard.py` |
| `/api/scheduler-escalate` | 已在 v2 | `edict/backend/app/api/dashboard.py` |
| `/api/scheduler-rollback` | 已在 v2 | `edict/backend/app/api/dashboard.py` |
| `/api/agent-config` | 已迁入 v2 兼容读接口 | `edict/backend/app/api/agents.py` |
| `/api/agents-status` | 已迁入 v2 兼容读接口 | `edict/backend/app/api/agents.py` |
| `/api/model-change-log` | 已迁入 v2 兼容读接口 | `edict/backend/app/api/models.py` |
| `/api/officials-stats` | 已迁入 v2 兼容读接口 | `edict/backend/app/api/officials.py` |
| `/api/morning-brief` | 已迁入 v2 兼容读接口 | `edict/backend/app/api/morning.py` |
| `/api/morning-config` | 已迁入 v2 兼容读接口 | `edict/backend/app/api/morning.py` |
| `/api/skill-content/{agentId}/{skillName}` | 已迁入 v2 兼容读接口 | `edict/backend/app/api/skills.py` |
| `/api/remote-skills-list` | 已迁入 v2 兼容读接口 | `edict/backend/app/api/skills.py` |
| `/api/set-model` | 待迁移 | legacy write，目标 `admin_actions.py` |
| `/api/set-dispatch-channel` | 待迁移 | legacy write，目标 `admin_actions.py` |
| `/api/agent-wake` | 待迁移 | legacy write，目标 `admin_actions.py` |
| `/api/add-skill` | 待迁移 | legacy write，目标 `skills_service.py` |
| `/api/add-remote-skill` | 待迁移 | legacy write，目标 `skills_service.py` |
| `/api/update-remote-skill` | 待迁移 | legacy write，目标 `skills_service.py` |
| `/api/remove-remote-skill` | 待迁移 | legacy write，目标 `skills_service.py` |
| `/api/court-discuss/*` | 待迁移 | 目标 `court_discuss_service.py` + `api/court_discuss.py` |

### P0-3. 设计切流与回滚闸门

- [ ] 定义切流前必须完成的备份项
- [ ] 定义切流顺序
- [ ] 定义回滚触发条件
- [ ] 定义回滚窗口内的数据回灌方案

涉及文件：

- `edict/docker-compose.yml`
- `docker-compose.yml`
- `README.md`
- `docs/getting-started.md`
- 新增 `ops/backup/`、`ops/restore/`、`ops/cutover/` 脚本

验收标准：

- 可以独立执行一次“备份 -> 切流 -> 回滚”演练

风险点：

- 没有回滚闸门时，真正切流等同于一次性赌博

---

## P1. 核心链路迁移

### 7. 数据模型统一

#### P1-1. 统一任务快照模型

- [x] 已完成✅ 让 `edict/backend/app/models/task.py` 与 `edict/backend/app/task_contract.py` 完全对齐
- [x] 已完成✅ 统一 snake_case 存储与 camelCase 对外输出映射
- [x] 已完成✅ 明确 `consult_log <-> consultLog`、`scheduler <-> _scheduler`、`prev_state <-> _prev_state` 映射
- [x] 已完成✅ 明确 `created_at/updated_at` 与 legacy `createdAt/updatedAt` 的转换规则

涉及文件：

- `edict/backend/app/models/task.py`
- `edict/backend/app/task_contract.py`
- `edict/migration/versions/001_initial.py`

验收标准：

- 任意任务对象在 `ensure_task_shape -> ORM -> to_dict` 过程里不丢字段
- 前端 `Task` 类型与后端返回结构对齐

风险点：

- 字段命名兼容若处理不统一，前端会出现静默渲染错误

#### P1-2. 决定 `todos` 最终存储策略

- [x] 已完成✅ 决定短期内以 `tasks.todos` 还是独立 `todos` 表为真相源
- [x] 已完成✅ 若先不启用独立 `todos` 表，则文档标明其仅用于未来扩展
- [ ] 若保留独立 `todos` 表，则实现可靠双写或异步投影

涉及文件：

- `edict/backend/app/models/task.py`
- `edict/backend/app/models/todo.py`
- `edict/backend/app/services/task_service.py`
- `edict/backend/app/services/event_bus.py`

验收标准：

- `todos` 更新不会出现一份更新、一份滞后的情况

风险点：

- `tasks.todos` 与 `todos` 表双写但无一致性策略，是典型数据漂移点

#### P1-3. 补齐 durable audit 模型

- [x] 已完成✅ 新增任务审计表或通用 audit 表
- [x] 已完成✅ 将 `TaskService._audit()` 从纯日志改为“数据库持久化 + 日志输出”
- [x] 已完成✅ 支持按 `task_id / actor / action / allowed` 查询

涉及文件：

- `edict/backend/app/services/task_service.py`
- 新增 `edict/backend/app/models/task_audit.py`
- 新增 Alembic migration

验收标准：

- 所有权限拒绝、状态跃迁、调度操作、手工动作都可审计回放

风险点：

- 如果只保留 stdout 日志，切流后可审计性会低于 legacy 的 `task_audit_log.json`

---

### 8. API 迁移

#### P1-4. 完成任务控制面 parity

- [x] 已完成✅ 保持并稳定以下 v2 兼容接口：
  - `/api/live-status`
  - `/api/task-activity/{task_id}`
  - `/api/scheduler-state/{task_id}`
  - `/api/queue-metrics`
  - `/api/create-task`
  - `/api/task-action`
  - `/api/review-action`
  - `/api/advance-state`
  - `/api/task-consult`
  - `/api/archive-task`
  - `/api/task-todos`
  - `/api/scheduler-scan`
  - `/api/scheduler-retry`
  - `/api/scheduler-escalate`
  - `/api/scheduler-rollback`

涉及文件：

- `edict/backend/app/api/dashboard.py`
- `edict/backend/app/services/task_service.py`

验收标准：

- 现有前端不改页面逻辑也能使用 v2 控制面

风险点：

- 如果只保留 RESTful `/api/tasks/*` 而不保留 dashboard-compatible 路由，前端切换成本会陡增

#### P1-5. 迁移 legacy-only 读接口

- [x] 已完成✅ 新增或完善 v2 实现：
  - `/api/agent-config`
  - `/api/model-change-log`
  - `/api/officials-stats`
  - `/api/morning-brief`
  - `/api/morning-config`
  - `/api/agents-status`
  - `/api/skill-content/{agentId}/{skillName}`
  - `/api/remote-skills-list`

涉及文件：

- `edict/backend/app/api/agents.py`
- 新增 `edict/backend/app/api/models.py`
- 新增 `edict/backend/app/api/skills.py`
- 新增 `edict/backend/app/api/morning.py`
- 新增 `edict/backend/app/api/officials.py`
- 新增服务层模块

验收标准：

- 前端所有读取接口均可从 FastAPI 返回，不再依赖 `data/*.json`

风险点：

- 如果这些接口仍旧回读 JSON，则“去 legacy”只是表面替换

当前进度：

- [x] 已完成✅ `/api/agent-config` 已迁入 FastAPI，并优先从 OpenClaw runtime/workspace 推导配置
- [x] 已完成✅ `/api/agents-status` 已迁入 FastAPI，并直接读取 runtime 进程 / session 状态
- [x] 已完成✅ `/api/officials-stats` 已迁入 FastAPI，并改为基于 Postgres `tasks` + runtime session 聚合
- [x] 已完成✅ `/api/skill-content/{agentId}/{skillName}` 已迁入 FastAPI，并直接读取 workspace skill 文件
- [x] 已完成✅ `/api/remote-skills-list` 已迁入 FastAPI，并直接扫描 workspace 远程 skill 元数据
- [x] 已完成✅ `/api/model-change-log` 已改为优先读取 durable audit `task_audits(action=config.set_model)`，并在迁移脚本中补入 legacy 历史
- [x] 已完成✅ `/api/morning-brief` / `/api/morning-config` 已改为优先读取 durable audit snapshot，并补齐 v2 保存/刷新入口
- [x] 已完成✅ P1-5 整体验收完成：legacy-only 读接口已迁入 FastAPI，`data/*.json` 仅保留兼容 shadow / 导入来源

#### P1-6. 迁移 legacy-only 写接口

- [x] 已完成✅ 新增或完善 v2 实现：
  - `/api/set-model`
  - `/api/set-dispatch-channel`
  - `/api/agent-wake`
  - `/api/add-skill`
  - `/api/add-remote-skill`
  - `/api/update-remote-skill`
  - `/api/remove-remote-skill`

涉及文件：

- 新增 `edict/backend/app/api/admin_actions.py`
- 新增 `edict/backend/app/services/agent_config_service.py`
- 新增 `edict/backend/app/services/skills_service.py`

验收标准：

- 所有控制动作均通过 FastAPI 执行并被审计

风险点：

- 这些动作多数涉及宿主机文件、OpenClaw 配置与 workspace，需要把副作用模型设计清楚

落地结果：

- `edict/backend/app/api/admin_actions.py` 已接管 `/api/set-model`、`/api/set-dispatch-channel`、`/api/agent-wake`
- `edict/backend/app/api/skills.py` 已补齐 `/api/add-skill`、`/api/add-remote-skill`、`/api/update-remote-skill`、`/api/remove-remote-skill`
- `edict/backend/app/services/admin_action_service.py`、`edict/backend/app/services/skills_service.py` 已替代 `dashboard/server.py` 中对应写逻辑
- 所有上述动作均接入 durable audit，并对本地 JSON/OpenClaw 配置使用原子写入

#### P1-7. 迁移朝堂议政功能

- [x] 已完成✅ 将 `court_discuss` 的会话存储迁入 Postgres durable audit 快照，并保留兼容 shadow 文件用于回退/恢复
- [x] 已完成✅ 完成以下端点迁移：
  - `/api/court-discuss/start`
  - `/api/court-discuss/list`
  - `/api/court-discuss/session/{id}`
  - `/api/court-discuss/advance`
  - `/api/court-discuss/conclude`
  - `/api/court-discuss/destroy`
  - `/api/court-discuss/fate`
  - `/api/court-discuss/officials`

涉及文件：

- `dashboard/court_discuss.py`
- 新增 `edict/backend/app/api/court_discuss.py`
- 新增 `edict/backend/app/services/court_discuss_service.py`

验收标准：

- 跨重启恢复、议政推进、结论生成、历史会话查询在 v2 中可用

风险点：

- 若朝堂议政仍写 JSON，本次迁移仍存在一条绕过 v2 的业务链路

落地结果：

- `edict/backend/app/services/court_discuss_service.py` 已接管会话创建、推进、散朝、销毁、历史查询与命运骰子
- `edict/backend/app/api/court_discuss.py` 已提供完整兼容路由，`edict/backend/app/main.py` 已完成挂载
- 会话主真相源改为 durable audit `task_audits(action in court_discuss.*)`；`data/court_discuss_sessions.json` 仅保留兼容 shadow / 无数据库回退
- `edict/migration/migrate_json_to_pg.py` 已补充 legacy `court_discuss_sessions.json` → audit snapshot 导入
- 已通过 `tests/test_v2_court_discuss.py`、相关 Python 回归以及 `npm --prefix edict/frontend run build`

---

### 9. 任务流转与权限校验一致性

#### P1-8. 用共享契约驱动所有状态跃迁

- [x] 已完成✅ 确保所有状态推进都经过 `task_contract.py` 的共享跃迁/审议/分派契约
- [x] 已完成✅ 已清理运行主链中的隐藏状态分叉，关键路径统一回归共享 helper 与契约测试
- [x] 已完成✅ 快车道、审议驳回、手工推进与 legacy 行为一致，并通过 fixture 回归验证

涉及文件：

- `edict/backend/app/task_contract.py`
- `edict/backend/app/services/task_service.py`
- `dashboard/legacy_tasks.py`
- `dashboard/legacy_scheduler.py`

验收标准：

- 对同一组 fixture，legacy 与 v2 的状态结果一致

风险点：

- `Blocked -> 恢复`、`Review -> Doing`、`Assigned -> Next` 是最容易产生行为漂移的节点

落地结果：

- `tests/test_architecture_contracts.py` 已覆盖 manual advance、review approve/reject、dispatch 幂等、权限矩阵等共享契约回归
- legacy `dashboard/legacy_tasks.py` / `dashboard/legacy_scheduler.py` 与 v2 `TaskService` 共同依赖 `task_contract.py` 的 `next_manual_transition()`、`review_transition()`、`authorize_*` 系列函数
- 已通过 legacy 主链回归、v2 回归和架构契约回归，确认 `Blocked -> resume`、`Review -> Doing`、`Assigned -> Next` 等关键节点结果一致

#### P1-9. 权限矩阵运行时一致

- [x] 已完成✅ 统一 Agent 权限判断仅通过 `authorize_*` 系列函数
- [x] 已完成✅ 保留 actor / source / request_id / signature 语义，并补充回归测试
- [x] 已完成✅ 明确 dashboard 用户、scheduler、system、agent 的权限边界

涉及文件：

- `edict/backend/app/task_contract.py`
- `edict/backend/app/api/auth.py`
- `edict/backend/app/security.py`
- `tests/test_architecture_contracts.py`

验收标准：

- 审批、咨询、调度、唤醒、手工控制的 allow/deny 结果与 legacy 保持一致

风险点：

- control plane 的 HTTP 鉴权和任务 actor 鉴权不是一回事，不能混为一层

落地结果：

- 已通过代码扫描确认 legacy/v2 运行主链的审批、咨询、调度、唤醒、手工控制均调用 `authorize_*` 系列函数
- `tests/test_task_contract.py` 已补充 `actor/source/request_id/signature` 语义回归，确保 durable audit 里保留请求上下文
- `tests/test_security_defaults.py` + `tests/test_architecture_contracts.py` 已覆盖 control plane token/loopback 边界与 actor allow/deny 契约

---

### 10. 事件系统迁移

#### P1-10. 所有关键副作用改为事件驱动

- [x] 将任务创建、状态变更、派发、咨询、todo 更新、完成、升级、回滚全部标准化为事件 已完成✅
- [x] 明确 topic 命名与事件负载 schema 已完成✅
- [x] 统一 dedupe key 规则 已完成✅

涉及文件：

- `edict/backend/app/services/event_bus.py`
- `edict/backend/app/services/task_service.py`
- `edict/backend/app/workers/orchestrator_worker.py`
- `edict/backend/app/workers/dispatch_worker.py`

验收标准：

- 同一任务同一状态版本下不会重复派发
- Worker 崩溃后可通过 pending reclaim 恢复

风险点：

- 没有统一 dedupe 规则时，最容易产生重复执行与幂等灾难

落地结果：

- 已新增 `edict/backend/app/event_contract.py` 作为 v2 事件契约清单，集中定义关键 topic、payload 最小字段集合与 dedupe 规则
- `TaskService` 已统一通过共享 helper 生成创建、状态变更、todo、升级等 dedupe key；`DispatchWorker` 的 heartbeat / output 也改为统一 helper
- 调度扫描已补发 `task.stalled / task.scheduler.stalled` 事件；回滚状态事件补充 `transition_kind=rollback`，便于后续投影和回放识别
- `/api/events/topics` 与 `docs/task-dispatch-architecture.md` 已暴露 topic/schema/dedupe 约定
- 已验证 `82 passed`：覆盖同状态版本防重复派发、worker stale pending reclaim 恢复、legacy/v2 契约回归

#### P1-11. 补齐事件投影

- [x] 将 `agent.thoughts` 持久化到 `thoughts` 已完成✅
- [x] 明确 `agent.todo.update` 是否投影到独立表 已完成✅
- [x] 增加任务活动流的统一查询视图 已完成✅

涉及文件：

- `edict/backend/app/services/event_bus.py`
- `edict/backend/app/models/thought.py`
- `edict/backend/app/models/todo.py`
- `edict/backend/app/api/events.py`

验收标准：

- 可以基于事件和投影重建任务活动流

风险点：

- 如果只有 `events` 原始表，没有投影层，查询复杂度和前端负担会升高

落地结果：

- `edict/backend/app/services/event_bus.py` 已确认 `agent.thoughts -> thoughts` 持久化链路，并补充回归测试锁定投影行为
- `agent.todo.update` 已明确为“`tasks.todos` 仍是真相源，`todos` 表承接最新快照投影”的策略；投影逻辑集中在 `edict/backend/app/services/activity_service.py`
- 已新增统一活动流查询构建器，并同时接入 `GET /api/events/activity/{task_id}` 与兼容 `GET /api/task-activity/{task_id}`
- 统一活动流会融合任务快照、持久化事件、`thoughts` 投影、`todos` 投影，返回 `taskMeta / activity / todosSummary / resourceSummary / phaseDurations`
- 已验证 `87 passed`：覆盖 thoughts/todos 投影、统一活动流视图、legacy/v2 契约回归

---

### 11. 调度系统迁移

#### P1-12. 将调度扫描从外部轮询改为后台 worker

- [x] 新增独立 `scheduler_worker` 已完成✅
- [x] 由 worker 按固定间隔运行停滞扫描，不再依赖 `curl /api/scheduler-scan` 已完成✅
- [x] 支持部署级单实例或分布式锁，避免多 scheduler 重复动作 已完成✅

涉及文件：

- `scripts/run_loop.sh`
- `edict/backend/app/services/task_service.py`
- 新增 `edict/backend/app/workers/scheduler_worker.py`
- `edict/docker-compose.yml`

验收标准：

- 停掉 legacy loop 后，v2 仍会自动 retry / escalate / rollback

风险点：

- 若没有调度实例互斥控制，多副本部署时会重复触发恢复动作

落地结果：

- 已新增 `edict/backend/app/workers/scheduler_worker.py`，直接通过 `TaskService.scheduler_scan()` 执行后台停滞扫描，不再依赖 HTTP 自调用
- `scheduler_worker` 使用 Redis leader lock（`edict:scheduler:leader`）实现部署级单实例互斥，并支持同实例续租/停止时释放锁
- `edict/docker-compose.yml` 已新增 `scheduler` 服务，v2 compose 启动后会自动带起调度 worker
- `scripts/run_loop.sh` 保留 legacy 兼容，但新增 `EDICT_DISABLE_HTTP_SCHEDULER_SCAN=1` 开关，避免混合运行时重复触发 `curl /api/scheduler-scan`
- 已验证 `90 passed`：覆盖 scheduler worker 抢锁、续租、跳过非 leader、停止释放锁，以及既有 legacy/v2 契约回归

#### P1-13. 对齐 legacy 调度策略

- [x] 已完成✅ 对齐 `stallThresholdSec / maxRetry / escalationLevel / autoRollback / snapshot`
- [x] 已完成✅ 对齐 flow_log 中的调度备注风格
- [x] 已完成✅ 对齐门下省与尚书省中央队列指标

涉及文件：

- `dashboard/legacy_scheduler.py`
- `edict/backend/app/services/task_service.py`

验收标准：

- 同一组停滞任务 fixture 下，legacy 与 v2 产生相同调度决策

风险点：

- 如果 stallThreshold 计算改了但没同步文档和监控，行为会看起来“随机”

落地结果：

- `edict/backend/app/services/task_service.py` 已将 scheduler retry / escalate / rollback 拆分为 legacy parity 内部路径，补齐 `sili-retry`、`sili-scan-retry`、`sili-rollback`、`sili-auto-rollback` 等触发语义，并对齐自动重试、自动升级、自动回滚时的 `flow_log` 备注文案
- v2 调度链路已补齐 `lastDispatchAt / lastDispatchStatus / lastDispatchAgent / lastDispatchTrigger / lastDispatchKey` 元数据写入；回滚后重新入队的 dispatch key 与 legacy 触发语义保持一致
- `edict/backend/app/workers/orchestrator_worker.py` 已接管 `task.scheduler.escalated` 事件，对应协调方会收到与 legacy 一致的唤醒通知消息，不再只停留在事件落库
- `tests/test_backend_dispatch.py` 已新增/更新 v2 parity 回归，覆盖调度升级唤醒、scan-retry 触发语义、自动回滚文案与中央队列指标；已验证 `python3 -m pytest tests/test_backend_dispatch.py -q`、`python3 -m pytest tests/test_architecture_contracts.py -q`、`python3 -m pytest tests/test_scheduler_worker.py -q`

---

### 12. 前端适配

#### P1-14. 将前端切到单一 v2 API base

- [x] 已完成✅ 去掉前端对 legacy 默认同源 API 的依赖
- [x] 已完成✅ 将所有读取与写入统一收敛到 FastAPI
- [x] 已完成✅ 保留 WebSocket 优先，HTTP fallback 次之

涉及文件：

- `edict/frontend/src/api.ts`
- `edict/frontend/src/store.ts`
- `edict/frontend/src/components/*`
- `edict/docker-compose.yml`

验收标准：

- 前端只需要一个 `VITE_API_URL`
- 不再需要通过 `dashboard/server.py` 反向代理到 control plane

风险点：

- 响应 shape 微小差异就可能导致前端静默异常，需要完整 UI 回归

落地结果：

- `edict/frontend/src/api.ts` 已移除 `VITE_EDICT_CONTROL_PLANE_URL` / `CONTROL_PLANE_BASE` 双基址模型，所有 HTTP 接口统一改为由单一 `VITE_API_URL` 推导的 `buildApiUrl()` 生成
- WebSocket 连接已改为从同一 `VITE_API_URL` 推导，不再依赖 legacy 同源 dashboard/server 或独立控制面地址；`store.ts` 与 `TaskModal.tsx` 继续保持 WebSocket 优先、轮询兜底
- `edict/docker-compose.yml` 已收口前端环境变量，只保留 `VITE_API_URL`，去掉未使用的 `VITE_WS_URL`
- 已验证 `npm --prefix edict/frontend run build`

#### P1-15. 前端类型与 UI 状态统一

- [x] 已完成✅ 对齐 `Task`、`FlowEntry`、`ProgressEntry`、`TodoItem` 等类型
- [x] 已完成✅ 对齐心跳状态、监控面板、官方统计、朝报、技能配置等面板的数据结构
- [x] 已完成✅ 确保 `live-status` 返回结构稳定

涉及文件：

- `edict/frontend/src/api.ts`
- `edict/frontend/src/generated/institutionSchema.ts`
- `edict/frontend/src/components/*.tsx`

验收标准：

- 前端 build 通过，且各面板能在 v2 数据下正常渲染

风险点：

- `live-status` 是多个面板共享入口，一旦结构漂移会产生连锁故障

落地结果：

- `edict/frontend/src/api.ts` 已新增前端 read-model adapter，统一归一 `Task / FlowEntry / ProgressEntry / TodoItem / ActivityEntry / SchedulerStateData`
- `live-status`、`task-activity`、`scheduler-state`、`agent-config`、`model-change-log`、`officials-stats`、`agents-status`、`morning-brief`、`morning-config`、`remote-skills-list`、`skill-content` 现在都会先归一后再进入 UI，组件不再直接消费兼容态返回
- 心跳状态、监控面板、官员总览、模型配置、技能配置、朝报面板的主要入口数据已补齐稳定默认值，减少 snake_case / camelCase、空数组 / 空对象、缺省字段导致的静默渲染异常
- 已验证 `npm --prefix edict/frontend run build`

---

### 13. 监控告警

#### P1-16. 建立深度健康检查与运行指标

- [ ] 补齐 Postgres、Redis、Worker、consumer lag、pending event 健康检查
- [ ] 输出任务量、状态分布、中央队列积压、调度动作次数
- [ ] 提供 Prometheus 或等价指标暴露

涉及文件：

- `edict/backend/app/api/admin.py`
- `edict/backend/app/api/events.py`
- 新增 `edict/backend/app/api/metrics.py`

验收标准：

- 能监控数据库、Redis、Worker、事件流、中央队列、调度动作

风险点：

- 仅有 `/health` 无法支撑迁移期间的问题定位

#### P1-17. 建立告警规则

- [ ] 配置以下最低告警：
  - Redis pending event 积压
  - Dispatcher / Orchestrator / Scheduler worker 心跳丢失
  - 中央队列超 SLA 堆积
  - 重复回滚/重复升级异常增长
  - 前端 WebSocket 连续断连

涉及文件：

- 新增 `ops/alerts/`
- 部署平台配置

验收标准：

- 演练中能主动发现停滞、积压、worker 崩溃等问题

风险点：

- 没有告警时，v2 的问题只会在用户反馈后暴露

---

## P2. 数据迁移、影子双跑、分阶段切流

### 14. 数据迁移

#### P2-1. 重写 JSON -> PG 迁移器

- [ ] 修复 `edict/migration/migrate_json_to_pg.py` 与当前 ORM 的失配
- [ ] 支持 dry-run、正式导入、重复导入跳过、详细对账报告
- [ ] 迁移 `flow_log / progress_log / consultLog / _scheduler / archived / review_round`
- [ ] 为 legacy 记录补齐最小可追溯 metadata

涉及文件：

- `edict/migration/migrate_json_to_pg.py`
- `edict/backend/app/models/task.py`
- `edict/backend/app/models/event.py`

验收标准：

- 干跑时可输出任务总量、状态分布、错误数
- 正式导入后数据行数和分布与 legacy 一致

风险点：

- 只迁当前快照、不迁历史上下文，会让审计与回放能力倒退

#### P2-2. 迁移非任务附属数据

- [ ] 迁移模型配置
- [ ] 迁移技能索引与远程技能元数据
- [ ] 迁移朝报配置
- [ ] 迁移朝堂议政会话
- [ ] 明确官员统计是运行时聚合还是持久化投影

涉及文件：

- `data/agent_config.json`
- `data/model_change_log.json`
- `data/morning_brief*.json`
- `data/court_discuss_sessions.json`
- 相应 v2 service / model

验收标准：

- v2 上所有 UI 面板都具备完整初始化数据来源

风险点：

- 如果只迁任务，不迁这些附属数据，前端切流依旧不完整

### 15. 影子双跑与对账

#### P2-3. 建立 parity diff 工具

- [ ] 对比 legacy 与 v2 的 `live-status`
- [ ] 对比任务动作结果
- [ ] 对比队列指标与调度结果
- [ ] 输出字段级 diff 报告

涉及文件：

- 新增 `scripts/diff_legacy_vs_v2.py`
- 新增 `tests/test_migration_parity.py`

验收标准：

- 连续观察窗口内核心字段 diff 为 0 或在预期白名单内

风险点：

- 没有对账工具时，双跑只能靠人工观察，容易遗漏深层偏差

#### P2-4. 分阶段切流

- [ ] 阶段 1：前端读流量切到 v2
- [ ] 阶段 2：人工控制写流量切到 v2
- [ ] 阶段 3：Agent 派发与事件消费切到 v2
- [ ] 阶段 4：调度切到 v2
- [ ] 阶段 5：legacy 只读观察窗口

涉及文件：

- 前端环境配置
- 部署配置
- OpenClaw / worker 启动配置

验收标准：

- 每次切流后都能完成一轮业务回归

风险点：

- 一次性全切会让问题定位难度指数上升

---

## P3. 下线 legacy 与收尾

### 16. 下线 legacy 运行时

#### P3-1. legacy 先只读，再删除

- [ ] 将 legacy server 标记为只读或明确废弃
- [ ] 停止所有运行时对 `tasks_source.json` 的写入
- [ ] 删除或下线 `edict/backend/app/api/legacy.py`
- [ ] 停止 `scripts/run_loop.sh` 和 JSON 轮询刷新

涉及文件：

- `dashboard/server.py`
- `dashboard/legacy_tasks.py`
- `dashboard/legacy_scheduler.py`
- `dashboard/legacy_agents.py`
- `dashboard/legacy_skills.py`
- `dashboard/legacy_morning.py`
- `edict/backend/app/api/legacy.py`
- `scripts/run_loop.sh`
- `scripts/kanban_update.py`
- `scripts/refresh_live_data.py`
- `scripts/sync_officials_stats.py`
- `scripts/sync_agent_config.py`

验收标准：

- 核心代码路径中不再依赖 legacy runtime

风险点：

- 若删除过早，在回滚窗口内会失去安全垫

#### P3-2. 默认启动路径改为 v2

- [ ] 默认 compose / dev 命令全部指向 `edict/docker-compose.yml`
- [ ] 安装脚本与文档默认说明改为 v2
- [ ] 如果保留 legacy demo，需明确标注“仅历史演示，不再作为主链路”

涉及文件：

- `README.md`
- `README_EN.md`
- `docs/getting-started.md`
- `install.sh`
- `install.ps1`

验收标准：

- 新同学只按文档执行，也不会再启动 legacy 作为默认链路

风险点：

- 文档不改，legacy 会被继续误用

## 17. 验收矩阵

### 17.1 必做测试

- [ ] `pytest tests/test_task_contract.py -q`
- [ ] `pytest tests/test_architecture_contracts.py -q`
- [ ] `pytest tests/test_backend_dispatch.py -q`
- [ ] 前端构建：`npm --prefix edict/frontend run build`
- [ ] v2 集成测试：补充 FastAPI + Postgres + Redis 集成用例
- [ ] 数据迁移 dry-run
- [ ] 数据迁移正式导入后对账
- [ ] 浏览器回归：任务看板、任务详情、模型配置、技能配置、朝报、朝堂议政、官员面板

### 17.2 业务回归路径

- [ ] 创建旨意 -> 司礼监分办 -> 中书起草 -> 门下审批 -> 尚书派发 -> 六部执行 -> 审查 -> 完成
- [ ] 门下驳回 -> 中书返工 -> 二次审批
- [ ] 快车道任务自动进入执行队列
- [ ] stop / resume / cancel
- [ ] review approve / reject
- [ ] consult 不改主状态
- [ ] scheduler retry / escalate / rollback
- [ ] archive / archiveAllDone

### 17.3 非功能验收

- [ ] Worker 崩溃恢复
- [ ] 重复 dispatch 幂等
- [ ] Redis pending reclaim 正常
- [ ] 中央队列指标正确
- [ ] 深度健康检查正常
- [ ] 日志与审计可检索

## 18. 回滚方案

### 18.1 切流前准备

- [ ] 备份 `data/` 目录
- [ ] 导出 Postgres 逻辑备份
- [ ] 备份 Redis AOF / RDB
- [ ] 冻结一个可回退的 legacy tag 或镜像

### 18.2 切流时回滚触发条件

满足任一条件即回滚：

- 核心任务动作出现高比例 5xx
- 任务状态和 legacy 对账持续出现关键字段不一致
- Redis pending event 积压超阈值
- 调度器失效或持续错误升级/回滚
- 前端关键面板不可用

### 18.3 回滚步骤

- [ ] 先冻结 v2 写流量
- [ ] 恢复前端到 legacy API
- [ ] 恢复 legacy scheduler / loop
- [ ] 将最近一次可用快照恢复到 `data/tasks_source.json`
- [ ] 根据需要回灌 v2 期间新增任务

### 18.4 回滚窗口结束条件

- [ ] v2 稳定运行一个完整观察窗口
- [ ] 不再需要 legacy 接管
- [ ] 完成一次恢复演练并通过

## 19. 建议执行顺序

建议按以下 critical path 推进：

1. P0-1 冻结契约
2. P0-2 API inventory
3. P0-3 回滚闸门
4. P1-1 / P1-2 / P1-3 数据模型与审计
5. P1-4 ~ P1-7 API 全量迁移
6. P1-8 / P1-9 状态机与权限一致性
7. P1-10 / P1-11 事件投影
8. P1-12 / P1-13 调度迁移
9. P1-14 / P1-15 前端切换
10. P1-16 / P1-17 监控告警
11. P2-1 / P2-2 数据迁移
12. P2-3 / P2-4 影子双跑与切流
13. P3-1 / P3-2 下线 legacy 和文档收尾

## 20. 一句话执行原则

先冻结契约和回滚闸门，再把 v2 补到能完整承接所有读写与调度，最后才下线 legacy；任何试图跳过对账、跳过影子双跑、跳过回滚演练的做法，都不应进入正式切流。
