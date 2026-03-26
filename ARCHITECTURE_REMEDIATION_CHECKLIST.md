# Architecture Remediation Checklist

> 项目：三省六部 / Edict / OpenClaw 多 Agent 协作系统
>
> 更新时间：2026-03-26
>
> 用途：把本轮架构评估结论收敛成可执行的改造总表，作为项目根目录的统一整改基线。

## 1. 文档定位

这不是历史回顾文档，也不是“已经完成”的迁移庆功文档，而是一份面向当前仓库状态的落地清单。

当前基线必须按下面事实理解：

- v2 主链路是 `edict/backend/app/**`、`edict/frontend/src/**`、`edict/migration/**`
- legacy source runtime 已移除，`dashboard/dashboard.html` 与 `dashboard/server.py` 不再是仓库内可维护源码
- `dashboard/dist/index.html` 仅是冻结产物，不应继续当作主实现入口
- 兼容与回滚资产仍然存在，主要集中在 `scripts/**`、`data/*.json`、`ops/cutover/**`

因此，本清单的目标不是“再造一套系统”，而是：

1. 修正仍会影响 v2 主链路正确性的阻断项
2. 收敛兼容层边界，防止 legacy / v2 / cutover 三套语义继续漂移
3. 补齐生产可用性，包括可观测性、安全默认值和运维路径
4. 清理已删除 legacy runtime 遗留的测试、文档和构建噪音

## 2. 优先级总览

| 优先级 | 目标 | 结果定义 |
| --- | --- | --- |
| P0 | 先止血，修正真实运行风险 | 本地、Docker、cutover 环境都能稳定运行主链路 |
| P1 | 收敛主链路契约与可靠性 | 任务契约、派发、审计、实时链路不再多头定义 |
| P2 | 提升可运维性和使用体验 | Dashboard、告警、文档、兼容边界更适合长期维护 |
| P3 | 做治理收尾 | 删除不该继续存在的历史包袱，降低后续演进成本 |

## 3. P0 阻断项

### P0-1. 修正 Agent 回调地址与服务发现

问题背景：

- `edict/backend/app/workers/dispatch_worker.py` 在子进程环境中硬编码 `EDICT_API_URL=http://localhost:{settings.port}`
- `edict/scripts/kanban_update_edict.py` 也默认回调 `http://localhost:8000`
- 这在本机开发时可工作，但在 Docker / compose / 多服务网络中，`localhost` 往往指向 Agent 容器自身，不一定是后端服务

改造动作：

- [ ] 为后端新增显式配置项，例如 `agent_callback_base_url` 或 `public_api_base_url`
- [ ] 将 `dispatch_worker.py` 改为优先使用显式配置，而不是默认拼接 `localhost`
- [ ] 让 `edict/scripts/kanban_update_edict.py` 与 worker 共享同一套回调地址约定
- [ ] 在 `edict/docker-compose.yml` 中为 dispatcher / orchestrator / agent 执行环境注入正确服务地址
- [ ] 补一组“本机开发 + Docker 服务发现”回归测试，防止后续改回隐式 localhost

涉及文件：

- `edict/backend/app/workers/dispatch_worker.py`
- `edict/scripts/kanban_update_edict.py`
- `edict/backend/app/config.py`
- `edict/docker-compose.yml`

验收标准：

- 本机直跑时 agent 可以稳定回调 v2 API
- Docker 组合启动时 agent 不依赖容器内 `localhost`
- 回调失败时日志能明确看到解析后的目标地址和失败原因

建议验证：

```bash
pytest tests/test_backend_dispatch.py tests/test_v2_task_compat.py -q
docker compose -f edict/docker-compose.yml up --build backend redis postgres orchestrator dispatcher
```

### P0-2. 清理已删除 legacy runtime 的残留依赖

问题背景：

- 旧文档和旧清单仍频繁引用 `dashboard/dashboard.html`、`dashboard/server.py`
- 当前仓库内只剩 `dashboard/dist/index.html` 与 `dashboard/__pycache__/*`
- `tests/test_security_defaults.py` 仍然 `import server as srv`，与“source-based legacy runtime 已移除”的仓库现实不一致

改造动作：

- [ ] 明确区分“冻结演示产物”和“仍在维护的源码”，不要再把 `dashboard/dist/**` 当主实现目标
- [ ] 重写仍依赖 `server.py` 的测试，使其改为验证 v2 代码或显式测试夹具
- [ ] 删除仓库里不应提交的 `dashboard/__pycache__` 产物，并补 `.gitignore` / 清理说明
- [ ] 扫描根目录和 `docs/**`，移除所有把已删除 legacy 文件当作当前源码的表述
- [ ] 保留对 legacy 的引用时，必须明确标注“rollback / frozen image / historical context”

涉及文件：

- `tests/test_security_defaults.py`
- `ARCHITECTURE_REMEDIATION_CHECKLIST.md`
- `V2_FULL_MIGRATION_CHECKLIST.md`
- `README.md`
- `README_EN.md`
- `docs/**`
- `dashboard/dist/index.html`

验收标准：

- 干净 checkout 下不会因为缺失 `dashboard/server.py` 而让测试或文档失真
- 所有 legacy 引用都带清晰语义：运行中、冻结产物、回滚资产或历史上下文
- 仓库不再依赖 `__pycache__` 一类副产物“碰巧可用”

建议验证：

```bash
pytest tests/test_security_defaults.py tests/test_stage_cutover_routing.py -q
rg -n "dashboard/server.py|dashboard/dashboard.html" README.md README_EN.md docs ARCHITECTURE_REMEDIATION_CHECKLIST.md V2_FULL_MIGRATION_CHECKLIST.md
```

### P0-3. 冻结兼容边界，避免任务契约继续漂移

问题背景：

- `docs/task-dispatch-architecture.md` 已声明 `config/institution_schema.json` 和 `Task.to_dict()` 是关键真相源
- 但前端 `edict/frontend/src/api.ts` 仍承担大量兼容字段兜底和别名归一化
- 如果继续把兼容逻辑散落在前端、后端、迁移脚本、CLI 脚本中，后续每次改字段都会出现“看似兼容、实则漂移”

改造动作：

- [ ] 列出当前仍在被兼容层吞掉的字段别名，并标记哪些是必须长期兼容、哪些只是迁移期遗留
- [ ] 让 `edict/backend/app/task_contract.py`、`models/task.py`、`frontend/src/api.ts`、`edict/scripts/kanban_update_edict.py` 使用同一份字段映射说明
- [ ] 继续把部门、状态、权限矩阵的真相源收敛到 `config/institution_schema.json`
- [ ] 为 `Task.to_dict()` 输出和前端 `normalizeTask()` 之间建立契约测试，而不是依赖人工肉眼对齐
- [ ] 对新增字段设立规则：必须先改真相源，再改 compat 适配层，最后改 UI 消费层

涉及文件：

- `config/institution_schema.json`
- `docs/task-dispatch-architecture.md`
- `edict/backend/app/task_contract.py`
- `edict/backend/app/models/task.py`
- `edict/frontend/src/api.ts`
- `edict/frontend/src/store.ts`
- `edict/scripts/kanban_update_edict.py`

验收标准：

- 一个字段的定义、存储、输出、消费和兼容别名能在单一路径中解释清楚
- 新增或修改字段时，不需要在多个模块重复猜测语义

建议验证：

```bash
pytest tests/test_task_contract.py tests/test_v2_task_compat.py -q
npm --prefix edict/frontend run build
```

## 4. P1 主链路可靠性

### P1-1. 把实时链路真正收敛为“WebSocket 主、轮询兜底”

问题背景：

- `edict/frontend/src/App.tsx` 启动时无条件调用 `startPolling()`
- `edict/frontend/src/store.ts` 当前策略是“先连 WebSocket，同时始终保留倒计时轮询”
- 这已经比纯轮询更好，但仍不是严格意义上的“实时优先、降级明确”

改造动作：

- [ ] 将轮询改成显式 fallback：只有 WebSocket 断开、数据序号缺口、页面恢复前台时才触发补偿拉取
- [ ] 为实时消息补齐断线重连后的追平策略，例如按序号补抓或按时间窗口重载
- [ ] 区分快数据和慢数据，避免 `loadAll()` 在实时健康时反复全量刷新
- [ ] 在界面上明确展示“实时正常 / 降级兜底 / 数据可能过期”的状态，而不只是一个连接 chip

涉及文件：

- `edict/frontend/src/App.tsx`
- `edict/frontend/src/store.ts`
- `edict/frontend/src/api.ts`
- `edict/backend/app/api/websocket.py`

验收标准：

- WebSocket 正常时，不再有高频全量轮询作为常态
- 断线后能自动恢复，且用户能看见当前是否处于降级模式
- 慢接口刷新与任务活动流刷新被拆开，避免一次刷新拉太多数据

建议验证：

```bash
npm --prefix edict/frontend run build
```

补充人工验收：

- 打开 Dashboard，确认 WebSocket 正常时不会持续全量刷屏
- 主动断开后端或 WebSocket，确认前端进入兜底刷新并恢复提示

### P1-2. 强化派发链路的失败恢复、幂等与审计

问题背景：

- 当前派发链路已有去重与 stale reclaim 基础
- 但仍需继续补强“失败归因、重试策略、死信观测、结构化审计”这几个生产化短板

改造动作：

- [ ] 为重复失败的 dispatch 引入明确重试上限和 dead-letter / quarantine 语义
- [ ] 将 agent 执行失败的 `stderr`、超时、return code 和 dispatch key 结构化落审计或事件表
- [ ] 暴露每个 worker 的 backlog、claim、ack、retry、timeout 指标
- [ ] 为不同失败类型区分自动重试与人工介入，不要所有错误都靠 Streams 重投

涉及文件：

- `edict/backend/app/workers/dispatch_worker.py`
- `edict/backend/app/workers/orchestrator_worker.py`
- `edict/backend/app/services/event_bus.py`
- `edict/backend/app/models/event.py`
- `docs/task-dispatch-architecture.md`

验收标准：

- 同一派发不会因为重启或 reclaim 被重复执行多次
- 执行失败后，排障人员能查到 task、agent、dispatch key、stderr、重试次数和最终状态
- backlog 和 dead-letter 状态能被健康检查或指标系统观测到

建议验证：

```bash
pytest tests/test_backend_dispatch.py -q
curl -fsS http://127.0.0.1:8000/api/admin/health/deep
curl -fsS http://127.0.0.1:8000/api/metrics/snapshot
```

### P1-3. 收紧管理面、安全默认值与敏感信息暴露

问题背景：

- `edict/backend/app/security.py` 已具备脱敏能力，但安全能力还需要从 helper 推到端到端默认值
- 管理接口、活动流、远程技能和跨域配置都处在真实 trust boundary 上

改造动作：

- [ ] 明确区分本地开发默认值与生产默认值，禁止生产继续依赖空 token 或过宽 CORS
- [ ] 让“full activity sensitivity”只对明确授权的管理面开放
- [ ] 审计所有 admin / remote skills / dispatch control 写接口的鉴权与来源校验
- [ ] 对 prompt、路径、stdout、stderr、token 等高风险字段补端到端脱敏测试

涉及文件：

- `edict/backend/app/security.py`
- `edict/backend/app/config.py`
- `edict/backend/app/main.py`
- `edict/backend/app/api/admin_actions.py`
- `edict/backend/app/api/dashboard.py`
- `docs/remote-skills-guide.md`
- `docs/remote-skills-quickstart.md`

验收标准：

- 默认部署不暴露高风险管理写接口
- 活动流默认是 redacted，full 模式必须可审计、可授权
- 远程技能和管理接口的调用方身份可以被追踪

建议验证：

```bash
pytest tests/test_security_defaults.py -q
python3 -m py_compile edict/backend/app/main.py
```

### P1-4. 把 staged cutover 从“文档存在”升级成“演练可执行”

问题背景：

- `docs/v2-cutover-runbook.md` 与 `tests/test_stage_cutover_routing.py` 已经定义了 Stage 1-5 的行为契约
- 但真正的风险在于：文档、Nginx 渲染、compose 服务集、回滚补偿脚本是否始终同步

改造动作：

- [ ] 为每个 stage 绑定一份固定的 smoke checklist，而不是只留概念性说明
- [ ] 将 `render_stage_routing.py`、runbook、compose 服务名和回滚脚本做一致性校验
- [ ] 在 cutover 演练中验证“读流量、写流量、worker、scheduler、legacy observation”五个维度，而不只看首页能否打开
- [ ] 将演练结果沉淀为 `ops/` 下的样例记录，便于复盘和二次执行

涉及文件：

- `docs/v2-cutover-runbook.md`
- `ops/cutover/render_stage_routing.py`
- `ops/cutover/cutover_to_v2.sh`
- `ops/cutover/rollback_to_legacy.sh`
- `edict/docker-compose.yml`

验收标准：

- 任一 stage 的流量与服务开关都可以被脚本和测试共同验证
- 切流失败时可以在固定窗口内回滚并保留回滚期 delta 补偿路径

建议验证：

```bash
pytest tests/test_stage_cutover_routing.py -q
```

## 5. P2 运维与体验优化

### P2-1. 收敛 Dashboard 的信息架构与加载策略

问题背景：

- `edict/frontend/src/App.tsx` 当前将看板、监控、官员、模型、技能、晨报、朝堂议政等能力集中在单页总控台
- 这是演示友好方案，但随着数据域增加，容易继续把加载、错误态、空态、权限差异堆在一个入口里

改造动作：

- [ ] 为各个 tab 建立独立的 loading / empty / error / permission denied 状态
- [ ] 优先按标签页懒加载非主路径数据，降低总控台首屏负担
- [ ] 把“系统状态条”“任务看板”“运维面板”分成清晰层级，减少一个页面承担过多语义
- [ ] 重新评估哪些能力必须长期留在总控台，哪些更适合作为二级页面或管理页

涉及文件：

- `edict/frontend/src/App.tsx`
- `edict/frontend/src/store.ts`
- `edict/frontend/src/components/**`

验收标准：

- 首屏只加载当前视图必要数据
- 非主路径模块失败时不会拖垮整个总控台
- 用户能明确理解当前自己在看“业务看板”还是“系统运维面板”

### P2-2. 建立一致的运维观测面

问题背景：

- runbook 已要求深健康检查、指标导出和 Prometheus 规则
- 还需要把这些内容从“文档条目”变成“团队可持续执行的运维面”

改造动作：

- [ ] 统一健康检查、指标快照、队列指标、worker 心跳的字段定义
- [ ] 将关键指标接入 Prometheus / Grafana 或至少形成标准化截图与巡检步骤
- [ ] 为 dispatcher / orchestrator / scheduler 定义最低可接受 SLA
- [ ] 对 WebSocket 长连断开、队列堆积、调度升级、回滚频率建立告警模板

涉及文件：

- `docs/v2-cutover-runbook.md`
- `ops/alerts/**`
- `edict/backend/app/api/dashboard.py`
- `edict/backend/app/api/websocket.py`

验收标准：

- 故障发生时，团队不需要翻源码才能定位问题
- 观测面可以覆盖任务、事件、worker、WebSocket、cutover 五类核心风险

### P2-3. 同步文档，把“现在怎么跑”讲清楚

问题背景：

- 当前仓库同时存在 README、迁移清单、架构文档、cutover runbook、legacy 兼容说明
- 如果这些文档不一起维护，就会重复把“当前主链路”讲错

改造动作：

- [ ] 统一 README、README_EN、getting-started、task-dispatch-architecture、cutover runbook 的叙事口径
- [ ] 把“v2 主链路 / compat 脚本 / rollback 资产 / frozen demo”四种角色写清楚
- [ ] 删除所有已经不适合当前仓库状态的启动方式、截图路径和源码引用
- [ ] 为新贡献者补一页最短路径说明：改 v2 去哪里、改 compat 去哪里、不要改哪里

涉及文件：

- `README.md`
- `README_EN.md`
- `docs/getting-started.md`
- `docs/task-dispatch-architecture.md`
- `docs/v2-cutover-runbook.md`
- `AGENTS.md`

验收标准：

- 新成员阅读根文档后，不会误以为 legacy source runtime 仍在仓库中维护
- 任意一个用户可见操作路径都能在文档中找到对应的当前实现位置

## 6. P3 治理收尾

### P3-1. 删除不该继续保留的历史噪音

改造动作：

- [ ] 清理仓库内无用缓存、过期构建产物、误提交的临时文件
- [ ] 为必须保留的 dist / demo / sample 数据补“为什么保留”的说明
- [ ] 将“兼容保留”和“历史残留”分开管理，避免后续继续误改

验收标准：

- 仓库树能一眼看出什么是源码、什么是产物、什么是回滚资产

### P3-2. 建立持续回归门禁

改造动作：

- [ ] 将关键 Python 契约测试、前端 build、cutover 路由测试纳入默认 CI
- [ ] 为修改高风险文件的 PR 增加最小验证清单
- [ ] 明确哪些改动必须同步更新本清单和迁移文档

涉及文件：

- `tests/**`
- `.github/workflows/**`
- `AGENTS.md`

验收标准：

- 关键链路不再靠人工记忆维持一致性

## 7. 建议执行顺序

建议按下面顺序推进，避免返工：

1. 先做 `P0-1`，解决真实运行环境里的回调与服务发现问题
2. 再做 `P0-2`，把“已删除 runtime 仍被依赖”的尾巴清掉
3. 接着做 `P0-3`，冻结兼容边界与单一真相源
4. 然后推进 `P1-1` 与 `P1-2`，把实时链路和派发链路做稳
5. 再完成 `P1-3` 与 `P1-4`，补上生产安全与 cutover 演练闭环
6. 最后做 `P2` 与 `P3`，统一运维面、界面体验和文档治理

## 8. 最小验证矩阵

每完成一个工作流，至少跑与之对应的最小充分验证：

```bash
pytest tests/test_task_contract.py tests/test_v2_task_compat.py tests/test_backend_dispatch.py -q
pytest tests/test_stage_cutover_routing.py tests/test_security_defaults.py -q
npm --prefix edict/frontend run build
python3 -m py_compile edict/backend/app/main.py edict/scripts/kanban_update_edict.py
```

如果改动涉及界面、切流、告警或 Dashboard 行为，还应补：

- 一次浏览器验收
- 一次 compose 级联调
- 一次切流脚本 dry-run 或 stage smoke 测试

## 9. Done 定义

只有当下面四件事同时成立，本轮整改才算真正完成：

1. v2 主链路在本机和 Docker 都能稳定运行，不依赖隐式 localhost 或 legacy 缓存产物
2. 任务契约、前端消费、compat 脚本、cutover 路径使用一致语义，不再多头漂移
3. 管理面、活动流、派发链路具备最基本的生产可观测性和安全默认值
4. 文档和测试反映的是“当前仓库真实状态”，而不是已经删除的历史实现
