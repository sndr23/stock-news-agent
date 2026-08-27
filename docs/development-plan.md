# Development Plan — SNA-01（P0 数据源加固：Tushare Pro 接入）

> 权威职责：当前 READY Task 的详细执行合同。由 Coordinator 与 Worker 共同维护。
> 状态：**已执行**（commit 8a25a47，2026-08-27；验收①-④达成，⑤一致性抽查待 token——见 ND-002）
> 注：实际实现中个股链 Tushare 通道放在 `src/strategy/data.py`（`load_stock_primary`，与指数链共用 `_tushare_client` 单例），未动 `fund_data.py`；创业板50 PE 的 Tushare 通道按口径评估定位为**备份源**而非主源（index_dailybasic 整体法 PE 与乐咕 TTM 存在系统性口径差，主源保持乐咕以稳定回测口径，详见 `index_pe.py::_fetch_pe_tushare` docstring）。

## 目标

将 399006 日线 / 中际旭创日线 / 创业板50 TTM PE 三个核心数据点从"免费 HTTP 抓取"升级为"付费源（Tushare Pro）优先 → 免费源降级"双通道，免费源永不删除。

## 输入

- `docs/数据源加固方案_20260822.md`（P0 章节）
- 现有 loader：`src/strategy/data.py`（指数日线）、`src/strategy/fund_data.py`（个股）、`src/strategy/index_pe.py`（PE）
- 环境变量：`TUSHARE_TOKEN`（新）

## 输出（改动范围）

- `src/strategy/data.py`：`_fetch_index_full_frame` 系列 loader 增加 Tushare 优先通道
- `src/strategy/fund_data.py`：中际旭创日线 loader 增加 Tushare 优先通道
- `src/strategy/index_pe.py`：创业板50 TTM PE loader 增加 Tushare `index_dailybasic` 优先通道
- `tests/**`：新增 mock 测试覆盖双通道与降级
- `docs/数据源加固方案_20260822.md`：标记 P0 实施状态

## 边界

- **不改业务层**：所有 loader 保持返回同构 DataFrame，调用方零改动。
- **不改** `requirements.txt` 之外的必要依赖（如 tushare 包需要加，记录在 Task Notes）。
- **不删免费源**：付费源只是降级链头部。
- **不进 Git 的**：真实 token（只存环境变量）。

## Acceptance（DONE 门槛）

1. token 配置时，loader 优先走 Tushare 且返回同构 DataFrame。
2. token 缺失 / 过期 / 网络失败 → 自动降级免费源，不抛错、不无信号。
3. 新增 mock 测试覆盖双通道（有 token / 无 token / token 失效）。
4. `python -m pytest tests/ -q -m "unit"` 全绿。
5. 399006 日线一致性抽查：Tushare 与新浪源近 60 日 close 序列对齐（容忍除权/停牌日差异）。
6. 文档同步：`docs/status.md` NEXT 更新、`docs/task-board.md` SNA-01 → DONE。

## 验证方法

- 单元：`pytest -m unit`
- 集成抽查：设置测试 token 后 `python -c "from src.strategy.data import *; ..."` 拉取 399006 与新浪对比
- 回归：择时系统 `python scripts/run_chinext_timing.py --backtest` 数字不低于基线

## 风险 / 注意

- Tushare 积分制：基础日线接口免费积分足够个人日频使用；token 需用户注册获取。
- 若用户暂无 token：SNA-01 可拆为"先实现双通道框架 + mock 验证"，真实 token 联调待用户提供（记录 ND 或 Task Notes）。
