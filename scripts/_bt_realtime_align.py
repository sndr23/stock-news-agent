# -*- coding: utf-8 -*-
"""回测信息口径夹逼实验（_bt_realtime_align.py）—— 临时脚本，不入库
问题：官方回测的信号含 d 日【收盘价】，而实盘 14:30 决策时只知道 14:30 快照。
回测是否因此虚高？跑两个口径夹逼真实值：
  口径A（官方）：信号 comp[d] + cap(closes[:d+1])  ← 假设收盘后决策（乐观上界）
  口径B（保守）：信号 comp[d-1] + cap(closes[:d])  ← 只用 ≤d-1 收盘（丢弃当日全部信息）
实盘真实信息量（≤d-1 收盘 + d 日 14:30 快照）介于 A、B 之间。
两口径都用 closes[d] 成交、吃 closes[d+1] 收益（对齐场外基金 15:00 前下单）。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy import chinext_timing as ct
from src.strategy import chinext_factors as cf
from src.strategy.data import load_index_sina

SYMBOL = "399006"
FEE = 0.0


def run(comp, cap_lag: int) -> dict:
    """cap_lag=0 → cap 用 closes[:d+1]（官方）；cap_lag=1 → cap 用 closes[:d]（保守）。
    信号统一取 comp[d - cap_lag]：官方=comp[d]，保守=comp[d-1]。"""
    df = load_index_sina(SYMBOL)
    closes = df["close"].tolist()
    n = len(closes)
    start = 61 if cap_lag else 60
    prev = {"position": 0.0, "pending": None}
    nav, peak = 1.0, 1.0
    navs, rets, pos_sum, switches = [], [], 0.0, 0
    for d in range(start, n - 1):
        caps = cf.defensive_state(closes[: d + 1 - cap_lag], None,
                                  {"risk_off": False, "basis_min_ap": None,
                                   "intraday_pct": 0.0})
        dec = ct.decide_position(comp[d - cap_lag], caps["cap"], prev, tiers=ct.TIERS)
        if dec["changed"]:
            switches += 1
            nav *= (1 - FEE * abs(dec["position"] - prev["position"]))
        prev = {"position": dec["position"], "pending": dec["pending"]}
        r = closes[d + 1] / closes[d] - 1.0
        nav *= (1 + dec["position"] * r)
        peak = max(peak, nav)
        navs.append(nav)
        rets.append(dec["position"] * r)
        pos_sum += dec["position"]
    mu = sum(rets) / len(rets)
    sd = (sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5
    mdd = min(v / max(navs[:i + 1]) - 1.0 for i, v in enumerate(navs))
    years = len(navs) / 244.0
    return {"total": nav - 1.0, "sharpe": mu / sd * 244 ** 0.5, "mdd": mdd,
            "avg_pos": pos_sum / len(navs), "switches": switches,
            "cagr": nav ** (1 / years) - 1,
            "calmar": (nav ** (1 / years) - 1) / abs(mdd) if mdd else 0.0}


def main():
    df = load_index_sina(SYMBOL)
    closes = df["close"].tolist()
    amounts = (df["amount"].tolist() if "amount" in df else [0.0] * len(closes))
    signals = cf.core_signals(closes, amounts, erp_pctile=None)
    comp = cf.dimension_score(signals, {"趋势": 0.35, "量价": 0.20, "波动": 0.20,
                                        "估值": 0.10, "落袋": 0.15})
    a = run(comp, 0)  # 官方：含 d 收盘
    b = run(comp, 1)  # 保守：只用 ≤d-1
    bh = closes[-1] / closes[60] - 1.0
    print(f"区间 {df.index[60].date()} ~ {df.index[-1].date()}，买入持有 {bh:+.1%}")
    print(f"{'口径':<28}{'累计':>9}{'年化':>8}{'夏普':>7}{'回撤':>8}{'卡玛':>7}{'均仓':>7}{'换仓':>6}")
    print(f"{'A 官方(含d日收盘)':<26}{a['total']:>+9.1%}{a['cagr']:>+8.1%}"
          f"{a['sharpe']:>7.2f}{a['mdd']:>8.1%}{a['calmar']:>7.2f}{a['avg_pos']:>7.0%}{a['switches']:>6}")
    print(f"{'B 保守(只用≤d-1收盘)':<26}{b['total']:>+9.1%}{b['cagr']:>+8.1%}"
          f"{b['sharpe']:>7.2f}{b['mdd']:>8.1%}{b['calmar']:>7.2f}{b['avg_pos']:>7.0%}{b['switches']:>6}")
    print(f"\nA-B 累计差 {a['total'] - b['total']:+.1%}（回测相对14:30实时口径的乐观上界估计）")


if __name__ == "__main__":
    main()