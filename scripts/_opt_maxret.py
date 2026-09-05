# -*- coding: utf-8 -*-
"""收益最大化矩阵搜索（_opt_maxret.py）—— 临时脚本，不入库
用户约束：不考虑交易费，只求收益最大化。
变量：
  切换模式：confirm(2日确认+滞回 S0)  vs  instant(完全即时)
  ERP滤波：开/关（>0.9分位封顶6成）
  满仓阈值线：0.40 / 0.35 / 0.30 / 0.25（更早进满仓 = 更高仓位中枢）
口径：决策日d用≤d收盘，吃d+1收益，fee=0（用户明确不考虑）。硬风控保留。
报告 全区间 + 样本外(2020起) 双指标，防止只盯全区间过拟合。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy import chinext_timing as ct
from src.strategy import chinext_factors as cf
from src.strategy import index_pe as ipe
from src.strategy.data import load_index_sina

SYMBOL = "399006"


def build_tiers(full_th: float):
    # 只给开仓档，score 落到更低一律空仓(0)；适配 score_to_tier 逐档比较
    return ((full_th, 1.0), (-0.15, 0.9), (-0.30, 0.6))


def run(closes, comp, erp, mode: str, full_th: float, seg=None):
    tiers_eff = build_tiers(full_th)
    n = len(closes)
    start = 60
    prev = {"position": 0.0, "pending": None}
    prev_pos = 0.0
    nav = 1.0
    navs, rets = [], []
    switches = 0
    for d in range(start, n - 1):
        caps = cf.defensive_state(closes[: d + 1], None,
                                  {"risk_off": False, "basis_min_ap": None,
                                   "intraday_pct": 0.0})
        cap = caps["cap"]
        if erp is not None and erp[d] > 0.90:
            cap = min(cap, 0.6)
        if mode == "confirm":
            old_cd, old_hm = ct.UPGRADE_CONFIRM_DAYS, ct.HYST_MARGIN
            ct.UPGRADE_CONFIRM_DAYS, ct.HYST_MARGIN = 2, 0.05
            try:
                dec = ct.decide_position(comp[d], cap, prev, tiers=tiers_eff)
            finally:
                ct.UPGRADE_CONFIRM_DAYS, ct.HYST_MARGIN = old_cd, old_hm
            if dec["changed"]:
                switches += 1
            prev = {"position": dec["position"], "pending": dec["pending"]}
            pos = dec["position"]
        else:
            target = min(ct.score_to_tier(comp[d], tiers_eff), cap)
            if abs(target - prev_pos) > 1e-9:
                switches += 1
            prev_pos = target
            pos = target
        if seg and not (seg[0] <= d < seg[1]):
            continue
        r = closes[d + 1] / closes[d] - 1.0
        nav *= (1 + pos * r)
        navs.append(nav)
        rets.append(pos * r)
    mu = sum(rets) / len(rets) if rets else 0.0
    sd = (sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5 if len(rets) > 1 else 0.0
    mdd = min(v / max(navs[:i + 1]) - 1.0 for i, v in enumerate(navs)) if navs else 0.0
    years = len(navs) / 244.0
    cagr = nav ** (1 / years) - 1 if years > 0 else 0.0
    return nav - 1.0, mu / sd * 244 ** 0.5 if sd > 0 else 0.0, mdd, \
        cagr / abs(mdd) if mdd else 0.0, switches


def main():
    df = load_index_sina(SYMBOL)
    closes = df["close"].tolist()
    amounts = (df["amount"].tolist() if "amount" in df else [0.0] * len(closes))
    n = len(closes)
    signals = cf.core_signals(closes, amounts, erp_pctile=None)
    comp = cf.dimension_score(signals, {"趋势": 0.35, "量价": 0.20, "波动": 0.20,
                                        "估值": 0.10, "落袋": 0.15})
    i0 = next(i for i, d in enumerate(df.index) if d.year >= 2020)

    # ERP
    erp = None
    try:
        pe_map = ipe.load_cy50_pe(PROJECT_ROOT)
        if pe_map:
            date_strs = [d.strftime("%Y-%m-%d") for d in df.index]
            pe = ipe.align_pe_by_dates(pe_map, date_strs)
            erp = [0.5 if v is None else v for v in ipe.pe_to_cheap_pctile(pe, 500)]
    except Exception as e:
        print(f"ERP 载入失败：{type(e).__name__}")

    combos = []
    for mode in ("confirm", "instant"):
        for erp_on in (False, True):
            for th in (0.40, 0.35, 0.30):
                combos.append((mode, erp_on, th))
    # 对照：当前生产 S0 基线（确认 0.40 无ERP）
    combos.append(("BASE", False, 0.40))

    print(f"{'配置':<34}{'全区间':>9}{'夏普':>7}{'回撤':>8}{'卡玛':>7}"
          f"{'|样本外':>9}{'夏普':>7}{'回撤':>8}{'卡玛':>7}{'切换':>6}")
    show = {}
    for mode, erp_on, th in combos:
        if mode == "BASE":
            name = f"BASE 生产(确认,满仓≥0.40,无ERP)"
            erp_use = None
        else:
            erp_use = erp if erp_on else None
            name = f"{mode}{'+ERP' if erp_on else '      '},满仓≥{th:.2f}"
        t, s, m, c, sw = run(closes, comp, erp_use, mode if mode != "BASE" else "confirm", th)
        to, so, mo, co, swo = run(closes, comp, erp_use,
                                  mode if mode != "BASE" else "confirm", th,
                                  seg=(i0, n - 1))
        print(f"{name:<34}{t:>+9.1%}{s:>7.2f}{m:>8.1%}{c:>7.2f}"
              f"{to:>+9.1%}{so:>7.2f}{mo:>8.1%}{co:>7.2f}{sw:>6}")
        show[name] = (t, to, c, co)

    # 买入持有
    print(f"\n买入持有：全区间 {closes[-1]/closes[60]-1:+.1%}｜样本外 {closes[-1]/closes[i0]-1:+.1%}")


if __name__ == "__main__":
    main()