# Architecture Remediation Checklist

> 项目：三省六部 / Edict / OpenClaw 多 Agent 协作系统
>
> 更新时间：2026-03-24
>
> 目的：将当前架构评估结论沉淀为可执行的整改清单，按风险级别排序，便于分阶段落地。

## 总体判断

当前项目的制度化多 Agent 架构方向正确，核心优势在于：

- 将多 Agent 协作从自由对话升级为制度化流转
- 将质量控制前置到执行前
- 将任务过程暴露为可审计、可干预、可观测的活动流

当前项目的主要风险在于：

- 新旧两套架构长期并存，存在明显漂移风险
- 状态机、权限矩阵、数据模型未实现单一真相源
- 旧控制面对并发写入和幂等派发的保护不足
- 新事件驱动后端的主链路尚未完全收敛

整改建议按照 `P0 -> P1 -> P2 -> P3` 顺序推进。

---

## P0 立即处理

### 1. 统一任务数据模型、状态机和迁移脚本

状态：已完成（2026-03-24）

风险级别：极高

问题：

- 新后端 ORM、Alembic、TaskService、旧脚本中的任务结构已不一致
- 同一任务在不同运行模式下可能产生不同字段和状态语义

整改动作：

- 统一任务主键、组织字段、状态字段、调度字段命名
- 明确唯一状态集合与合法迁移路径
- 重写或修正 Alembic 初始迁移，保证与 ORM 一致
- 将旧脚本状态机与新后端状态机对齐

验收标准：

- ORM、迁移脚本、服务层、前端类型定义使用同一套任务结构
- 同一任务在 JSON 模式与事件模式下语义一致

重点文件：

- [edict/backend/app/models/task.py](/Users/xingzhan/Documents/edict/edict/backend/app/models/task.py)
- [edict/backend/app/services/task_service.py](/Users/xingzhan/Documents/edict/edict/backend/app/services/task_service.py)
- [edict/migration/versions/001_initial.py](/Users/xingzhan/Documents/edict/edict/migration/versions/001_initial.py)
- [scripts/kanban_update.py](/Users/xingzhan/Documents/edict/scripts/kanban_update.py)
- [dashboard/server.py](/Users/xingzhan/Documents/edict/dashboard/server.py)

### 2. 修复旧控制面的并发写风险

状态：已完成（2026-03-24）

风险级别：极高

问题：

- `dashboard/server.py` 大量采用 `load -> mutate -> save` 模式
- 多请求并发修改 `tasks_source.json` 时可能发生丢更新

整改动作：

- 将所有任务变更路径改为 `atomic_json_update()`
- 禁止任何绕过锁的直接读改写逻辑
- 为批量操作增加并发回归测试

验收标准：

- 根目录 JSON 数据的所有写入都通过文件锁工具
- 并发执行 stop/resume/review/advance/create 不发生状态覆盖

重点文件：

- [scripts/file_lock.py](/Users/xingzhan/Documents/edict/scripts/file_lock.py)
- [dashboard/server.py](/Users/xingzhan/Documents/edict/dashboard/server.py)

### 3. 将权限矩阵从文档规则变成运行时强校验

状态：已完成（2026-03-24）

风险级别：极高

问题：

- `allowAgents` 目前主要体现在配置与文档中
- 服务端未形成完整的 actor 身份校验与权限拒绝闭环

整改动作：

- 所有状态推进、审批、派发、进展上报请求都附带 actor
- 服务端校验 actor 是否有权执行本次动作
- 增加来源签名或受信通道验证
- 拒绝无权限状态变更，并落审计日志

验收标准：

- 无权限 Agent 无法推进状态、伪造审批或越级派发
- 权限拒绝有结构化审计记录

重点文件：

- [docs/task-dispatch-architecture.md](/Users/xingzhan/Documents/edict/docs/task-dispatch-architecture.md)
- [docker/demo_data/openclaw.json](/Users/xingzhan/Documents/edict/docker/demo_data/openclaw.json)
- [dashboard/server.py](/Users/xingzhan/Documents/edict/dashboard/server.py)
- [edict/backend/app/api](/Users/xingzhan/Documents/edict/edict/backend/app/api)

### 4. 修正状态机关键语义漏洞

状态：已完成（2026-03-24）

风险级别：极高

问题：

- 进入 `Doing/Next` 时不一定绑定具体执行部门
- `Cancelled` 目前可恢复，弱化终态语义
- `Review reject` 与 `Menxia reject` 返工路径不够清晰

整改动作：

- 明确 `Doing/Next` 必须携带目标执行部门或执行 Agent
- 将 `Cancelled` 改为真终态，若需恢复则新建任务或显式 reopen
- 区分门下封驳与审查退回的不同返工路径

验收标准：

- 所有状态推进后，负责部门和负责 Agent 可唯一确定
- 没有“状态推进了但执行人不确定”的任务

重点文件：

- [dashboard/server.py](/Users/xingzhan/Documents/edict/dashboard/server.py)
- [edict/backend/app/models/task.py](/Users/xingzhan/Documents/edict/edict/backend/app/models/task.py)
- [scripts/kanban_update.py](/Users/xingzhan/Documents/edict/scripts/kanban_update.py)

---

## P1 高优先级

### 5. 明确主架构，只保留一条控制面主线

状态：已完成（2026-03-24）

风险级别：高

问题：

- 目前同时存在 `dashboard/server.py + JSON` 和 `FastAPI + Redis + Postgres`
- 前端仍主要连接 legacy API，新架构尚未成为主链路

整改动作：

- 决策主系统：保留 legacy 还是切换 event-driven backend
- 给非主线架构设定兼容期和退场计划
- 输出正式迁移路线图

验收标准：

- 前后端都只围绕一套主架构演进
- 仓库中不再有长期并行但不一致的双实现

重点文件：

- [edict/frontend/src/api.ts](/Users/xingzhan/Documents/edict/edict/frontend/src/api.ts)
- [edict/backend/app/main.py](/Users/xingzhan/Documents/edict/edict/backend/app/main.py)
- [dashboard/server.py](/Users/xingzhan/Documents/edict/dashboard/server.py)

### 6. 给派发链路加幂等和去重

状态：已完成（2026-03-24）

风险级别：高

问题：

- 自动重试、启动恢复、worker 认领 stale event 都可能重复派发
- 重复唤醒 Agent 会制造重复工作或重复消息

整改动作：

- 为每次派发生成可重放的唯一 `dispatch_key`
- 对同一 `task + state + version` 的派发进行去重
- 将派发结果与 generation/version 绑定

验收标准：

- 服务重启和 worker 恢复后不会重复派发同一轮工作
- 重试不会造成重复消息洪泛

重点文件：

- [edict/backend/app/workers/dispatch_worker.py](/Users/xingzhan/Documents/edict/edict/backend/app/workers/dispatch_worker.py)
- [edict/backend/app/workers/orchestrator_worker.py](/Users/xingzhan/Documents/edict/edict/backend/app/workers/orchestrator_worker.py)
- [dashboard/server.py](/Users/xingzhan/Documents/edict/dashboard/server.py)

### 7. 让事件持久化真正闭环

状态：已完成（2026-03-24）

风险级别：高

问题：

- 新后端已有 `events/thoughts/todos` 表
- 但 `EventBus.publish()` 当前只写 Redis，不写 Postgres 审计表

整改动作：

- 定义事件持久化策略：同步写库或异步 consumer 落库
- 将关键 topic 持久化到 `events`
- 视需要持久化 `thoughts` 和结构化 todo 演化过程

验收标准：

- `/api/events` 能查询到真实运行事件
- 审计链可用于回放和排障

重点文件：

- [edict/backend/app/services/event_bus.py](/Users/xingzhan/Documents/edict/edict/backend/app/services/event_bus.py)
- [edict/backend/app/models/event.py](/Users/xingzhan/Documents/edict/edict/backend/app/models/event.py)
- [edict/backend/app/api/events.py](/Users/xingzhan/Documents/edict/edict/backend/app/api/events.py)

### 8. 降低门下省与尚书省中央瓶颈

状态：已完成（2026-03-24）

风险级别：高

问题：

- 必审与必派发机制提高质量，但也天然形成队列瓶颈
- 对中高吞吐任务不够友好

整改动作：

- 引入快车道任务类型
- 允许带约束的横向咨询，不改变主状态机所有权
- 为 `Menxia` 与 `Shangshu` 引入 SLA 与排队监控

验收标准：

- 简单任务不会被完整官僚流程过度拖慢
- 中央节点繁忙时系统仍具备可接受吞吐

重点文件：

- [docs/task-dispatch-architecture.md](/Users/xingzhan/Documents/edict/docs/task-dispatch-architecture.md)

---

## P2 中优先级

### 9. 将前端从轮询逐步切到事件驱动

状态：已完成（2026-03-24）

风险级别：中

问题：

- 现有看板主要依赖 5 秒轮询
- 任务详情活动流再走单独轮询，整体属于准实时

整改动作：

- 先将任务活动流、Agent 心跳、调度事件切到 WebSocket
- 轮询保留为兜底刷新机制
- 为前端增加断线重连与事件序号处理

验收标准：

- 活动流和 Agent 状态变化能够秒级反映
- 轮询从主机制退为 fallback

重点文件：

- [edict/frontend/src/store.ts](/Users/xingzhan/Documents/edict/edict/frontend/src/store.ts)
- [edict/backend/app/api/websocket.py](/Users/xingzhan/Documents/edict/edict/backend/app/api/websocket.py)

### 10. 拆分旧版 `dashboard/server.py`

状态：已完成（2026-03-24）

风险级别：中

问题：

- 单文件同时承担静态服务、任务控制、技能管理、调度器、晨报、议政
- 功能扩张会持续加剧维护成本

整改动作：

- 最少拆分为 `tasks`、`scheduler`、`agents`、`skills`、`morning`、`court`
- 把纯业务逻辑从 HTTP 层抽离

验收标准：

- 单文件职责边界明显收缩
- 新增功能无需继续堆入一个大文件

重点文件：

- [dashboard/server.py](/Users/xingzhan/Documents/edict/dashboard/server.py)

### 11. 建立架构契约测试

状态：已完成（2026-03-24）

风险级别：中

问题：

- 当前测试更多覆盖 legacy 脚本和 demo 行为
- 缺少架构级契约测试

整改动作：

- 增加状态机一致性测试
- 增加权限矩阵测试
- 增加幂等派发测试
- 增加 API contract 测试

验收标准：

- 状态机、权限和事件链路修改后能被自动回归验证

重点文件：

- [tests](/Users/xingzhan/Documents/edict/tests)

### 12. 将组织角色和状态定义配置化

状态：已完成（2026-03-24）

风险级别：中

问题：

- 前端、后端、脚本、文档都硬编码了角色与状态定义
- 长期必然漂移

整改动作：

- 抽出统一 schema 文件
- 自动生成前端 `PIPE/DEPTS`
- 自动生成后端枚举/映射
- 自动生成文档片段和 OpenClaw 配置

验收标准：

- 新增部门、状态、角色时只修改一处

重点文件：

- [edict/frontend/src/store.ts](/Users/xingzhan/Documents/edict/edict/frontend/src/store.ts)
- [dashboard/server.py](/Users/xingzhan/Documents/edict/dashboard/server.py)
- [docker/demo_data/openclaw.json](/Users/xingzhan/Documents/edict/docker/demo_data/openclaw.json)

---

## P3 优化项

### 13. 替换原生 `prompt/confirm`，统一为正式交互组件

状态：已完成（2026-03-24）

风险级别：低

问题：

- 当前关键任务操作仍依赖浏览器原生弹窗
- 容易打断操作流，不利于一致体验

整改动作：

- 统一接入 `ConfirmDialog`
- 增加结构化原因输入、风险提示、权限说明

验收标准：

- stop/cancel/review/advance/scheduler 操作都使用统一交互层

重点文件：

- [edict/frontend/src/components/EdictBoard.tsx](/Users/xingzhan/Documents/edict/edict/frontend/src/components/EdictBoard.tsx)
- [edict/frontend/src/components/TaskModal.tsx](/Users/xingzhan/Documents/edict/edict/frontend/src/components/TaskModal.tsx)
- [edict/frontend/src/components/ConfirmDialog.tsx](/Users/xingzhan/Documents/edict/edict/frontend/src/components/ConfirmDialog.tsx)

### 14. 持久化朝堂议政会话

状态：已完成（2026-03-24）

风险级别：低

问题：

- 当前朝堂议政 session 存在进程内存中
- 服务重启即丢失

整改动作：

- 将 session、消息、总结持久化到数据库或文件
- 支持会话恢复、历史查询和复盘

验收标准：

- 议政记录可以跨重启保留

重点文件：

- [dashboard/court_discuss.py](/Users/xingzhan/Documents/edict/dashboard/court_discuss.py)

### 15. 收紧安全默认值

状态：已完成（2026-03-24）

风险级别：低

问题：

- 默认密码、开放 CORS、管理接口无鉴权
- 活动流可能暴露 prompt、路径和敏感输出

整改动作：

- 改掉默认弱凭据
- 为管理接口增加鉴权
- 限制跨域来源
- 为 activity/thinking 输出增加脱敏策略

验收标准：

- 默认部署不暴露高风险管理面
- 活动流可按环境区分敏感级别

重点文件：

- [edict/backend/app/config.py](/Users/xingzhan/Documents/edict/edict/backend/app/config.py)
- [edict/backend/app/main.py](/Users/xingzhan/Documents/edict/edict/backend/app/main.py)
- [dashboard/server.py](/Users/xingzhan/Documents/edict/dashboard/server.py)

---

## 推荐执行顺序

### 第一阶段：先止血

- 确定主架构
- 统一 schema 和状态机
- 修正并发写风险
- 强化权限运行时校验

### 第二阶段：补主链路可靠性

- 做幂等派发
- 完成事件持久化
- 修正状态机边界漏洞
- 建立架构契约测试

### 第三阶段：做实时化和可维护性

- 前端切 WebSocket
- 拆分 legacy 服务端
- 配置化组织与状态定义

### 第四阶段：体验与治理收尾

- 统一交互弹窗
- 持久化议政会话
- 收紧安全默认值

---

## 备注

- 如果项目短期目标仍是可演示 Demo，可优先完成 `P0 + P1`
- 如果目标是演进为长期运行的 OpenClaw 控制平面，建议完整推进 `P0 -> P2`
- 本清单默认项目根目录作为后续拆任务与同步状态的基线文档

---

## Review 增补 Findings（2026-03-24）

以下问题来自本轮基于 `gstack /review` 标准的追加复核，属于在首版整改清单之外进一步确认出的落地风险。

### A1. 后端 `transition_state()` 在多数非执行态流转后不会同步 `org`

状态：已完成（2026-03-24）

风险级别：高

问题：

- `TaskService.transition_state()` 当前先基于旧任务快照取 `new_org`
- 但 `validate_transition()` 只会在 `Doing/Next` 时修正执行部门
- 导致 `Sili -> Zhongshu`、`Zhongshu -> Menxia`、`Review -> Done` 等流转后可能出现 `state` 已变而 `org` 仍是旧值

整改动作：

- 在服务层显式根据目标状态重算 `org`
- 对非执行态使用状态默认部门
- 对执行态继续使用 `targetDept/org` 解析
- 为后端 API 增加 `state/org` 一致性回归测试

验收标准：

- 任意一次合法流转后，`state` 与 `org` 总能对应到唯一责任部门
- 不存在 “状态已推进但责任部门仍停留在上一环节” 的任务记录

重点文件：

- [edict/backend/app/services/task_service.py](/Users/xingzhan/Documents/edict/edict/backend/app/services/task_service.py)
- [edict/backend/app/task_contract.py](/Users/xingzhan/Documents/edict/edict/backend/app/task_contract.py)

### A2. 事件驱动主链路进入执行态时会丢失派发目标

状态：已完成（2026-03-24）

风险级别：高

问题：

- `task.status` 事件当前只携带 `task_id/from/to/reason`
- `OrchestratorWorker` 在消费状态事件后调用 `resolve_dispatch_agent()`
- 当目标状态是 `Doing/Next` 时，若 payload 中没有 `org/targetDept`，就无法解析出六部 Agent

整改动作：

- 为状态事件补齐 `org/targetDept/stateVersion` 等派发必需字段
- 或者在 orchestrator 中二次查库获取任务完整快照
- 为执行态自动派发增加集成测试

验收标准：

- `Assigned -> Doing`、`Review -> Doing` 等路径能稳定解析到具体六部 Agent
- 事件驱动链路不会出现 “状态已切到执行态，但无人接单” 的情况

重点文件：

- [edict/backend/app/services/task_service.py](/Users/xingzhan/Documents/edict/edict/backend/app/services/task_service.py)
- [edict/backend/app/workers/orchestrator_worker.py](/Users/xingzhan/Documents/edict/edict/backend/app/workers/orchestrator_worker.py)
- [edict/backend/app/task_contract.py](/Users/xingzhan/Documents/edict/edict/backend/app/task_contract.py)

### A3. `OrchestratorWorker` 的 stale-event 恢复路径未 ACK

状态：已完成（2026-03-24）

风险级别：高

问题：

- `_recover_pending()` 里会认领并处理旧 pending 事件
- 但处理后没有执行 `ACK`
- worker 重启后可能持续重复回放旧事件，放大重复派发风险

整改动作：

- 在恢复路径中对成功处理的事件补 `ACK`
- 为 worker 重启恢复场景增加回归测试
- 将恢复路径与正常消费路径统一为同一套 “处理成功即 ACK” 逻辑

验收标准：

- 重启后 stale event 最多被成功处理一次
- 恢复模式与正常消费模式行为一致

重点文件：

- [edict/backend/app/workers/orchestrator_worker.py](/Users/xingzhan/Documents/edict/edict/backend/app/workers/orchestrator_worker.py)
- [edict/backend/app/services/event_bus.py](/Users/xingzhan/Documents/edict/edict/backend/app/services/event_bus.py)

### A4. `archive_all_done` 仍存在批量归档权限绕过

状态：已完成（2026-03-24）

风险级别：中高

问题：

- `handle_archive_task()` 对单任务归档做了 actor 限制
- 但 `archive_all_done=True` 的分支在权限判断之前直接执行
- 非高权限 actor 仍可批量归档已完成任务

整改动作：

- 将批量归档纳入与单任务归档一致的权限校验
- 增加非授权 actor 的拒绝测试

验收标准：

- 非 `emperor/system` actor 无法执行任何批量归档动作
- 审计日志中能准确记录拒绝原因

重点文件：

- [dashboard/server.py](/Users/xingzhan/Documents/edict/dashboard/server.py)
- [tests/test_server.py](/Users/xingzhan/Documents/edict/tests/test_server.py)

### A5. 高层 E2E 回归测试尚未与新状态机语义对齐

状态：已完成（2026-03-24）

风险级别：中高

问题：

- 新逻辑已将执行完成改为 `Doing/Next -> Review -> Done`
- 但现有 E2E 测试仍按旧语义断言 `cmd_done()` 直接进入 `Done`
- 分支当前不是完整绿灯状态

整改动作：

- 更新 E2E 用例以匹配新的审查链路
- 明确哪些测试代表旧兼容语义，哪些代表新契约语义
- 将 E2E 纳入架构整改验收门槛

验收标准：

- `test_e2e_kanban.py` 与新的状态机规则保持一致
- 整改分支在单测与高层回归测试上均通过

重点文件：

- [tests/test_e2e_kanban.py](/Users/xingzhan/Documents/edict/tests/test_e2e_kanban.py)
- [scripts/kanban_update.py](/Users/xingzhan/Documents/edict/scripts/kanban_update.py)

### A6. Alembic 初始迁移仍然依赖运行时代码，且事件模型长度约束不完全一致

状态：已完成（2026-03-24）

风险级别：中

问题：

- `001_initial.py` 通过 `import app.task_contract` 读取枚举值
- 这会让 migration 不再是稳定快照，而依赖当前运行时代码
- 同时 `events.trace_id` 在 ORM 与 migration 中的长度定义仍不一致

整改动作：

- 将 migration 中的枚举常量固化为 revision 当时的快照
- 对齐 `Event` ORM 与 Alembic 字段长度定义
- 补一次 schema diff 检查

验收标准：

- 老 revision 在未来代码继续演进后仍可独立执行
- `alembic revision --autogenerate` 不再生成同一批结构差异

重点文件：

- [edict/migration/versions/001_initial.py](/Users/xingzhan/Documents/edict/edict/migration/versions/001_initial.py)
- [edict/backend/app/models/event.py](/Users/xingzhan/Documents/edict/edict/backend/app/models/event.py)

### A7. 后端数据库模式下的任务 ID 生成仍有并发撞号风险

状态：已完成（2026-03-24）

风险级别：中

问题：

- `TaskService._next_task_id()` 采用 `select existing ids -> max + 1`
- 在多请求并发创建任务时仍可能生成相同编号
- legacy JSON 模式已通过文件锁规避，该问题主要留在新后端路径

整改动作：

- 改为数据库序列、独立 counter 表或唯一约束重试
- 为并发创建增加异步/并发测试

验收标准：

- 后端并发创建任务时不会出现主键冲突或编号重复
- 任务编号仍保持可读的 `JJC-YYYYMMDD-NNN` 规则

重点文件：

- [edict/backend/app/services/task_service.py](/Users/xingzhan/Documents/edict/edict/backend/app/services/task_service.py)
- [edict/backend/app/models/task.py](/Users/xingzhan/Documents/edict/edict/backend/app/models/task.py)

## 本轮验证记录

- `python3 -m pytest tests/test_server.py tests/test_kanban.py tests/test_file_lock.py` 通过，`24 passed`
- `python3 -m pytest tests/test_e2e_kanban.py` 失败，当前有 `2` 个用例仍按旧状态机语义断言

## 建议插队处理顺序

- 先修 `A1 + A2 + A3`，因为它们直接影响新主链路的一致性、派发可靠性和重复执行风险
- 紧接着修 `A4 + A5`，把权限闭环和回归门槛补齐
- 最后处理 `A6 + A7`，防止数据库路径在后续迁移和并发场景里继续埋雷
