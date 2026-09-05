# -*- coding: utf-8 -*-
"""因子批评诊断（_diag_critic.py）—— 临时脚本，不入库
针对外部因子审查意见的四项可测验证：
 1. momentum_60 饱和度（批评：20%阈值对创业板过钝，信号大量饱和）
 2. amihud 漂移检查（批评：指数规模扩大导致漂移；实现为60日滚动z，理论免疫）
 3. 核心分 vs 实盘决策分 日频相关性（"残酷测试"：<0.85 则回测与实盘是两套策略）
    —— 可回溯修正：缠论±0.08 + 旭创双确认±0.10（纯函数可重建）
    —— 不可回溯：资讯±0.06（无历史Gist）、外盘确认（需盘中14:30价，日线只能近似）
 4. 状态机切换频率 + 0.1%费率摩擦（批评：月均>3次切换吃掉5-10%收益）
"""
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy import chinext_timing as ct
from src.strategy import chinext_factors as cf
from src.strategy import chan_light as ch
from src.strategy.data import load_index_sina, load_stock_sina

SYMBOL, SYMBOL_STOCK = "399006", "300308"


def main():
    df = load_index_sina(SYMBOL)
    closes = df["close"].tolist()
    amounts = (df["amount"].tolist() if "amount" in df else [0.0] * len(closes))
    highs = (df["high"].tolist() if "high" in df else [])
    lows = (df["low"].tolist() if "low" in df else [])
    dates = df.index
    date_strs = [d.strftime("%Y-%m-%d") for d in dates]
    n = len(closes)
    start = 60

    signals = cf.core_signals(closes, amounts, erp_pctile=None)
    W = {"趋势": 0.35, "量价": 0.20, "波动": 0.20, "估值": 0.10, "落袋": 0.15}
    comp = cf.dimension_score(signals, W)

    # ---- 1. momentum 饱和度 + 各因子分布 ----
    mom = signals["trend_momentum_60"][start:]
    sat = sum(1 for v in mom if abs(v) >= 0.995)
    mid = sum(1 for v in mom if abs(v) <= 0.10)
    print("== 1. 因子分布诊断 ==")
    print(f"momentum_60: 饱和(|s|>=1)占比 {sat/len(mom):.1%}，"
          f"近零(|s|<=0.1)占比 {mid/len(mom):.1%}，样本 {len(mom)}")
    for k in ("trend_ma20_60", "volprice_quadrant", "volprice_amihud",
              "vol_regime", "vol_term", "pullback_52w", "dd60"):
        s = signals[k][start:]
        nz = sum(1 for v in s if v != 0)
        print(f"{k:<20} 非零 {nz/len(s):.1%} 唯一值 {len(set(s))}")

    # ---- 2. 核心分 vs 决策分（含可回溯修正） ----
    print("\n== 2. 核心分 vs 实盘决策分 相关性 ==")
    sdf = load_stock_sina(SYMBOL_STOCK)
    scloses = sdf["close"].tolist()
    sdates = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(sdf.index)}
    xs, ys = [], []
    chan_abs = stock_abs = 0
    cnt = 0
    for d in range(start, n - 1):
        core = comp[d]
        mods = 0.0
        # 缠论（同 run_chinext_timing._chan_signal 逻辑）
        try:
            cs = ch.scan(closes[: d + 1], highs[: d + 1], lows[: d + 1])
            s = 0.0
            if cs.get("bustop"):
                s -= 0.06
            elif cs.get("last_signal") == "一卖" or cs.get("last_signal") == "二卖":
                s -= 0.02
            elif cs.get("last_signal") == "一买" or cs.get("last_signal") == "二买":
                s += 0.02
            if cs.get("trend_ok"):
                s += 0.03
            mods += s
            chan_abs += abs(s)
        except Exception:
            pass
        # 旭创双确认（同 stock_confirm 逻辑）
        try:
            si = sdates.get(date_strs[d])
            if si is not None and si >= 60:
                st = ct.trend_score(scloses[: si + 1])
                sm = ct.momentum_score(scloses[: si + 1])
                idx_trend = {"score": float(signals["trend_ma20_60"][d])}
                sc = ct.stock_confirm(st, sm, idx_trend)
                mods += sc["score"]
                stock_abs += abs(sc["score"])
        except Exception:
            pass
        xs.append(core)
        ys.append(ct.clamp(core + mods))
        cnt += 1
    # Pearson
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    sx = (sum((a - mx) ** 2 for a in xs)) ** 0.5
    sy = (sum((b - my) ** 2 for b in ys)) ** 0.5
    r = cov / (sx * sy)
    # 差异统计
    diffs = [abs(b - a) for a, b in zip(xs, ys)]
    big = sum(1 for x in diffs if x > 0.05)
    huge = sum(1 for x in diffs if x > 0.10)
    # 档位变化计数：修正是否翻转档位（跨界）
    def tier_of(s):
        if s >= 0.40:
            return 3
        if s >= -0.15:
            return 2
        if s >= -0.30:
            return 1
        return 0
    flips = sum(1 for a, b in zip(xs, ys) if tier_of(a) != tier_of(b))
    print(f"样本 {cnt} 日（含可回溯修正：缠论+旭创）")
    print(f"Pearson r = {r:.4f}（>0.85 达标）")
    print(f"|决策分-核心分|>0.05 的天数 {big}（{big/cnt:.1%}）；>0.10 的天数 {huge}（{huge/cnt:.1%}）")
    print(f"修正导致档位跨线翻转的天数 {flips}（{flips/cnt:.1%}）")
    print(f"缠论修正平均绝对值 {chan_abs/cnt:.3f}｜旭创修正平均绝对值 {stock_abs/cnt:.3f}")
    print(f"注：资讯±0.06与外盘确认不可回溯（无历史盘中/Gist数据），未计入")

    # ---- 3. 状态机切换频率 + 摩擦 ----
    print("\n== 3. 状态机切换频率与摩擦 ==")
    prev = {"position": 0.0, "pending": None}
    switches = 0
    turn_sum = 0.0
    for d in range(start, n - 1):
        caps = cf.defensive_state(closes[: d + 1], None,
                                  {"risk_off": False, "basis_min_ap": None,
                                   "intraday_pct": 0.0})
        dec = ct.decide_position(comp[d], caps["cap"], prev, tiers=ct.TIERS)
        if dec["changed"]:
            switches += 1
            turn_sum += abs(dec["position"] - prev["position"])
        prev = {"position": dec["position"], "pending": dec["pending"]}
    years = (n - start) / 244.0
    print(f"总换仓 {switches} 次 / {years:.1f} 年 = 月均 {switches/years/12:.2f} 次")
    print(f"总换手 {turn_sum:.0f}%（每次平均 {turn_sum/max(1,switches):.0%}）")
    print(f"0.1% 费率下摩擦 ≈ {turn_sum * 0.001:.1%}（占 291.9% 的 "
          f"{turn_sum*0.001/2.919:.0%}）")


if __name__ == "__main__":
    main()