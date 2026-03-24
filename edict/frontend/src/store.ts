/**
 * Zustand Store — 三省六部看板状态管理
 * WebSocket 实时推送 + 轮询兜底
 */

import { create } from 'zustand';
import {
  api,
  buildWsUrl,
  type Task,
  type LiveStatus,
  type AgentConfig,
  type OfficialsData,
  type AgentsStatusData,
  type MorningBrief,
  type SubConfig,
  type ChangeLogEntry,
} from './api';
import {
  DEPTS as GENERATED_DEPTS,
  DEPT_COLOR as GENERATED_DEPT_COLOR,
  PIPE as GENERATED_PIPE,
  PIPE_STATE_IDX as GENERATED_PIPE_STATE_IDX,
  STATE_LABEL as GENERATED_STATE_LABEL,
} from './generated/institutionSchema';

type PipeDefinition = { key: string; dept: string; icon: string; action: string };
type DeptDefinition = { id: string; label: string; emoji: string; role: string; rank: string };

export const PIPE: ReadonlyArray<PipeDefinition> = GENERATED_PIPE;
export const PIPE_STATE_IDX: Record<string, number> = { ...GENERATED_PIPE_STATE_IDX };
export const DEPT_COLOR: Record<string, string> = { ...GENERATED_DEPT_COLOR };
export const STATE_LABEL: Record<string, string> = { ...GENERATED_STATE_LABEL };
export const DEPTS: ReadonlyArray<DeptDefinition> = GENERATED_DEPTS;

export function deptColor(d: string): string {
  return DEPT_COLOR[d] || '#6a9eff';
}

export function stateLabel(t: Task): string {
  const r = t.review_round || 0;
  if (t.state === 'Menxia' && r > 1) return `门下审议（第${r}轮）`;
  if (t.state === 'Zhongshu' && r > 0) return `中书修订（第${r}轮）`;
  return STATE_LABEL[t.state] || t.state;
}

export function isEdict(t: Task): boolean {
  return /^JJC-/i.test(t.id || '');
}

export function isSession(t: Task): boolean {
  return /^(OC-|MC-)/i.test(t.id || '');
}

export function isArchived(t: Task): boolean {
  return t.archived || ['Done', 'Cancelled'].includes(t.state);
}

export type PipeStatus = { key: string; dept: string; icon: string; action: string; status: 'done' | 'active' | 'pending' };

export function getPipeStatus(t: Task): PipeStatus[] {
  const stateIdx = PIPE_STATE_IDX[t.state] ?? 4;
  return PIPE.map((stage, i) => ({
    ...stage,
    status: (i < stateIdx ? 'done' : i === stateIdx ? 'active' : 'pending') as 'done' | 'active' | 'pending',
  }));
}

// ── Tabs ──

export type TabKey =
  | 'edicts' | 'monitor' | 'officials' | 'models'
  | 'skills' | 'sessions' | 'memorials' | 'templates' | 'morning' | 'court';

export const TAB_DEFS: { key: TabKey; label: string; icon: string }[] = [
  { key: 'edicts',    label: '旨意看板', icon: '📜' },
  { key: 'court',     label: '朝堂议政', icon: '🏛️' },
  { key: 'monitor',   label: '省部调度', icon: '🔌' },
  { key: 'officials', label: '官员总览', icon: '👔' },
  { key: 'models',    label: '模型配置', icon: '🤖' },
  { key: 'skills',    label: '技能配置', icon: '🎯' },
  { key: 'sessions',  label: '小任务',   icon: '💬' },
  { key: 'memorials', label: '奏折阁',   icon: '📜' },
  { key: 'templates', label: '旨库',     icon: '📋' },
  { key: 'morning',   label: '天下要闻', icon: '🌅' },
];

// ── Templates ──

export interface TemplateParam {
  key: string;
  label: string;
  type: 'text' | 'textarea' | 'select';
  default?: string;
  required?: boolean;
  options?: string[];
}

export interface Template {
  id: string;
  cat: string;
  icon: string;
  name: string;
  desc: string;
  depts: string[];
  est: string;
  cost: string;
  params: TemplateParam[];
  command: string;
}

export const TEMPLATES: Template[] = [
  {
    id: 'tpl-weekly-report', cat: '日常办公', icon: '📝', name: '周报生成',
    desc: '基于本周看板数据和各部产出，自动生成结构化周报',
    depts: ['户部', '礼部'], est: '~10分钟', cost: '¥0.5',
    params: [
      { key: 'date_range', label: '报告周期', type: 'text', default: '本周', required: true },
      { key: 'focus', label: '重点关注（逗号分隔）', type: 'text', default: '项目进展,下周计划' },
      { key: 'format', label: '输出格式', type: 'select', options: ['Markdown', '飞书文档'], default: 'Markdown' },
    ],
    command: '生成{date_range}的周报，重点覆盖{focus}，输出为{format}格式',
  },
  {
    id: 'tpl-code-review', cat: '工程开发', icon: '🔍', name: '代码审查',
    desc: '对指定代码仓库/文件进行质量审查，输出问题清单和改进建议',
    depts: ['兵部', '刑部'], est: '~20分钟', cost: '¥2',
    params: [
      { key: 'repo', label: '仓库/文件路径', type: 'text', required: true },
      { key: 'scope', label: '审查范围', type: 'select', options: ['全量', '增量(最近commit)', '指定文件'], default: '增量(最近commit)' },
      { key: 'focus', label: '重点关注（可选）', type: 'text', default: '安全漏洞,错误处理,性能' },
    ],
    command: '对 {repo} 进行代码审查，范围：{scope}，重点关注：{focus}',
  },
  {
    id: 'tpl-api-design', cat: '工程开发', icon: '⚡', name: 'API 设计与实现',
    desc: '从需求描述到 RESTful API 设计、实现、测试一条龙',
    depts: ['中书省', '兵部'], est: '~45分钟', cost: '¥3',
    params: [
      { key: 'requirement', label: '需求描述', type: 'textarea', required: true },
      { key: 'tech', label: '技术栈', type: 'select', options: ['Python/FastAPI', 'Node/Express', 'Go/Gin'], default: 'Python/FastAPI' },
      { key: 'auth', label: '鉴权方式', type: 'select', options: ['JWT', 'API Key', '无'], default: 'JWT' },
    ],
    command: '设计并实现一个 {tech} 的 RESTful API：{requirement}。鉴权方式：{auth}',
  },
  {
    id: 'tpl-competitor', cat: '数据分析', icon: '📊', name: '竞品分析',
    desc: '爬取竞品网站数据，分析对比，生成结构化报告',
    depts: ['兵部', '户部', '礼部'], est: '~60分钟', cost: '¥5',
    params: [
      { key: 'targets', label: '竞品名称/URL（每行一个）', type: 'textarea', required: true },
      { key: 'dimensions', label: '分析维度', type: 'text', default: '产品功能,定价策略,用户评价' },
      { key: 'format', label: '输出格式', type: 'select', options: ['Markdown报告', '表格对比'], default: 'Markdown报告' },
    ],
    command: '对以下竞品进行分析：\n{targets}\n\n分析维度：{dimensions}，输出格式：{format}',
  },
  {
    id: 'tpl-data-report', cat: '数据分析', icon: '📈', name: '数据报告',
    desc: '对给定数据集进行清洗、分析、可视化，输出分析报告',
    depts: ['户部', '礼部'], est: '~30分钟', cost: '¥2',
    params: [
      { key: 'data_source', label: '数据源描述/路径', type: 'text', required: true },
      { key: 'questions', label: '分析问题（每行一个）', type: 'textarea' },
      { key: 'viz', label: '是否需要可视化图表', type: 'select', options: ['是', '否'], default: '是' },
    ],
    command: '对数据 {data_source} 进行分析。{questions}\n需要可视化：{viz}',
  },
  {
    id: 'tpl-blog', cat: '内容创作', icon: '✍️', name: '博客文章',
    desc: '给定主题和要求，生成高质量博客文章',
    depts: ['礼部'], est: '~15分钟', cost: '¥1',
    params: [
      { key: 'topic', label: '文章主题', type: 'text', required: true },
      { key: 'audience', label: '目标读者', type: 'text', default: '技术人员' },
      { key: 'length', label: '期望字数', type: 'select', options: ['~1000字', '~2000字', '~3000字'], default: '~2000字' },
      { key: 'style', label: '风格', type: 'select', options: ['技术教程', '观点评论', '案例分析'], default: '技术教程' },
    ],
    command: '写一篇关于「{topic}」的博客文章，面向{audience}，{length}，风格：{style}',
  },
  {
    id: 'tpl-deploy', cat: '工程开发', icon: '🚀', name: '部署方案',
    desc: '生成完整的部署检查单、Docker配置、CI/CD流程',
    depts: ['兵部', '工部'], est: '~25分钟', cost: '¥2',
    params: [
      { key: 'project', label: '项目名称/描述', type: 'text', required: true },
      { key: 'env', label: '部署环境', type: 'select', options: ['Docker', 'K8s', 'VPS', 'Serverless'], default: 'Docker' },
      { key: 'ci', label: 'CI/CD 工具', type: 'select', options: ['GitHub Actions', 'GitLab CI', '无'], default: 'GitHub Actions' },
    ],
    command: '为项目「{project}」生成{env}部署方案，CI/CD使用{ci}',
  },
  {
    id: 'tpl-email', cat: '内容创作', icon: '📧', name: '邮件/通知文案',
    desc: '根据场景和目的，生成专业邮件或通知文案',
    depts: ['礼部'], est: '~5分钟', cost: '¥0.3',
    params: [
      { key: 'scenario', label: '使用场景', type: 'select', options: ['商务邮件', '产品发布', '客户通知', '内部公告'], default: '商务邮件' },
      { key: 'purpose', label: '目的/内容', type: 'textarea', required: true },
      { key: 'tone', label: '语调', type: 'select', options: ['正式', '友好', '简洁'], default: '正式' },
    ],
    command: '撰写一封{scenario}，{tone}语调。内容：{purpose}',
  },
  {
    id: 'tpl-standup', cat: '日常办公', icon: '🗓️', name: '每日站会摘要',
    desc: '汇总各部今日进展和明日计划，生成站会摘要',
    depts: ['尚书省'], est: '~5分钟', cost: '¥0.3',
    params: [
      { key: 'range', label: '汇总范围', type: 'select', options: ['今天', '最近24小时', '昨天+今天'], default: '今天' },
    ],
    command: '汇总{range}各部工作进展和待办，生成站会摘要',
  },
];

export const TPL_CATS = [
  { name: '全部', icon: '📋' },
  { name: '日常办公', icon: '💼' },
  { name: '数据分析', icon: '📊' },
  { name: '工程开发', icon: '⚙️' },
  { name: '内容创作', icon: '✍️' },
];

// ── Main Store ──

interface AppStore {
  // Data
  liveStatus: LiveStatus | null;
  agentConfig: AgentConfig | null;
  changeLog: ChangeLogEntry[];
  officialsData: OfficialsData | null;
  agentsStatusData: AgentsStatusData | null;
  morningBrief: MorningBrief | null;
  subConfig: SubConfig | null;

  // UI State
  activeTab: TabKey;
  edictFilter: 'active' | 'archived' | 'all';
  sessFilter: string;
  tplCatFilter: string;
  selectedOfficial: string | null;
  modalTaskId: string | null;
  countdown: number;
  realtimeConnected: boolean;
  realtimeSeq: number;

  // Toast
  toasts: { id: number; msg: string; type: 'ok' | 'err' }[];

  // Actions
  setActiveTab: (tab: TabKey) => void;
  setEdictFilter: (f: 'active' | 'archived' | 'all') => void;
  setSessFilter: (f: string) => void;
  setTplCatFilter: (f: string) => void;
  setSelectedOfficial: (id: string | null) => void;
  setModalTaskId: (id: string | null) => void;
  setCountdown: (n: number) => void;
  toast: (msg: string, type?: 'ok' | 'err') => void;

  // Data fetching
  loadLive: () => Promise<void>;
  loadAgentConfig: () => Promise<void>;
  loadOfficials: () => Promise<void>;
  loadAgentsStatus: () => Promise<void>;
  loadMorning: () => Promise<void>;
  loadSubConfig: () => Promise<void>;
  loadAll: () => Promise<void>;
}

let _toastId = 0;

export const useStore = create<AppStore>((set, get) => ({
  liveStatus: null,
  agentConfig: null,
  changeLog: [],
  officialsData: null,
  agentsStatusData: null,
  morningBrief: null,
  subConfig: null,

  activeTab: 'edicts',
  edictFilter: 'active',
  sessFilter: 'all',
  tplCatFilter: '全部',
  selectedOfficial: null,
  modalTaskId: null,
  countdown: 5,
  realtimeConnected: false,
  realtimeSeq: 0,

  toasts: [],

  setActiveTab: (tab) => {
    set({ activeTab: tab });
    const s = get();
    if (['models', 'skills', 'sessions'].includes(tab) && !s.agentConfig) s.loadAgentConfig();
    if (tab === 'officials' && !s.officialsData) s.loadOfficials();
    if (tab === 'monitor') s.loadAgentsStatus();
    if (tab === 'morning' && !s.morningBrief) s.loadMorning();
  },
  setEdictFilter: (f) => set({ edictFilter: f }),
  setSessFilter: (f) => set({ sessFilter: f }),
  setTplCatFilter: (f) => set({ tplCatFilter: f }),
  setSelectedOfficial: (id) => set({ selectedOfficial: id }),
  setModalTaskId: (id) => set({ modalTaskId: id }),
  setCountdown: (n) => set({ countdown: n }),

  toast: (msg, type = 'ok') => {
    const id = ++_toastId;
    set((s) => ({ toasts: [...s.toasts, { id, msg, type }] }));
    setTimeout(() => {
      set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }));
    }, 3000);
  },

  loadLive: async () => {
    try {
      const data = await api.liveStatus();
      set({ liveStatus: data });
      // Also preload officials for monitor tab
      const s = get();
      if (!s.officialsData) {
        api.officialsStats().then((d) => set({ officialsData: d })).catch(() => {});
      }
    } catch {
      // silently fail
    }
  },

  loadAgentConfig: async () => {
    try {
      const cfg = await api.agentConfig();
      const log = await api.modelChangeLog();
      set({ agentConfig: cfg, changeLog: log });
    } catch {
      // silently fail
    }
  },

  loadOfficials: async () => {
    try {
      const data = await api.officialsStats();
      set({ officialsData: data });
    } catch {
      // silently fail
    }
  },

  loadAgentsStatus: async () => {
    try {
      const data = await api.agentsStatus();
      set({ agentsStatusData: data });
    } catch {
      set({ agentsStatusData: null });
    }
  },

  loadMorning: async () => {
    try {
      const [brief, config] = await Promise.all([api.morningBrief(), api.morningConfig()]);
      set({ morningBrief: brief, subConfig: config });
    } catch {
      // silently fail
    }
  },

  loadSubConfig: async () => {
    try {
      const config = await api.morningConfig();
      set({ subConfig: config });
    } catch {
      // silently fail
    }
  },

  loadAll: async () => {
    const s = get();
    await s.loadLive();
    const tab = s.activeTab;
    if (['models', 'skills'].includes(tab)) await s.loadAgentConfig();
    if (tab === 'monitor') await s.loadAgentsStatus();
  },
}));

// ── Realtime & Fallback Polling ──

let _cdTimer: ReturnType<typeof setInterval> | null = null;
let _ws: WebSocket | null = null;
let _wsReconnectTimer: ReturnType<typeof setTimeout> | null = null;
let _wsBackoffMs = 1000;
let _realtimeSeq = 0;
const _recentEventIds: string[] = [];
const _recentEventSet = new Set<string>();
let _liveRefreshTimer: ReturnType<typeof setTimeout> | null = null;
let _agentsRefreshTimer: ReturnType<typeof setTimeout> | null = null;

const POLL_INTERVAL_SEC = 5;
const REALTIME_FALLBACK_SEC = 30;

function _rememberEventId(eventId: string): boolean {
  if (!eventId) return true;
  if (_recentEventSet.has(eventId)) return false;
  _recentEventSet.add(eventId);
  _recentEventIds.push(eventId);
  if (_recentEventIds.length > 200) {
    const expired = _recentEventIds.shift();
    if (expired) _recentEventSet.delete(expired);
  }
  return true;
}

function _scheduleLiveRefresh(delayMs = 120) {
  if (_liveRefreshTimer) return;
  _liveRefreshTimer = setTimeout(() => {
    _liveRefreshTimer = null;
    void useStore.getState().loadLive();
  }, delayMs);
}

function _scheduleAgentsRefresh(delayMs = 120) {
  if (_agentsRefreshTimer) return;
  _agentsRefreshTimer = setTimeout(() => {
    _agentsRefreshTimer = null;
    const state = useStore.getState();
    if (state.activeTab === 'monitor') {
      void state.loadAgentsStatus();
    }
  }, delayMs);
}

function _setRealtimeConnected(connected: boolean) {
  useStore.setState({
    realtimeConnected: connected,
    countdown: connected ? REALTIME_FALLBACK_SEC : POLL_INTERVAL_SEC,
  });
}

function _handleRealtimeMessage(raw: string) {
  let message: unknown;
  try {
    message = JSON.parse(raw);
  } catch {
    return;
  }
  if (!message || typeof message !== 'object') return;
  const packet = message as { type?: string; topic?: string; data?: { event_id?: string } };
  if (packet.type !== 'event') return;
  const eventId = packet.data?.event_id || `${packet.topic || 'unknown'}:${_realtimeSeq + 1}`;
  if (!_rememberEventId(eventId)) return;
  _realtimeSeq += 1;
  useStore.setState({ realtimeSeq: _realtimeSeq, realtimeConnected: true });

  const topic = packet.topic || '';
  if (topic.startsWith('task.')) {
    _scheduleLiveRefresh();
  }
  if (topic.startsWith('agent.') || topic === 'task.dispatch' || topic === 'task.escalated') {
    _scheduleAgentsRefresh();
  }
}

function _scheduleRealtimeReconnect() {
  if (_wsReconnectTimer || typeof window === 'undefined') return;
  const delay = _wsBackoffMs;
  _wsReconnectTimer = setTimeout(() => {
    _wsReconnectTimer = null;
    _connectRealtime();
  }, delay);
  _wsBackoffMs = Math.min(_wsBackoffMs * 2, 15000);
}

function _connectRealtime() {
  if (_ws || typeof window === 'undefined') return;
  const ws = new WebSocket(buildWsUrl('/ws'));
  _ws = ws;
  ws.onopen = () => {
    _wsBackoffMs = 1000;
    _setRealtimeConnected(true);
    ws.send(JSON.stringify({ type: 'subscribe', topics: ['task.*', 'agent.heartbeat'] }));
  };
  ws.onmessage = (event) => {
    if (typeof event.data === 'string') {
      _handleRealtimeMessage(event.data);
    }
  };
  ws.onerror = () => {
    ws.close();
  };
  ws.onclose = () => {
    if (_ws === ws) _ws = null;
    _setRealtimeConnected(false);
    _scheduleRealtimeReconnect();
  };
}

export function startPolling() {
  if (_cdTimer) return;
  useStore.getState().loadAll();
  _connectRealtime();
  _cdTimer = setInterval(() => {
    const s = useStore.getState();
    const interval = s.realtimeConnected ? REALTIME_FALLBACK_SEC : POLL_INTERVAL_SEC;
    const cd = s.countdown - 1;
    if (cd <= 0) {
      s.setCountdown(interval);
      s.loadAll();
    } else {
      s.setCountdown(cd);
    }
  }, 1000);
}

export function stopPolling() {
  if (_cdTimer) {
    clearInterval(_cdTimer);
    _cdTimer = null;
  }
  if (_wsReconnectTimer) {
    clearTimeout(_wsReconnectTimer);
    _wsReconnectTimer = null;
  }
  if (_liveRefreshTimer) {
    clearTimeout(_liveRefreshTimer);
    _liveRefreshTimer = null;
  }
  if (_agentsRefreshTimer) {
    clearTimeout(_agentsRefreshTimer);
    _agentsRefreshTimer = null;
  }
  if (_ws) {
    _ws.close();
    _ws = null;
  }
  _setRealtimeConnected(false);
}

// ── Utility ──

export function esc(s: string | undefined | null): string {
  if (!s) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

export function timeAgo(iso: string | undefined): string {
  if (!iso) return '';
  try {
    const d = new Date(iso.includes('T') ? iso : iso.replace(' ', 'T') + 'Z');
    if (isNaN(d.getTime())) return '';
    const diff = Date.now() - d.getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return '刚刚';
    if (mins < 60) return mins + '分钟前';
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return hrs + '小时前';
    return Math.floor(hrs / 24) + '天前';
  } catch {
    return '';
  }
}
