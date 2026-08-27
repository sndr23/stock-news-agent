# Status（2026-08-27）

> 权威职责：当前阶段 / 当前事实 / 唯一 NEXT。由 Coordinator 维护。

## 当前阶段

**Stage 2：生产稳定 + 数据源加固**（已从 Stage 1 全链路跑通进入加固期）

## 当前事实

- 生产入口全部运行正常：实时推送（30min）、因子采集（15min）、创业板择时（14:45）、中信持仓日报（17:00）、盘前盘后简报、周六信号回测。
- 创业板择时系统为 2026-08-22 核心：核心层 10 因子 + 缠论 + 旭创双确认 + 外盘辅助确认 → 目标仓位 0/60/90/100%。
- 回测（生产配置口径，2026-08-22 纠错）：2014-07~2026-08 累计 +188.8%（持有 +171.3%），夏普 0.60，卡玛 0.22；基线（无 ERP 估值滤波）+291.9%。
- **审慎解读**：超额主要来自熊市低仓位（平均仓位约 52%）而非方向预测；权重/阈值同一样本 in-sample 寻优，存在过拟合风险。
- 测试门禁：910 单元测试（`pytest -m unit`）通过。
- 工程审查（2026-08-22 第三轮）：Gist 读-改-写、config.py import 副作用、run_once 挂起条目三处均已防御到位。
- **SNA-01 已合入 main（commit 8a25a47）**：399006 日线 / 旭创日线 / 创业板50 PE 三链 Tushare Pro 优先通道，token 缺失/失效自动降级免费源（当前无 token，生产行为与此前一致）；验收①-④达成，⑤一致性抽查待 token（ND-002）。
- **SNA-04 已合入 main（commit b2391c1）**：`_is_same_event` 行为等价重构（174 行深嵌套 → 6 个规则族函数），203 去重专项测试全绿。
- **SNA-02 代码+测试完成（待 commit）**：期货基差 新浪→中金所、资金流 东财→Tushare 聚合双降级链（`scripts/factor_collector.py`），15 专项 mock 测试全绿；Tushare 资金流通道待 token 云端生效。
- 数据源现状：免费 HTTP 抓取为主 + Tushare 优先通道（待 token 激活）；东财 push2his 已被反爬（实证 RemoteDisconnected）。

## 未提交工作（保护项）

- `scripts/_backtest_1m.py`、`scripts/_backtest_2m.py`、`scripts/_bt_realtime_align.py`、`scripts/_diag_critic.py`、`scripts/_exp_factors.py`、`scripts/_exp_state.py`、`scripts/_opt_maxret.py` 等临时脚本为未提交状态，**不得覆盖/删除**；如需处理先询问用户。

## 唯一 NEXT

**SNA-02 集成收尾**：commit + 自 review 后合入（Integrator 角色），随后排期 SNA-03（外盘双源）或 SNA-05（数据源健康度告警）。领取入口见 `docs/task-board.md`。

## 待用户决策

- ND-002（OPEN）：Tushare token 提供 → 解锁 SNA-01 验收⑤ + SNA-02/SNA-05 云端生效（见 `docs/needs-decision.md`）。
