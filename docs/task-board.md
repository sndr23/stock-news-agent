# Task Board（2026-08-28）

> 权威职责：Task / Owner / 依赖 / Allowed Paths / Acceptance。由 Coordinator 维护。
> 状态语义：BACKLOG → READY → IN_PROGRESS → REVIEW → DONE；异常：BLOCKED。

## 当前任务

| ID | Task | Status | Owner | Depends On | Allowed Paths | Acceptance |
|---|---|---|---|---|---|---|
| SNA-01 | P0 数据源加固：免费接口与缓存链（399006 日线 / 旭创日线 / 创业板50 PE） | **DONE**（新浪/东财/腾讯/乐咕链路已验证） | agent | — | `src/strategy/data.py`, `src/strategy/index_pe.py`, `tests/**` | 免费源成功返回同构数据；失败按既有回退链运行；unit 全绿 |
| SNA-02 | P1 数据源加固：期货基差 + 资金流免费链 | **DONE**（新浪→中金所；东财资金流） | agent | SNA-01 | `scripts/factor_collector.py`, `tests/**` | 期货主链失败时中金所补缺；资金流失败时独立缺省；unit 全绿 |
| SNA-03 | P2 数据源加固：外盘（Yahoo/Stooq 免费回退）+ 免费估值稳定性 | **DONE**（代码、异常降级、缓存边界和 mock 已验收；外部站点无 SLA） | agent | SNA-01 | `src/strategy/overseas.py`, `src/strategy/index_pe.py`, `tests/**` | 独立免费回退 + 失败降级；mock 测试 + unit 全绿 |
| SNA-04 | 复杂度治理：`_is_same_event` 重构降风险（`real_time_push.py` L803-962） | **DONE**（commit b2391c1） | agent | — | `scripts/real_time_push.py`, `tests/**` | 行为等价重构；diff 审查通过；去重专项测试全绿；unit 全绿 |
| SNA-05 | 数据源健康度监控：连续 N 轮免费源失败 → 推送告警 | **DONE**（连续 3 轮；`--collect` 告警、普通异动仍停推） | agent | SNA-01 | `scripts/factor_collector.py`, `.github/workflows/realtime-factor.yml`, `tests/**` | 失败计数 + 阈值触发告警；恢复清零；push 失败可重试；unit 全绿 |
| STR-01 | 换手优化：`UPGRADE_CONFIRM_DAYS` 2→3（V2） | **DONE**（2026-08-28 用户拍板不合入，ND-002 关闭；维持"不考虑交易费"口径，参数不变） | agent | — | `src/strategy/chinext_timing.py` 常量行, `docs/**` | 已出 OOS 证据（fee=0.3% 双优/换手-50%，fee=0 负贡献）；决策=不合入 |
| STR-04 | v5.1 d日快照口径 + 权重重配 T.35/P.25 | **DONE**（2026-08-29 用户拍板；工作区已验收未单独提交） | agent | — | `scripts/run_chinext_timing.py`, `tests/**`, `docs/**` | 新基线回测 +156.0%/夏普0.60/回撤-42.5%；OOS +89.5%/夏普0.54/回撤-27.0%，卡玛差距-0.08；门禁 1092 passed + 2 个周末日期敏感既有失败（非本次回归） |
| STR-02 | 阴跌期降档：需与趋势因子不同源的避险信号研究 | **BACKLOG**（本轮候选均不达门槛；事件硬信号 IC 验门亦未通过） | agent | STR-01 | `scripts/_*.py`（研究） | 回撤显著下降且收益不劣化；walk-forward OOS 验证；unit 全绿 |
| STR-03 | v5 权重合入：趋势 .35→.50、落袋 .15→.10、估值死权重清零 | **DONE**（2026-08-28 用户拍板合入；工作区已验收未单独提交，与 SNA-03/05 同模式） | agent | — | `scripts/run_chinext_timing.py:755`, `docs/**` | 新基线严格回测 +153.0%/夏普0.58/回撤-41.1%；OOS +106.1%/夏普0.59/回撤-27.0%；拐点倒U+邻域高原+分折胜率6/9；门禁 `1094 passed` 全绿 |

## 领取规则

- 只有 `READY` 且依赖满足的任务可领取（当前无 READY；STR-02 BACKLOG，SNA-01~05/STR-01/STR-03 均已完成，进入留出样本观察）。
- 领取时由 Coordinator 更新 Owner / branch / worktree / Base Commit 并提交协调状态。
- 一个 Task = 一个 Owner + 一个 branch + 一个 worktree。
- Worker 不直接在 main 开发。

## 历史（DONE）

| ID | Task | 合入 commit |
|---|---|---|
| SNA-02 | P1 数据源加固：期货基差免费补缺 + 东财资金流（15 专项 mock + unit 全绿） | 0579220 |
| SNA-04 | 复杂度治理：`_is_same_event` 行为等价重构（174 行深嵌套 → 6 个规则族函数，203 去重专项 + 895 unit 全绿） | b2391c1 |
| SNA-05 | 数据源健康度告警：连续 3 轮失败触发一次告警，恢复清零；`--collect` 仅采集并允许故障告警（959 unit 全绿） | 工作区已验收（未单独提交） |
| SNA-03 | 外盘 Yahoo/Stooq 免费回退、独立新鲜度校验；乐咕 PE 字段/缺失/缓存稳定性（定向回归 + 959 unit 全绿） | 工作区已验收（未单独提交） |
