import { useEffect, useState, useRef, useCallback } from 'react';
import { useStore, getPipeStatus, deptColor, stateLabel, STATE_LABEL } from '../store';
import { api, buildWsUrl } from '../api';
import ConfirmDialog from './ConfirmDialog';
import type {
  Task,
  TaskActivityData,
  SchedulerStateData,
  ActivityEntry,
  TodoItem,
  PhaseDuration,
} from '../api';

const AGENT_LABELS: Record<string, string> = {
  main: '司礼监',
  zhongshu: '中书省',
  menxia: '门下省',
  shangshu: '尚书省',
  libu: '礼部',
  hubu: '户部',
  bingbu: '兵部',
  xingbu: '刑部',
  gongbu: '工部',
  libu_hr: '吏部',
  zaochao: '钦天监',
};

const NEXT_LABELS: Record<string, string> = {
  Sili: '中书省起草',
  Zhongshu: '门下省审议',
  Menxia: '尚书省派发',
  Assigned: '开始执行',
  Doing: '进入审查',
  Review: '完成',
};

type ConfirmState = {
  key: string;
  title: string;
  message: string;
  okLabel: string;
  okClass?: string;
  note?: string;
  riskHint?: string;
  permissionHint?: string;
  reasonLabel?: string;
  reasonPlaceholder?: string;
  showReason?: boolean;
  defaultReason?: string;
  onConfirm: (reason: string) => Promise<void> | void;
};

function fmtStalled(sec: number): string {
  const v = Math.max(0, sec);
  if (v < 60) return `${v}秒`;
  if (v < 3600) return `${Math.floor(v / 60)}分${v % 60}秒`;
  const h = Math.floor(v / 3600);
  const m = Math.floor((v % 3600) / 60);
  return `${h}小时${m}分`;
}

function fmtActivityTime(ts: number | string | undefined): string {
  if (!ts) return '';
  if (typeof ts === 'number') {
    const d = new Date(ts);
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
  }
  if (typeof ts === 'string' && ts.length >= 19) return ts.substring(11, 19);
  return String(ts).substring(0, 8);
}

export default function TaskModal() {
  const modalTaskId = useStore((s) => s.modalTaskId);
  const setModalTaskId = useStore((s) => s.setModalTaskId);
  const liveStatus = useStore((s) => s.liveStatus);
  const loadAll = useStore((s) => s.loadAll);
  const toast = useStore((s) => s.toast);

  const [activityData, setActivityData] = useState<TaskActivityData | null>(null);
  const [schedData, setSchedData] = useState<SchedulerStateData | null>(null);
  const [dialog, setDialog] = useState<ConfirmState | null>(null);
  const fallbackTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const wsReconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wsBackoffRef = useRef(1000);
  const wsSeqRef = useRef(0);
  const logRef = useRef<HTMLDivElement>(null);

  const task = liveStatus?.tasks?.find((t) => t.id === modalTaskId) || null;

  const fetchActivity = useCallback(async () => {
    if (!modalTaskId) return;
    try {
      const d = await api.taskActivity(modalTaskId);
      setActivityData(d);
    } catch {
      setActivityData(null);
    }
  }, [modalTaskId]);

  const fetchSched = useCallback(async () => {
    if (!modalTaskId) return;
    try {
      const d = await api.schedulerState(modalTaskId);
      setSchedData(d);
    } catch {
      setSchedData(null);
    }
  }, [modalTaskId]);

  useEffect(() => {
    if (!modalTaskId || !task) return;
    fetchActivity();
    fetchSched();

    const isDone = ['Done', 'Cancelled'].includes(task.state);
    if (!isDone) {
      fallbackTimerRef.current = setInterval(() => {
        fetchActivity();
        fetchSched();
      }, 15000);

      const connect = () => {
        if (wsRef.current || !modalTaskId) return;
        const ws = new WebSocket(buildWsUrl(`/ws/task/${encodeURIComponent(modalTaskId)}`));
        wsRef.current = ws;

        ws.onopen = () => {
          wsBackoffRef.current = 1000;
        };

        ws.onmessage = () => {
          wsSeqRef.current += 1;
          fetchActivity();
          fetchSched();
        };

        ws.onerror = () => {
          ws.close();
        };

        ws.onclose = () => {
          if (wsRef.current === ws) wsRef.current = null;
          if (wsReconnectRef.current || ['Done', 'Cancelled'].includes(task.state)) return;
          wsReconnectRef.current = setTimeout(() => {
            wsReconnectRef.current = null;
            connect();
          }, wsBackoffRef.current);
          wsBackoffRef.current = Math.min(wsBackoffRef.current * 2, 15000);
        };
      };

      connect();
    }

    return () => {
      if (fallbackTimerRef.current) {
        clearInterval(fallbackTimerRef.current);
        fallbackTimerRef.current = null;
      }
      if (wsReconnectRef.current) {
        clearTimeout(wsReconnectRef.current);
        wsReconnectRef.current = null;
      }
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [modalTaskId, task?.state, fetchActivity, fetchSched]);

  // scroll log on new entries
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [activityData?.activity?.length]);

  if (!modalTaskId || !task) return null;

  const close = () => setModalTaskId(null);

  const stages = getPipeStatus(task);
  const activeStage = stages.find((s) => s.status === 'active');
  const hb = task.heartbeat || { status: 'unknown' as const, label: '⚪ 无数据' };
  const flowLog = task.flow_log || [];
  const todos = task.todos || [];
  const todoDone = todos.filter((x) => x.status === 'completed').length;
  const todoTotal = todos.length;
  const canStop = !['Done', 'Blocked', 'Cancelled'].includes(task.state);
  const canResume = ['Blocked', 'Cancelled'].includes(task.state);

  const doTaskAction = async (action: string, reason: string) => {
    try {
      const r = await api.taskAction(task.id, action, reason);
      if (r.ok) {
        toast(r.message || '操作成功', 'ok');
        loadAll();
        close();
      } else {
        toast(r.error || '操作失败', 'err');
      }
    } catch {
      toast('服务器连接失败', 'err');
    }
  };

  const doReview = async (action: string) => {
    const labels: Record<string, string> = { approve: '准奏', reject: '封驳' };
    setDialog({
      key: `review-${action}-${task.id}`,
      title: `${labels[action]} ${task.id}`,
      message: action === 'approve' ? '确认通过当前审批节点并推进到下一责任部门。' : '确认退回当前任务，并按状态机语义进入返工路径。',
      okLabel: action === 'approve' ? '确认准奏' : '确认退回',
      okClass: action === 'reject' ? 'danger' : undefined,
      reasonLabel: '审批批注',
      reasonPlaceholder: action === 'approve' ? '可填写通过说明、附加要求或提醒事项' : '可填写驳回原因、返工要求或补充意见',
      riskHint: action === 'approve' ? '准奏后会直接推进到下一个节点。' : '驳回会改变流转方向，并通知相关责任方返工。',
      permissionHint: task.state === 'Menxia' ? '门下省或高权限角色可以审批。' : '皇上或高权限角色可以审批。',
      onConfirm: async (comment) => {
        try {
          const r = await api.reviewAction(task.id, action, comment || '');
          if (r.ok) {
            toast(`✅ ${task.id} 已${labels[action]}`, 'ok');
            loadAll();
            close();
          } else {
            toast(r.error || '操作失败', 'err');
          }
        } catch {
          toast('服务器连接失败', 'err');
        } finally {
          setDialog(null);
        }
      },
    });
  };

  const doAdvance = async () => {
    const next = NEXT_LABELS[task.state] || '下一步';
    setDialog({
      key: `advance-${task.id}`,
      title: `手动推进 ${task.id}`,
      message: `当前状态：${STATE_LABEL[task.state] || task.state}，下一步：${next}。`,
      okLabel: '确认推进',
      note: '该入口主要用于解卡、补偿或人工兜底，不应替代正常状态机推进。',
      riskHint: '手动推进会直接切换任务阶段，并可能触发自动派发。',
      permissionHint: '仅当前负责人或高权限角色可以推进。',
      reasonLabel: '推进说明',
      reasonPlaceholder: '说明为什么要人工推进，例如：Agent 卡住、人工确认已完成、需要补偿重放',
      onConfirm: async (comment) => {
        try {
          const r = await api.advanceState(task.id, comment || '');
          if (r.ok) {
            toast(`⏩ ${r.message}`, 'ok');
            loadAll();
            close();
          } else {
            toast(r.error || '推进失败', 'err');
          }
        } catch {
          toast('服务器连接失败', 'err');
        } finally {
          setDialog(null);
        }
      },
    });
  };

  const doSchedAction = async (action: string) => {
    if (action === 'scan') {
      setDialog({
        key: `sched-scan-${task.id}`,
        title: `立即扫描 ${task.id}`,
        message: '让司礼监立即检查该任务的停滞、重试、升级与回滚条件。',
        okLabel: '开始扫描',
        showReason: false,
        permissionHint: '仅司礼监调度链路可执行扫描。',
        onConfirm: async () => {
          try {
            const r = await api.schedulerScan(180);
            if (r.ok) toast(`🔍 扫描完成：${r.count || 0} 个动作`, 'ok');
            else toast(r.error || '扫描失败', 'err');
            fetchSched();
          } catch {
            toast('服务器连接失败', 'err');
          } finally {
            setDialog(null);
          }
        },
      });
      return;
    }
    const labels: Record<string, string> = { retry: '重试', escalate: '升级', rollback: '回滚' };
    const handlers: Record<string, (id: string, r: string) => Promise<{ ok: boolean; message?: string; error?: string }>> = {
      retry: api.schedulerRetry,
      escalate: api.schedulerEscalate,
      rollback: api.schedulerRollback,
    };
    setDialog({
      key: `sched-${action}-${task.id}`,
      title: `${labels[action]}调度 ${task.id}`,
      message: action === 'retry'
        ? '重新派发当前节点，适用于派发中断或执行方未接单。'
        : action === 'escalate'
          ? '将停滞任务升级给更高层协调节点介入推进。'
          : '把任务回滚到最近稳定快照，用于从失效状态恢复。',
      okLabel: `确认${labels[action]}`,
      okClass: action === 'rollback' ? 'danger' : undefined,
      riskHint: action === 'rollback' ? '回滚会直接修改任务状态并可能重新派发。' : undefined,
      permissionHint: '仅司礼监调度链路可执行此类操作。',
      reasonLabel: `${labels[action]}原因`,
      reasonPlaceholder: action === 'retry'
        ? '例如：上次派发超时、目标 Agent 未响应'
        : action === 'escalate'
          ? '例如：已超过 SLA，需要门下/尚书介入'
          : '例如：状态异常、恢复到最近稳定节点',
      onConfirm: async (reason) => {
        try {
          const r = await handlers[action](task.id, reason);
          if (r.ok) toast(r.message || '操作成功', 'ok');
          else toast(r.error || '操作失败', 'err');
          fetchSched();
          loadAll();
        } catch {
          toast('服务器连接失败', 'err');
        } finally {
          setDialog(null);
        }
      },
    });
  };

  const handleStop = () => {
    setDialog({
      key: `stop-${task.id}`,
      title: `叫停 ${task.id}`,
      message: '把任务切到 Blocked，暂时中止执行与后续流转。',
      okLabel: '确认叫停',
      reasonLabel: '叫停原因',
      reasonPlaceholder: '例如：等待外部信息、需求变更、需要圣裁',
      riskHint: '叫停会立即中断当前执行节奏。',
      permissionHint: '仅皇上或司礼监可以执行该控制动作。',
      onConfirm: async (reason) => {
        await doTaskAction('stop', reason);
        setDialog(null);
      },
    });
  };

  const handleCancel = () => {
    setDialog({
      key: `cancel-${task.id}`,
      title: `取消 ${task.id}`,
      message: '把任务切到 Cancelled。取消后不允许直接 resume。',
      okLabel: '确认取消',
      okClass: 'danger',
      reasonLabel: '取消原因',
      reasonPlaceholder: '例如：旨意作废、已并入其他任务、方向调整',
      riskHint: 'Cancelled 是真终态，后续如需恢复应新建任务或显式 reopen。',
      permissionHint: '仅皇上或司礼监可以执行该控制动作。',
      onConfirm: async (reason) => {
        await doTaskAction('cancel', reason);
        setDialog(null);
      },
    });
  };

  // Scheduler state
  const sched = schedData?.scheduler;
  const stalledSec = schedData?.stalledSec || 0;

  return (
    <div className="modal-bg open" onClick={close}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        {dialog && (
          <ConfirmDialog
            key={dialog.key}
            title={dialog.title}
            message={dialog.message}
            okLabel={dialog.okLabel}
            okClass={dialog.okClass}
            note={dialog.note}
            riskHint={dialog.riskHint}
            permissionHint={dialog.permissionHint}
            reasonLabel={dialog.reasonLabel}
            reasonPlaceholder={dialog.reasonPlaceholder}
            showReason={dialog.showReason}
            defaultReason={dialog.defaultReason}
            onOk={(reason) => void dialog.onConfirm(reason)}
            onCancel={() => setDialog(null)}
          />
        )}
        <button className="modal-close" onClick={close}>✕</button>
        <div className="modal-body">
          <div className="modal-id">{task.id}</div>
          <div className="modal-title">{task.title || '(无标题)'}</div>

          {/* Current Stage Banner */}
          {activeStage && (
            <div className="cur-stage">
              <div className="cs-icon">{activeStage.icon}</div>
              <div className="cs-info">
                <div className="cs-dept" style={{ color: deptColor(activeStage.dept) }}>{activeStage.dept}</div>
                <div className="cs-action">当前阶段：{activeStage.action}</div>
              </div>
              <span className={`hb ${hb.status} cs-hb`}>{hb.label}</span>
            </div>
          )}

          {/* Pipeline */}
          <div className="m-pipe">
            {stages.map((s, i) => (
              <div className="mp-stage" key={s.key}>
                <div className={`mp-node ${s.status}`}>
                  {s.status === 'done' && <div className="mp-done-tick">✓</div>}
                  <div className="mp-icon">{s.icon}</div>
                  <div className="mp-dept" style={s.status === 'active' ? { color: 'var(--acc)' } : s.status === 'done' ? { color: 'var(--ok)' } : {}}>
                    {s.dept}
                  </div>
                  <div className="mp-action">{s.action}</div>
                </div>
                {i < stages.length - 1 && (
                  <div className="mp-arrow" style={s.status === 'done' ? { color: 'var(--ok)', opacity: 0.6 } : {}}>→</div>
                )}
              </div>
            ))}
          </div>

          {/* Action Buttons */}
          <div className="task-actions">
            {canStop && (
              <>
                <button className="btn-action btn-stop" onClick={handleStop}>⏸ 叫停任务</button>
                <button className="btn-action btn-cancel" onClick={handleCancel}>🚫 取消任务</button>
              </>
            )}
            {canResume && (
              <button className="btn-action btn-resume" onClick={() => doTaskAction('resume', '恢复执行')}>▶️ 恢复执行</button>
            )}
            {['Review', 'Menxia'].includes(task.state) && (
              <>
                <button className="btn-action" style={{ background: '#2ecc8a22', color: '#2ecc8a', border: '1px solid #2ecc8a44' }} onClick={() => doReview('approve')}>✅ 准奏</button>
                <button className="btn-action" style={{ background: '#ff527022', color: '#ff5270', border: '1px solid #ff527044' }} onClick={() => doReview('reject')}>🚫 封驳</button>
              </>
            )}
            {['Pending', 'Sili', 'Zhongshu', 'Menxia', 'Assigned', 'Doing', 'Review', 'Next'].includes(task.state) && (
              <button className="btn-action" style={{ background: '#7c5cfc18', color: '#7c5cfc', border: '1px solid #7c5cfc44' }} onClick={doAdvance}>⏩ 推进到下一步</button>
            )}
          </div>

          {/* Scheduler Section */}
          <div className="sched-section">
            <div className="sched-head">
              <span className="sched-title">🧭 司礼监调度</span>
              <span className="sched-status">
                {sched ? `${sched.enabled === false ? '已禁用' : '运行中'} · 阈值 ${sched.stallThresholdSec || 180}s` : '加载中...'}
              </span>
            </div>
            <div className="sched-grid">
              <div className="sched-kpi"><div className="k">停滞时长</div><div className="v">{fmtStalled(stalledSec)}</div></div>
              <div className="sched-kpi"><div className="k">重试次数</div><div className="v">{sched?.retryCount || 0}</div></div>
              <div className="sched-kpi"><div className="k">升级级别</div><div className="v">{!sched?.escalationLevel ? '无' : sched.escalationLevel === 1 ? '门下省' : '尚书省'}</div></div>
              <div className="sched-kpi"><div className="k">派发状态</div><div className="v">{sched?.lastDispatchStatus || 'idle'}</div></div>
            </div>
            {sched && (
              <div className="sched-line">
                {sched.lastProgressAt && <span>最近进展 {(sched.lastProgressAt || '').replace('T', ' ').substring(0, 19)}</span>}
                {sched.lastDispatchAt && <span>最近派发 {(sched.lastDispatchAt || '').replace('T', ' ').substring(0, 19)}</span>}
                <span>自动回滚 {sched.autoRollback === false ? '关闭' : '开启'}</span>
                {sched.lastDispatchAgent && <span>目标 {sched.lastDispatchAgent}</span>}
              </div>
            )}
            <div className="sched-actions">
              <button className="sched-btn" onClick={() => doSchedAction('retry')}>🔁 重试派发</button>
              <button className="sched-btn warn" onClick={() => doSchedAction('escalate')}>📣 升级协调</button>
              <button className="sched-btn danger" onClick={() => doSchedAction('rollback')}>↩️ 回滚稳定点</button>
              <button className="sched-btn" onClick={() => doSchedAction('scan')}>🔍 立即扫描</button>
            </div>
          </div>

          {/* Todo List */}
          {todoTotal > 0 && (
            <TodoSection todos={todos} todoDone={todoDone} todoTotal={todoTotal} />
          )}

          {/* Basic Info */}
          <div className="m-section">
            <div className="m-rows">
              <div className="m-row">
                <div className="mr-label">状态</div>
                <div className="mr-val">
                  <span className={`tag st-${task.state}`}>{stateLabel(task)}</span>
                  {(task.review_round || 0) > 0 && <span style={{ fontSize: 11, color: 'var(--muted)', marginLeft: 8 }}>共磋商 {task.review_round} 轮</span>}
                </div>
              </div>
              <div className="m-row">
                <div className="mr-label">执行部门</div>
                <div className="mr-val"><span className={`tag dt-${(task.org || '').replace(/\s/g, '')}`}>{task.org || '—'}</span></div>
              </div>
              {task.eta && task.eta !== '-' && (
                <div className="m-row"><div className="mr-label">预计完成</div><div className="mr-val">{task.eta}</div></div>
              )}
              {task.block && task.block !== '无' && task.block !== '-' && (
                <div className="m-row"><div className="mr-label" style={{ color: 'var(--danger)' }}>阻塞项</div><div className="mr-val" style={{ color: 'var(--danger)' }}>{task.block}</div></div>
              )}
              {task.now && task.now !== '-' && (
                <div className="m-row" style={{ gridColumn: '1/-1' }}>
                  <div className="mr-label">当前进展</div>
                  <div className="mr-val" style={{ fontWeight: 400, fontSize: 12 }}>{task.now}</div>
                </div>
              )}
              {task.ac && (
                <div className="m-row" style={{ gridColumn: '1/-1' }}>
                  <div className="mr-label">验收标准</div>
                  <div className="mr-val" style={{ fontWeight: 400, fontSize: 12 }}>{task.ac}</div>
                </div>
              )}
            </div>
          </div>

          {/* Flow Log */}
          {flowLog.length > 0 && (
            <div className="m-section">
              <div className="m-sec-label">流转日志（{flowLog.length} 条）</div>
              <div className="fl-timeline">
                {flowLog.map((fl, i) => {
                  const col = deptColor(fl.from || '');
                  return (
                    <div className="fl-item" key={i}>
                      <div className="fl-time">{fl.at ? fl.at.substring(11, 16) : ''}</div>
                      <div className="fl-dot" style={{ background: col }} />
                      <div className="fl-content">
                        <div className="fl-who">
                          <span className="from" style={{ color: col }}>{fl.from}</span>
                          <span style={{ color: 'var(--muted)' }}> → </span>
                          <span className="to" style={{ color: deptColor(fl.to || '') }}>{fl.to}</span>
                        </div>
                        <div className="fl-rem">{fl.remark}</div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* Output */}
          {task.output && task.output !== '-' && task.output !== '' && (
            <div className="m-section">
              <div className="m-sec-label">产出物</div>
              <code>{task.output}</code>
            </div>
          )}

          {/* Live Activity */}
          <LiveActivitySection data={activityData} isDone={['Done', 'Cancelled'].includes(task.state)} logRef={logRef} />
        </div>
      </div>
    </div>
  );
}

function TodoSection({ todos, todoDone, todoTotal }: { todos: TodoItem[]; todoDone: number; todoTotal: number }) {
  return (
    <div className="todo-section">
      <div className="todo-header">
        <div className="m-sec-label" style={{ marginBottom: 0, border: 'none', padding: 0 }}>
          子任务清单（{todoDone}/{todoTotal}）
        </div>
        <div className="todo-progress">
          <div className="todo-bar">
            <div className="todo-bar-fill" style={{ width: `${Math.round((todoDone / todoTotal) * 100)}%` }} />
          </div>
          <span>{Math.round((todoDone / todoTotal) * 100)}%</span>
        </div>
      </div>
      <div className="todo-list">
        {todos.map((td) => {
          const ico = td.status === 'completed' ? '✅' : td.status === 'in-progress' ? '🔄' : '⬜';
          const stLabel = td.status === 'completed' ? '已完成' : td.status === 'in-progress' ? '进行中' : '待开始';
          const stCls = td.status === 'completed' ? 's-done' : td.status === 'in-progress' ? 's-progress' : 's-notstarted';
          const itemCls = td.status === 'completed' ? 'done' : '';
          return (
            <div className={`todo-item ${itemCls}`} key={td.id}>
              <div className="t-row">
                <span className="t-icon">{ico}</span>
                <span className="t-id">#{td.id}</span>
                <span className="t-title">{td.title}</span>
                <span className={`t-status ${stCls}`}>{stLabel}</span>
              </div>
              {td.detail && <div className="todo-detail">{td.detail}</div>}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function LiveActivitySection({
  data,
  isDone,
  logRef,
}: {
  data: TaskActivityData | null;
  isDone: boolean;
  logRef: React.RefObject<HTMLDivElement | null>;
}) {
  if (!data) return null;

  const activity = data.activity || [];
  const isActive = (() => {
    if (!activity.length) return false;
    const last = activity[activity.length - 1];
    if (!last.at) return false;
    const ts = typeof last.at === 'number' ? last.at : new Date(last.at).getTime();
    return Date.now() - ts < 300000;
  })();

  const agentParts: string[] = [];
  if (data.agentLabel) agentParts.push(data.agentLabel);
  if (data.relatedAgents && data.relatedAgents.length > 1) agentParts.push(`${data.relatedAgents.length}个 Agent`);
  if (data.lastActive) agentParts.push(`最后活跃: ${data.lastActive}`);

  // Phase durations
  const phaseDurations = data.phaseDurations || [];
  const maxDur = Math.max(...phaseDurations.map((p) => p.durationSec || 1), 1);
  const phaseColors: Record<string, string> = {
    '皇上': '#eab308', '司礼监': '#f97316', '中书省': '#3b82f6', '门下省': '#8b5cf6',
    '尚书省': '#10b981', '六部': '#06b6d4', '礼部': '#ec4899', '户部': '#f59e0b',
    '兵部': '#ef4444', '刑部': '#6366f1', '工部': '#14b8a6', '吏部': '#d946ef',
  };

  // Todos summary
  const ts = data.todosSummary;

  // Resource summary
  const rs = data.resourceSummary;

  // Group non-flow activity by agent
  const flowItems = activity.filter((a) => a.kind === 'flow');
  const nonFlow = activity.filter((a) => a.kind !== 'flow');
  const grouped = new Map<string, ActivityEntry[]>();
  nonFlow.forEach((a) => {
    const key = a.agent || 'unknown';
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key)!.push(a);
  });

  return (
    <div className="la-section">
      <div className="la-header">
        <span className="la-title">
          <span className={`la-dot${isActive ? '' : ' idle'}`} />
          {isDone ? '执行回顾' : '实时动态'}
        </span>
        <span className="la-agent">{agentParts.join(' · ') || '加载中...'}</span>
      </div>

      {/* Phase Bars */}
      {phaseDurations.length > 0 && (
        <div style={{ padding: '4px 0 8px', borderBottom: '1px solid var(--line)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
            <span style={{ fontSize: 11, fontWeight: 600 }}>⏱ 阶段耗时</span>
            {data.totalDuration && <span style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--muted)' }}>总耗时 {data.totalDuration}</span>}
          </div>
          {phaseDurations.map((p, i) => {
            const pct = Math.max(5, Math.round(((p.durationSec || 1) / maxDur) * 100));
            const color = phaseColors[p.phase] || '#6b7280';
            return (
              <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 6, margin: '2px 0', fontSize: 11 }}>
                <span style={{ minWidth: 48, color: 'var(--muted)', textAlign: 'right' }}>{p.phase}</span>
                <div style={{ flex: 1, height: 14, background: 'var(--panel)', borderRadius: 3, overflow: 'hidden' }}>
                  <div style={{ width: `${pct}%`, height: '100%', background: color, borderRadius: 3, opacity: p.ongoing ? 0.6 : 0.85 }} />
                </div>
                <span style={{ minWidth: 60, fontSize: 10, color: 'var(--muted)' }}>
                  {p.durationText}
                  {p.ongoing && <span style={{ fontSize: 9, color: '#60a5fa' }}> ●进行中</span>}
                </span>
              </div>
            );
          })}
        </div>
      )}

      {/* Todos Progress */}
      {ts && (
        <div style={{ padding: '4px 0 8px', borderBottom: '1px solid var(--line)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
            <span style={{ fontSize: 11, fontWeight: 600 }}>📊 执行进度</span>
            <span style={{ fontSize: 20, fontWeight: 700, color: ts.percent >= 100 ? '#22c55e' : ts.percent >= 50 ? '#60a5fa' : 'var(--text)' }}>{ts.percent}%</span>
            <span style={{ fontSize: 10, color: 'var(--muted)' }}>✅{ts.completed} 🔄{ts.inProgress} ⬜{ts.notStarted} / 共{ts.total}项</span>
          </div>
          <div style={{ height: 8, background: 'var(--panel)', borderRadius: 4, overflow: 'hidden', display: 'flex' }}>
            <div style={{ width: `${ts.total ? (ts.completed / ts.total) * 100 : 0}%`, background: '#22c55e', transition: 'width .3s' }} />
            <div style={{ width: `${ts.total ? (ts.inProgress / ts.total) * 100 : 0}%`, background: '#3b82f6', transition: 'width .3s' }} />
          </div>
        </div>
      )}

      {/* Resource Summary */}
      {rs && (rs.totalTokens || rs.totalCost) && (
        <div style={{ padding: '4px 0 8px', borderBottom: '1px solid var(--line)', display: 'flex', gap: 12, alignItems: 'center' }}>
          <span style={{ fontSize: 11, fontWeight: 600 }}>📈 资源消耗</span>
          {rs.totalTokens != null && <span style={{ fontSize: 11, color: 'var(--muted)' }}>🔢 {rs.totalTokens.toLocaleString()} tokens</span>}
          {rs.totalCost != null && <span style={{ fontSize: 11, color: 'var(--muted)' }}>💰 ${rs.totalCost.toFixed(4)}</span>}
          {rs.totalElapsedSec != null && (
            <span style={{ fontSize: 11, color: 'var(--muted)' }}>
              ⏳ {rs.totalElapsedSec >= 60 ? `${Math.floor(rs.totalElapsedSec / 60)}分` : ''}{rs.totalElapsedSec % 60}秒
            </span>
          )}
        </div>
      )}

      {/* Activity Log */}
      <div className="la-log" ref={logRef as React.RefObject<HTMLDivElement>}>
        {/* Flow entries */}
        {flowItems.length > 0 && (
          <div className="la-flow-wrap">
            {flowItems.map((a, i) => (
              <div className="la-entry la-tool" key={`flow-${i}`}>
                <span className="la-icon">📋</span>
                <span className="la-body"><b>{a.from}</b> → <b>{a.to}</b>　{a.remark || ''}</span>
                <span className="la-time">{fmtActivityTime(a.at)}</span>
              </div>
            ))}
          </div>
        )}

        {/* Grouped entries */}
        {grouped.size > 0 ? (
          <div className="la-groups">
            {Array.from(grouped.entries()).map(([agent, items]) => {
              const label = AGENT_LABELS[agent] || agent || '未标识';
              const last = items[items.length - 1];
              const lastTime = last?.at ? fmtActivityTime(last.at) : '--:--:--';
              return (
                <div className="la-group" key={agent}>
                  <div className="la-group-hd">
                    <span className="name">{label}</span>
                    <span>最近更新 {lastTime}</span>
                  </div>
                  <div className="la-group-bd">
                    {items.map((a, i) => (
                      <ActivityEntryView key={i} entry={a} />
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          !flowItems.length && (
            <div className="la-empty">
              {data.message || data.error || 'Agent 尚未上报进展（等待 Agent 调用 progress 命令）'}
            </div>
          )
        )}
      </div>
    </div>
  );
}

function ActivityEntryView({ entry: a }: { entry: ActivityEntry }) {
  const time = fmtActivityTime(a.at);
  const agBadge = a.agent ? (
    <span style={{ fontSize: 9, color: 'var(--muted)', background: 'var(--panel)', padding: '1px 4px', borderRadius: 3, marginRight: 4 }}>
      {AGENT_LABELS[a.agent] || a.agent}
    </span>
  ) : null;

  if (a.kind === 'progress') {
    return (
      <div className="la-entry la-assistant">
        <span className="la-icon">🔄</span>
        <span className="la-body">{agBadge}<b>当前进展：</b>{a.text}</span>
        <span className="la-time">{time}</span>
      </div>
    );
  }

  if (a.kind === 'todos') {
    const items = a.items || [];
    const diffMap = new Map<string, { type: string; from?: string; to?: string }>();
    if (a.diff) {
      (a.diff.changed || []).forEach((c) => diffMap.set(c.id, { type: 'changed', from: c.from, to: c.to }));
      (a.diff.added || []).forEach((c) => diffMap.set(c.id, { type: 'added' }));
    }
    return (
      <div className="la-entry" style={{ flexDirection: 'column', alignItems: 'flex-start', gap: 2 }}>
        <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 2 }}>{agBadge}📝 执行计划</div>
        {items.map((td) => {
          const icon = td.status === 'completed' ? '✅' : td.status === 'in-progress' ? '🔄' : '⬜';
          const d = diffMap.get(String(td.id));
          const style: React.CSSProperties = td.status === 'completed'
            ? { opacity: 0.5, textDecoration: 'line-through' }
            : td.status === 'in-progress'
              ? { color: '#60a5fa', fontWeight: 'bold' }
              : {};
          return (
            <div key={td.id} style={style}>
              {icon} {td.title}
              {d && d.type === 'changed' && d.to === 'completed' && <span style={{ color: '#22c55e', fontSize: 9, marginLeft: 4 }}>✨刚完成</span>}
              {d && d.type === 'changed' && d.to !== 'completed' && <span style={{ color: '#f59e0b', fontSize: 9, marginLeft: 4 }}>↻{d.from}→{d.to}</span>}
              {d && d.type === 'added' && <span style={{ color: '#3b82f6', fontSize: 9, marginLeft: 4 }}>🆕新增</span>}
            </div>
          );
        })}
        {a.diff?.removed?.map((r) => (
          <div key={r.id} style={{ opacity: 0.4, textDecoration: 'line-through' }}>🗑 {r.title}</div>
        ))}
      </div>
    );
  }

  if (a.kind === 'assistant') {
    return (
      <>
        {a.thinking && (
          <div className="la-entry la-thinking">
            <span className="la-icon">💭</span>
            <span className="la-body">{agBadge}{a.thinking}</span>
            <span className="la-time">{time}</span>
          </div>
        )}
        {a.tools?.map((tc, i) => (
          <div className="la-entry la-tool" key={i}>
            <span className="la-icon">🔧</span>
            <span className="la-body">{agBadge}<span className="la-tool-name">{tc.name}</span><span className="la-trunc">{tc.input_preview || ''}</span></span>
            <span className="la-time">{time}</span>
          </div>
        ))}
        {a.text && (
          <div className="la-entry la-assistant">
            <span className="la-icon">🤖</span>
            <span className="la-body">{agBadge}{a.text}</span>
            <span className="la-time">{time}</span>
          </div>
        )}
      </>
    );
  }

  if (a.kind === 'tool_result') {
    const ok = a.exitCode === 0 || a.exitCode === null || a.exitCode === undefined;
    return (
      <div className={`la-entry la-tool-result ${ok ? 'ok' : 'err'}`}>
        <span className="la-icon">{ok ? '✅' : '❌'}</span>
        <span className="la-body">{agBadge}<span className="la-tool-name">{a.tool || ''}</span>{a.output ? a.output.substring(0, 150) : ''}</span>
        <span className="la-time">{time}</span>
      </div>
    );
  }

  if (a.kind === 'user') {
    return (
      <div className="la-entry la-user">
        <span className="la-icon">📥</span>
        <span className="la-body">{agBadge}{a.text || ''}</span>
        <span className="la-time">{time}</span>
      </div>
    );
  }

  return null;
}
