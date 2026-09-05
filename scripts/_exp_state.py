# -*- coding: utf-8 -*-
"""状态机防摩擦机制放开实验（_exp_state.py）—— 临时脚本，不入库
用户月交易>3次、已建底仓无赎回费约束 → 测试去掉防摩擦设计的影响：
  S0 基线：升档2日确认 + 滞回带0.05（当前）
  S1 升档当日生效（confirm_days=1）
  S2 升档当日生效 + 去滞回带
  S3 S2 + 0.1%费率（检验放开后摩擦敏感度）
另外做资讯修正敏感性：核心分到档位线的距离分布（±0.06/±0.15 能翻转多少天的档位）。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy import chinext_timing as ct
from src.strategy import chinext_factors as cf
from src.strategy.data import load_index_sina

SYMBOL = "399006"


def run(closes, comp, confirm_days: int, hyst: float, fee: float = 0.0,
        seg=None):
    n = len(closes)
    start = 60
    prev = {"position": 0.0, "pending": None}
    nav = 1.0
    navs, rets = [], []
    switches = 0
    for d in range(start, n - 1):
        caps = cf.defensive_state(closes[: d + 1], None,
                                  {"risk_off": False, "basis_min_ap": None,
                                   "intraday_pct": 0.0})
        # 自定义 tiers/hysteresis：通过 monkeypatch 模块常量
        old_cd, old_hm = ct.UPGRADE_CONFIRM_DAYS, ct.HYST_MARGIN
        ct.UPGRADE_CONFIRM_DAYS, ct.HYST_MARGIN = confirm_days, hyst
        try:
            dec = ct.decide_position(comp[d], caps["cap"], prev, tiers=ct.TIERS)
        finally:
            ct.UPGRADE_CONFIRM_DAYS, ct.HYST_MARGIN = old_cd, old_hm
        if dec["changed"]:
            switches += 1
            nav *= (1 - fee * abs(dec["position"] - prev["position"]))
        prev = {"position": dec["position"], "pending": dec["pending"]}
        if seg and not (seg[0] <= d < seg[1]):
            continue
        r = closes[d + 1] / closes[d] - 1.0
        nav *= (1 + dec["position"] * r)
        navs.append(nav)
        rets.append(dec["position"] * r)
    mu = sum(rets) / len(rets)
    sd = (sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5
    mdd = min(v / max(navs[:i + 1]) - 1.0 for i, v in enumerate(navs))
    years = len(navs) / 244.0
    cagr = nav ** (1 / years) - 1
    return nav - 1.0, mu / sd * 244 ** 0.5, mdd, cagr / abs(mdd) if mdd else 0, \
        switches, switches / years / 12


def run_instant(closes, comp, fee: float = 0.0, seg=None):
    """完全即时档位：score→tier 当日生效，无确认无滞回，cap 仍封顶。"""
    n = len(closes)
    start = 60
    prev_pos = 0.0
    nav = 1.0
    navs, rets = [], []
    switches = 0
    for d in range(start, n - 1):
        caps = cf.defensive_state(closes[: d + 1], None,
                                  {"risk_off": False, "basis_min_ap": None,
                                   "intraday_pct": 0.0})
        target = min(ct.score_to_tier(comp[d], ct.TIERS), caps["cap"])
        if abs(target - prev_pos) > 1e-9:
            switches += 1
            nav *= (1 - fee * abs(target - prev_pos))
        prev_pos = target
        if seg and not (seg[0] <= d < seg[1]):
            continue
        r = closes[d + 1] / closes[d] - 1.0
        nav *= (1 + target * r)
        navs.append(nav)
        rets.append(target * r)
    mu = sum(rets) / len(rets)
    sd = (sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5
    mdd = min(v / max(navs[:i + 1]) - 1.0 for i, v in enumerate(navs))
    years = len(navs) / 244.0
    cagr = nav ** (1 / years) - 1
    return nav - 1.0, mu / sd * 244 ** 0.5, mdd, cagr / abs(mdd) if mdd else 0, \
        switches, switches / years / 12


def main():
    df = load_index_sina(SYMBOL)
    closes = df["close"].tolist()
    amounts = (df["amount"].tolist() if "amount" in df else [0.0] * len(closes))
    n = len(closes)
    signals = cf.core_signals(closes, amounts, erp_pctile=None)
    comp = cf.dimension_score(signals, {"趋势": 0.35, "量价": 0.20, "波动": 0.20,
                                        "估值": 0.10, "落袋": 0.15})
    i0 = next(i for i, d in enumerate(df.index) if d.year >= 2020)

    print(f"{'变体':<34}{'全区间':>9}{'夏普':>7}{'回撤':>8}{'卡玛':>7}"
          f"{'|样本外':>9}{'|月均切换':>9}{'全切换':>7}")
    for name, cd, hm, fee in [
        ("S0 基线(2日确认+滞回)", 2, 0.05, 0.0),
        ("S2 升档即时+去滞回", 1, 0.0, 0.0),
        ("S3 S2+0.1%费", 1, 0.0, 0.001),
    ]:
        t, s, m, c, sw, msw = run(closes, comp, cd, hm, fee)
        to, so, mo, co, swo, mswo = run(closes, comp, cd, hm, fee, seg=(i0, n - 1))
        print(f"{name:<34}{t:>+9.1%}{s:>7.2f}{m:>8.1%}{c:>7.2f}"
              f"{to:>+9.1%}{msw:>9.2f}{sw:>7}")
    for name, fee in [
        ("S4 完全即时(无确认无滞回)", 0.0),
        ("S5 完全即时+0.1%费", 0.001),
    ]:
        t, s, m, c, sw, msw = run_instant(closes, comp, fee)
        to, so, mo, co, swo, mswo = run_instant(closes, comp, fee, seg=(i0, n - 1))
        print(f"{name:<34}{t:>+9.1%}{s:>7.2f}{m:>8.1%}{c:>7.2f}"
              f"{to:>+9.1%}{msw:>9.2f}{sw:>7}")

    # ---- 资讯修正敏感性：核心分距档位线距离 ----
    print("\n== 资讯修正敏感性（±X 能翻转档位的天数）==")
    lines = [th for th, _ in ct.TIERS]  # 档位阈值
    for x in (0.06, 0.10, 0.15, 0.30):
        flips = 0
        for d in range(60, len(comp) - 1):
            def tier_of(s):
                for th, pos in ct.TIERS:
                    if s >= th:
                        return pos
                return 0.0
            if tier_of(comp[d]) != tier_of(comp[d] + x):
                flips += 1
        print(f"资讯修正 +{x:.2f} → 档位翻转 {flips} 天（{flips/(len(comp)-61):.1%}）")


if __name__ == "__main__":
    main()