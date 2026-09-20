# 创业板量化择时系统深度优化分析（只分析，不改代码）

> 分析对象：`stock-news-agent / 量化监测agent` 创业板择时 v5.1。
> 分析基准 commit：工作区当前 HEAD（`_git_commit()` 记录的版本）。
> 口径声明：本报告只做静态代码与文档审计，**未运行任何回测、未改动任何源文件**。所有结论均给出「文件:行号 + 代码片段」证据，供后续独立复现。
> 关键规则遵循：任何策略改动必须附带回测且不低于基线；OOS 期间不调参（`AGENTS.md`、`docs/status.md` 唯一 NEXT）。

---

## 1. 系统现状速览

### 1.1 信号链路图（数据 → 因子 → 打分 → 档位 → 推送）

```
┌─ 数据层 ────────────────────────────────────────────────────────────────┐
│ 399006 日线:  新浪全量(12年) ──失败──▶ 东财增量 load_index_daily_full   │
│                (load_index_sina, data.py:613)      (data.py:765)        │
│ 当日 partial:  腾讯 fetch_intraday_bar_tencent (data.py:704)            │
│                └─ 拼入日线末根 _append_intraday_bar_if_needed(run:1270) │
│ 修正层快照:    factor_state(basis/flows/sentiment/breadth/option/risk)   │
│ 中信持仓:      citic_pos_state                                          │
│ 资讯:          real_time_state(已推强档 ∪ 当日候选)                     │
│ 外盘:          overseas(Sina→Yahoo→Stooq) 隔夜最差跌幅                  │
│ 个股:          中际旭创 300308 (新浪→腾讯→东财)                         │
│ 估值:          创业板50 TTM PE(乐咕)                                    │
└────────────────────────────────────────────────────────────────────────┘
                                   │  gather_context (run:141)
                                   ▼
┌─ 核心层（可回测，权重=1.0）─────────────────────────────────────────────┐
│ cf.core_signals(closes, amounts)                       (factors.py:296) │
│   9 注册因子 / 8 有效（value_erp 生产不注入 → 恒 0）                    │
│   cf.dimension_score(…, CHINEXT_V51_WEIGHTS)           (factors.py:346) │
│   趋势0.35 + 量价0.20 + 波动0.20 + 估值0.00 + 落袋0.25                  │
└────────────────────────────────────────────────────────────────────────┘
                                   │ core["score"] ∈ [-1,1]
                                   ▼
┌─ 修正层（不可回测，有界 ±0.30）_dimension_modifier (run:308) ───────────┐
│   贴水±0.06 + 资金±0.05 + 情绪±0.04 + 资讯±0.06                        │
│   + 缠论 ±0.08 (_chan_signal run:363)                                  │
│   + 旭创双确认 ±0.10 (ct.stock_confirm timing.py:386)                  │
│   合计再 clamp ±0.30 (run:423-424)                                      │
└────────────────────────────────────────────────────────────────────────┘
                                   │ score = clamp(core + mods) (run:427)
                                   ▼
┌─ 硬风控（只降不升）cf.defensive_state (factors.py:253) ─────────────────┐
│   dd60≤-8%→0.6 | vol_pctile≥95→0.6 | risk_off→0.3 | 贴水≤-15→0.6        │
│   盘中≤-2.5%→0.3 | 外盘≤-3%&盘中≤-1.5%→0.3                             │
│   + ERP 便宜度<0.10→0.6 (run:450) + 缠论顶背驰→0.6 (run:455)            │
└────────────────────────────────────────────────────────────────────────┘
                                   │ cap
                                   ▼
┌─ 档位状态机 ct.decide_position (timing.py:347) ─────────────────────────┐
│   TIERS (0.40,100%)/(-0.15,90%)/(-0.30,60%)/更低 0%   (timing.py:321)   │
│   升档需连续 2 日同目标确认；降档当日生效；降档带 HYST_MARGIN=0.05      │
└────────────────────────────────────────────────────────────────────────┘
                                   ▼
        render_report (run:851) → push_report (run:975) → save_state
                    └── update_shadow_history (run:495) 累计影子 IC
```

### 1.2 当前参数一览（全部硬编码）

| 参数 | 值 | 位置 |
|---|---|---|
| 核心维度权重 | 趋势.35/量价.20/波动.20/估值.00/落袋.25 | `chinext_factors.py:316-322` |
| 档位线 TIERS | (0.40,1.0)、(-0.15,0.9)、(-0.30,0.6) | `chinext_timing.py:321` |
| 降档滞回 HYST_MARGIN | 0.05 | `chinext_timing.py:322` |
| 升档确认天数 | 2 | `chinext_timing.py:323` |
| 修正层总分封顶 | ±0.30 | `chinext_timing.py:383`；`run:423` |
| 贴水/资金/情绪/资讯封顶 | ±0.06 / ±0.05 / ±0.04 / ±0.06 | `run:330-354` |
| 缠论/旭创封顶 | ±0.08 / ±0.10 | `timing.py:365,386` |
| 硬风控 dd60 | ≤-8% → cap 0.6 | `chinext_factors.py:266` |
| 硬风控波动分位 | ≥95 → cap 0.6 | `chinext_factors.py:269` |
| 硬风控 risk_off | → cap 0.3 | `chinext_factors.py:274` |
| 硬风控贴水 | ≤-15% → cap 0.6 | `chinext_factors.py:277` |
| 硬风控盘中急跌 | ≤-2.5% → cap 0.3 | `chinext_factors.py:281` |
| 外盘同源确认 | ≤-3% 且盘中≤-1.5% → 0.3；≤-1.0% → 0.6 | `chinext_factors.py:286-292` |
| ERP 极端滤波 | 便宜度<0.10 → cap 0.6 | `run:450-452` |
| 缠论顶背驰 | → cap 0.6 | `run:455-457` |
| 影子 history 上限 / 最小历史 | 120 条 / 62 根 | `run:64-65` |

调度：仅 cron-job.org 北京 14:45 外部触发（`.github/workflows/chinext-timing.yml:9-18`），运行 `--push --shadow`（`chinext-timing.yml:76`）。

---

## 2. 发现的问题与优化机会

> 分级：P0 = 明显缺陷 / 亏钱风险；P1 = 稳健性提升；P2 = 锦上添花。

### P0-1 硬风控「缺源即 fail-open」——数据断流时几乎所有封顶同时失效

**证据**

```python
# src/strategy/chinext_factors.py:253-293  cf.defensive_state
cap = 1.0
if n >= 60:
    dd = close[-1] / max(close[-60:]) - 1.0
    if dd <= -0.08:                       # ← 唯一只依赖 closes 的封顶，永远可用
        cap = min(cap, 0.6)
if vol_pctile is not None and vol_pctile >= 95:   # ← 快照缺失 → None → 跳过
    cap = min(cap, 0.6)
if glass:
    if glass.get("risk_off"):             # ← 快照缺失 → False → 跳过
        cap = min(cap, 0.3)
    ap = glass.get("basis_min_ap")
    if ap is not None and ap <= -15:      # ← 快照缺失 → None → 跳过
        cap = min(cap, 0.6)
    ip = glass.get("intraday_pct")
    if ip is not None and ip <= -2.5:     # ← 盘中行情失败 → 0.0 → 跳过
        cap = min(cap, 0.3)
```

```python
# scripts/run_chinext_timing.py:178-183  盘中行情失败静默置 0
intraday = 0.0
try:
    q = get_quotes([f"0.{SYMBOL}"])
    intraday = float(q.get(SYMBOL) or 0.0)
except Exception as e:
    logger.warning("盘中行情失败（降级为0）: %s", type(e).__name__)
```

```python
# scripts/run_chinext_timing.py:428-430  盘中 0.0 被当作真实值送进硬风控
glass = {"risk_off": str((snapshot.get("risk_state") or "")) == "risk_off",
         "basis_min_ap": None, "intraday_pct": ctx["intraday"],
         "overseas_drop": ctx.get("overseas_drop", 0.0)}
```

```python
# scripts/run_chinext_timing.py:221-227  外盘失败静默置 0
overseas_drop = 0.0
try:
    ov = ovs.load_overseas(PROJECT_ROOT)
    overseas_drop = ovs.overnight_drop(ov, datetime.now(BJT))
except Exception as e:
    logger.warning("外盘状态读取失败（降级为0）: %s", type(e).__name__)
```

```python
# scripts/run_chinext_timing.py:449-453  PE 失败 → erp 空 → 滤波不触发
_erp_series = ctx.get("erp_pctile") or []
if _erp_series and _erp_series[-1] is not None and _erp_series[-1] < 0.10:
    caps["cap"] = min(caps["cap"], 0.6)
```

**问题机理**
- 6 个硬风控触发项里，只有 **dd60 回撤** 完全依赖内部 `closes`，永远有效；其余 5 项（波动分位 / risk_off / 贴水 / 盘中急跌 / 外盘确认）+ ERP 滤波 + 缠论顶背驰（`run:366-369`，备用源缺 high/low 即跳过）**全部依赖外部数据，缺源一律走「不封顶」分支**。
- 这是**方向错误的降级**：缺数据时系统默认"没有风险"，会在数据断流 + 市场急跌的同一天给出高仓位。风控的正确默认应偏保守。
- `intraday=0.0` 与"盘中真实平盘"不可区分：报告行 `■ 盘中：创业板指 +0.00%`（`run:919`）主动**伪装成真实平盘**，掩盖了行情源失败。当日大跌但腾讯接口失败时，硬风控全部看不到跌幅。

**建议改法**
1. `gather_context` 中把 `intraday` 改为 `Optional[float]`（失败= `None`），报告显式打印"盘中行情缺失"，`glass["intraday_pct"]` 传 `None` 而非 `0.0`。
2. 在 `defensive_state` 引入"数据缺失保守带"：当 `vol_pctile/risk_state/basis/盘中` 同时不可用时，允许一个**弱封顶**（如 cap 0.9 或 0.6）并写 trigger，而不是完全不封顶。
3. 报告 `data_quality` 已能给出 level D（`run:463-490`），但**仍照常推送可执行仓位**；建议缺盘中快照时在建议行强制加"数据缺失，建议人工复核/减半执行"的显式提示。

**验证方式**
- 复用 `scripts/backtest_intraday_snapshots.py` 与 `src/strategy/intraday_replay.py` 构造"快照缺失日"对照：把部分交易日的 `index_snapshot` 置空，比较 `replay_snapshot_backtest` 在 `allow_gaps=True` 下的仓位分布，确认 fail-open 期间的平均仓位与最大回撤劣化幅度。
- 单测门禁：`python -m pytest tests/test_chinext_timing.py tests/test_chinext_factors.py -q -m unit`。现有测试 `tests/test_chinext_timing.py:196-225` 覆盖的是 `ct.defensive_caps`，生产用的 `cf.defensive_state` 缺源行为只在 `tests/test_chinext_factors.py:89-120` 部分覆盖，需补"缺源时保守封顶"用例。

---

### P0-2 回测信号日使用「全天收盘」，实盘 14:45 只有 partial bar——前视量未量化

**证据**

```python
# src/strategy/chinext_factors.py:20-24（模块口径声明）
时空口径：实时 t 日因子值可使用 ≤t 的盘中快照，历史回测只使用完整收盘：
  回测 = 信号日 d 使用 d 日完整收盘近似 14:45 快照、吃 d+1 收益；
  实盘 = 14:45 盘中快照替代 d 日收盘（最后一根 bar 为当日实时价，量价使用累计成交量）。
历史回测的 d 日完整收盘/全天成交量只是 14:45 快照的可复现近似，不能与实盘快照混称。
```

```python
# scripts/run_chinext_timing.py:1195-1197（回测决策点）
# 口径（2026-08-28 v5.1 用户拍板）：决策用 d 日 14:45 快照（回测用 d 日收盘近似，
# 14:45→15:00 价差接受），而非 d-1 收盘
dec = ct.decide_position(comp[d], cap, prev, tiers=tiers)
```

```python
# scripts/run_chinext_timing.py:1186-1214  回测以 closes[d] 作为信号输入
for d in range(start, end):
    ...
    dec = ct.decide_position(comp[d], cap, prev, tiers=tiers)   # comp[d] 用 close[d]
    ...
    r = closes[d + 1] / closes[d] - 1.0                         # 收益 d→d+1（正确）
```

**问题机理**
- 回测中 `comp[d]` 由 `closes[:d+1]` 计算，最后一个数据点是 **d 日全天收盘**；而实盘 14:45 只掌握 d 日 partial（收盘前 15 分钟）。等价于决策时"偷看"了 d 日 14:45→15:00 的走势，而执行恰好按该收盘价成交。
- 该偏差方向**系统性有利**：趋势因子（`factor_trend_ma20_60` 的 `last=close[d]`）、量价因子（`ret1=close[d]/close[d-1]`）、旭创个股方向（`timing.py:407`）都以当日涨跌定方向。
- 偏差大小 = `execution_slippage()` 的 `slip = close_pct − intraday_pct/100`（`run:830`），当前有效影子样本仅 3 条，**尚无法量化**。因此 `+145.6%`（`docs/status.md:13`）应视为**乐观上界**。
- 这不是"必须立刻改的 bug"（口径已拍板接受近似），但**必须被量化**，否则无法回答"回测=实盘"。

**建议改法**
- 不改生产打分，只补齐**可复现的真实快照回放**（扩展现有 `intraday_replay`，见 P1-1），与日线收盘代理回测同窗口对跑，输出差值作为新验收基线。
- 当 14:45 快照归档覆盖 ≥ 一个完整牛熊段后，才把回放数字写入 `docs/status.md` 作为正式基线。

**验证方式**
- `python scripts/backtest_intraday_snapshots.py --snapshot-file <状态文件|快照JSON> --daily-csv <399006.csv>`
- 与 `python scripts/run_chinext_timing.py --backtest` 同窗口对拍，记录累计/夏普/回撤差值。

---

### P1-1 快照回放与实盘不等价：回放丢掉了修正层/缠论/旭创/盘中硬风控

**证据**

```python
# src/strategy/intraday_replay.py:159-169
close_history = closes[:i] + [item["close"]]
amount_history = amounts[:i] + [item["amount"]]
signals = cf.core_signals(close_history, amount_history, erp_pctile=None)
comp = cf.dimension_score(signals, weights)
score = float(comp[-1])
caps = cf.defensive_state(
    close_history, None,                       # ← vol_pctile 恒 None
    {"risk_off": False, "basis_min_ap": None,
     "intraday_pct": 0.0},                     # ← 盘中/贴水/risk_off 恒关闭
)
```

**问题机理**
- 回放只重放**核心层**，`vol_pctile=None`、`intraday_pct=0.0`、`risk_off=False`、`basis=None`，且**完全不跑** `_dimension_modifier`、`_chan_signal`、`stock_confirm`、ERP 滤波、顶背驰封顶。
- 而实盘 `score_all` 会叠加 ±0.30 修正层；`docs/缺陷诊断与推进方案_20260828.md` P1-6 已量化：资讯修正 ±0.06 可翻转 **15.7%** 交易日档位。
- 因此即使快照回放跑通，验证的也只是"核心层 + 不完整硬风控"，**无法回答"实盘信号是否可信"**。

**建议改法**
- 给 `replay_snapshot_backtest` 增加可选入参 `intraday_pct` / `vol_pctile/basis/risk_off`，让回放能复现 `defensive_state` 的盘中/波动封顶。
- 修正层确实不可回溯，保持"只记录不重放"，但报告须标注"回放=核心层+硬风控子集，不含修正层"。

**验证方式**
- `tests/test_intraday_replay.py:59-67` 已有 mock 骨架；新增"盘中急跌快照 → cap 0.3"用例。
- 用 `--snapshot-file logs/chinext_timing_state.json` 回放并核对 `events[*].cap` 与实盘当日一致。

---

### P1-2 硬风控阈值无滞回：60% 封顶线在阈值附近抖动

**证据**

```python
# src/strategy/chinext_factors.py:260-268
if n >= 60:
    dd = close[-1] / max(close[-60:]) - 1.0
    if dd <= -0.08:
        cap = min(cap, 0.6)
        trig.append(f"距60日高点回撤{dd * 100:.1f}%封顶6成")
```

```python
# src/strategy/chinext_timing.py:364-378  降档即时、升档需 2 日
if target < cur - 1e-9:
    via_cap = "（风控封顶，无滞回）" if capped else "（滞回带确认）"
    return {"position": target, ..., "direction": "down", ...}
if target > cur + 1e-9:
    if pending and abs(pending.get("target", 0) - target) < 1e-9:
        days = int(pending.get("days", 0)) + 1
        if days >= UPGRADE_CONFIRM_DAYS:        # 2 日
            ...
    return {"position": cur, "pending": {"target": target, "days": 1}, ...}
```

**问题机理**
- `HYST_MARGIN=0.05` 只作用于**打分档位**的降档（`_tier_with_hysteresis`，`timing.py:334-344`）；**硬风控 cap 触发的降档无滞回**（`timing.py:365` 注释明确"风控封顶，无滞回"）。
- 当 `dd60` 在 -0.08 附近来回穿越，cap 会在 1.0/0.6 之间逐日翻转，在 60% 档线附近**反复换仓**；而回测 fee=0（ND-002）**完全看不到这部分摩擦**。
- 同样问题存在于 `vol_pctile≥95`（`factors.py:269`）、ERP<0.10（`run:450`）、顶背驰（`run:455`）。

**建议改法（保守，不违反"风控优先"）**
- 对回撤型/波动型 cap 加释放缓冲：触发 `dd ≤ -0.08`，解除 `dd ≥ -0.05`（触发侧不变，风控仍即时）。
- 或要求 cap 连续 N 日不再满足才释放。只延后"松绑"，不延后降档。

**验证方式**
- `scripts/backtest_fee_sensitivity.py` 对 `fee ∈ {0, 0.001, 0.003}` 扫描换手与收益；
- `scripts/walk_forward_validation.py --fee 0.003` 看 OOS 是否"收益不劣化、回撤不劣化"。
- 注意：`docs/策略缺陷实验报告_20260828.md` 方案 B.5 已证"距前高硬风控"OOS 不过关，本项须独立验证。

---

### P1-3 升档确认的 pending 目标一旦变化即清零——0.40 线附近可能永远无法升档

**证据**

```python
# src/strategy/chinext_timing.py:368-378
if target > cur + 1e-9:
    if pending and abs(pending.get("target", 0) - target) < 1e-9:   # ← 要求目标完全相等
        days = int(pending.get("days", 0)) + 1
        if days >= UPGRADE_CONFIRM_DAYS:
            return {"position": target, "pending": None, "changed": True, ...}
        return {"position": cur, "pending": {"target": target, "days": days}, ...}
    return {"position": cur, "pending": {"target": target, "days": 1}, ...}  # ← 目标变了→重记 1 天
```

**问题机理**
- 确认要求"连续两日**同一 target 值**"。当综合分在 0.40 档线上下震荡时，target 在 `1.0` 与 `0.9` 之间跳变，`abs(pending["target"] − target) < 1e-9` 永不成立，pending 每天清零为 `days=1`，**仓位长期停在原档，永不升档**。
- 场景：core=0.38、mods=+0.05 → 0.43（target 1.0）；次日 core=0.36、mods=+0.02 → 0.38（target 0.9）。两日都强烈看多，却因 target 跳档无法确认，叠加"进场慢"会加剧 `缺陷诊断与推进方案` P0-1 的"牛市踏空"。
- `walk_forward_validation.py:222-224` 参数切换时也清空 pending，属同类"pending 易失"。

**建议改法**
- 判据从"target 完全相等"放宽为"target **不低于**已有 pending 的 target"（或"同方向且差值在 1 个档位内"）。
- 或记录 pending 时同记 `raw` 档位，用"档位单调不降"作为确认条件。

**验证方式**
- 单测：score 序列 `[0.43, 0.38]`（cur=0），断言第 2 日应确认升到至少 0.9（当前实现保持 0）。
- `scripts/walk_forward_validation.py` 重点看 2020/2025 牛市折。

---

### P1-4 回测 warmup=60，但落袋因子需要 252 根——2015 年最大回撤窗口恰在暖机污染区

**证据**

```python
# scripts/run_chinext_timing.py:1163-1165
warmup = 60
start = warmup if eval_start is None else int(eval_start)
end = n - 1 if eval_end is None else int(eval_end)
```

```python
# src/strategy/chinext_factors.py:215-219  52周高点因子硬要求 252 根
def factor_pullback_52w(close: Sequence[float]) -> list:
    out = [0.0] * len(close)
    for i in range(252, len(close)):
        hi = max(close[i - 251 : i + 1])
        dd = close[i] / hi - 1.0
```

```python
# src/strategy/chinext_factors.py:152  波动分位也需要 252 窗口
pct = _roll_pctile(vol, 252)
```

**问题机理**
- `factor_pullback_52w` 在 `i < 252` 时输出恒为 `0.0`，被 `dimension_score` 当作**真实中性值**参与加权（`factors.py:362-366`）。评估区间前 ~192 个交易日（约 2014-08 ~ 2015-07），**落袋维（权重 0.25）实际只剩 dd60 半权**。
- 而报告历史起点正是 2014-07-30（`创业说明:95`），2015-06 崩盘正是最大回撤窗口起点（`策略缺陷实验报告:36`）。**最需要落袋维的那段被暖机削弱**，回测对该段表现估计偏高。

**建议改法**
- 回测 `warmup` 提到 252（对齐最长因子暖机）；或让 `dimension_score` 对未暖机因子返回 `None` 并从维度内剔除，而不是当 0 加权。
- 若保留 60，报告应把"2014-07~2015-07"标为暖机段单列，不与正式基线混算。

**验证方式**
- `python scripts/run_chinext_timing.py --backtest` 对比改动前后；`scripts/walk_forward_validation.py` 确认 OOS 不劣化。

---

### P1-5 存在两套并行且阈值不一致的核心合成 / 硬风控实现（潜伏一致性陷阱）

**证据**

```python
# src/strategy/chinext_timing.py:284-318  ct.defensive_caps（阈值 -12%→0.3）
if dd <= -0.12:
    cap = min(cap, 0.3)
    reasons.append(f"距60日高点回撤{dd * 100:.1f}%（深回撤，封顶3成）")
```

```python
# src/strategy/chinext_factors.py:266-268  cf.defensive_state（阈值 -8%→0.6）
if dd <= -0.08:
    cap = min(cap, 0.6)
```

```python
# src/strategy/chinext_timing.py:130-138  ct.core_score（四维 .40/.30/.15/.15）
score = 0.40 * t["score"] + 0.30 * m["score"] + 0.15 * v["score"] + 0.15 * d["score"]
```

```python
# src/strategy/chinext_timing.py:429-437  ct.composite
mods = clamp(deriv["score"] + flow["score"] + mood["score"] + news["score"], -0.30, 0.30)
return round(clamp(core["score"] + mods), 3)
```

**问题机理**
- 生产实际用 `cf.dimension_score` + `cf.defensive_state`（`run:408,445,1179,1187`）；`ct.defensive_caps`、`ct.core_score`、`ct.composite` **生产零调用**，仅被 `tests/test_chinext_timing.py` 引用（`:196-225`、`:333-343`、`:109-112`）。
- 两套实现**回撤阈值不同**（-12%/0.3 vs -8%/0.6）、**合成权重不同**（四维 vs 五维）。若有人改 `ct.defensive_caps`，生产**完全不变**（静默 no-op）；反之改 `cf.defensive_state` 时旧测试仍全绿，给出**虚假安全感**。

**建议改法**
- 删除 `ct.defensive_caps` / `ct.core_score` / `ct.composite`，或改为对 `cf.*` 的薄封装并加 deprecation 注释；同步调整对应测试为测 `cf.*`。纯重构，不改生产数值。

**验证方式**
- 删除后 `python -m pytest tests/ -q -m unit` 必须全绿（1160 passed 基线，`docs/status.md:23`），`--backtest` 输出逐位一致。

---

### P2-1 「修正层封顶 ±0.30 → 中性最多到六成」的文档/注释结论错误，实为可到九成

**证据**

```python
# src/strategy/chinext_timing.py:383
MOD_TOTAL_CAP = 0.30  # 修正层合计封顶：中性市场最多被推到六成档，永远到不了满仓档
```

```python
# src/strategy/chinext_timing.py:431-433（composite docstring）
核心分 0（中性）时，即使四项修正全部拉满也只有 +0.30 < 满仓线 0.35。
```

```
docs/创业板择时系统说明_20260822.md:27
合计封顶 ±0.30——中性市场最多被推到六成档，永远到不了满仓档
```

**问题机理**
- 按 `TIERS`，`score = 0 + 0.30 = 0.30 ≥ -0.15` → 命中 **0.9（九成）**，而非 0.6。要落 0.6 需 `score ∈ [-0.30,-0.15)`。
- 修正层拉满在数学上可达（贴水 +0.03、资金 +0.05、情绪封顶 +0.04、资讯 +0.06、缠论 +0.08、旭创 +0.08 → clamp 0.30）。故"中性 + 修正拉满"可达**九成仓**，注释低估实际敞口约 30pp。

**建议改法**
- 仅改注释/文档；或要严格实现"中性不到九成"，需引入"修正层单独不得跨级"约束（如修正层贡献单独 clamp ≤0.25）。后者属策略改动，须回测。

**验证方式**
- 单测：`ct.decide_position(0.30, 1.0, {"position":0.0})` 返回 0.9（确认行为），据此改文档。

---

### P2-2 盘中 partial 成交量系统性低于全天量，量价分位会被压低

**证据**

```python
# src/strategy/data.py:750-751  腾讯 [36] 是「成交量(手)」累计值，×100 对齐「股」
bar = {"close": close, "amount": vol_shares * 100.0,
       "amount_unit": "shares"}
```

```python
# src/strategy/chinext_factors.py:111-113  量能分位对 amount 做 60 日分位
pct = _roll_pctile(list(amount), 60)
for i in range(1, len(close)):
    ret1 = close[i] / close[i - 1] - 1.0 if close[i - 1] else 0.0
    p = pct[i]
```

**问题机理**
- 14:45 的 `amounts[-1]` 是**当日累计成交量**，与历史"全天成交量"同尺度但系统性偏小（缺 14:45 后成交）。会把当日量能分位 `pct` 压低，使"放量"象限（`p ≥ 0.8`，+0.8）更难触发、"缩量"判断更易触发，方向偏保守。
- `run:920-922` 打印 `day_amount_ratio = amounts[-1]/amounts[-2]`（今累计/昨全天），该比值<1 并不代表缩量，目前无归一化换算。

**建议改法**
- 用 14:45 历史"同刻量占比"中位数把累计量折算到全天当量（需先归档各时点快照）；或至少把 `day_amount_ratio` 更名为"累计/昨全天（未折算）"并注明不可直接当量能强弱。

**验证方式**
- 依赖 P1-1 快照回放：比较"partial 量" vs"全天量"两种输入下核心分与换仓次数差异。

---

### P2-3 CI 门禁只跑 3 个测试文件，与本地全量门禁不一致

**证据**

```yaml
# .github/workflows/chinext-timing.yml:63-64
- name: 运行单元测试门禁
  run: python -m pytest tests/test_chinext_timing.py tests/test_chinext_factors.py tests/test_chan_light.py -q --no-header -p no:cacheprovider
```

`docs/status.md:23` 本地门禁为全量 `python -m pytest tests/ -q -m "unit" -p no:cacheprovider`（1160 passed）。

**问题机理**：CI 只覆盖择时相关 3 个文件，数据层/回放/状态写回回归不在云端门禁内（历史上 `data_freshness.py`/`state_io.py` 未提交曾致 CI ImportError，`docs/status.md:25`）。

**建议改法**：把 CI 门禁换成全量 `-m unit`（或追加 `test_intraday_replay.py`、`test_intraday_snapshot*`、`test_state_io*`）。

**验证方式**：本地先跑全量确认耗时可控，再改 workflow。

---

## 3. 重点检查方向逐条回应

### 3.1 前视偏差（14:45 vs 回测数据）
- **核心因子无前视**：逐个核对 `chinext_factors.factor_*`，全部只引用 `[:i+1]` 或滚动历史窗口（`_roll_pctile`/`_roll_z` 的 `w=x[lo:i]` 不含未来），未发现未来数据泄漏。检查过，无问题。
- **回放无前视**：`intraday_replay.py:159` 用 `closes[:i] + [snapshot_close]`，未来行从未切入，正确。检查过，无问题。
- **影子回填无前视**：`update_shadow_history` 回填 `next_ret`/`r3/r5/r10` 时要求 `next_day < today`（`run:538,545,568,578`），正确。
- **存在问题**：回测信号日使用 d 日全天收盘（P0-2），实盘 14:45 看不到；已定性、待量化。
- **派生问题**：回放未复现实盘修正层（P1-1）。

### 3.2 档位切换换手成本与抖动
- 设计方向正确（`_tier_with_hysteresis` `timing.py:334-344` + `UPGRADE_CONFIRM_DAYS=2`），但在两处失效：硬风控 cap 释放无滞回（P1-2）、升档 pending 目标跳变即清零（P1-3）。
- 成本口径 `fee=0`（ND-002 拍板，`needs-decision.md:17`），回测看不到真实申赎摩擦（`策略缺陷实验报告` 已证 0.3% 吃掉大部分优势）。检查过，属既定决策，不重开。提示：实盘另有 15 分钟执行滑点（`run:803-848` 已埋点）。

### 3.3 参数敏感性 / 过拟合
- 阈值全部硬编码（见 1.2）。OOS 只验证档位线/ERP，未把权重纳入训练段寻优（`策略缺陷实验报告:227-230,279-281` 显示动态寻优跨折漂移，故固定权重）→ 权重 OOS 证据弱于档位线，属已知风险；`docs/status.md:58` 已定"OOS 期间不再调参"。检查过，暂不应动。
- 敏感点：`-0.15` 与 `-0.30` 线间距较大，`-0.15` 附近易触发 P1-3。

### 3.4 数据源断流降级路径
- 399006 日线双源均失败则 `raise SystemExit`，**不推伪信号**（`run:1386-1390`），设计正确。
- 但增强/风控数据断流 **fail-open**（P0-1）：盘中行情、外盘、PE、因子快照、备用源 high/low 缺失时硬风控默认不封顶，是本系统最需在真实事故前收口的风险。
- `factor_state` 过期 → 修正层整体置 0（`run:314-316`），显式正确；资讯失败 → 标 `(缺)`（`run:894`），正确。

### 3.5 walk-forward / OOS 是否完整
- 训练 3 年/测试 1 年滚动 9 折、按卡玛选参、跨折继承仓位（`walk_forward_validation.py:88-140,217-237`），切分与继承正确。
- 但：① 网格只含档位线 × ERP，未含权重；② 测试段仍用全天收盘口径，继承 P0-2 偏差；③ `summarize_oos` 拼接口径正确，未发现前视。整体"流程完整、覆盖不全"。

### 3.6 缺失信号维度（成交量突变 / 北向融资 / 跨市场）
- **量能突变**：与 `volprice_quadrant`/`amihud` 同源（均来自 amount），`策略缺陷实验报告:87-93` 已证同源叠加无增量。不建议。
- **北向/融资**：北向实时停发、融资 T+1、历史 IC 不达标已关闭（`创业说明:57-58`）。不建议。
- **跨市场**：SOX 全样本 IC<0.05 已关闭、KOSPI 被墙（`创业说明:58,66`）。不建议重启打分。
- **唯一非 close/amount 同源信号**是旭创双确认（`timing.py:386`）。可考虑扩展为一小组科技龙头降噪，但须先过影子 IC 验门（|IC|≥0.05 且样本≥10，`timing.py:324`），当前样本不足，**只列方向、不建议现在动**。

### 3.7 数据正确性细节（顺手核验）
- 单位一致性：新浪 `amount=成交量(股)`（`data.py:698-699`），腾讯 ×100 对齐（`data.py:750-751`），转换函数一致（`run:1030-1070`）。正确。
- `factor_amihud` docstring 称"|ret|/成交额"，实际是成交量（`factors.py:125-132`），但先做滚动 z 标准化，量纲被吸收，不影响数值，属注释瑕疵。

---

## 4. 不建议做的事（看似合理但不值得改）

| 事项 | 理由 |
|---|---|
| 重新开启 `value_erp` 进打分 | ERP 打分样本外负贡献，现仅保留"极贵封顶"（`创业说明:55`；`factors.py:316-322` 估值权重=0） |
| 重启外盘/两融/业绩预告打分 | 全样本 IC 不达标，已正式关闭（`创业说明:56-59`） |
| 新增更多 close/amount 同源因子（量能突变、短期反转等） | 同源叠加无增量（`策略缺陷实验报告:87-93`）；`factor_short_reversal` 保持候选、未验门（`factors.py:327-343`） |
| 在 OOS 观察期调权重/档位线/滞回/确认天数 | 违反 `docs/status.md` 唯一 NEXT 与 ND-002/ND-004 拍板 |
| 现在强推 `--snapshot-only` 到生产 | 会因任一数据源抖动阻断每日唯一信号（`run:1098-1105`）；应先量化快照覆盖率 |
| 换取更高频（分钟级）择时 | 场外基金 T+1 收盘成交 + 手动申赎，频率红利被摩擦吃掉，与定位不符 |
| 用 ML/截面因子/卫星图 | 单资产时序架构错配，不可回测（`创业说明:59`） |
| 引入动态仓位缩放（波动率目标） | 触及仓位逻辑与 OOS 纪律，`docs/status.md:21` 已暂缓 |

---

## 5. 优先级路线图（只做 3 件事）

### 第 1 件：让"真实快照回放"成为验收基线，量化 14:45→15:00 缺口
- **做什么**：扩展 `intraday_replay.replay_snapshot_backtest` 支持传入快照对应的 `intraday_pct/vol_pctile/basis/risk_off`（补齐 P1-1）；同窗口对拍 `--backtest` 与快照回放，输出三指标差；把差值写入 `docs/status.md` 作为正式基线的"乐观上界"标注。
- **预期收益/风险比**：**最高**。它决定当前 `+145.6%/+83.7%` 有多少是 15 分钟前视（P0-2）+ 修正层未复现（P1-1）。不改策略、只增证据，风险极低。

### 第 2 件：硬风控 fail-open 收口 + 盘中行情缺失显式化
- **做什么**：`intraday` 改 `Optional`，失败不再填 0；`defensive_state` 对"关键风控源缺失"引入保守弱封顶；缺快照时报告强制提示人工复核（P0-1）。
- **预期收益/风险比**：**高**。防范"数据断流 + 市场急跌 → 系统满仓"的尾部事故，纯防守型改动；风险中低（可能因数据抖动触发保守档，需回测确认不显著拖累长期收益）。

### 第 3 件：回测保真修复 + 去重复实现（可打包）
- **做什么**：① 回测 `warmup` 与最长因子暖机对齐（60→252）或让未暖机因子从维度内剔除（P1-4）；② 删除/收敛 `ct.defensive_caps`、`ct.core_score`、`ct.composite` 三处死代码（P1-5）。
- **预期收益/风险比**：**中**，成本极低、零策略风险。①提升历史数字可信度，②消除未来静默改错的口子。

**排序理由**：先建立"可检验的真相"（第 1 件），再堵"会直接亏钱的口子"（第 2 件），最后做零风险清理与保真（第 3 件）。P1-2/P1-3 属策略行为改动，须等 OOS 观察期结束 + 独立回测证据，**不列入本轮 3 件**。

---

## 附录：本次审计覆盖与未决项

**已完整读取并审计**：`src/strategy/chinext_timing.py`、`src/strategy/chinext_factors.py`、`src/strategy/chan_light.py`、`src/strategy/intraday_snapshot.py`、`src/strategy/intraday_replay.py`、`scripts/run_chinext_timing.py`、`scripts/signal_backtest.py`、`scripts/walk_forward_validation.py`、`scripts/backtest_intraday_snapshots.py`、`docs/创业板择时系统说明_20260822.md`、`docs/策略缺陷实验报告_20260828.md`、`docs/缺陷诊断与推进方案_20260828.md`、`docs/needs-decision.md`、`docs/status.md`、`.github/workflows/chinext-timing.yml`、`src/strategy/data.py`（相关函数）。

**未运行**：任何回测/回放/单测（遵循"只分析不改代码"约束）。所有策略数字均引用自现有文档，未自行计算。

**未决项（等待样本/用户决定）**：
- 14:45 缺口量级：依赖影子 `close_pct`/`intraday_pct` 配对样本积累（建议 ≥20 交易日，`docs/status.md:17`）。
- 修正层 IC 验门：影子样本不足，`scripts/_exp_modifier_ic.py` 就绪但暂不结论（`docs/status.md:22`）。
- `--snapshot-only` 是否转正：待快照覆盖率/质量统计。

*文档生成：2026-09-10 · 分析人：DeepSeek（WB Task：择时系统优化分析）· 只读分析，未改任何源码*
