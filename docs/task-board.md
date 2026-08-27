# Task Board（2026-08-23）

> 权威职责：Task / Owner / 依赖 / Allowed Paths / Acceptance。由 Coordinator 维护。
> 状态语义：BACKLOG → READY → IN_PROGRESS → REVIEW → DONE；异常：BLOCKED。

## 当前任务

| ID | Task | Status | Owner | Depends On | Allowed Paths | Acceptance |
|---|---|---|---|---|---|---|
| SNA-01 | P0 数据源加固：接入 Tushare Pro（399006 日线 / 旭创日线 / 创业板50 PE 付费源优先通道） | **REVIEW**（验收①-④达成，commit 8a25a47；⑤待 token，见 ND-002） | agent | — | `src/strategy/data.py`, `src/strategy/fund_data.py`, `src/strategy/index_pe.py`, `tests/**` | ① token 配置时 loader 优先走 Tushare 且返回同构 DataFrame；② token 缺失/过期自动降级免费源且不抛错；③ 新增 mock 测试覆盖双通道；④ `pytest -m unit` 全绿；⑤ 399006 日线拉取与新浪源做一致性抽查（近 60 日） |
| SNA-02 | P1 数据源加固：期货基差 + 资金流替代通道 | **READY** | — | SNA-01（代码侧已满足） | `src/strategy/margin_data.py`, `src/strategy/chinext_factors.py`, `tests/**` | 中金所/Tushare 基差源可用；东财失败时自动切换；mock 测试 + unit 全绿 |
| SNA-03 | P2 数据源加固：外盘（Stooq/雅虎双源）+ 估值（Tushare index_dailybasic） | BACKLOG | — | SNA-01 | `src/strategy/overseas.py`, `src/strategy/index_pe.py`, `tests/**` | 双源冗余 + 自动降级；mock 测试 + unit 全绿 |
| SNA-04 | 复杂度治理：`_is_same_event` 重构降风险（`real_time_push.py` L803-962） | **DONE**（commit b2391c1） | agent | — | `scripts/real_time_push.py`, `tests/**` | 行为等价重构；diff 审查通过；去重专项测试全绿；unit 全绿 |
| SNA-05 | 数据源健康度监控：连续 N 轮付费源失败 → 推送告警 | BACKLOG | — | SNA-01 | `scripts/factor_collector.py`, `src/tools/push.py`, `tests/**` | 失败计数 + 阈值触发告警；mock 测试全绿 |

## 领取规则

- 只有 `READY` 且依赖满足的任务可领取（当前 SNA-02）。
- 领取时由 Coordinator 更新 Owner / branch / worktree / Base Commit 并提交协调状态。
- 一个 Task = 一个 Owner + 一个 branch + 一个 worktree。
- Worker 不直接在 main 开发。

## 历史（DONE）

| ID | Task | 合入 commit |
|---|---|---|
| SNA-04 | 复杂度治理：`_is_same_event` 行为等价重构（174 行深嵌套 → 6 个规则族函数，203 去重专项 + 895 unit 全绿） | b2391c1 |
