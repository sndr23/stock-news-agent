# 方法与同源审计

## 回测入口与生产打分路径

`scripts/chinext_timing_backtest.py:30-46` 是影子记录质量周报入口，读取已保存的 `chinext_timing_state.json`；本次数值回测使用生产入口 `scripts/run_chinext_timing.py:1151-1276` 的 `backtest_metrics` / `run_backtest`。

生产回测在 `run_chinext_timing.py:1187-1188` 调用 `cf.core_signals` 和 `cf.dimension_score`，在 `:1211` 调用 `ct.decide_position`。实时 `score_all` 在 `:397-404` 复用同一核心函数。Amihud 本体位于 `src/strategy/chinext_factors.py:125-138`，维度等权与权重位于 `:316-323`、`:346-367`；档位状态机位于 `src/strategy/chinext_timing.py:324-379`。A/B/C 对照仅 monkeypatch `cf.factor_amihud`，没有改生产文件或并行重写策略。

回测仅含可由日线复现的核心层。生产代码在 `run_chinext_timing.py:1164-1165` 明确说明 d 日完整收盘和全天成交量是 14:45 快照的近似；实时修正层、缠论和个股双确认没有混入核心回测。

## 数据、样本与费用

- 数据：399006 新浪量价 CSV，3,000 根，2014-06-03 至 2026-09-29；金额列单位为 shares。输入副本和 SHA-256 见 `evidence/input_manifest.txt`。
- 评估下限：2019-01-01 后首个交易日 2019-01-02；决策日到 2026-09-28，收益落到 2026-09-29，共 1,878 个有效收益日。
- 2019-01-02 之前保留 1,121 根 warmup bar，超过 300 根门槛。生产函数默认 warmup 是 60；显式 `eval_start=1121` 使评估起点前的完整序列用于全部滚动指标。
- 为让生产函数把数据末根 2026-09-29 作为已收盘历史 bar，审计进程仅将 `run_chinext_timing.datetime.now` 冻结在 2026-09-30 08:00 +08:00；数据本身未改写。
- 费用跑了两档：0 和每单位仓位变动 0.0005（单边 5bp；满仓进出合计 10bp）。决策不读取费用，非零费率只作用于换仓净值。

## 预先声明的映射

| 代号 | Amihud 映射 | 定义 |
|---|---|---|
| A | 生产 clip | `clip(-z/2, -1, 1)` |
| B | 5%/95% winsorize | 先以此前 60 根 illiq 计算 5%/95% 界限，winsorize参考窗口和当日观测，再算 z 并 clip |
| C | 去 clip | 与 A 共用生产 60 日 z，映射为 `tanh(-z/2)` |

所有指标由 `evidence/metrics.csv`、逐日决策由 `evidence/daily_A.csv`、`daily_B_5_95.csv`、`daily_C.csv` 承载；运行 stdout 原文为 `evidence/run_audit_stdout.txt`。两条保留快照源于旧审计的原始 stdout，只用于锚点和单日口径对照，解析后的输入列于 `evidence/snapshot_inputs.csv`。
