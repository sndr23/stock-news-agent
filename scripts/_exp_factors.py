# -*- coding: utf-8 -*-
"""因子建议控制变量实验（_exp_factors.py）—— 临时脚本，不入库
针对外部审查的四条因子级建议，逐一单独替换/调参，全区间+样本外(2020后)回测对比：
  V0 基线（当前9因子）
  V1 momentum_60 连续化：固定0.20除数 → 252日滚动z-score（去饱和）
  V2 quadrant → 20日量价滚动相关系数（连续化）
  V3 vol_term 权重 0.20→0.30（波动维度提权，从趋势挪）
  V4 amihud → volume ma5/ma20 量能趋势
  V5 ERP 极端滤波：估值分位>0.9 封顶6成（硬风控式，不进日频打分）
口径与官方 backtest_metrics 一致（决策日d用≤d收盘，吃d+1收益，fee=0）。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy import chinext_timing as ct
from src.strategy import chinext_factors as cf
from src.strategy.data import load_index_sina

SYMBOL = "399006"


def _mom_z(close):
    """V1：ret_60 的 252 日滚动 z，clip±2 → [-1,1]。"""
    raw = [0.0] * len(close)
    for i in range(60, len(close)):
        raw[i] = close[i] / close[i - 60] - 1.0
    out = [0.0] * len(close)
    for i in range(60, len(close)):
        lo = max(0, i - 252)
        w = raw[lo:i]
        if len(w) < 30:
            out[i] = 0.0
            continue
        mu = sum(w) / len(w)
        sd = (sum((v - mu) ** 2 for v in w) / (len(w) - 1)) ** 0.5
        out[i] = max(-1.0, min(1.0, (raw[i] - mu) / sd / 2.0)) if sd > 1e-9 else 0.0
    return [round(v, 3) for v in out]


def _pv_corr(close, amount):
    """V2：20日 close/amount 滚动相关系数 → [-1,1]（量价配合度）。"""
    out = [0.0] * len(close)
    for i in range(20, len(close)):
        cs, as_ = close[i - 19:i + 1], amount[i - 19:i + 1]
        mc, ma = sum(cs) / 20, sum(as_) / 20
        cov = sum((c - mc) * (a - ma) for c, a in zip(cs, as_))
        sc = (sum((c - mc) ** 2 for c in cs)) ** 0.5
        sa = (sum((a - ma) ** 2 for a in as_)) ** 0.5
        out[i] = round(max(-1.0, min(1.0, cov / (sc * sa))), 3) if sc * sa > 0 else 0.0
    return out


def _vol_trend(close, amount):
    """V4：量能趋势 ma5/ma20，扩张看多。"""
    out = [0.0] * len(close)
    for i in range(20, len(close)):
        m5 = sum(amount[i - 4:i + 1]) / 5
        m20 = sum(amount[i - 19:i + 1]) / 20
        out[i] = round(max(-1.0, min(1.0, (m5 / m20 - 1.0) * 5.0)), 3) if m20 > 0 else 0.0
    return out


def run_variant(name, closes, amounts, signals_override=None, weights=None,
                erp_filter=False, erp=None, seg=None):
    signals = cf.core_signals(closes, amounts, erp_pctile=None)
    if signals_override:
        signals.update(signals_override)
    w = weights or {"趋势": 0.35, "量价": 0.20, "波动": 0.20, "估值": 0.10, "落袋": 0.15}
    comp = cf.dimension_score(signals, w)
    n = len(closes)
    start = 60
    prev = {"position": 0.0, "pending": None}
    nav = 1.0
    navs = []
    rets = []
    for d in range(start, n - 1):
        if seg and not (seg[0] <= d < seg[1]):
            # 样本外段外不重置状态机（保持状态连续性），只统计段内收益
            prev = {"position": prev["position"], "pending": prev["pending"]}
            caps = cf.defensive_state(closes[: d + 1], None,
                                      {"risk_off": False, "basis_min_ap": None,
                                       "intraday_pct": 0.0})
            dec = ct.decide_position(comp[d], caps["cap"], prev, tiers=ct.TIERS)
            prev = {"position": dec["position"], "pending": dec["pending"]}
            continue
        caps = cf.defensive_state(closes[: d + 1], None,
                                  {"risk_off": False, "basis_min_ap": None,
                                   "intraday_pct": 0.0})
        cap = caps["cap"]
        if erp_filter and erp is not None and erp[d] is not None and erp[d] > 0.90:
            cap = min(cap, 0.6)
        dec = ct.decide_position(comp[d], cap, prev, tiers=ct.TIERS)
        prev = {"position": dec["position"], "pending": dec["pending"]}
        r = closes[d + 1] / closes[d] - 1.0
        nav *= (1 + dec["position"] * r)
        navs.append(nav)
        rets.append(dec["position"] * r)
    mu = sum(rets) / len(rets)
    sd = (sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5
    mdd = min(v / max(navs[:i + 1]) - 1.0 for i, v in enumerate(navs))
    years = len(navs) / 244.0
    cagr = nav ** (1 / years) - 1
    return nav - 1.0, mu / sd * 244 ** 0.5, mdd, cagr / abs(mdd) if mdd else 0


def main():
    df = load_index_sina(SYMBOL)
    closes = df["close"].tolist()
    amounts = (df["amount"].tolist() if "amount" in df else [0.0] * len(closes))
    n = len(closes)
    # 样本外段：2020-01-01 起
    seg_out = None
    for i, d in enumerate(df.index):
        if d.year >= 2020:
            seg_out = (i, n - 1)
            break

    # ERP（V5 用，读缓存）
    erp = None
    try:
        from src.strategy import index_pe as ipe
        pe_map = ipe.load_cy50_pe(PROJECT_ROOT)
        if pe_map:
            date_strs = [d.strftime("%Y-%m-%d") for d in df.index]
            pe = ipe.align_pe_by_dates(pe_map, date_strs)
            erp = [0.5 if v is None else v for v in ipe.pe_to_cheap_pctile(pe, 500)]
    except Exception as e:
        print(f"ERP 载入失败（V5 跳过）：{type(e).__name__}")

    variants = [
        ("V0 基线", {}, None, False),
        ("V1 momentum滚动z", {"trend_momentum_60": _mom_z(closes)}, None, False),
        ("V2 量价相关系数", {"volprice_quadrant": _pv_corr(closes, amounts)}, None, False),
        ("V3 vol_term权重0.30", None,
         {"趋势": 0.25, "量价": 0.20, "波动": 0.30, "估值": 0.10, "落袋": 0.15}, False),
        ("V4 amihud→量能趋势", {"volprice_amihud": _vol_trend(closes, amounts)}, None, False),
        ("V5 ERP>0.9封顶6成", {}, None, True),
    ]

    print(f"{'变体':<22}{'全区间':>9}{'夏普':>7}{'回撤':>8}{'卡玛':>7}"
          f"{'|样本外20-26':>12}{'夏普':>7}{'回撤':>8}{'卡玛':>7}")
    for name, ov, w, v5 in variants:
        t, s, m, c = run_variant(name, closes, amounts, ov, w,
                                 erp_filter=v5, erp=erp)
        # 样本外：2020 起
        i0 = seg_out[0] if seg_out else 60
        nav = 1.0
        prev = {"position": 0.0, "pending": None}
        signals = cf.core_signals(closes, amounts, erp_pctile=None)
        if ov:
            signals.update(ov)
        comp = cf.dimension_score(signals, w or {"趋势": 0.35, "量价": 0.20,
                                                 "波动": 0.20, "估值": 0.10, "落袋": 0.15})
        navs, rets = [], []
        for d in range(60, n - 1):
            caps = cf.defensive_state(closes[: d + 1], None,
                                      {"risk_off": False, "basis_min_ap": None,
                                       "intraday_pct": 0.0})
            cap = caps["cap"]
            if v5 and erp is not None and erp[d] > 0.90:
                cap = min(cap, 0.6)
            dec = ct.decide_position(comp[d], cap, prev, tiers=ct.TIERS)
            prev = {"position": dec["position"], "pending": dec["pending"]}
            if d < i0:
                continue
            r = closes[d + 1] / closes[d] - 1.0
            nav *= (1 + dec["position"] * r)
            navs.append(nav)
            rets.append(dec["position"] * r)
        mu = sum(rets) / len(rets)
        sd = (sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5
        mdd_o = min(v / max(navs[:i2 + 1]) - 1.0 for i2, v in enumerate(navs))
        years_o = len(navs) / 244.0
        cagr_o = nav ** (1 / years_o) - 1
        print(f"{name:<22}{t:>+9.1%}{s:>7.2f}{m:>8.1%}{c:>7.2f}"
              f"{nav - 1.0:>+12.1%}{mu / sd * 244 ** 0.5:>7.2f}{mdd_o:>8.1%}"
              f"{cagr_o / abs(mdd_o):>7.2f}")

    # 对照：买入持有
    bh_all = closes[-1] / closes[60] - 1.0
    bh_out = closes[-1] / closes[i0] - 1.0
    print(f"\n买入持有：全区间 {bh_all:+.1%}｜样本外 {bh_out:+.1%}")


if __name__ == "__main__":
    main()