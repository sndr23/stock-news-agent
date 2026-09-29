# Amihud 地板效应审计留档（2026-09-29）

**结论一句话**：Amihud 地板效应已定性为**噪音**，生产系统**无需修改**，勿重复重开同一问题。

**权威结论位置**：`~/.hermes/skills/software-development/stock-news-agent-debugging/SKILL.md`
的「Amihud 地板效应已定性（2026-09-29）」一节 —— **该节是唯一权威，本目录仅为原始证据**。
skill 内已写明下列限制与作废条件；数据条件变化时以 skill 的翻转条件为准。

## 一、结论与判定

| 档 | 累计收益 | 年化 | 最大回撤 | 夏普 |
|---|---|---|---|---|
| **A 基线（现生产）** | +178.65% | 14.24% | -32.34% | 0.746 |
| B winsorize 5/95 | +172.82% | 13.93% | -31.22% | 0.734 |
| C 去 clip (tanh) | +177.65% | 14.19% | -32.34% | 0.744 |
| 买入持有 | +155.75% | — | **-57.05%** | — |

**判定**：**两者等价，判别力不足**。措辞不可改写为"证明等价"——A vs C 年化差 0.044pp，
HAC(20) p=0.569，观察效应功效仅 8.8%，80% 功效所需 MDE ≈ 0.218pp 年化，**测不出差异**。

**事件研究**：106 次触地板事件，t+1 / +3 / +5 / +10 收益检验全部不显著
（p = 0.27 / 0.10 / 0.22 / 0.15，block-bootstrap 95% CI 全部跨 0）。
A 与 C 全样本仅 **5 天**仓位不同，且 **5 天全部不是触地板日**。

## 二、机制（为何会跳分）

`factor_amihud` = `|ret| / (amount/1e9)` → 60 日滚动 z → `clip(-z/2, -1, 1)`。

- **地板单边**：近 500 交易日触地板 -1.000 共 33 次，触天花板 +1.000 共 **0** 次
- 暴跌日因子锁死 -1，次日**必然机械解冻**回正值
- 单日涨跌幅 [-4.5%, +4.5%] 可撬动 core **0.194 分 = 距首档距离 0.089 的 2.2 倍**
- 因子峰值在当日 **0%**（平盘流动性最好），不是上涨日

**2026-09-28/29 实例**：09-28 跌 -4.53% → Amihud -1.000、core -0.770；
09-29 涨 +0.17% → Amihud +0.554、core -0.555。core 的 +0.215 中
**Amihud 占 70.5%**、量价象限 23.3%、波动期限 6.2%；
而 `trend_ma20_60` / `trend_momentum_60` / `pullback_52w` / `dd60`
**四项持续性因子两日全部 -1.000，一分未动**。分数跳 0.47 而市场结构零改善。

## 三、限制（勿当盲区）

1. **历史只有 1 天**（2026-09-29）真实 14:46 盘中快照可与日线代理对照。
   当日 14:45 口径 vs 全日口径：close 差 +0.078%、amount 差 -8.54%、**illiq 差 +107%**。
   **结论只对完整日线代理口径成立，对真实盘中口径外推可信度低。**
2. 全样本统一缩放成交量（0.9375 / 0.922）三档排序不变，但 **z 标准化会抵消统一缩放**，
   该检验**不能代替真实的日内成交分布**。
3. 回测口径为 14:45 决策 / 15:00 收盘成交（`next_ret_basis = execution_close_to_next_close`）。
   项目已在 `scripts/run_chinext_timing.py:272-274` 注释写明"回测需明确这是完整日量的近似"。

## 四、作废条件（触发任一必须按原流程重判）

1. 项目开始**持续留存每日 14:45 快照**并积累了足够市场阶段（跨越牛熊与流动性 regimes）—— 最可能的触发点
2. 单边费率 / 换手假设变化，使 A 不再跑赢买入持有
3. `factor_amihud` 实现、clip 阈值、量价/波动/落袋任一维权重被改动

重判须走三档对比 + 事件研究 + 买入持有基准 + 过拟合五项检验，
锚点必须复现 09-28 core = -0.770、09-29 快照 core = -0.555。**禁止只跑一档就下结论。**

## 五、目录说明

代码版本 **commit 9ce7904**（生产实际运行版本），隔离副本由 `git archive` 导出。

```
audit_20260929_amihud_floor/
├── SUMMARY.md                  最终判定 + 验收清单
├── 0_method.md                 回测入口判断、生产同源证据、样本区间、warmup、费率、三档定义
├── A_caliber_symmetry.md        口径偏差量化 + 双口径三档排序对比
├── B_event_study.md             106 次触地板事件研究
├── C_backtest_compare.md        三档对比（逐年表、买入持有、仓位不一致清单）
├── D_overfit.md                 过拟合检验 O1-O5
├── E_lookahead.md               前视检查 L2-L6 + 复权口径
└── evidence/                    25 份原始证据
    ├── anchor_results.csv           四个锚点（delta 全 0.0）
    ├── metrics.csv                  三档 × 两档费率完整指标
    ├── event_statistics.csv         事件检验（Welch t / p / bootstrap CI）
    ├── floor_events.csv             106 次事件明细
    ├── floor_events_by_year.csv     逐年事件数
    ├── positions_A_vs_C_differences.csv  A−C 仓位不一致 5 日清单
    ├── caliber_symmetry.csv         全样本口径对称性
    ├── caliber_snapshot_comparison.csv   09-29 双口径实测差
    ├── A_vs_C_power.csv             检验功效与 MDE
    ├── lookahead_evidence.txt       L2-L6 现场输出
    ├── daily_A.csv / daily_B_5_95.csv / daily_C.csv   三档逐日序列
    ├── market_399006_sina_3000.csv  行情输入（399006 日线 3000 根）
    ├── input_manifest.txt           输入清单
    ├── run_audit.py / run_audit_stdout.txt   回测脚本与全部现场输出
    └── verify_markdown.py / markdown_integrity.txt  编码完整性校验
```

**关键锚点**（`anchor_results.csv`，delta 全 0.0，验证回测与生产同源）：

| date | mode | core |
|---|---|---|
| 2026-09-28 | full_day | -0.770 |
| 2026-09-28 | retained_snapshot | -0.770 |
| 2026-09-29 | full_day | -0.550 |
| 2026-09-29 | retained_snapshot | -0.555 |

样本：2019-01-02 ~ 2026-09-28，**1878 收益日 + 1121 预热日线**，费率 0 与单边 5bp 两档。
