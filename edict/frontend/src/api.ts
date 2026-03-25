/**
 * API 层
 * - 前端所有读写统一收敛到 FastAPI
 * - 只使用单一入口 `VITE_API_URL`
 */

const DEFAULT_V2_API_ORIGIN = 'http://127.0.0.1:8000';
const RAW_API_BASE = String(import.meta.env.VITE_API_URL || '').trim();

function normalizeApiBase(raw: string): string {
  const value = raw.trim();
  if (!value) return '';
  if (value.startsWith('/')) {
    const runtimeOrigin = typeof window !== 'undefined' ? window.location.origin : DEFAULT_V2_API_ORIGIN;
    return `${runtimeOrigin}${value}`.replace(/\/+$/, '').replace(/\/api$/, '');
  }
  if (value.startsWith('http://') || value.startsWith('https://')) {
    return value.replace(/\/+$/, '').replace(/\/api$/, '');
  }
  return `http://${value}`.replace(/\/+$/, '').replace(/\/api$/, '');
}

function inferRuntimeApiBase(): string {
  if (typeof window === 'undefined') return DEFAULT_V2_API_ORIGIN;
  const { protocol, hostname, port, origin } = window.location;
  if (!port || port === '80' || port === '443' || port === '8000') {
    return origin;
  }
  return `${protocol}//${hostname}:8000`;
}

function resolveApiOrigin(): string {
  return normalizeApiBase(RAW_API_BASE) || inferRuntimeApiBase();
}

const API_BASE = resolveApiOrigin();

function buildApiUrl(path: string): string {
  const suffix = path.startsWith('/') ? path : `/${path}`;
  return `${API_BASE.replace(/\/$/, '')}${suffix}`;
}

function toWebSocketOrigin(origin: string): string {
  if (origin.startsWith('https://')) return `wss://${origin.slice('https://'.length)}`;
  if (origin.startsWith('http://')) return `ws://${origin.slice('http://'.length)}`;
  if (origin.startsWith('wss://') || origin.startsWith('ws://')) return origin;
  if (origin.startsWith('/')) {
    const runtimeOrigin = typeof window !== 'undefined' ? window.location.origin : 'http://127.0.0.1:8000';
    return toWebSocketOrigin(`${runtimeOrigin}${origin}`);
  }
  return toWebSocketOrigin(`http://${origin}`);
}

export function buildWsUrl(path: string): string {
  const base = toWebSocketOrigin(API_BASE).replace(/\/$/, '');
  const suffix = path.startsWith('/') ? path : `/${path}`;
  return `${base}${suffix}`;
}

// ── 通用请求 ──

async function fetchJ<T>(url: string): Promise<T> {
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) throw new Error(String(res.status));
  return res.json();
}

async function postJ<T>(url: string, data: unknown): Promise<T> {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return res.json();
}

function withActor<T extends object>(data: T, actor = 'emperor', source = 'dashboard'): T & { actor: string; source: string } {
  return {
    ...data,
    actor,
    source,
  };
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function asString(value: unknown, fallback = ''): string {
  if (typeof value === 'string') return value;
  if (value == null) return fallback;
  return String(value);
}

function asNumber(value: unknown, fallback = 0): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

function asBoolean(value: unknown, fallback = false): boolean {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (['true', '1', 'yes', 'on'].includes(normalized)) return true;
    if (['false', '0', 'no', 'off'].includes(normalized)) return false;
  }
  return fallback;
}

function pickDefined<T>(...values: Array<T | null | undefined>): T | undefined {
  return values.find((value) => value !== undefined && value !== null);
}

function normalizeHeartbeat(value: unknown, fallbackLabel = '⚪ 无数据'): Heartbeat {
  const raw = asRecord(value);
  const status = asString(raw.status || 'unknown');
  if (status === 'active' || status === 'warn' || status === 'stalled' || status === 'unknown' || status === 'idle') {
    return {
      status,
      label: asString(raw.label, fallbackLabel),
    };
  }
  return { status: 'unknown', label: fallbackLabel };
}

function normalizeFlowEntry(value: unknown): FlowEntry {
  const raw = asRecord(value);
  return {
    at: asString(raw.at),
    from: asString(raw.from),
    to: asString(raw.to),
    remark: asString(raw.remark),
  };
}

function normalizeTodoStatus(value: unknown): TodoItem['status'] {
  const status = asString(value).trim().toLowerCase();
  if (status === 'completed' || status === 'done') return 'completed';
  if (status === 'in-progress' || status === 'in_progress' || status === 'doing') return 'in-progress';
  return 'not-started';
}

function normalizeTodoItem(value: unknown, index: number): TodoItem {
  const raw = asRecord(value);
  return {
    id: pickDefined(raw.id as string | number | undefined, index) ?? index,
    title: asString(raw.title || raw.text || raw.name, `Todo ${index + 1}`),
    status: normalizeTodoStatus(raw.status),
    detail: asString(raw.detail || raw.description),
  };
}

function normalizeProgressEntry(value: unknown): ProgressEntry {
  const raw = asRecord(value);
  return {
    at: asString(raw.at),
    agent: asString(raw.agent),
    agentLabel: asString(raw.agentLabel),
    text: asString(raw.text || raw.content),
    todos: asArray(raw.todos).map((item, index) => normalizeTodoItem(item, index)),
    state: asString(raw.state),
    org: asString(raw.org),
    tokens: asNumber(raw.tokens, 0),
    cost: asNumber(raw.cost, 0),
    elapsed: asNumber(raw.elapsed, 0),
  };
}

function normalizeActivityEntry(value: unknown): ActivityEntry {
  const raw = asRecord(value);
  const diffRaw = asRecord(raw.diff);
  return {
    kind: asString(raw.kind),
    at: pickDefined(raw.at as string | number | undefined, raw.ts as string | number | undefined),
    text: asString(raw.text),
    thinking: asString(raw.thinking),
    agent: asString(raw.agent),
    from: asString(raw.from),
    to: asString(raw.to),
    remark: asString(raw.remark),
    tools: asArray(raw.tools).map((tool) => {
      const item = asRecord(tool);
      return {
        name: asString(item.name),
        input_preview: asString(item.input_preview),
      };
    }),
    tool: asString(raw.tool),
    output: asString(raw.output),
    exitCode: pickDefined(raw.exitCode as number | null | undefined, raw.exit_code as number | null | undefined) ?? null,
    items: asArray(raw.items).map((item, index) => normalizeTodoItem(item, index)),
    diff: {
      changed: asArray(diffRaw.changed).map((item) => {
        const row = asRecord(item);
        return { id: asString(row.id), from: asString(row.from), to: asString(row.to) };
      }),
      added: asArray(diffRaw.added).map((item) => {
        const row = asRecord(item);
        return { id: asString(row.id), title: asString(row.title) };
      }),
      removed: asArray(diffRaw.removed).map((item) => {
        const row = asRecord(item);
        return { id: asString(row.id), title: asString(row.title) };
      }),
    },
  };
}

function normalizeTask(value: unknown): Task {
  const raw = asRecord(value);
  const scheduler = asRecord(pickDefined(raw._scheduler, raw.scheduler));
  return {
    id: asString(raw.id),
    title: asString(raw.title),
    state: asString(raw.state),
    org: asString(raw.org),
    now: asString(raw.now),
    eta: asString(raw.eta, '-'),
    block: asString(raw.block, '无'),
    ac: asString(raw.ac),
    output: asString(raw.output),
    heartbeat: normalizeHeartbeat(raw.heartbeat),
    flow_log: asArray(pickDefined(raw.flow_log, raw.flowLog)).map(normalizeFlowEntry),
    progress_log: asArray(pickDefined(raw.progress_log, raw.progressLog)).map(normalizeProgressEntry),
    todos: asArray(raw.todos).map((item, index) => normalizeTodoItem(item, index)),
    review_round: asNumber(pickDefined(raw.review_round, raw.reviewRound), 0),
    archived: asBoolean(raw.archived, false),
    archivedAt: asString(pickDefined(raw.archivedAt, raw.archived_at)),
    createdAt: asString(pickDefined(raw.createdAt, raw.created_at)),
    updatedAt: asString(pickDefined(raw.updatedAt, raw.updated_at)),
    lane: asString(raw.lane) === 'fast' ? 'fast' : 'standard',
    targetDept: asString(pickDefined(raw.targetDept, raw.target_dept)),
    templateId: asString(pickDefined(raw.templateId, raw.template_id)),
    templateParams: asRecord(pickDefined(raw.templateParams, raw.template_params)),
    _scheduler: scheduler,
    sourceMeta: asRecord(raw.sourceMeta),
    activity: asArray(raw.activity).map(normalizeActivityEntry),
    _prev_state: asString(pickDefined(raw._prev_state, raw.prev_state)),
  };
}

function normalizeLiveStatus(value: unknown): LiveStatus {
  const raw = asRecord(value);
  const syncStatus = asRecord(raw.syncStatus);
  return {
    tasks: asArray(raw.tasks).map(normalizeTask),
    syncStatus: {
      ...syncStatus,
      ok: asBoolean(syncStatus.ok, false),
    },
  };
}

function normalizeSkillInfo(value: unknown): SkillInfo {
  const raw = asRecord(value);
  return {
    name: asString(raw.name),
    description: asString(raw.description),
    path: asString(raw.path),
  };
}

function normalizeAgentConfig(value: unknown): AgentConfig {
  const raw = asRecord(value);
  return {
    agents: asArray(raw.agents).map((agent) => {
      const item = asRecord(agent);
      return {
        id: asString(item.id),
        label: asString(item.label),
        emoji: asString(item.emoji),
        role: asString(item.role),
        model: asString(item.model),
        skills: asArray(item.skills).map(normalizeSkillInfo),
      };
    }),
    knownModels: asArray(raw.knownModels).map((model) => {
      const item = asRecord(model);
      return {
        id: asString(item.id),
        label: asString(item.label),
        provider: asString(item.provider),
      };
    }),
    dispatchChannel: asString(raw.dispatchChannel),
  };
}

function normalizeChangeLog(value: unknown): ChangeLogEntry[] {
  return asArray(value).map((entry) => {
    const item = asRecord(entry);
    return {
      at: asString(item.at),
      agentId: asString(item.agentId),
      oldModel: asString(item.oldModel),
      newModel: asString(item.newModel),
      rolledBack: asBoolean(item.rolledBack, false),
    };
  });
}

function normalizeOfficialInfo(value: unknown): OfficialInfo {
  const raw = asRecord(value);
  return {
    id: asString(raw.id),
    label: asString(raw.label),
    emoji: asString(raw.emoji),
    role: asString(raw.role),
    rank: asString(raw.rank),
    model: asString(raw.model),
    model_short: asString(raw.model_short),
    tokens_in: asNumber(raw.tokens_in, 0),
    tokens_out: asNumber(raw.tokens_out, 0),
    cache_read: asNumber(raw.cache_read, 0),
    cache_write: asNumber(raw.cache_write, 0),
    cost_cny: asNumber(raw.cost_cny, 0),
    cost_usd: asNumber(raw.cost_usd, 0),
    sessions: asNumber(raw.sessions, 0),
    messages: asNumber(raw.messages, 0),
    tasks_done: asNumber(raw.tasks_done, 0),
    tasks_active: asNumber(raw.tasks_active, 0),
    flow_participations: asNumber(raw.flow_participations, 0),
    merit_score: asNumber(raw.merit_score, 0),
    merit_rank: asNumber(raw.merit_rank, 0),
    last_active: asString(raw.last_active),
    heartbeat: normalizeHeartbeat(raw.heartbeat, '⚪ 待命'),
    participated_edicts: asArray(raw.participated_edicts).map((edict) => {
      const item = asRecord(edict);
      return {
        id: asString(item.id),
        title: asString(item.title),
        state: asString(item.state),
      };
    }),
  };
}

function normalizeOfficialsData(value: unknown): OfficialsData {
  const raw = asRecord(value);
  const totals = asRecord(raw.totals);
  return {
    officials: asArray(raw.officials).map(normalizeOfficialInfo),
    totals: {
      tasks_done: asNumber(totals.tasks_done, 0),
      cost_cny: asNumber(totals.cost_cny, 0),
    },
    top_official: asString(raw.top_official),
  };
}

function normalizeAgentsStatusData(value: unknown): AgentsStatusData {
  const raw = asRecord(value);
  const gateway = asRecord(raw.gateway);
  return {
    ok: asBoolean(raw.ok, false),
    gateway: {
      alive: asBoolean(gateway.alive, false),
      probe: asBoolean(gateway.probe, false),
      status: asString(gateway.status),
    },
    agents: asArray(raw.agents).map((agent) => {
      const item = asRecord(agent);
      return {
        id: asString(item.id),
        label: asString(item.label),
        emoji: asString(item.emoji),
        role: asString(item.role),
        status: (['running', 'idle', 'offline', 'unconfigured'].includes(asString(item.status))
          ? asString(item.status)
          : 'offline') as AgentStatusInfo['status'],
        statusLabel: asString(item.statusLabel),
        lastActive: asString(item.lastActive),
      };
    }),
    checkedAt: asString(raw.checkedAt),
  };
}

function normalizeMorningBrief(value: unknown): MorningBrief {
  const raw = asRecord(value);
  const categoriesRaw = asRecord(raw.categories);
  const categories: Record<string, MorningNewsItem[]> = {};
  Object.entries(categoriesRaw).forEach(([key, items]) => {
    categories[key] = asArray(items).map((item) => {
      const row = asRecord(item);
      return {
        title: asString(row.title),
        summary: asString(row.summary),
        desc: asString(row.desc),
        link: asString(row.link),
        source: asString(row.source),
        image: asString(row.image),
        pub_date: asString(row.pub_date),
      };
    });
  });
  return {
    date: asString(raw.date),
    generated_at: asString(raw.generated_at),
    categories,
  };
}

function normalizeSubConfig(value: unknown): SubConfig {
  const raw = asRecord(value);
  return {
    categories: asArray(raw.categories).map((item) => {
      const row = asRecord(item);
      return { name: asString(row.name), enabled: asBoolean(row.enabled, false) };
    }),
    keywords: asArray(raw.keywords).map((item) => asString(item)).filter(Boolean),
    custom_feeds: asArray(raw.custom_feeds).map((item) => {
      const row = asRecord(item);
      return {
        name: asString(row.name),
        url: asString(row.url),
        category: asString(row.category),
      };
    }),
    feishu_webhook: asString(raw.feishu_webhook),
  };
}

function normalizeTaskActivityData(value: unknown): TaskActivityData {
  const raw = asRecord(value);
  return {
    ok: asBoolean(raw.ok, false),
    message: asString(raw.message),
    error: asString(raw.error),
    activity: asArray(raw.activity).map(normalizeActivityEntry),
    relatedAgents: asArray(raw.relatedAgents).map((agent) => asString(agent)).filter(Boolean),
    agentLabel: asString(raw.agentLabel),
    lastActive: asString(raw.lastActive),
    phaseDurations: asArray(raw.phaseDurations).map((item) => {
      const row = asRecord(item);
      return {
        phase: asString(row.phase),
        durationSec: asNumber(row.durationSec, 0),
        durationText: asString(row.durationText),
        ongoing: asBoolean(row.ongoing, false),
      };
    }),
    totalDuration: asString(raw.totalDuration),
    todosSummary: {
      total: asNumber(asRecord(raw.todosSummary).total, 0),
      completed: asNumber(asRecord(raw.todosSummary).completed, 0),
      inProgress: asNumber(asRecord(raw.todosSummary).inProgress, 0),
      notStarted: asNumber(asRecord(raw.todosSummary).notStarted, 0),
      percent: asNumber(asRecord(raw.todosSummary).percent, 0),
    },
    resourceSummary: {
      totalTokens: asNumber(asRecord(raw.resourceSummary).totalTokens, 0),
      totalCost: asNumber(asRecord(raw.resourceSummary).totalCost, 0),
      totalElapsedSec: asNumber(asRecord(raw.resourceSummary).totalElapsedSec, 0),
    },
  };
}

function normalizeSchedulerStateData(value: unknown): SchedulerStateData {
  const raw = asRecord(value);
  const scheduler = asRecord(raw.scheduler);
  return {
    ok: asBoolean(raw.ok, false),
    error: asString(raw.error),
    scheduler: {
      retryCount: asNumber(scheduler.retryCount, 0),
      escalationLevel: asNumber(scheduler.escalationLevel, 0),
      lastDispatchStatus: asString(scheduler.lastDispatchStatus),
      stallThresholdSec: asNumber(scheduler.stallThresholdSec, 0),
      enabled: asBoolean(scheduler.enabled, false),
      lastProgressAt: asString(scheduler.lastProgressAt),
      lastDispatchAt: asString(scheduler.lastDispatchAt),
      lastDispatchAgent: asString(scheduler.lastDispatchAgent),
      autoRollback: asBoolean(scheduler.autoRollback, false),
    },
    stalledSec: asNumber(raw.stalledSec, 0),
  };
}

function normalizeSkillContentResult(value: unknown): SkillContentResult {
  const raw = asRecord(value);
  return {
    ok: asBoolean(raw.ok, false),
    name: asString(raw.name),
    agent: asString(raw.agent),
    content: asString(raw.content),
    path: asString(raw.path),
    error: asString(raw.error),
  };
}

function normalizeRemoteSkillsList(value: unknown): RemoteSkillsListResult {
  const raw = asRecord(value);
  return {
    ok: asBoolean(raw.ok, false),
    remoteSkills: asArray(raw.remoteSkills).map((item) => {
      const row = asRecord(item);
      return {
        skillName: asString(row.skillName),
        agentId: asString(row.agentId),
        sourceUrl: asString(row.sourceUrl),
        description: asString(row.description),
        localPath: asString(row.localPath),
        addedAt: asString(row.addedAt),
        lastUpdated: asString(row.lastUpdated),
        status: asString(row.status),
      };
    }),
    count: asNumber(raw.count, 0),
    listedAt: asString(raw.listedAt),
    error: asString(raw.error),
  };
}

// ── API 接口 ──

export const api = {
  // 核心数据
  liveStatus: () => fetchJ<LiveStatus>(buildApiUrl('/api/live-status')).then(normalizeLiveStatus),
  agentConfig: () => fetchJ<AgentConfig>(buildApiUrl('/api/agent-config')).then(normalizeAgentConfig),
  modelChangeLog: () => fetchJ<ChangeLogEntry[]>(buildApiUrl('/api/model-change-log')).then(normalizeChangeLog).catch(() => []),
  officialsStats: () => fetchJ<OfficialsData>(buildApiUrl('/api/officials-stats')).then(normalizeOfficialsData),
  morningBrief: () => fetchJ<MorningBrief>(buildApiUrl('/api/morning-brief')).then(normalizeMorningBrief),
  morningConfig: () => fetchJ<SubConfig>(buildApiUrl('/api/morning-config')).then(normalizeSubConfig),
  agentsStatus: () => fetchJ<AgentsStatusData>(buildApiUrl('/api/agents-status')).then(normalizeAgentsStatusData),

  // 任务实时动态
  taskActivity: (id: string) =>
    fetchJ<TaskActivityData>(buildApiUrl(`/api/task-activity/${encodeURIComponent(id)}`)).then(normalizeTaskActivityData),
  schedulerState: (id: string) =>
    fetchJ<SchedulerStateData>(buildApiUrl(`/api/scheduler-state/${encodeURIComponent(id)}`)).then(normalizeSchedulerStateData),

  // 技能内容
  skillContent: (agentId: string, skillName: string) =>
    fetchJ<SkillContentResult>(
      buildApiUrl(`/api/skill-content/${encodeURIComponent(agentId)}/${encodeURIComponent(skillName)}`)
    ).then(normalizeSkillContentResult),

  // 操作类
  setModel: (agentId: string, model: string) =>
    postJ<ActionResult>(buildApiUrl('/api/set-model'), { agentId, model }),
  setDispatchChannel: (channel: string) =>
    postJ<ActionResult>(buildApiUrl('/api/set-dispatch-channel'), { channel }),
  agentWake: (agentId: string) =>
    postJ<ActionResult>(buildApiUrl('/api/agent-wake'), withActor({ agentId })),
  taskAction: (taskId: string, action: string, reason: string) =>
    postJ<ActionResult>(buildApiUrl('/api/task-action'), withActor({ taskId, action, reason })),
  reviewAction: (taskId: string, action: string, comment: string) =>
    postJ<ActionResult>(buildApiUrl('/api/review-action'), withActor({ taskId, action, comment })),
  advanceState: (taskId: string, comment: string) =>
    postJ<ActionResult>(buildApiUrl('/api/advance-state'), withActor({ taskId, comment })),
  archiveTask: (taskId: string, archived: boolean) =>
    postJ<ActionResult>(buildApiUrl('/api/archive-task'), withActor({ taskId, archived })),
  archiveAllDone: () =>
    postJ<ActionResult & { count?: number }>(buildApiUrl('/api/archive-task'), withActor({ archiveAllDone: true })),
  schedulerScan: (thresholdSec = 180) =>
    postJ<ActionResult & { count?: number; actions?: ScanAction[]; checkedAt?: string }>(
      buildApiUrl('/api/scheduler-scan'),
      withActor({ thresholdSec }, 'sili', 'scheduler')
    ),
  schedulerRetry: (taskId: string, reason: string) =>
    postJ<ActionResult>(buildApiUrl('/api/scheduler-retry'), withActor({ taskId, reason }, 'sili', 'scheduler')),
  schedulerEscalate: (taskId: string, reason: string) =>
    postJ<ActionResult>(buildApiUrl('/api/scheduler-escalate'), withActor({ taskId, reason }, 'sili', 'scheduler')),
  schedulerRollback: (taskId: string, reason: string) =>
    postJ<ActionResult>(buildApiUrl('/api/scheduler-rollback'), withActor({ taskId, reason }, 'sili', 'scheduler')),
  refreshMorning: () =>
    postJ<ActionResult>(buildApiUrl('/api/morning-brief/refresh'), {}),
  saveMorningConfig: (config: SubConfig) =>
    postJ<ActionResult>(buildApiUrl('/api/morning-config'), config),
  addSkill: (agentId: string, skillName: string, description: string, trigger: string) =>
    postJ<ActionResult>(buildApiUrl('/api/add-skill'), { agentId, skillName, description, trigger }),

  // 远程 Skills 管理
  addRemoteSkill: (agentId: string, skillName: string, sourceUrl: string, description?: string) =>
    postJ<ActionResult & { skillName?: string; agentId?: string; source?: string; localPath?: string; size?: number; addedAt?: string }>(
      buildApiUrl('/api/add-remote-skill'), { agentId, skillName, sourceUrl, description: description || '' }
    ),
  remoteSkillsList: () =>
    fetchJ<RemoteSkillsListResult>(buildApiUrl('/api/remote-skills-list')).then(normalizeRemoteSkillsList),
  updateRemoteSkill: (agentId: string, skillName: string) =>
    postJ<ActionResult>(buildApiUrl('/api/update-remote-skill'), { agentId, skillName }),
  removeRemoteSkill: (agentId: string, skillName: string) =>
    postJ<ActionResult>(buildApiUrl('/api/remove-remote-skill'), { agentId, skillName }),

  createTask: (data: CreateTaskPayload) =>
    postJ<ActionResult & { taskId?: string }>(buildApiUrl('/api/create-task'), withActor(data)),

  // ── 朝堂议政 ──
  courtDiscussStart: (topic: string, officials: string[], taskId?: string) =>
    postJ<CourtDiscussSessionData>(buildApiUrl('/api/court-discuss/start'), { topic, officials, taskId }),
  courtDiscussList: () =>
    fetchJ<{ ok: boolean; sessions: CourtDiscussSessionSummary[] }>(buildApiUrl('/api/court-discuss/list')),
  courtDiscussOfficials: () =>
    fetchJ<CourtDiscussOfficialsResult>(buildApiUrl('/api/court-discuss/officials')),
  courtDiscussSession: (sessionId: string) =>
    fetchJ<CourtDiscussSessionData>(buildApiUrl(`/api/court-discuss/session/${encodeURIComponent(sessionId)}`)),
  courtDiscussAdvance: (sessionId: string, userMessage?: string, decree?: string) =>
    postJ<CourtDiscussResult>(buildApiUrl('/api/court-discuss/advance'), { sessionId, userMessage, decree }),
  courtDiscussConclude: (sessionId: string) =>
    postJ<ActionResult & { summary?: string }>(buildApiUrl('/api/court-discuss/conclude'), { sessionId }),
  courtDiscussDestroy: (sessionId: string) =>
    postJ<ActionResult>(buildApiUrl('/api/court-discuss/destroy'), { sessionId }),
  courtDiscussFate: () =>
    fetchJ<{ ok: boolean; event: string }>(buildApiUrl('/api/court-discuss/fate')),
};

// ── Types ──

export interface ActionResult {
  ok: boolean;
  message?: string;
  error?: string;
}

export interface FlowEntry {
  at: string;
  from: string;
  to: string;
  remark: string;
}

export interface ProgressEntry {
  at: string;
  agent: string;
  agentLabel?: string;
  text: string;
  todos?: TodoItem[];
  state?: string;
  org?: string;
  tokens?: number;
  cost?: number;
  elapsed?: number;
}

export interface TodoItem {
  id: string | number;
  title: string;
  status: 'not-started' | 'in-progress' | 'completed';
  detail?: string;
}

export interface Heartbeat {
  status: 'active' | 'warn' | 'stalled' | 'unknown' | 'idle';
  label: string;
}

export interface Task {
  id: string;
  title: string;
  state: string;
  org: string;
  now: string;
  eta: string;
  block: string;
  ac: string;
  output: string;
  heartbeat: Heartbeat;
  flow_log: FlowEntry[];
  progress_log?: ProgressEntry[];
  todos: TodoItem[];
  review_round: number;
  archived: boolean;
  archivedAt?: string;
  createdAt?: string;
  updatedAt?: string;
  lane?: 'standard' | 'fast';
  targetDept?: string;
  templateId?: string;
  templateParams?: Record<string, unknown>;
  _scheduler?: Record<string, unknown>;
  sourceMeta?: Record<string, unknown>;
  activity?: ActivityEntry[];
  _prev_state?: string;
}

export interface SyncStatus {
  ok: boolean;
  [key: string]: unknown;
}

export interface LiveStatus {
  tasks: Task[];
  syncStatus: SyncStatus;
}

export interface AgentInfo {
  id: string;
  label: string;
  emoji: string;
  role: string;
  model: string;
  skills: SkillInfo[];
}

export interface SkillInfo {
  name: string;
  description: string;
  path: string;
}

export interface KnownModel {
  id: string;
  label: string;
  provider: string;
}

export interface AgentConfig {
  agents: AgentInfo[];
  knownModels?: KnownModel[];
  dispatchChannel?: string;
}

export interface ChangeLogEntry {
  at: string;
  agentId: string;
  oldModel: string;
  newModel: string;
  rolledBack?: boolean;
}

export interface OfficialInfo {
  id: string;
  label: string;
  emoji: string;
  role: string;
  rank: string;
  model: string;
  model_short: string;
  tokens_in: number;
  tokens_out: number;
  cache_read: number;
  cache_write: number;
  cost_cny: number;
  cost_usd: number;
  sessions: number;
  messages: number;
  tasks_done: number;
  tasks_active: number;
  flow_participations: number;
  merit_score: number;
  merit_rank: number;
  last_active: string;
  heartbeat: Heartbeat;
  participated_edicts: { id: string; title: string; state: string }[];
}

export interface OfficialsData {
  officials: OfficialInfo[];
  totals: { tasks_done: number; cost_cny: number };
  top_official: string;
}

export interface AgentStatusInfo {
  id: string;
  label: string;
  emoji: string;
  role: string;
  status: 'running' | 'idle' | 'offline' | 'unconfigured';
  statusLabel: string;
  lastActive?: string;
}

export interface GatewayStatus {
  alive: boolean;
  probe: boolean;
  status: string;
}

export interface AgentsStatusData {
  ok: boolean;
  gateway: GatewayStatus;
  agents: AgentStatusInfo[];
  checkedAt: string;
}

export interface MorningNewsItem {
  title: string;
  summary?: string;
  desc?: string;
  link: string;
  source: string;
  image?: string;
  pub_date?: string;
}

export interface MorningBrief {
  date?: string;
  generated_at?: string;
  categories: Record<string, MorningNewsItem[]>;
}

export interface SubCategoryConfig {
  name: string;
  enabled: boolean;
}

export interface CustomFeed {
  name: string;
  url: string;
  category: string;
}

export interface SubConfig {
  categories: SubCategoryConfig[];
  keywords: string[];
  custom_feeds: CustomFeed[];
  feishu_webhook: string;
}

export interface ActivityEntry {
  kind: string;
  at?: number | string;
  text?: string;
  thinking?: string;
  agent?: string;
  from?: string;
  to?: string;
  remark?: string;
  tools?: { name: string; input_preview?: string }[];
  tool?: string;
  output?: string;
  exitCode?: number | null;
  items?: TodoItem[];
  diff?: {
    changed?: { id: string; from: string; to: string }[];
    added?: { id: string; title: string }[];
    removed?: { id: string; title: string }[];
  };
}

export interface PhaseDuration {
  phase: string;
  durationSec: number;
  durationText: string;
  ongoing?: boolean;
}

export interface TodosSummary {
  total: number;
  completed: number;
  inProgress: number;
  notStarted: number;
  percent: number;
}

export interface ResourceSummary {
  totalTokens?: number;
  totalCost?: number;
  totalElapsedSec?: number;
}

export interface TaskActivityData {
  ok: boolean;
  message?: string;
  error?: string;
  activity?: ActivityEntry[];
  relatedAgents?: string[];
  agentLabel?: string;
  lastActive?: string;
  phaseDurations?: PhaseDuration[];
  totalDuration?: string;
  todosSummary?: TodosSummary;
  resourceSummary?: ResourceSummary;
}

export interface SchedulerInfo {
  retryCount?: number;
  escalationLevel?: number;
  lastDispatchStatus?: string;
  stallThresholdSec?: number;
  enabled?: boolean;
  lastProgressAt?: string;
  lastDispatchAt?: string;
  lastDispatchAgent?: string;
  autoRollback?: boolean;
}

export interface SchedulerStateData {
  ok: boolean;
  error?: string;
  scheduler?: SchedulerInfo;
  stalledSec?: number;
}

export interface SkillContentResult {
  ok: boolean;
  name?: string;
  agent?: string;
  content?: string;
  path?: string;
  error?: string;
}

export interface ScanAction {
  taskId: string;
  action: string;
  to?: string;
  toState?: string;
  stalledSec?: number;
}

export interface CreateTaskPayload {
  title: string;
  org: string;
  lane?: 'standard' | 'fast';
  targetDept?: string;
  priority?: string;
  templateId?: string;
  params?: Record<string, string>;
}

export interface RemoteSkillItem {
  skillName: string;
  agentId: string;
  sourceUrl: string;
  description: string;
  localPath: string;
  addedAt: string;
  lastUpdated: string;
  status: 'valid' | 'not-found' | string;
}

export interface RemoteSkillsListResult {
  ok: boolean;
  remoteSkills?: RemoteSkillItem[];
  count?: number;
  listedAt?: string;
  error?: string;
}

// ── 朝堂议政 ──

export interface CourtDiscussMessage {
  type: string;
  content: string;
  official_id?: string;
  official_name?: string;
  emotion?: string;
  action?: string;
  timestamp?: number;
}

export interface CourtDiscussOfficial {
  id: string;
  name: string;
  emoji: string;
  role: string;
  personality: string;
  speaking_style: string;
}

export interface CourtDiscussSessionData {
  ok: boolean;
  session_id?: string;
  topic?: string;
  task_id?: string;
  officials?: CourtDiscussOfficial[];
  messages?: CourtDiscussMessage[];
  round?: number;
  phase?: string;
  summary?: string;
  created_at?: number;
  updated_at?: number;
  concluded_at?: number;
  error?: string;
}

export interface CourtDiscussSessionSummary {
  session_id: string;
  topic: string;
  task_id?: string;
  round: number;
  phase: string;
  official_count: number;
  message_count: number;
  summary?: string;
  created_at?: number;
  updated_at?: number;
}

export interface CourtDiscussOfficialsResult {
  ok: boolean;
  officials: Record<string, CourtDiscussOfficial>;
}

export interface CourtDiscussResult {
  ok: boolean;
  session_id?: string;
  topic?: string;
  round?: number;
  new_messages?: Array<{
    official_id: string;
    name: string;
    content: string;
    emotion?: string;
    action?: string;
  }>;
  scene_note?: string;
  total_messages?: number;
  error?: string;
}
