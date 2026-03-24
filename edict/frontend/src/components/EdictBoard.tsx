import { useState } from 'react';
import { useStore, isEdict, isArchived, getPipeStatus, stateLabel, deptColor, PIPE } from '../store';
import { api, type Task } from '../api';
import ConfirmDialog from './ConfirmDialog';

// 排序权重
const STATE_ORDER: Record<string, number> = {
  Doing: 0, Review: 1, Assigned: 2, Menxia: 3, Zhongshu: 4,
  Sili: 5, Inbox: 6, Blocked: 7, Next: 8, Done: 9, Cancelled: 10,
};

function MiniPipe({ task }: { task: Task }) {
  const stages = getPipeStatus(task);
  return (
    <div className="ec-pipe">
      {stages.map((s, i) => (
        <span key={s.key} style={{ display: 'contents' }}>
          <div className={`ep-node ${s.status}`}>
            <div className="ep-icon">{s.icon}</div>
            <div className="ep-name">{s.dept}</div>
          </div>
          {i < stages.length - 1 && <div className="ep-arrow">›</div>}
        </span>
      ))}
    </div>
  );
}

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

function EdictCard({ task }: { task: Task }) {
  const setModalTaskId = useStore((s) => s.setModalTaskId);
  const toast = useStore((s) => s.toast);
  const loadAll = useStore((s) => s.loadAll);
  const [dialog, setDialog] = useState<ConfirmState | null>(null);

  const hb = task.heartbeat || { status: 'unknown', label: '⚪' };
  const stCls = 'st-' + (task.state || '');
  const deptCls = 'dt-' + (task.org || '').replace(/\s/g, '');
  const curStage = PIPE.find((_, i) => getPipeStatus(task)[i].status === 'active');
  const todos = task.todos || [];
  const todoDone = todos.filter((x) => x.status === 'completed').length;
  const todoTotal = todos.length;
  const canStop = !['Done', 'Blocked', 'Cancelled'].includes(task.state);
  const canResume = ['Blocked', 'Cancelled'].includes(task.state);
  const archived = isArchived(task);
  const isBlocked = task.block && task.block !== '无' && task.block !== '-';

  const handleAction = async (action: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (action === 'stop' || action === 'cancel') {
      setDialog({
        key: `${task.id}-${action}`,
        title: action === 'stop' ? `叫停 ${task.id}` : `取消 ${task.id}`,
        message: action === 'stop' ? '将当前旨意切换为 Blocked，等待恢复后再继续流转。' : '将当前旨意切换为 Cancelled，取消后不会直接 resume。',
        okLabel: action === 'stop' ? '确认叫停' : '确认取消',
        okClass: action === 'cancel' ? 'danger' : undefined,
        reasonLabel: action === 'stop' ? '叫停原因' : '取消原因',
        reasonPlaceholder: action === 'stop' ? '例如：需求变化、等待圣裁、暂停执行' : '例如：旨意作废、方向调整、并单处理',
        riskHint: action === 'stop' ? '叫停会中断当前执行节奏。' : '取消是终态动作，后续若需恢复应新建任务或显式重开。',
        permissionHint: '仅皇上或司礼监可以执行此类控制动作。',
        onConfirm: async (reason) => {
          try {
            const r = await api.taskAction(task.id, action, reason);
            if (r.ok) { toast(r.message || '操作成功'); loadAll(); }
            else toast(r.error || '操作失败', 'err');
          } catch {
            toast('服务器连接失败', 'err');
          } finally {
            setDialog(null);
          }
        },
      });
    } else if (action === 'resume') {
      try {
        const r = await api.taskAction(task.id, 'resume', '恢复执行');
        if (r.ok) { toast(r.message || '已恢复'); loadAll(); }
        else toast(r.error || '操作失败', 'err');
      } catch { toast('服务器连接失败', 'err'); }
    }
  };

  const handleArchive = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      const r = await api.archiveTask(task.id, !task.archived);
      if (r.ok) { toast(r.message || '操作成功'); loadAll(); }
      else toast(r.error || '操作失败', 'err');
    } catch { toast('服务器连接失败', 'err'); }
  };

  return (
    <div
      className={`edict-card${archived ? ' archived' : ''}`}
      onClick={() => setModalTaskId(task.id)}
    >
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
      <MiniPipe task={task} />
      <div className="ec-id">{task.id}</div>
      <div className="ec-title">{task.title || '(无标题)'}</div>
      <div className="ec-meta">
        <span className={`tag ${stCls}`}>{stateLabel(task)}</span>
        {task.org && <span className={`tag ${deptCls}`}>{task.org}</span>}
        {curStage && (
          <span style={{ fontSize: 11, color: 'var(--muted)' }}>
            当前: <b style={{ color: deptColor(curStage.dept) }}>{curStage.dept} · {curStage.action}</b>
          </span>
        )}
      </div>
      {task.now && task.now !== '-' && (
        <div style={{ fontSize: 11, color: 'var(--muted)', lineHeight: 1.5, marginBottom: 6 }}>
          {task.now.substring(0, 80)}
        </div>
      )}
      {(task.review_round || 0) > 0 && (
        <div style={{ fontSize: 11, marginBottom: 6 }}>
          {Array.from({ length: task.review_round || 0 }, (_, i) => (
            <span
              key={i}
              style={{
                display: 'inline-block', width: 14, height: 14, borderRadius: '50%',
                background: i < (task.review_round || 0) - 1 ? '#1a3a6a22' : 'var(--acc)22',
                border: `1px solid ${i < (task.review_round || 0) - 1 ? '#2a4a8a' : 'var(--acc)'}`,
                fontSize: 9, textAlign: 'center', lineHeight: '13px', marginRight: 2,
                color: i < (task.review_round || 0) - 1 ? '#4a6aaa' : 'var(--acc)',
              }}
            >
              {i + 1}
            </span>
          ))}
          <span style={{ color: 'var(--muted)', fontSize: 10 }}>第 {task.review_round} 轮磋商</span>
        </div>
      )}
      {todoTotal > 0 && (
        <div className="ec-todo-bar">
          <span>📋 {todoDone}/{todoTotal}</span>
          <div className="ec-todo-track">
            <div className="ec-todo-fill" style={{ width: `${Math.round((todoDone / todoTotal) * 100)}%` }} />
          </div>
          <span>{todoDone === todoTotal ? '✅ 全部完成' : '🔄 进行中'}</span>
        </div>
      )}
      <div className="ec-footer">
        <span className={`hb ${hb.status}`}>{hb.label}</span>
        {isBlocked && (
          <span className="tag" style={{ borderColor: '#ff527044', color: 'var(--danger)', background: '#200a10' }}>
            🚫 {task.block}
          </span>
        )}
        {task.eta && task.eta !== '-' && (
          <span style={{ fontSize: 11, color: 'var(--muted)' }}>📅 {task.eta}</span>
        )}
      </div>
      <div className="ec-actions" onClick={(e) => e.stopPropagation()}>
        {canStop && (
          <>
            <button className="mini-act" onClick={(e) => handleAction('stop', e)}>⏸ 叫停</button>
            <button className="mini-act danger" onClick={(e) => handleAction('cancel', e)}>🚫 取消</button>
          </>
        )}
        {canResume && (
          <button className="mini-act" onClick={(e) => handleAction('resume', e)}>▶ 恢复</button>
        )}
        {archived && !task.archived && (
          <button className="mini-act" onClick={handleArchive}>📦 归档</button>
        )}
        {task.archived && (
          <button className="mini-act" onClick={handleArchive}>📤 取消归档</button>
        )}
      </div>
    </div>
  );
}

export default function EdictBoard() {
  const liveStatus = useStore((s) => s.liveStatus);
  const edictFilter = useStore((s) => s.edictFilter);
  const setEdictFilter = useStore((s) => s.setEdictFilter);
  const toast = useStore((s) => s.toast);
  const loadAll = useStore((s) => s.loadAll);
  const [dialog, setDialog] = useState<ConfirmState | null>(null);

  const tasks = liveStatus?.tasks || [];
  const allEdicts = tasks.filter(isEdict);
  const activeEdicts = allEdicts.filter((t) => !isArchived(t));
  const archivedEdicts = allEdicts.filter((t) => isArchived(t));

  let edicts: Task[];
  if (edictFilter === 'active') edicts = activeEdicts;
  else if (edictFilter === 'archived') edicts = archivedEdicts;
  else edicts = allEdicts;

  edicts.sort((a, b) => (STATE_ORDER[a.state] ?? 9) - (STATE_ORDER[b.state] ?? 9));

  const unArchivedDone = allEdicts.filter((t) => !t.archived && ['Done', 'Cancelled'].includes(t.state));

  const handleArchiveAll = async () => {
    setDialog({
      key: 'archive-all',
      title: '批量归档已完成旨意',
      message: '将所有已完成或已取消的旨意统一移入归档列表。',
      okLabel: '确认归档',
      showReason: false,
      riskHint: '归档不会改变任务终态，但会影响默认看板可见性。',
      permissionHint: '仅高权限 actor 可以执行批量归档。',
      onConfirm: async () => {
        try {
          const r = await api.archiveAllDone();
          if (r.ok) { toast(`📦 ${r.count || 0} 道旨意已归档`); loadAll(); }
          else toast(r.error || '批量归档失败', 'err');
        } catch {
          toast('服务器连接失败', 'err');
        } finally {
          setDialog(null);
        }
      },
    });
  };

  const handleScan = async () => {
    try {
      const r = await api.schedulerScan();
      if (r.ok) toast(`🧭 司礼监巡检完成：${r.count || 0} 个动作`);
      else toast(r.error || '巡检失败', 'err');
      loadAll();
    } catch { toast('服务器连接失败', 'err'); }
  };

  return (
    <div>
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
      {/* Archive Bar */}
      <div className="archive-bar">
        <span className="ab-label">筛选:</span>
        {(['active', 'archived', 'all'] as const).map((f) => (
          <button
            key={f}
            className={`ab-btn ${edictFilter === f ? 'active' : ''}`}
            onClick={() => setEdictFilter(f)}
          >
            {f === 'active' ? '活跃' : f === 'archived' ? '归档' : '全部'}
          </button>
        ))}
        {unArchivedDone.length > 0 && (
          <button className="ab-btn" onClick={handleArchiveAll}>📦 一键归档</button>
        )}
        <span className="ab-count">
          活跃 {activeEdicts.length} · 归档 {archivedEdicts.length} · 共 {allEdicts.length}
        </span>
        <button className="ab-scan" onClick={handleScan}>🧭 司礼监巡检</button>
      </div>

      {/* Grid */}
      <div className="edict-grid">
        {edicts.length === 0 ? (
          <div className="empty" style={{ gridColumn: '1/-1' }}>
            暂无旨意<br />
            <small style={{ fontSize: 11, marginTop: 6, display: 'block', color: 'var(--muted)' }}>
              通过飞书向司礼监发送任务，司礼监分办后转中书省处理
            </small>
          </div>
        ) : (
          edicts.map((t) => <EdictCard key={t.id} task={t} />)
        )}
      </div>
    </div>
  );
}
