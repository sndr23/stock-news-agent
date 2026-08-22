# -*- coding: utf-8 -*-
"""
创业板多因子择时核心（chinext_timing.py）
====================================================
纯逻辑层（无网络），可完整单测。设计目标：可靠 > 灵敏。

三层架构：
  1) 核心层（可回测，权重 100% 基准）：
     趋势 0.40 + 动量 0.30 + 量价 0.15 + 波动 0.15
     全部由 399006 日线（close+amount）计算，可用两年半历史回测验证。
  2) 修正层（不可回测的当日实时数据，有界）：
     衍生品(±0.15) + 资金(±0.10) + 市场情绪(±0.08) + 资讯情绪(±0.15)
     合计封顶 ±0.30——中性市场最多被推到六成档，永远到不了满仓档。
  3) 硬风控（最高优先级，覆盖一切）：
     回撤/波动分位/risk_off/深贴水+大加空/盘中暴跌 → 仓位封顶。

档位状态机（0/60/90/100%，v5 收益优先中枢上移）：升档需连续两日同目标确认，降档当日生效
（非对称：进场慢、出场快——场外基金申赎费高，宁可错过不可做错）。

无前视约定：t 日 14:30 信号只使用 ≤t-1 收盘 + t 日盘中实时涨跌幅。
"""
from __future__ import annotations

from typing import Optional

# ---------------- 通用工具 ----------------


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def cum_return(closes: list) -> float:
    """区间累计涨幅（小数）。"""
    if len(closes) < 2 or not closes[0]:
        return 0.0
    return closes[-1] / closes[0] - 1.0


# ---------------- 核心层四维 ----------------

def trend_score(closes: list) -> dict:
    """趋势：收盘vs MA20(±0.5)、MA20斜率(±0.3)、收盘vs MA60(±0.2)。"""
    c = closes[-60:]
    if len(closes) < 60:
        return {"score": 0.0, "ma20": None, "ma60": None, "above20": None}
    ma20 = _mean(closes[-20:])
    ma20_5ago = _mean(closes[-25:-5])
    ma60 = _mean(closes[-60:])
    last = closes[-1]
    s = 0.0
    s += 0.5 if last > ma20 else -0.5
    s += 0.3 if ma20 > ma20_5ago else -0.3
    s += 0.2 if last > ma60 else -0.2
    return {"score": round(s, 3), "ma20": round(ma20, 1), "ma60": round(ma60, 1),
            "above20": last > ma20}


def momentum_score(closes: list, intraday_pct: float = 0.0) -> dict:
    """动量：20日(0.5) + 60日(0.3) + 盘中实时(0.2)，各按尺度归一到 [-1,1]。"""
    if len(closes) < 60:
        return {"score": 0.0, "m20": 0.0, "m60": 0.0}
    m20 = cum_return(closes[-20:]) * 100
    m60 = cum_return(closes[-60:]) * 100
    s = 0.5 * clamp(m20 / 10.0) + 0.3 * clamp(m60 / 20.0) + 0.2 * clamp(intraday_pct / 3.0)
    return {"score": round(s, 3), "m20": round(m20, 2), "m60": round(m60, 2)}


def volume_price_score(closes: list, amounts: list, intraday_pct: float = 0.0) -> dict:
    """量价：昨bar量能分位 × 涨跌方向的经典四象限 + 盘中方向修正。

    放量(≥80分位)上涨 +0.8 / 放量下跌 -0.8；缩量(≤30分位)反弹 +0.2 /
    缩量阴跌 -0.3；中性量 0.3×方向。盘中暴跌(≤-1.5%)时压制（今日可能在出货）。
    """
    if len(closes) < 21 or len(amounts) < 21 or not any(amounts[-60:]):
        return {"score": 0.0, "pctile": None}
    ret1 = (closes[-1] / closes[-2] - 1.0) * 100 if closes[-2] else 0.0
    window = [a for a in amounts[-60:] if a > 0]
    pctile = sum(1 for a in window if a <= amounts[-1]) / len(window) if window else 0.5
    if pctile >= 0.8:
        base = 0.8 if ret1 > 0 else -0.8
    elif pctile <= 0.3:
        base = 0.2 if ret1 > 0 else -0.3
    else:
        base = 0.3 if ret1 > 0 else -0.3
    if intraday_pct <= -1.5:
        base = min(base, 0.0) - 0.2
    elif intraday_pct >= 1.5:
        base = max(base, 0.0) + 0.1
    return {"score": round(clamp(base), 3), "pctile": round(pctile, 2)}


def volatility_score(closes: list) -> dict:
    """波动（只罚不奖）：20日波动率z(近1年) + 距60日高点回撤，取更严者。"""
    if len(closes) < 252:
        if len(closes) < 61:
            return {"score": 0.0, "dd60": 0.0, "vol_z": 0.0}
        dd = closes[-1] / max(closes[-60:]) - 1.0
        z = 0.0
    else:
        rets = [(closes[i] / closes[i - 1] - 1.0) for i in range(len(closes) - 252, len(closes))]
        vols = [float("nan")] * 19
        for i in range(19, len(rets)):
            vols.append(_std(rets[i - 19:i + 1]))
        hist = vols[19:-1]
        cur = vols[-1]
        mu, sd = _mean(hist), _std(hist)
        z = (cur - mu) / sd if sd > 0 else 0.0
        dd = closes[-1] / max(closes[-60:]) - 1.0
    z_pen = -clamp((z - 1.0) / 1.5, 0.0, 1.0) if z > 1.0 else 0.0
    if dd <= -0.12:
        dd_pen = -1.0
    elif dd <= -0.08:
        dd_pen = -0.5
    else:
        dd_pen = 0.0
    return {"score": round(min(z_pen, dd_pen), 3),
            "dd60": round(dd * 100, 2), "vol_z": round(z, 2)}


def _std(xs):
    xs = list(xs)
    if len(xs) < 2:
        return 0.0
    mu = _mean(xs)
    return (sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def core_score(closes: list, amounts: list, intraday_pct: float = 0.0) -> dict:
    """核心层合成：0.40趋势+0.30动量+0.15量价+0.15波动。"""
    t = trend_score(closes)
    m = momentum_score(closes, intraday_pct)
    v = volume_price_score(closes, amounts, intraday_pct)
    d = volatility_score(closes)
    score = 0.40 * t["score"] + 0.30 * m["score"] + 0.15 * v["score"] + 0.15 * d["score"]
    return {"score": round(score, 3), "trend": t, "momentum": m,
            "volprice": v, "vol": d}


# ---------------- 修正层四维（有界） ----------------

def derivatives_modifier(basis: dict, citic_net: Optional[float] = None) -> dict:
    """衍生品：IC/IM 年化贴水（百分点）+ 中信全合约净持仓（手）。±0.15。"""
    aps = []
    for code in ("IC", "IM", "IF"):
        b = (basis or {}).get(code) or {}
        try:
            aps.append(float(b.get("annual_pct")))
        except (TypeError, ValueError):
            continue
    worst = min(aps) if aps else None
    s = 0.0
    detail = []
    if worst is not None:
        if worst <= -15:
            s -= 0.15
        elif worst <= -8:
            s -= 0.10
        elif worst <= -4:
            s -= 0.05
        elif worst >= 0:
            s += 0.05
        detail.append(f"IC/IM最差年化{worst:.1f}%")
    if citic_net is not None:
        if citic_net <= -2000:
            s -= 0.10
        elif citic_net >= 2000:
            s += 0.05
        if citic_net:
            detail.append(f"中信净{citic_net:+.0f}手")
    return {"score": round(clamp(s, -0.15, 0.15), 3), "detail": "；".join(detail)}


def flows_modifier(flows: dict, sector_flows: dict = None,
                   tech_kw: tuple = ()) -> dict:
    """资金：两市主力净流入(亿) + 行业资金流科技方向。±0.10。"""
    s = 0.0
    detail = []
    try:
        main = float((flows or {}).get("main_net_yi"))
    except (TypeError, ValueError):
        main = None
    if main is not None:
        if main <= -100:
            s -= 0.10
        elif main <= -50:
            s -= 0.05
        elif main >= 100:
            s += 0.05
        elif main >= 50:
            s += 0.03
        detail.append(f"主力净{main:+.0f}亿")
    if tech_kw:
        inflow = " ".join(str(x) for x in ((sector_flows or {}).get("inflow") or []))
        outflow = " ".join(str(x) for x in ((sector_flows or {}).get("outflow") or []))
        if any(k in outflow for k in tech_kw):
            s -= 0.03
            detail.append("科技板块在净流出榜")
        elif any(k in inflow for k in tech_kw):
            s += 0.03
            detail.append("科技板块在净流入榜")
    return {"score": round(clamp(s, -0.10, 0.10), 3), "detail": "；".join(detail)}


def mood_modifier(sentiment: dict, breadth: dict, option: dict = None) -> dict:
    """市场情绪：涨停温度计 + 涨跌宽度 + 期权PCR。±0.08。"""
    s = 0.0
    detail = []
    mood = str((sentiment or {}).get("mood") or "")
    if mood == "亢奋":
        s += 0.05
    elif mood == "冰点":
        s -= 0.05
    if mood:
        detail.append(f"涨停情绪{mood}")
    try:
        down_pct = float((breadth or {}).get("down_pct"))
    except (TypeError, ValueError):
        down_pct = None
    if down_pct is not None:
        if down_pct >= 70:
            s -= 0.05
            detail.append(f"下跌家数{down_pct:.0f}%")
        elif down_pct <= 30:
            s += 0.03
            detail.append(f"下跌家数仅{down_pct:.0f}%")
    try:
        pcr = float((option or {}).get("pcr"))
    except (TypeError, ValueError):
        pcr = None
    if pcr is not None:
        if pcr >= 1.5:
            s -= 0.03
            detail.append(f"PCR {pcr:.2f}")
        elif pcr <= 0.55:
            s += 0.03
            detail.append(f"PCR {pcr:.2f}")
    return {"score": round(clamp(s, -0.08, 0.08), 3), "detail": "；".join(detail)}


TECH_KW = ("光模块", "CPO", "AI", "算力", "半导", "芯片", "通信", "PCB",
           "消费电子", "电子", "机器人", "新能源", "锂电", "光伏", "创业板",
           "科技", "软件", "云计算", "大数据", "智能")
MACRO_KW = ("证监会", "央行", "财政", "国务院", "GDP", "CPI", "印花税",
            "汇率", "美联储", "关税", "货币", "利率")


def news_modifier(events: list, dir_sign: dict = None) -> dict:
    """资讯情绪：今日已推事件方向×科技相关度加权。±0.15。

    events: [{t, title_norm, dir, sectors, entities, ...}]
    dir_sign: {"bullish": 1.0, ...}（与 news_link._DIR_LABEL 同口径）
    相关度：科技 1.0 / 宏观 0.6 / 其他 0.2。
    """
    dir_sign = dir_sign or {"bullish": 1.0, "mildly_bullish": 0.5, "neutral": 0.0,
                            "mixed": 0.0, "mildly_bearish": -0.5, "bearish": -1.0}
    if not events:
        return {"score": 0.0, "detail": "今日无已推事件", "n": 0}
    total = 0.0
    for e in events:
        d = str(e.get("dir") or "")
        sign = dir_sign.get(d, 0.0)
        if not sign:
            continue
        text = " ".join([str(e.get("title_norm") or ""),
                         " ".join(str(x) for x in (e.get("sectors") or [])),
                         " ".join(str(x) for x in (e.get("entities") or []))])
        if any(k in text for k in TECH_KW):
            rel = 1.0
        elif any(k in text for k in MACRO_KW):
            rel = 0.6
        else:
            rel = 0.2
        total += sign * rel
    raw = total / max(3, len(events))
    n = len(events)
    detail = f"{n}条事件 净情绪{total:+.1f}"
    return {"score": round(clamp(raw, -1, 1) * 0.15, 3), "detail": detail, "n": n}


# ---------------- 硬风控 + 档位状态机 ----------------

def defensive_caps(closes: list, intraday_pct: float, snapshot: dict,
                   citic_net: Optional[float] = None) -> dict:
    """硬风控仓位封顶（只降不升）。返回 {"cap": 0~1, "reasons": [...]}。"""
    cap = 1.0
    reasons = []
    if len(closes) >= 60:
        dd = closes[-1] / max(closes[-60:]) - 1.0
        if dd <= -0.12:
            cap = min(cap, 0.3)
            reasons.append(f"距60日高点回撤{dd * 100:.1f}%（深回撤，封顶3成）")
    vol = ((snapshot or {}).get("vol") or {}).get("创业板指") or {}
    try:
        pctile = float(vol.get("pctile"))
    except (TypeError, ValueError):
        pctile = None
    if pctile is not None and pctile >= 95:
        cap = min(cap, 0.6)
        reasons.append(f"创业板波动率{pctile:.0f}分位（极端高波，封顶6成）")
    if str((snapshot or {}).get("risk_state") or "") == "risk_off":
        cap = min(cap, 0.3)
        reasons.append("宏观风险状态 risk_off（封顶3成）")
    aps = []
    for code in ("IC", "IM"):
        b = ((snapshot or {}).get("basis") or {}).get(code) or {}
        try:
            aps.append(float(b.get("annual_pct")))
        except (TypeError, ValueError):
            pass
    if aps and min(aps) <= -12 and citic_net is not None and citic_net <= -2000:
        cap = min(cap, 0.6)
        reasons.append("深度贴水+中信大幅加空（封顶6成）")
    if intraday_pct <= -2.5:
        cap = min(cap, 0.3)
        reasons.append(f"盘中{intraday_pct:.1f}%急跌（当日封顶3成）")
    return {"cap": cap, "reasons": reasons}


TIERS = ((0.40, 1.0), (-0.15, 0.9), (-0.30, 0.6))
HYST_MARGIN = 0.05  # 降档滞回带：需明确跌破阈值-0.05 才降，防阈值震荡换仓
UPGRADE_CONFIRM_DAYS = 2  # 升档需连续 N 日同目标确认（降档无此约束，风控优先）
MIN_IC_SAMPLES = 10  # 影子 IC 最小样本门槛（对齐 |IC|≥0.05 + 样本≥10 的因子验门）


def score_to_tier(score: float, tiers: tuple = TIERS) -> float:
    for th, pos in tiers:
        if score >= th:
            return pos
    return 0.0


def _tier_with_hysteresis(score: float, cur: float, tiers: tuple = TIERS) -> float:
    """滞回分档：维持/降档档位的进入阈值放宽 margin，升档阈值不变。

    场景：分数在 0.35 线上下震荡时，原逻辑会 满仓→六成(即时)→满仓(两日确认)
    反复换仓吃申赎费；加带后需明确跌破 0.30 才降档。
    """
    for th, pos in tiers:
        th_eff = th if pos > cur else th - HYST_MARGIN
        if score >= th_eff:
            return pos
    return 0.0


def decide_position(score: float, cap: float, prev: dict,
                    tiers: tuple = TIERS) -> dict:
    """非对称档位状态机：升档需连续两日同目标确认；降档当日生效（带滞回带）；
    防守 cap 触发的降档无滞回（风控优先）。

    prev: {"position": float, "pending": {"target": float, "days": int} | None}
    返回: {"position", "pending", "changed", "direction", "note"}
    """
    cur = float((prev or {}).get("position") or 0.0)
    pending = (prev or {}).get("pending") or None
    raw = _tier_with_hysteresis(score, cur, tiers)
    capped = score_to_tier(score, tiers) < raw  # cap 触发的降档走无滞回路径
    target = min(raw, cap)
    note = []

    if target < cur - 1e-9:
        via_cap = "（风控封顶，无滞回）" if capped else "（滞回带确认）"
        return {"position": target, "pending": None, "changed": True,
                "direction": "down", "note": ["降档当日生效" + via_cap]}
    if target > cur + 1e-9:
        if pending and abs(pending.get("target", 0) - target) < 1e-9:
            days = int(pending.get("days", 0)) + 1
            if days >= UPGRADE_CONFIRM_DAYS:
                return {"position": target, "pending": None, "changed": True,
                        "direction": "up", "note": [f"连续{days}日确认，升档生效"]}
            return {"position": cur, "pending": {"target": target, "days": days},
                    "changed": False, "direction": "hold",
                    "note": [f"升档待确认（{days}/{UPGRADE_CONFIRM_DAYS}日）"]}
        return {"position": cur, "pending": {"target": target, "days": 1},
                "changed": False, "direction": "hold", "note": ["升档首日，待次日确认"]}
    return {"position": cur, "pending": None, "changed": False,
            "direction": "hold", "note": []}


MOD_TOTAL_CAP = 0.30  # 修正层合计封顶：中性市场最多被推到六成档，永远到不了满仓档


def stock_confirm(stock_trend: dict, stock_mom: dict, index_trend: dict,
                  intraday_pct: float = 0.0) -> dict:
    """科技龙头情绪标的（中际旭创）二次确认：指数信号方向 × 个股趋势/动量/当日走势。

    定位：用户持有基金重仓中际旭创，它作为创业板科技龙头的**情绪领先指标**，
    提供方向确认而非替代主信号（创业板指数）。
    - 指数看多但情绪标的走弱（龙头先于指数走弱）→ 降档确认（-0.10）
    - 指数看空但情绪标的走强（龙头先于指数企稳）→ 升档确认（+0.08）
    - 两者同向 → 中性（不额外加分，主信号已覆盖）
    当日盘中涨幅 intraday_pct 并入个股方向（当日龙头强弱反映情绪），
    不单独构成触发，只微调 stock_dir 的敏感窗。
    返回 {"score": 有界修正, "agree": bool, "detail": str}。
    """
    if not stock_trend or not stock_mom or not index_trend:
        return {"score": 0.0, "agree": None, "detail": "个股数据不足，跳过"}
    st = float(stock_trend.get("score") or 0.0)
    sm = float(stock_mom.get("score") or 0.0)
    it = float(index_trend.get("score") or 0.0)
    # 个股综合方向（趋势+动量等权）；当日盘中强弱折算为动量增量（上限±0.4）
    day = max(-0.4, min(0.4, intraday_pct / 5.0))
    stock_dir = 0.5 * st + 0.5 * sm + day
    agree = (stock_dir >= 0) == (it >= 0)
    s = 0.0
    detail = (f"个股方向{stock_dir:+.2f}/指数{it:+.2f}"
              f"(盘中{intraday_pct:+.1f}%)")
    if not agree:
        # 背离：指数看多但个股走弱 → 降档；指数看空但个股走强 → 温和升档
        if it >= 0 and stock_dir < -0.1:
            s = -0.10
            detail += "（指数多/个股弱，降档确认）"
        elif it < 0 and stock_dir > 0.1:
            s = 0.08
            detail += "（指数空/个股强，企稳确认）"
        else:
            detail += "（轻微背离，不动作）"
    else:
        detail += "（同向，主信号已覆盖）"
    return {"score": round(clamp(s, -0.10, 0.10), 3), "agree": agree,
            "detail": detail}


def composite(core: dict, deriv: dict, flow: dict, mood: dict, news: dict) -> float:
    """总分 = 核心层 + 修正层（合计封顶 ±0.30），整体 clamp 到 [-1, 1]。

    设计约束：实时修正数据不可回测，只能"倾斜"不能"定档"——
    核心分 0（中性）时，即使四项修正全部拉满也只有 +0.30 < 满仓线 0.35。
    """
    mods = clamp(deriv["score"] + flow["score"] + mood["score"] + news["score"],
                 -MOD_TOTAL_CAP, MOD_TOTAL_CAP)
    return round(clamp(core["score"] + mods), 3)


# ---------------- 影子期因子有效性验证（影子 IC） ----------------

def _rank(a):
    """平均秩（同值取平均秩），返回与 a 等长的秩序列。"""
    n = len(a)
    order = sorted(range(n), key=lambda i: a[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and a[order[j + 1]] == a[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1  # 1-based 平均秩
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def shadow_ic(history: list, fields: tuple = ("core", "basis", "flow", "mood",
                                              "news", "position")) -> dict:
    """影子期各因子的 Spearman IC（因子秩 vs 前瞻收益秩）。
    前瞻口径：next_ret(1日)/r3(3日)/r5(5日)/r10(10日)。样本 < MIN_IC_SAMPLES 记不足。
    返回 {field: {"ic": float|None, "n": int, **{"horizon_<h>": ic_d...}}}。
    history 元素形如 {"date","score","core","basis","flow","mood","news",
                       "chan","stock","kospi","sox","vix","a50","position",
                       "next_ret","r3","r5","r10"}。
    """
    horizons = {"1": "next_ret", "3": "r3", "5": "r5", "10": "r10"}
    out = {}
    for f in fields:
        entry = {}
        for hlab, hkey in horizons.items():
            pairs = [(float(h.get(f, 0.0)), float(h[hkey]))
                     for h in history if h.get(hkey) is not None and f in h]
            if len(pairs) >= MIN_IC_SAMPLES:
                xs = [p[0] for p in pairs]
                ys = [p[1] for p in pairs]
                entry[f"h{hlab}"] = {"ic": round(spearman_ic(xs, ys), 4),
                                     "n": len(pairs)}
            else:
                entry[f"h{hlab}"] = {"ic": None, "n": len(pairs)}
        entry["ic"] = entry["h1"]["ic"]  # 主口径仍为 1 日(main 预览兼容)
        entry["n"] = entry["h1"]["n"]
        out[f] = entry
    return out


def spearman_ic(xs: list, ys: list) -> float:
    rx, ry = _rank(list(xs)), _rank(list(ys))
    n = len(rx)
    if n < 2:
        return 0.0
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    den = (sum((v - mx) ** 2 for v in rx) * sum((v - my) ** 2 for v in ry)) ** 0.5
    return num / den if den else 0.0
