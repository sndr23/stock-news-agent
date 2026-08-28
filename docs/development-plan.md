# Development Plan — 免费数据源加固

> 权威职责：当前 READY Task 的详细执行合同。由 Coordinator 与 Worker 共同维护。
> 状态：**已执行**（2026-08-27；免费数据源链路保留并完成回退验证）
> 注：生产只使用免费公开接口，不保存、不读取付费数据源 token。

## 目标

稳定 399006 日线 / 中际旭创日线 / 创业板50 TTM PE 三个核心数据点的免费数据源链路，免费源失败时按既有回退路径运行，缺失增强维度不阻断主信号。

## 输入

- `docs/数据源加固方案_20260822.md`（P0 章节）
- 现有 loader：`src/strategy/data.py`（指数日线）、`src/strategy/fund_data.py`（个股）、`src/strategy/index_pe.py`（PE）
- 环境变量：无新增数据源鉴权配置

## 输出（改动范围）

- `src/strategy/data.py`：保留新浪全量/个股、东财和腾讯免费回退链
- `src/strategy/index_pe.py`：保留乐咕创业板50 TTM PE，失败时估值维度降为 0
- `scripts/factor_collector.py`：保留新浪→中金所期货链、东财资金流链
- `tests/**`：覆盖免费源成功、失败和独立维度降级

## 边界

- **不改业务层**：所有 loader 保持返回同构 DataFrame，调用方零改动。
- **不改** `requirements.txt` 之外的必要依赖；不引入需要鉴权的行情接口。
- **不删免费源**：所有免费接口和本地缓存都保留在降级链中。
- **不进 Git 的**：真实 token（只存环境变量）。

## Acceptance（DONE 门槛）

1. 免费源成功时返回现有调用方需要的同构数据。
2. 免费接口网络失败或返回空数据时按既有回退链运行，不抛错、不无信号。
3. 主信号数据失败时明确退出，不推送伪造信号；增强维度失败时独立降为 0。
4. `python -m pytest tests/ -q -m "unit"` 全绿。
5. 文档同步：免费数据源链路、降级语义和运行状态保持一致。

## 验证方法

- 单元：`pytest -m unit`
- 集成抽查：运行 `python scripts/run_chinext_timing.py --dry-run`，检查 399006、旭创、因子快照和估值缺失时的降级提示
- 回归：择时系统 `python scripts/run_chinext_timing.py --backtest` 使用 d-1 最近完整收盘信息，输出结果可复现并如实记录；不以历史宽松口径收益作为硬门槛

## 风险 / 注意

- 免费接口存在限流、字段漂移和临时不可达风险，需依赖缓存与多源回退。
- 估值和资金流属于增强维度，源失败时不改变核心层主信号。
