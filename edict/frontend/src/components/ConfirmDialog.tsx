import { useState } from 'react';

interface Props {
  title: string;
  message: string;
  okLabel: string;
  okClass?: string;
  reasonLabel?: string;
  reasonPlaceholder?: string;
  note?: string;
  riskHint?: string;
  permissionHint?: string;
  showReason?: boolean;
  defaultReason?: string;
  onOk: (reason: string) => void;
  onCancel: () => void;
}

export default function ConfirmDialog({
  title,
  message,
  okLabel,
  okClass,
  reasonLabel = '原因说明',
  reasonPlaceholder = '输入原因（可留空）',
  note,
  riskHint,
  permissionHint,
  showReason = true,
  defaultReason = '',
  onOk,
  onCancel,
}: Props) {
  const [reason, setReason] = useState(defaultReason);

  return (
    <div className="confirm-bg open" onClick={onCancel}>
      <div className="confirm-box" onClick={(e) => e.stopPropagation()}>
        <div className="confirm-title">{title}</div>
        <div className="confirm-msg">{message}</div>
        {note && <div className="confirm-msg" style={{ marginTop: 8, color: 'var(--muted)' }}>{note}</div>}
        {riskHint && (
          <div className="confirm-msg" style={{ marginTop: 12, color: 'var(--danger)' }}>
            风险提示：{riskHint}
          </div>
        )}
        {permissionHint && (
          <div className="confirm-msg" style={{ marginTop: 8, color: 'var(--muted)' }}>
            权限说明：{permissionHint}
          </div>
        )}
        {showReason && (
          <>
            <div className="confirm-msg" style={{ marginTop: 12, marginBottom: 6 }}>{reasonLabel}</div>
            <textarea
              className="confirm-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder={reasonPlaceholder}
              rows={3}
            />
          </>
        )}
        <div className="confirm-btns">
          <button className="btn btn-g" onClick={onCancel}>取消</button>
          <button className={`btn btn-action ${okClass || ''}`} onClick={() => onOk(reason)}>
            {okLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
