# -*- coding: utf-8 -*-
"""批评实证诊断（_diag_critic_20260826.py）—— 临时脚本，不入库
对 2026-08-26 用户三条批评做数据验证（只读，不写任何状态）：
  A) 现状复现：最新一日各因子分值（对齐用户引用的 -0.53/-1.00/-0.85 等数字）
  B) 批评1/2 实证：深度回撤(dd60)分桶 + 回撤中反弹信号的前瞻收益（1/3/5/10/20日）
  C) 候选因子 IC：下跌速度衰减 decline_decay（新） vs short_reversal/dd60/pullback_52w
  D) 变体回测：V0 基线 / V1 落袋+衰减 / V2 落袋+衰减+短反 / V3 反转独立维，
     含分段（2014-2020 / 2020-2026）与费率敏感性
  E) 批评3：云端影子历史中资金流 main_net 的 IC 与样本量（是否足以支持扩权）
用法：
  python scripts/_diag_critic_20260826.py            # 全部
  python scripts/_diag_critic_20260826.py --part bc  # 只跑 B/C
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src.strategy import chinext_factors as cf
from src.strategy import chinext_timing as ct
from src.strategy.index_pe import load_cy50_pe

SYMBOL = "399006"
HORIZONS = (1, 3, 5, 10, 20)


# ---------------- 新候选因子：下跌速度衰减 ----------------

def factor_decline_decay(close, dd_gate=-0.08, horizon=5, prior=20):
    """下跌速度衰减（候选，2026-08-26）：深度回撤中，近期跌速较前期显著衰减
    或已转涨 → 看多（卖压出清信号）。非回撤环境恒为 0，不干扰趋势层。

    口径：v_recent = 近 horizon 日累计收益；v_prior = 再往前 prior 日累计收益。
    ratio = 每日跌速比（recent/prior）。ratio≤0（转涨/横盘）记 +1；
    ratio≥0.75（跌速未衰减）记 0；中间线性。仅当 v_prior<0（此前在跌）且
    dd60 ≤ dd_gate 时激活。
    """
    out = [0.0] * len(close)
    need = max(60, horizon + prior)
    for i in range(need, len(close)):
        hi = max(close[i - 59:i + 1])
        dd = close[i] / hi - 1.0
        if dd > dd_gate:
            continue
        c_recent = close[i] / close[i - horizon] - 1.0
        c_prior = close[i - horizon] / close[i - horizon - prior] - 1.0
        if c_prior >= 0:
            continue
        ratio = (c_recent / horizon) / (c_prior / prior)
        if ratio <= 0:
            s = 1.0
        elif ratio < 0.75:
            s = 1.0 - ratio / 0.75
        else:
            s = 0.0
        out[i] = round(s, 3)
    return out


# ---------------- 数据 ----------------

def load_data():
    from src.strategy.data import load_index_sina
    df = load_index_sina(SYMBOL, datalen=3000)
    if df is None or df.empty:
        from src.strategy.data import load_index_daily_full
        df = load_index_daily_full(SYMBOL, "20140101")
    if df is None or df.empty:
        raise SystemExit("399006 日线加载失败")
    return df


# ---------------- A) 现状复现 ----------------

def part_a(df):
    closes = df["close"].tolist()
    amounts = (df["amount"].tolist() if "amount" in df else [0.0] * len(closes))
    n = len(closes)
    sig = cf.core_signals(closes, amounts, erp_pctile=None)
    comp = cf.dimension_score(sig, {"趋势": 0.35, "量价": 0.20, "波动": 0.20,
                                    "估值": 0.10, "落袋": 0.15})
    dd60 = closes[-1] / max(closes[-60:]) - 1.0
    hi52 = max(closes[-252:])
    dd52 = closes[-1] / hi52 - 1.0
    m60 = closes[-1] / closes[-61] - 1.0
    print("=" * 72)
    print(f"A) 现状复现｜数据 {df.index[0].date()} ~ {df.index[-1].date()}（{n} 根）")
    print(f"   最新收盘 {closes[-1]:.2f}｜距60日高点回撤 {dd60:+.1%}｜距52周高点 {dd52:+.1%}｜"
          f"60日动量 {m60:+.1%}")
    print(f"   trend_ma20_60={sig['trend_ma20_60'][-1]:+.3f}  "
          f"trend_momentum_60={sig['trend_momentum_60'][-1]:+.3f}")
    print(f"   pullback_52w={sig['pullback_52w'][-1]:+.3f}  dd60={sig['dd60'][-1]:+.3f}")
    print(f"   volprice_quadrant={sig['volprice_quadrant'][-1]:+.3f}  "
          f"volprice_amihud={sig['volprice_amihud'][-1]:+.3f}")
    print(f"   vol_regime={sig['vol_regime'][-1]:+.3f}  vol_term={sig['vol_term'][-1]:+.3f}")
    print(f"   核心综合分 comp={comp[-1]:+.3f}（用户引用 -0.53，含修正层后总分另计）")
    dec = cf.factor_decline_decay if hasattr(cf, "factor_decline_decay") else None
    decay = factor_decline_decay(closes)
    print(f"   decline_decay(候选)={decay[-1]:+.3f}（gate=-0.08）")
    return closes, amounts, sig, comp, decay


# ---------------- B) 条件前瞻收益 ----------------

def fwd_returns(closes, i, h):
    if i + h >= len(closes) or not closes[i]:
        return None
    return closes[i + h] / closes[i] - 1.0


def stat_line(label, idxs, closes):
    n = len(idxs)
    if n == 0:
        print(f"   {label:<34} n=0")
        return
    parts = [f"   {label:<34} n={n:<5}"]
    for h in HORIZONS:
        rets = [fwd_returns(closes, i, h) for i in idxs]
        rets = [r for r in rets if r is not None]
        if not rets:
            parts.append(f"h{h}: —")
            continue
        mu = sum(rets) / len(rets)
        win = sum(1 for r in rets if r > 0) / len(rets)
        parts.append(f"h{h}: {mu:+.2%}/{win:.0%}")
    print("  ".join(parts))


def part_b(closes):
    n = len(closes)
    dd60s = [0.0] * n
    for i in range(60, n):
        dd60s[i] = closes[i] / max(closes[i - 59:i + 1]) - 1.0
    decay = factor_decline_decay(closes)
    print("=" * 72)
    print("B) 条件前瞻收益（均值/胜率，条件只用 ≤t 信息，收益为 t→t+h）")
    print("   —— 批评2：深回撤后到底该防守还是反弹？——")
    allidx = list(range(300, n - 20))
    stat_line("无条件（全体样本）", allidx, closes)
    buckets = [("> -4%", lambda d: d > -0.04), ("-4% ~ -8%", lambda d: -0.08 < d <= -0.04),
               ("-8% ~ -12%", lambda d: -0.12 < d <= -0.08),
               ("-12% ~ -22%", lambda d: -0.22 < d <= -0.12),
               ("≤ -22%", lambda d: d <= -0.22)]
    for name, cond in buckets:
        idxs = [i for i in allidx if cond(dd60s[i])]
        stat_line(f"dd60 {name}", idxs, closes)
    print("\n   —— 批评1：深回撤中的反弹信号前瞻收益 ——")
    deep = [i for i in allidx if dd60s[i] <= -0.12]
    stat_line("dd60≤-12%（全部）", deep, closes)
    r5 = [i for i in deep if closes[i] / closes[i - 5] - 1.0 > 0]
    stat_line("dd60≤-12% & 近5日转涨", r5, closes)
    r5n = [i for i in deep if closes[i] / closes[i - 5] - 1.0 <= 0]
    stat_line("dd60≤-12% & 近5日仍跌", r5n, closes)
    first_up = []
    for i in deep:
        if closes[i] <= closes[i - 1]:
            continue
        k = 0
        j = i - 1
        while j > 60 and closes[j] < closes[j - 1]:
            k += 1
            j -= 1
        if k >= 3:
            first_up.append(i)
    stat_line("dd60≤-12% & ≥3连阴后首阳", first_up, closes)
    stat_line("dd60≤-12% & decay>0", [i for i in deep if decay[i] > 0], closes)
    stat_line("dd60≤-12% & decay>0.5", [i for i in deep if decay[i] > 0.5], closes)
    stat_line("dd60≤-12% & decay=0", [i for i in deep if decay[i] == 0], closes)


# ---------------- C) 因子 IC ----------------

def ic_report(name, factor, closes, n_years=3):
    print(f"\n   {name}")
    for fwd, label in ((1, "次日"), (5, "后5日"), (10, "后10日"), (20, "后20日")):
        pairs = []
        for i in range(len(factor)):
            r = fwd_returns(closes, i, fwd)
            if r is not None:
                pairs.append((float(factor[i]), r))
        if len(pairs) < 10:
            print(f"     {label}: 样本不足 n={len(pairs)}")
            continue
        xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
        ic = ct.spearman_ic(xs, ys)
        seg = pairs[-(n_years * 244):]
        ic_seg = ct.spearman_ic([p[0] for p in seg], [p[1] for p in seg]) \
            if len(seg) >= 10 else None
        segtxt = f"近{n_years}年 {ic_seg:+.4f}" if ic_seg is not None else "分段不足"
        print(f"     {label}: 全史 IC={ic:+.4f}（n={len(pairs)}）｜{segtxt}")


def part_c(closes, amounts):
    print("=" * 72)
    print("C) 因子 IC（Spearman，因子值 vs 前瞻收益；验门 |IC|≥0.05 且近3年符号一致）")
    ic_report("dd60（核心层落袋·60日回撤）", cf.factor_dd60(closes), closes)
    ic_report("pullback_52w（核心层落袋·52周高点）", cf.factor_pullback_52w(closes), closes)
    ic_report("trend_ma20_60（对照·趋势）", cf.factor_trend_ma20_60(closes), closes)
    ic_report("short_reversal_5（既有候选·短期反转）",
              cf.factor_short_reversal(closes, horizon=5), closes)
    for gate in (-0.08, -0.12):
        ic_report(f"decline_decay（新候选·跌速衰减 gate={gate}）",
                  factor_decline_decay(closes, dd_gate=gate), closes)


# ---------------- D) 变体回测 ----------------

def factor_dd60_ushape(close, deep_cap=0.5):
    """dd60 U形重映射（候选）：浅回撤(-4~-8%)延续风险记负分（实证 h20 -1.1%/41%），
    -8% 处最看空 -0.5；深回撤(≤-12%)均值回归转正（实证 h20 +1.9%/57%），
    -25% 处到 +deep_cap 封顶。替代现行"越深越 -1.0"的单调映射。"""
    out = [0.0] * len(close)
    for i in range(60, len(close)):
        hi = max(close[i - 59:i + 1])
        dd = close[i] / hi - 1.0
        if dd > -0.04:
            s = 0.0
        elif dd > -0.08:
            s = -0.5 * (-0.04 - dd) / 0.04          # -4%→0，-8%→-0.5
        elif dd > -0.12:
            s = -0.5 + 0.5 * (-0.08 - dd) / 0.04    # -8%→-0.5，-12%→0
        else:
            s = min(deep_cap, deep_cap * (-0.12 - dd) / 0.13)  # -12%→0，-25%→cap
        out[i] = round(max(-1.0, min(1.0, s)), 3)
    return out


LADDER_ORIG = ((-0.08, 0.6), (-0.12, 0.3))            # 现行生产阶梯
LADDER_RELAX = ((-0.08, 0.6), (-0.12, 0.6))           # 深档 0.3→0.6
LADDER_DEEPER = ((-0.08, 0.6), (-0.12, 0.6), (-0.20, 0.9))  # 再深放至 0.9


def ladder_cap(closes, d, ladder):
    """按给定回撤阶梯算仓位帽（本回测中其余触发器均关闭，等价于 defensive_state）。
    语义：深档覆盖浅档（与原 elif 链一致），如 dd≤-12% 用 -12% 档而非 -8% 档。"""
    dd = closes[d] / max(closes[d - 59:d + 1]) - 1.0 if d >= 59 else 0.0
    cap = 1.0
    for th, c in ladder:
        if dd <= th:
            cap = c
    return cap


def variant_comp(closes, amounts, variant):
    """按变体合成核心分序列（与 cf.dimension_score 同构，仅因子组/权重不同）。"""
    sig = cf.core_signals(closes, amounts, erp_pctile=None)
    n = len(closes)
    base_dims = {"趋势": (0.35, ["trend_ma20_60", "trend_momentum_60"]),
                 "量价": (0.20, ["volprice_quadrant", "volprice_amihud"]),
                 "波动": (0.20, ["vol_regime", "vol_term"]),
                 "估值": (0.10, ["value_erp"]),
                 "落袋": (0.15, ["pullback_52w", "dd60"])}
    if variant == "V0":   # 基线：现行生产配置
        series = dict(sig)
        dims = base_dims
    elif variant == "V5":  # 诊断：落袋维剔除 dd60（只留 pullback_52w）
        series = dict(sig)
        dims = dict(base_dims)
        dims["落袋"] = (0.15, ["pullback_52w"])
    elif variant in ("V1", "V1b"):  # dd60 U形重映射（深端封顶 0.5 / 0.8）
        deep_cap = 0.8 if variant == "V1b" else 0.5
        series = dict(sig)
        series["dd60"] = factor_dd60_ushape(closes, deep_cap=deep_cap)
        dims = base_dims
    else:
        raise ValueError(variant)
    comp = [0.0] * n
    for dim, (w, names) in dims.items():
        for i in range(n):
            vals = [series[k][i] for k in names if k in series]
            if vals:
                comp[i] += w * (sum(vals) / len(vals))
    return [round(max(-1.0, min(1.0, v)), 3) for v in comp]


def bt_metrics(closes, comp, tiers=ct.TIERS, fee=0.0, start=60, end=None,
               ladder=LADDER_ORIG, dd_tier_relax=None):
    """与 run_chinext_timing.backtest_metrics 同口径的本地回测循环（ladder 可注入）。
    dd_tier_relax=(阈值, 放宽后的60%档线)：dd≤阈值时 60% 档线从 -0.30 放宽。
    返回 metrics + 末日状态（prev/comp/cap），供"今天会怎样"复现。"""
    n = end if end is not None else len(closes)
    prev = {"position": 0.0, "pending": None}
    nav = 1.0
    navs, rets = [], []
    switches = 0
    pos_sum = 0.0
    for d in range(start, n - 1):
        cap = ladder_cap(closes, d, ladder)
        t = tiers
        if dd_tier_relax is not None:
            th, line60 = dd_tier_relax
            dd = closes[d] / max(closes[d - 59:d + 1]) - 1.0 if d >= 59 else 0.0
            if dd <= th:
                t = ((0.40, 1.0), (-0.15, 0.9), (line60, 0.6))
        dec = ct.decide_position(comp[d], cap, prev, tiers=t)
        fee_cost = 0.0
        if dec["changed"]:
            switches += 1
            fee_cost = fee * abs(dec["position"] - prev["position"])
            nav *= (1 - fee_cost)
        prev = {"position": dec["position"], "pending": dec["pending"]}
        pos_sum += dec["position"]
        r = closes[d + 1] / closes[d] - 1.0
        nav *= (1 + dec["position"] * r)
        rets.append(dec["position"] * r - fee_cost)
        navs.append(nav)
    total = nav - 1.0
    years = len(navs) / 244.0
    cagr = nav ** (1 / years) - 1 if years > 0 else 0.0
    mdd = min((v / max(navs[:i + 1]) - 1.0) if i else 0.0
              for i, v in enumerate(navs)) if navs else 0.0
    mu = sum(rets) / len(rets) if rets else 0.0
    sd = (sum((x - mu) ** 2 for x in rets) / max(1, len(rets) - 1)) ** 0.5
    sharpe = mu / sd * (244 ** 0.5) if sd > 0 else 0.0
    # 末日决策（用最后一根 bar，不等 d+1 收益；与循环内同一动态 tiers 口径）
    last_d = n - 1
    last_cap = ladder_cap(closes, last_d, ladder)
    t_last = tiers
    if dd_tier_relax is not None:
        th, line60 = dd_tier_relax
        dd = closes[last_d] / max(closes[last_d - 59:last_d + 1]) - 1.0
        if dd <= th:
            t_last = ((0.40, 1.0), (-0.15, 0.9), (line60, 0.6))
    last_dec = ct.decide_position(comp[last_d], last_cap, prev, tiers=t_last)
    return {"total": total, "cagr": cagr, "sharpe": sharpe, "mdd": mdd,
            "calmar": cagr / abs(mdd) if mdd else 0.0,
            "switches": switches, "avg_pos": pos_sum / max(1, len(navs)),
            "n": len(navs), "last": {"comp": comp[last_d], "cap": last_cap,
                                     "target": last_dec["position"],
                                     "note": last_dec["note"]}}


def part_d(df):
    closes = df["close"].tolist()
    amounts = (df["amount"].tolist() if "amount" in df else [0.0] * len(closes))
    n = len(closes)
    date_strs = [d.strftime("%Y-%m-%d") for d in df.index]
    print("=" * 72)
    print("D) 变体回测（口径与官方 backtest_metrics 一致：d 收盘出信号吃 d+1 收益，"
          "纯核心层，fee=0）")
    print(f"   区间 {date_strs[60]} ~ {date_strs[-1]}（{n - 61} 个信号日）")

    # 官方口径对照（校验本地循环 V0 与官方实现一致）
    from scripts.run_chinext_timing import backtest_metrics
    pe_map = load_cy50_pe(PROJECT_ROOT)
    m_off = backtest_metrics(df, fee=0.0, pe_map=pe_map or None, erp_cap=False)
    print(f"   [官方对照·无ERP] {m_off['total']:+.1%} 夏普{m_off['sharpe']:.2f} "
          f"卡玛{m_off['calmar']:.2f}（status.md 基线 +291.9%）")

    variants = [
        ("V0 基线(现行)", "V0", LADDER_ORIG),
        ("V5 剔除dd60", "V5", LADDER_ORIG),
        ("V1 dd60→U形0.5", "V1", LADDER_ORIG),
        ("V1b dd60→U形0.8", "V1b", LADDER_ORIG),
        ("V4 仅放松帽(深档0.6)", "V0", LADDER_RELAX),
        ("V2 U形0.5+深档0.6", "V1", LADDER_RELAX),
        ("V3 基线comp+深0.6/极深0.9", "V0", LADDER_DEEPER),
    ]
    print(f"\n   {'变体':<24}{'累计':>9}{'年化':>8}{'夏普':>7}{'回撤':>8}"
          f"{'卡玛':>7}{'换仓':>5}{'均仓':>6}{'今日目标':>9}")
    results = {}
    for label, cv, ladder in variants:
        comp = variant_comp(closes, amounts, cv)
        m = bt_metrics(closes, comp, ladder=ladder)
        results[label] = (cv, ladder, m)
        print(f"   {label:<24}{m['total']:>+8.1%}{m['cagr']:>+8.1%}"
              f"{m['sharpe']:>7.2f}{m['mdd']:>8.1%}{m['calmar']:>7.2f}"
              f"{m['switches']:>5}{m['avg_pos']:>6.0%}"
              f"{m['last']['target']:>8.0%}")

    # 分段稳健性（前段/后段，2020 切分，状态各自冷启动）
    mid = next((i for i, s in enumerate(date_strs) if s >= "2020-01-01"), n // 2)
    print(f"\n   分段稳健性（前段 {date_strs[60]}~{date_strs[mid-1]} / "
          f"后段 {date_strs[mid]}~{date_strs[-1]}）:")
    for label, cv, ladder in variants:
        comp = variant_comp(closes, amounts, cv)
        m1 = bt_metrics(closes, comp, start=60, end=mid, ladder=ladder)
        m2 = bt_metrics(closes, comp, start=max(60, mid), ladder=ladder)
        print(f"   {label:<24}前段{m1['total']:>+8.1%}/夏普{m1['sharpe']:.2f}"
              f"/回撤{m1['mdd']:.1%} ｜ 后段{m2['total']:>+8.1%}/夏普{m2['sharpe']:.2f}"
              f"/回撤{m2['mdd']:.1%}")

    # 费率敏感性
    print("\n   费率敏感性（fee=0.3%/次换仓）:")
    for label, cv, ladder in variants:
        comp = variant_comp(closes, amounts, cv)
        m = bt_metrics(closes, comp, fee=0.003, ladder=ladder)
        print(f"   {label:<24}累计{m['total']:>+8.1%} 夏普{m['sharpe']:.2f} "
              f"换仓{m['switches']}")

    # 深档帽参数敏感性（防单点运气；comp 固定为基线）
    print("\n   深档帽敏感性（只改回撤阶梯，comp=基线；含分段/费率）:")
    comp0 = variant_comp(closes, amounts, "V0")
    for deepc in (0.3, 0.45, 0.6, 0.75, 0.9):
        lad = ((-0.08, 0.6), (-0.12, deepc))
        m = bt_metrics(closes, comp0, ladder=lad)
        m1 = bt_metrics(closes, comp0, start=60, end=mid, ladder=lad)
        m2 = bt_metrics(closes, comp0, start=mid, ladder=lad)
        mf = bt_metrics(closes, comp0, fee=0.003, ladder=lad)
        print(f"   -12%档帽={deepc:.0%}: 累计{m['total']:+.1%} 夏普{m['sharpe']:.2f} "
              f"回撤{m['mdd']:.1%} 卡玛{m['calmar']:.2f} 换仓{m['switches']}"
              f" ｜ 前段{m1['total']:+.1%} 后段{m2['total']:+.1%} "
              f"｜ fee0.3%: {mf['total']:+.1%}")
    for deepc in (0.75, 0.9):
        lad = ((-0.08, 0.6), (-0.12, 0.6), (-0.20, deepc))
        m = bt_metrics(closes, comp0, ladder=lad)
        print(f"   -12%档0.6+极深-20%档{deepc:.0%}: 累计{m['total']:+.1%} "
              f"夏普{m['sharpe']:.2f} 回撤{m['mdd']:.1%} 卡玛{m['calmar']:.2f} "
              f"换仓{m['switches']}")

    # 深回撤档位线放宽（批评3"钝化"：趋势满负分压制深回撤区的再入场速度）
    print("\n   深回撤档位线放宽（dd≤阈值时 60%档线 -0.30→放宽线，comp=基线）:")
    for th, line, lad, tag in ((-0.20, -0.45, LADDER_ORIG, "原帽"),
                                (-0.20, -0.45, LADDER_RELAX, "深档帽0.6"),
                                (-0.15, -0.40, LADDER_RELAX, "深档帽0.6"),
                                (-0.20, -0.50, LADDER_RELAX, "深档帽0.6"),
                                (-0.25, -0.45, LADDER_RELAX, "深档帽0.6")):
        kw = dict(ladder=lad, dd_tier_relax=(th, line))
        m = bt_metrics(closes, comp0, **kw)
        m1 = bt_metrics(closes, comp0, start=60, end=mid, **kw)
        m2 = bt_metrics(closes, comp0, start=mid, **kw)
        mf = bt_metrics(closes, comp0, fee=0.003, **kw)
        print(f"   dd≤{th:.0%}线{line:+.2f}[{tag}]: 累计{m['total']:+.1%} "
              f"夏普{m['sharpe']:.2f} 回撤{m['mdd']:.1%} 卡玛{m['calmar']:.2f} "
              f"今日{m['last']['target']:.0%} ｜ 前段{m1['total']:+.1%} "
              f"后段{m2['total']:+.1%} ｜ fee0.3%: {mf['total']:+.1%}")

    # 生产口径复检（erp_cap=True + 放松帽）：确认结论在真实生产配置下同样成立
    print("\n   生产口径复检（erp_cap=True，monkeypatch defensive_state 深档帽）:")
    from scripts.run_chinext_timing import backtest_metrics as _btm
    orig_ds = cf.defensive_state

    def _patched_ds(deepc):
        def ds(close, vol_pctile=None, glass=None):
            cap = 1.0
            trig = []
            n = len(close)
            if n >= 60:
                dd = close[-1] / max(close[-60:]) - 1.0
                if dd <= -0.12:
                    cap = min(cap, deepc)
                    trig.append(f"距60日高点回撤{dd * 100:.1f}%封顶{deepc:.0%}")
                elif dd <= -0.08:
                    cap = min(cap, 0.6)
                    trig.append(f"距60日高点回撤{dd * 100:.1f}%封顶6成")
            if vol_pctile is not None and vol_pctile >= 95:
                cap = min(cap, 0.6)
                trig.append(f"创业板波动率{vol_pctile:.0f}分位封顶6成")
            if glass:
                if glass.get("risk_off"):
                    cap = min(cap, 0.3)
                    trig.append("宏观风险状态risk_off封顶3成")
                ap = glass.get("basis_min_ap")
                if ap is not None and ap <= -15:
                    cap = min(cap, 0.6)
                    trig.append(f"深贴水{ap:.1f}%封顶6成")
                ip = glass.get("intraday_pct")
                if ip is not None and ip <= -2.5:
                    cap = min(cap, 0.3)
                    trig.append(f"盘中{ip:.1f}%急跌封顶3成")
            return {"cap": cap, "triggers": trig}
        return ds

    for deepc in (0.3, 0.6, 0.75):
        cf.defensive_state = _patched_ds(deepc)
        m = _btm(df, fee=0.0, pe_map=pe_map or None, erp_cap=True)
        print(f"   深档帽={deepc:.0%}: 累计{m['total']:+.1%} 夏普{m['sharpe']:.2f} "
              f"回撤{m['mdd']:.1%} 卡玛{m['calmar']:.2f} 换仓{m['switches']} "
              f"均仓{m['avg_pos']:.0%}")
    cf.defensive_state = orig_ds

    # 今日状态明细（对齐用户当前情境：dd60 -21.9%）
    print("\n   今日（最后一根bar）各变体状态:")
    for label, cv, ladder in variants:
        comp = variant_comp(closes, amounts, cv)
        m = bt_metrics(closes, comp, ladder=ladder)
        last = m["last"]
        print(f"   {label:<24}comp={last['comp']:+.3f} cap={last['cap']:.0%} "
              f"目标仓位={last['target']:.0%} {'/'.join(last['note'])}")


# ---------------- E) 影子历史资金流 IC ----------------

def part_e():
    print("=" * 72)
    print("E) 批评3：修正层资金流的实证地位（云端影子历史）")
    try:
        from src.strategy.news_link import _read_gist_file
        state = _read_gist_file("chinext_timing_state.json")
    except Exception as ex:
        print(f"   Gist 状态读取失败：{ex}")
        return
    hist = (state or {}).get("shadow_history") or []
    print(f"   影子历史 {len(hist)} 条"
          + (f"（{hist[0].get('date')} ~ {hist[-1].get('date')}）" if hist else ""))
    if not hist:
        print("   无影子样本 → 资金流 IC 无法评估（不满足验门样本≥10）")
        return
    ic = ct.shadow_ic(hist)
    for f in ("flow", "raw.main_net", "mood", "news"):
        if f in ic:
            e = ic[f]
            print(f"   {f:<14} 次日IC={e['ic']} n={e['n']}"
                  + (f"｜5日IC={e['h5']['ic']}(n={e['h5']['n']})" if "h5" in e else ""))
    last = hist[-1]
    print(f"   最新一条：date={last.get('date')} core={last.get('core')} "
          f"flow={last.get('flow')} news={last.get('news')} "
          f"raw.main_net={((last.get('raw') or {}).get('main_net'))}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", default="abcde", help="要跑的部分，如 bc")
    args = ap.parse_args()
    df = load_data()
    closes = df["close"].tolist()
    amounts = (df["amount"].tolist() if "amount" in df else [0.0] * len(closes))
    if "a" in args.part:
        part_a(df)
    if "b" in args.part:
        part_b(closes)
    if "c" in args.part:
        part_c(closes, amounts)
    if "d" in args.part:
        part_d(df)
    if "e" in args.part:
        part_e()


if __name__ == "__main__":
    main()
