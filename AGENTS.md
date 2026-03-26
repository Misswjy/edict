# AGENTS.md

## 目标

本文件给进入本仓库协作的编码代理使用。目标不是重复通用编码礼仪，而是帮助代理快速判断：

- 这个仓库当前有几条实现链路
- 这次需求应该改哪里，不该误改哪里
- 哪些技能最适合这个项目
- 改完至少要做哪些验证


## 仓库现状总览

这个仓库现在的默认形态是「v2 主链路 + 少量 JSON/回滚遗留资产」。

- `edict/backend/app/**` + `edict/frontend/src/**` + `edict/migration/**` 是当前默认主链路
- 仓库内 source-based legacy runtime（`dashboard/server.py`、`dashboard/dashboard.html`、`scripts/run_loop.sh`、`scripts/refresh_live_data.py`）已移除
- 仍保留的遗留资产主要是 `data/*.json`、`scripts/kanban_update.py`、`scripts/file_lock.py`、`scripts/sync_from_openclaw_runtime.py`、`ops/cutover/merge_delta_into_legacy.py`，用于迁移、shadow、回灌和兼容 CLI
- 历史 Demo / rollback 依赖冻结 legacy 镜像与备份工件，而不是仓库内本地 legacy 源码

开始改代码前，先判断任务属于哪条链路。不要因为目录名字像“新版”就默认它已经完全替代旧实现。


## 目录判断规则

### 1. JSON 兼容 / 回滚遗留链路

优先涉及这些文件：

- `scripts/kanban_update.py`
- `scripts/file_lock.py`
- `scripts/sync_from_openclaw_runtime.py`
- `ops/cutover/merge_delta_into_legacy.py`
- `docker-compose.yml`
- `tests/test_kanban.py`
- `tests/test_e2e_kanban.py`
- `tests/test_file_lock.py`

这条链路通常对应：

- JSON shadow / 导入源
- Agent CLI 状态上报
- 回滚窗口 delta 回灌
- 冻结 legacy 镜像的历史 Demo
- 并发写入与文件锁

### 2. v2 拆分式链路

优先涉及这些文件：

- `edict/backend/app/**`
- `edict/frontend/src/**`
- `edict/migration/**`
- `edict/docker-compose.yml`

这条链路通常对应：

- FastAPI / SQLAlchemy / Redis / Postgres
- React + Vite 前端
- WebSocket / Event Bus / Worker
- 新的任务契约、数据库模型、迁移脚本

### 3. 兼容迁移类需求

如果一个需求同时影响以下任意两项，就按“兼容迁移”处理：

- `edict/frontend/src/api.ts` 的字段约定
- `edict/backend/app/task_contract.py` 的状态与 Actor 语义
- `scripts/kanban_update.py` 的 JSON / CLI 约定
- `edict/backend/app/api/dashboard.py`、`admin_actions.py` 等兼容端点

这种需求不要只改一处。先检查契约，再决定需要同步哪些层。


## 推荐技能

以下技能最适合当前项目。若当前 Codex 环境已安装，优先按触发场景使用；若未安装，则按同等思路手工执行。

本仓库已将一组项目本地技能 vendored 到 `.agents/skills/`。如果运行环境支持项目内技能发现，优先使用这些本地副本，避免依赖个人全局目录。

### 默认高频

- `investigate`
  适用于任务流转异常、状态机回退、调度失灵、并发丢写、数据同步异常、兼容层行为不一致。

- `review`
  适用于准备合并前的差异检查，尤其是状态转换、权限矩阵、远程 skills、调度副作用、跨模块契约变更。

- `qa`
  适用于改完接口、看板交互、任务操作之后做系统性回归；这个仓库的 UI 和任务流都很适合走 QA 闭环。

- `browse`
  适用于看板标签页、任务详情、技能配置、模板库、截图更新、交互可用性验证。通常和 `qa` 搭配最好。

- `document-release`
  适用于改动 README、架构文档、上手指南、安装流程、截图说明、CLI 命令后做文档同步。

### 按需使用

- `cso`
  适用于远程 skill 导入、Agent 权限、API trust boundary、签名/actor/source 字段、安全校验、Webhook / OpenClaw 集成。

- `benchmark`
  适用于轮询频率、面板渲染性能、资源体积、首屏加载、长列表更新、前端 build 产物回归。

- `design-review`
  适用于看板视觉层级、间距、信息密度、组件统一性、交互反馈、截图质量提升。

- `frontend-skill`
  适用于在 `edict/frontend/src/**` 新做整块 UI，而不是小修小补。

### 非默认技能

以下技能不是本仓库的默认起手式，除非任务明确要求：

- `ship`
- `land-and-deploy`
- `canary`
- `setup-browser-cookies`
- `openai-docs`

### 已安装到项目内的技能

当前已安装到 `.agents/skills/`：

- `investigate`
- `review`
- `qa`
- `browse`
- `document-release`
- `cso`
- `benchmark`
- `design-review`
- `frontend-skill`

如果后续要补技能，优先遵循这条原则：

- 先安装项目高频技能，再安装个人偏好型技能
- 优先安装能直接服务当前仓库的技能，不要把项目技能库变成个人工具箱镜像


## 开发规范

### 先做定位，再做修改

- 先确认需求落在 legacy、v2，还是兼容迁移。
- 除非任务明确要求，不要在一次改动里同时重写两条链路。
- 如果确实需要双改，提交说明里要明确“为什么两边都要改”。

### 任务契约是高风险区

以下内容视为高风险变更：

- 任务状态名与状态流转
- `org` / `targetDept` / actor / source / signature / timestamp 语义
- `flow_log`、`progress_log`、`todos` 字段结构
- legacy task id 与 v2 task id 的兼容方式

改这些时，至少同时检查：

- `scripts/kanban_update.py`
- `edict/backend/app/task_contract.py`
- `edict/backend/app/api/tasks.py`
- `edict/backend/app/api/dashboard.py`
- `edict/backend/app/api/admin_actions.py`
- `edict/frontend/src/api.ts`
- 相关测试

### 保持兼容优先

- 优先保持 v2 API 对前端、CLI、迁移/回灌工具的兼容，不要重新引入 source-based legacy runtime。
- 如果要破坏兼容字段或 JSON shadow 约定，必须同步修改调用方、测试和文档。
- 根文档默认链路已经切到 v2，但历史 Demo / rollback 说明仍会提到冻结 legacy 镜像。

### Dist 与 Demo 数据不是默认编辑目标

- `dashboard/dist/**` 属于构建产物。只有在明确需要提交发布产物、截图对齐或 Demo 镜像更新时才改。
- `docker/demo_data/**` 与 `data/**` 更接近示例 / 运行态数据，不是主业务逻辑的 source of truth。
- 如果只是修逻辑，优先改源码与测试，不要直接改生成数据“修结果”。

### 文案与领域语言保持稳定

- 中文术语优先保持一致：`旨意`、`奏折`、`中书省`、`门下省`、`尚书省`、`六部`、`朝堂议政` 等。
- 新增英文文档或更新用户可见命令时，注意是否需要同步 `README_EN.md`。
- 不要把 UI 文案随意改成工程黑话，尤其是状态、按钮、看板标题。

### 前后端联动时的约束

- 改接口前先看 `edict/frontend/src/api.ts` 是否仍依赖 legacy 路由格式。
- 改 v2 backend 的任务字段时，检查 React 类型定义是否也要同步。
- 改 legacy server 的返回结构时，检查现有测试与前端调用是否会被影响。

### 并发与调度变更要格外谨慎

涉及以下主题时，默认补测试：

- 文件锁
- 原子更新
- 并发创建任务
- stop / resume / review / scheduler 并行修改
- Worker 派发、副作用重试、回滚升级


## 验证要求

优先跑与改动范围匹配的最小充分验证。常用命令如下。

### Python 测试

```bash
pytest -q
```

如果只改 legacy 主链路，至少跑：

```bash
pytest tests/test_kanban.py tests/test_e2e_kanban.py tests/test_file_lock.py tests/test_task_contract.py -q
```

### 语法级快速检查

```bash
python3 -m py_compile scripts/kanban_update.py edict/backend/app/main.py
```

### v2 前端构建

```bash
npm --prefix edict/frontend run build
```

### 运行入口

历史 Demo（冻结 legacy 镜像）：

```bash
docker compose up sansheng-demo
```

v2 Docker 组合：

```bash
docker compose -f edict/docker-compose.yml up --build
```

如果任务改的是界面、交互、截图或用户操作路径，尽量追加一次浏览器验收，而不只看测试通过。


## 文档同步要求

出现以下情况时，默认同步文档：

- 启动命令变了
- API / 状态流转 / 任务行为变了
- 看板截图需要更新
- 远程 skills 接入方式变了
- 安装流程、依赖、目录结构变了

优先检查：

- `README.md`
- `README_EN.md`
- `CONTRIBUTING.md`
- `docs/getting-started.md`
- `docs/task-dispatch-architecture.md`
- `docs/remote-skills-guide.md`
- `docs/remote-skills-quickstart.md`


## 提交前自检

提交前至少过一遍下面这份清单：

- 我改的是正确的链路，没有误改另一套实现
- 高风险字段变更已经同步到契约、调用方和测试
- 没有直接修改生成产物来掩盖源码问题
- 验证命令和改动范围匹配
- 用户可见行为变化已经同步文档
- 如果用了推荐技能，使用时机与任务是匹配的，而不是为了“看起来完整”


## 一句话原则

先分清 legacy / v2 / 兼容迁移，再动手；先守住任务契约和回归测试，再谈重构与美化。
