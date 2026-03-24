"""Generated from config/institution_schema.json. Do not edit by hand."""

from __future__ import annotations

import enum


class TaskState(str, enum.Enum):
    Pending = "Pending"
    Sili = "Sili"
    Zhongshu = "Zhongshu"
    Menxia = "Menxia"
    Assigned = "Assigned"
    Next = "Next"
    Doing = "Doing"
    Review = "Review"
    Done = "Done"
    Blocked = "Blocked"
    Cancelled = "Cancelled"


PIPE_STAGES = [{'key': 'Inbox', 'dept': '皇上', 'icon': '👑', 'action': '下旨'},
 {'key': 'Sili', 'dept': '司礼监', 'icon': '🧾', 'action': '分办'},
 {'key': 'Zhongshu', 'dept': '中书省', 'icon': '📜', 'action': '起草'},
 {'key': 'Menxia', 'dept': '门下省', 'icon': '🔍', 'action': '审议'},
 {'key': 'Assigned', 'dept': '尚书省', 'icon': '📮', 'action': '派发'},
 {'key': 'Doing', 'dept': '六部', 'icon': '⚙️', 'action': '执行'},
 {'key': 'Review', 'dept': '尚书省', 'icon': '🔎', 'action': '汇总'},
 {'key': 'Done', 'dept': '回奏', 'icon': '✅', 'action': '完成'}]
STATE_ALIASES = {'Inbox': 'Pending'}
TASK_STATE_VALUES = tuple(state.value for state in TaskState)
TERMINAL_STATES = set(['Done', 'Cancelled'])
EXECUTION_STATES = set(['Next', 'Doing'])
STATE_PIPE_INDEX = {'Pending': 0,
 'Sili': 1,
 'Zhongshu': 2,
 'Menxia': 3,
 'Assigned': 4,
 'Next': 4,
 'Doing': 5,
 'Review': 6,
 'Done': 7,
 'Blocked': 5,
 'Cancelled': 5}
STATE_LABELS = {'Pending': '待处理',
 'Sili': '司礼监',
 'Zhongshu': '中书省',
 'Menxia': '门下省',
 'Assigned': '尚书省',
 'Next': '待执行',
 'Doing': '执行中',
 'Review': '审查',
 'Done': '完成',
 'Blocked': '阻塞',
 'Cancelled': '已取消'}
UI_STATE_LABELS = {'Pending': '待处理',
 'Sili': '司礼监分办',
 'Zhongshu': '中书起草',
 'Menxia': '门下审议',
 'Assigned': '已派发',
 'Next': '待执行',
 'Doing': '执行中',
 'Review': '待审查',
 'Done': '已完成',
 'Blocked': '阻塞',
 'Cancelled': '已取消',
 'Inbox': '收件'}
STATE_DEFAULT_ORG = {'Pending': '皇上',
 'Sili': '司礼监',
 'Zhongshu': '中书省',
 'Menxia': '门下省',
 'Assigned': '尚书省',
 'Review': '尚书省',
 'Done': '皇上',
 'Blocked': '阻塞',
 'Cancelled': '皇上'}
STATE_OWNER_AGENT = {'Pending': 'sili',
 'Sili': 'sili',
 'Zhongshu': 'zhongshu',
 'Menxia': 'menxia',
 'Assigned': 'shangshu',
 'Review': 'shangshu'}
STATE_DISPATCH_AGENT = {'Sili': 'sili',
 'Zhongshu': 'zhongshu',
 'Menxia': 'menxia',
 'Assigned': 'shangshu',
 'Review': 'shangshu'}
CENTRAL_QUEUE_STATES = {'Menxia': 'menxia', 'Assigned': 'shangshu', 'Review': 'shangshu'}
CENTRAL_QUEUE_SLA = {'standard': {'Menxia': 1800, 'Assigned': 1200, 'Review': 1200},
 'fast': {'Menxia': 480, 'Assigned': 300, 'Review': 480}}
VALID_TRANSITIONS = {'Pending': ['Sili', 'Cancelled'],
 'Sili': ['Zhongshu', 'Cancelled'],
 'Zhongshu': ['Menxia', 'Blocked', 'Cancelled'],
 'Menxia': ['Assigned', 'Zhongshu', 'Cancelled'],
 'Assigned': ['Next', 'Doing', 'Blocked', 'Cancelled'],
 'Next': ['Doing', 'Blocked', 'Cancelled'],
 'Doing': ['Review', 'Blocked', 'Cancelled'],
 'Review': ['Done', 'Doing', 'Cancelled'],
 'Blocked': ['Pending', 'Sili', 'Zhongshu', 'Menxia', 'Assigned', 'Next', 'Doing', 'Review'],
 'Done': [],
 'Cancelled': []}
MANUAL_ADVANCE_FLOW = {'Pending': ('Sili', '皇上', '司礼监', '待处理旨意转交司礼监分办'),
 'Sili': ('Zhongshu', '司礼监', '中书省', '司礼监分办完毕，转中书省起草'),
 'Zhongshu': ('Menxia', '中书省', '门下省', '中书省方案提交门下省审议'),
 'Menxia': ('Assigned', '门下省', '尚书省', '门下省准奏，转尚书省派发'),
 'Assigned': ('Doing', '尚书省', '__execution__', '尚书省开始派发执行'),
 'Next': ('Doing', '__execution__', '__execution__', '待执行任务开始执行'),
 'Doing': ('Review', '__execution__', '尚书省', '执行部门完成，移交尚书省审查汇总'),
 'Review': ('Done', '尚书省', '皇上', '全流程完成，回奏皇上')}
ORG_AGENT_MAP = {'礼部': 'libu', '户部': 'hubu', '兵部': 'bingbu', '刑部': 'xingbu', '工部': 'gongbu', '吏部': 'libu_hr'}
AGENT_ORG_MAP = {'libu': '礼部', 'hubu': '户部', 'bingbu': '兵部', 'xingbu': '刑部', 'gongbu': '工部', 'libu_hr': '吏部'}
DEFAULT_ALLOW_AGENTS = {'sili': ['zhongshu'],
 'zhongshu': ['menxia', 'shangshu'],
 'menxia': ['shangshu', 'zhongshu'],
 'shangshu': ['hubu', 'libu', 'bingbu', 'xingbu', 'gongbu', 'libu_hr'],
 'libu': ['shangshu'],
 'hubu': ['shangshu'],
 'bingbu': ['shangshu'],
 'xingbu': ['shangshu'],
 'gongbu': ['shangshu'],
 'libu_hr': ['shangshu'],
 'zaochao': []}
CONSULTATION_ALLOW_AGENTS = {'zhongshu': ['menxia', 'shangshu'],
 'menxia': ['zhongshu', 'shangshu'],
 'shangshu': ['menxia', 'hubu', 'libu', 'bingbu', 'xingbu', 'gongbu', 'libu_hr'],
 'libu': ['shangshu'],
 'hubu': ['shangshu'],
 'bingbu': ['shangshu'],
 'xingbu': ['shangshu'],
 'gongbu': ['shangshu'],
 'libu_hr': ['shangshu']}
AGENT_DIRECTORY = [{'id': 'sili', 'label': '司礼监', 'emoji': '🧾', 'role': '掌印秉笔', 'rank': '内廷'},
 {'id': 'zhongshu', 'label': '中书省', 'emoji': '📜', 'role': '中书令', 'rank': '正一品'},
 {'id': 'menxia', 'label': '门下省', 'emoji': '🔍', 'role': '侍中', 'rank': '正一品'},
 {'id': 'shangshu', 'label': '尚书省', 'emoji': '📮', 'role': '尚书令', 'rank': '正一品'},
 {'id': 'libu', 'label': '礼部', 'emoji': '📝', 'role': '礼部尚书', 'rank': '正二品'},
 {'id': 'hubu', 'label': '户部', 'emoji': '💰', 'role': '户部尚书', 'rank': '正二品'},
 {'id': 'bingbu', 'label': '兵部', 'emoji': '⚔️', 'role': '兵部尚书', 'rank': '正二品'},
 {'id': 'xingbu', 'label': '刑部', 'emoji': '⚖️', 'role': '刑部尚书', 'rank': '正二品'},
 {'id': 'gongbu', 'label': '工部', 'emoji': '🔧', 'role': '工部尚书', 'rank': '正二品'},
 {'id': 'libu_hr', 'label': '吏部', 'emoji': '👔', 'role': '吏部尚书', 'rank': '正二品'},
 {'id': 'zaochao', 'label': '钦天监', 'emoji': '📰', 'role': '朝报官', 'rank': '正三品'}]
DEPT_COLOR = {'司礼监': '#e8a040',
 '中书省': '#a07aff',
 '门下省': '#6a9eff',
 '尚书省': '#6aef9a',
 '礼部': '#f5c842',
 '户部': '#ff9a6a',
 '兵部': '#ff5270',
 '刑部': '#cc4444',
 '工部': '#44aaff',
 '吏部': '#9b59b6',
 '皇上': '#ffd700',
 '回奏': '#2ecc8a'}
