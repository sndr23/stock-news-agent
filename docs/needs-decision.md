# Needs Decision（2026-08-23）

> 权威职责：等待用户决定的问题与恢复条件。由 Coordinator 维护。
> 产生规则：产品行为重定 / 核心 Domain 或数据语义改变 / 重要架构决策 / 多个合理方案无法取舍 / 重大技术路线。

## OPEN

| ID | 日期 | 问题 | 恢复条件 |
|---|---|---|---|
| ND-002 | 2026-08-27 | SNA-01 验收⑤（399006 Tushare vs 新浪 60 日一致性抽查）需要真实 Tushare token；同时 SNA-02 期货基差 Tushare 通道、SNA-05 健康度监控也依赖该 token 才能线上生效。代码侧已全部就绪且零 token 时自动降级免费源（无 token 不影响现有生产）。用户需注册 Tushare Pro（tushare.pro，积分制）并提供 token | 用户提供 `TUSHARE_TOKEN`（本地设环境变量 / 云端配 GitHub Secrets）→ 执行验收⑤抽查 → SNA-01 关闭，SNA-02/SNA-05 云端接线生效 |

## DONE / 已决策

| ID | 日期 | 问题 | 决策 | Related Task |
|---|---|---|---|---|
| ND-001 | 2026-08-23 | 工作流接入时选择目标项目与定时唤醒载体 | 目标项目 = 量化监测agent（stock-news-agent）；定时唤醒 = ChatGPT 定时任务 | — |

## 规则

- 新决策：创建 `ND-xxx` → 对应 Task → `BLOCKED` → 写清问题/方案/影响/推荐/恢复条件 → 保存安全进度 → 停止本轮，不替用户决定。
- 普通 bug、可由文档推导的实现细节、局部可逆选择、普通依赖阻塞**不进入**本表。
