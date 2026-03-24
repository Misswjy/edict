// Generated from config/institution_schema.json. Do not edit by hand.

export type PipeStage = { key: string; dept: string; icon: string; action: string };
export type DeptDef = { id: string; label: string; emoji: string; role: string; rank: string };

export const PIPE: PipeStage[] = [
  {
    "key": "Inbox",
    "dept": "皇上",
    "icon": "👑",
    "action": "下旨"
  },
  {
    "key": "Sili",
    "dept": "司礼监",
    "icon": "🧾",
    "action": "分办"
  },
  {
    "key": "Zhongshu",
    "dept": "中书省",
    "icon": "📜",
    "action": "起草"
  },
  {
    "key": "Menxia",
    "dept": "门下省",
    "icon": "🔍",
    "action": "审议"
  },
  {
    "key": "Assigned",
    "dept": "尚书省",
    "icon": "📮",
    "action": "派发"
  },
  {
    "key": "Doing",
    "dept": "六部",
    "icon": "⚙️",
    "action": "执行"
  },
  {
    "key": "Review",
    "dept": "尚书省",
    "icon": "🔎",
    "action": "汇总"
  },
  {
    "key": "Done",
    "dept": "回奏",
    "icon": "✅",
    "action": "完成"
  }
];

export const PIPE_STATE_IDX: Record<string, number> = {
  "Pending": 0,
  "Sili": 1,
  "Zhongshu": 2,
  "Menxia": 3,
  "Assigned": 4,
  "Next": 4,
  "Doing": 5,
  "Review": 6,
  "Done": 7,
  "Blocked": 5,
  "Cancelled": 5,
  "Inbox": 0
};

export const DEPT_COLOR: Record<string, string> = {
  "司礼监": "#e8a040",
  "中书省": "#a07aff",
  "门下省": "#6a9eff",
  "尚书省": "#6aef9a",
  "礼部": "#f5c842",
  "户部": "#ff9a6a",
  "兵部": "#ff5270",
  "刑部": "#cc4444",
  "工部": "#44aaff",
  "吏部": "#9b59b6",
  "皇上": "#ffd700",
  "回奏": "#2ecc8a"
};

export const STATE_LABEL: Record<string, string> = {
  "Pending": "待处理",
  "Sili": "司礼监分办",
  "Zhongshu": "中书起草",
  "Menxia": "门下审议",
  "Assigned": "已派发",
  "Next": "待执行",
  "Doing": "执行中",
  "Review": "待审查",
  "Done": "已完成",
  "Blocked": "阻塞",
  "Cancelled": "已取消",
  "Inbox": "收件"
};

export const DEPTS: DeptDef[] = [
  {
    "id": "sili",
    "label": "司礼监",
    "emoji": "🧾",
    "role": "掌印秉笔",
    "rank": "内廷"
  },
  {
    "id": "zhongshu",
    "label": "中书省",
    "emoji": "📜",
    "role": "中书令",
    "rank": "正一品"
  },
  {
    "id": "menxia",
    "label": "门下省",
    "emoji": "🔍",
    "role": "侍中",
    "rank": "正一品"
  },
  {
    "id": "shangshu",
    "label": "尚书省",
    "emoji": "📮",
    "role": "尚书令",
    "rank": "正一品"
  },
  {
    "id": "libu",
    "label": "礼部",
    "emoji": "📝",
    "role": "礼部尚书",
    "rank": "正二品"
  },
  {
    "id": "hubu",
    "label": "户部",
    "emoji": "💰",
    "role": "户部尚书",
    "rank": "正二品"
  },
  {
    "id": "bingbu",
    "label": "兵部",
    "emoji": "⚔️",
    "role": "兵部尚书",
    "rank": "正二品"
  },
  {
    "id": "xingbu",
    "label": "刑部",
    "emoji": "⚖️",
    "role": "刑部尚书",
    "rank": "正二品"
  },
  {
    "id": "gongbu",
    "label": "工部",
    "emoji": "🔧",
    "role": "工部尚书",
    "rank": "正二品"
  },
  {
    "id": "libu_hr",
    "label": "吏部",
    "emoji": "👔",
    "role": "吏部尚书",
    "rank": "正二品"
  },
  {
    "id": "zaochao",
    "label": "钦天监",
    "emoji": "📰",
    "role": "朝报官",
    "rank": "正三品"
  }
];
