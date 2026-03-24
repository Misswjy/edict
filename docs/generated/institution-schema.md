# Institution Schema

该文档片段由 `config/institution_schema.json` 自动生成。

## 状态定义

| 状态 | 契约标签 | UI 标签 | 默认归属 |
| --- | --- | --- | --- |
| `Pending` | 待处理 | 待处理 | 皇上 |
| `Sili` | 司礼监 | 司礼监分办 | 司礼监 |
| `Zhongshu` | 中书省 | 中书起草 | 中书省 |
| `Menxia` | 门下省 | 门下审议 | 门下省 |
| `Assigned` | 尚书省 | 已派发 | 尚书省 |
| `Next` | 待执行 | 待执行 | 执行部门 |
| `Doing` | 执行中 | 执行中 | 执行部门 |
| `Review` | 审查 | 待审查 | 尚书省 |
| `Done` | 完成 | 已完成 | 皇上 |
| `Blocked` | 阻塞 | 阻塞 | 阻塞 |
| `Cancelled` | 已取消 | 已取消 | 皇上 |

## Agent 定义

| Agent | 官署 | 角色 | 可派发给 | 可咨询 |
| --- | --- | --- | --- | --- |
| `sili` | 司礼监 | 掌印秉笔 | zhongshu | - |
| `zhongshu` | 中书省 | 中书令 | menxia, shangshu | menxia, shangshu |
| `menxia` | 门下省 | 侍中 | shangshu, zhongshu | zhongshu, shangshu |
| `shangshu` | 尚书省 | 尚书令 | hubu, libu, bingbu, xingbu, gongbu, libu_hr | menxia, hubu, libu, bingbu, xingbu, gongbu, libu_hr |
| `libu` | 礼部 | 礼部尚书 | shangshu | shangshu |
| `hubu` | 户部 | 户部尚书 | shangshu | shangshu |
| `bingbu` | 兵部 | 兵部尚书 | shangshu | shangshu |
| `xingbu` | 刑部 | 刑部尚书 | shangshu | shangshu |
| `gongbu` | 工部 | 工部尚书 | shangshu | shangshu |
| `libu_hr` | 吏部 | 吏部尚书 | shangshu | shangshu |
| `zaochao` | 钦天监 | 朝报官 | - | - |
