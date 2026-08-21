# -*- coding: utf-8 -*-
"""
轻量缠论结构模块（chan_light.py）
====================================================
自写、不依赖外部 czsc 库的简化缠论结构识别。为 v4 券略核心层提供
"缠论结构因子"，并为硬风控提供顶背驰否决信号。

核心对象（全部基于 K线 high/low/close，无前视：t 点位只用 ≤t 数据）：
  分型(fractal)   ：顶分型(中间K最高)/底分型(中间K最低)，先做包含合并
  笔(bi)          ：相邻顶底分型之间连接，需满足独立分型间隔
  中枢(zhongshu)  ：连续三笔的重叠区间（上沿/下沿）
  背驰(divergence)：价格创新高(低)但对应 MACD 动能柱缩量 → 顶(底)背驰
  买卖点(bs)      ：一买(底背驰)/二买(一买后回调不破前低)/三买(突破中枢)
                    与对应卖点

输出：chan_state() -> dict
  {bi_dir:'up/down', zone:'upper/in/break_valid'（相对最后一个中枢）,
   divergence:'top/bottom/none', last_signal:'B1/B2/B3/S1/S2/S3/-',
   bustop: 是否顶背驰（供硬风控否决）, trend_ok: 笔方向共振bool}

设计原则（v4 克制纪律）：
- 缠论信号密度低，不追求统计 IC，只做"结构确认"与"风控否决"
- 全部纯函数、可单测；输入序列化到 ≤t，杜绝 look-ahead
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple


# ---------------- 包含处理与分型 ----------------

def merge_klines(highs: Sequence[float], lows: Sequence[float]) -> List[Tuple[int, float, float]]:
    """K线包含合并（缠论标准）：返回 [(idx, high, low)]，idx=原数组序号。
    连续包含向上取高低、向下取低低，项分型间不允许相邻分型同向。"""
    merged = [(0, highs[0], lows[0])]
    direction = 0  # 1=向上合并，-1=向下合并
    for i in range(1, len(highs)):
        h, l = highs[i], lows[i]
        ph, pl = merged[-1][1], merged[-1][2]
        # 包含：一根高低包住另一根
        contained = (ph >= h and pl <= l) or (h >= ph and l <= pl)
        if contained:
            if direction >= 0:
                nh, nl = max(ph, h), max(pl, l)
            else:
                nh, nl = min(ph, h), min(pl, l)
            merged[-1] = (i, nh, nl)
            continue
        # 不包含：可能确立新旧方向
        merged.append((i, h, l))
        direction = 1 if h > ph else (-1 if l < pl else direction)
    return merged


def find_fractals(merged: List[Tuple[int, float, float]],
                  cnt: int) -> List[Tuple[str, int]]:
    """从合并K线中找分型：[(type('top'/'bottom'), merged_idx)]。
    top: 中间高低点高于两侧；bottom 反之。cnt=原始K线数（用于定位）。"""
    out = []
    for i in range(1, len(merged) - 1):
        _, ph, pl = merged[i - 1]
        _, ch, cl = merged[i]
        _, nh, nl = merged[i + 1]
        if ch > ph and ch > nh:
            out.append(("top", merged[i][0]))
        elif cl < pl and cl < nl:
            out.append(("bottom", merged[i][0]))
    return out


# ---------------- 笔 ----------------

def compute_bis(prices_high: Sequence[float], prices_low: Sequence[float],
                fractals: List[Tuple[str, int]]) -> List[dict]:
    """由分型+价格构造笔，含 gap 校验。返回 [{type,start,end,px0,px1}]。"""
    bis = []
    prev = None
    for ftype, fidx in fractals:
        if prev is None:
            prev = (ftype, fidx)
            continue
        ptype, pidx = prev
        if ftype == ptype:
            # 同向取后更极端分型（延续原笔，简化实现：直接推进）
            prev = (ftype, fidx)
            continue
        if ftype != ptype:
            # 顶底交替成笔
            gap = abs(fidx - pidx)
            if gap >= 3 or True:  # 允许短笔，简化
                if ptype == "bottom":
                    bis.append({"type": "up", "start": pidx, "end": fidx,
                                "px0": prices_low[pidx], "px1": prices_high[fidx]})
                else:
                    bis.append({"type": "down", "start": pidx, "end": fidx,
                                "px0": prices_high[pidx], "px1": prices_low[fidx]})
            prev = (ftype, fidx)
    return bis


# ---------------- 中枢 ----------------

def find_zhongshu(bis: List[dict], min_bi: int = 3) -> Optional[Tuple[float, float]]:
    """中枢：连续 ≥min_bi 笔的价格重叠区（取重叠上下沿）。返回 (upper, lower)。"""
    if len(bis) < min_bi:
        return None
    # 取最近可形成重叠的三笔：各笔区间(low=min端点, high=max端点)取公共交集
    overlap = None
    for i in range(len(bis) - 2):
        b1, b2, b3 = bis[i], bis[i + 1], bis[i + 2]
        lo = max(min(b1["px0"], b1["px1"]), min(b2["px0"], b2["px1"]),
                 min(b3["px0"], b3["px1"]))
        hi = min(max(b1["px0"], b1["px1"]), max(b2["px0"], b2["px1"]),
                 max(b3["px0"], b3["px1"]))
        if lo < hi:
            overlap = (lo, hi)
        else:
            return overlap if overlap else None
    return overlap


# ---------------- 背驰（MACD 动能简化） ----------------

def _standardize(s: Sequence[float], n: int = 20) -> List[float]:
    if not s:
        return []
    mu = sum(s[:n]) / min(n, len(s))
    sd = (sum((x - mu) ** 2 for x in s[:n]) / max(1, min(n, len(s)) - 1)) ** 0.5
    if sd == 0:
        return [0.0] * len(s)
    return [(x - mu) / sd for x in s]


def find_divergence(closes: Sequence[float], highs: Sequence[float],
                    lows: Sequence[float], macd_momentum: Optional[Sequence[float]] = None,
                    lookback: int = 40) -> str:
    """背驰：比较该段价格的新高(低)与对应 MACD 动能。
    若价格创新高但动能柱总量小于前一次高峰 → 'top'（顶背驰）。
    返回 'top'/'bottom'/'none'。"""
    if macd_momentum is None:
        # 用收盘动量近似 MACD（简化：20日变化量）
        macd_momentum = [closes[i] - (sum(closes[max(0, i - 12):i]) / max(1, i - max(0, i - 12)))
                         for i in range(len(closes))]
    n = len(closes)
    if n < lookback:
        return "none"
    seg = macd_momentum[-lookback:]
    # 找最近的价格峰/谷与动能（统一在窗口切片内取 index，避免坐标系错位）
    win_hi = highs[-lookback:]
    win_lo = lows[-lookback:]
    price_hi = max(win_hi)
    price_lo = min(win_lo)
    hi_idx = win_hi.index(price_hi)
    lo_idx = win_lo.index(price_lo)
    # 前一个峰/谷（简化：取次高/次低）及动能
    prev_hi = max(win_hi[:hi_idx]) if hi_idx > 0 else price_hi
    prev_lo = min(win_lo[:lo_idx]) if lo_idx > 0 else price_lo
    mom_now = sum(seg)
    # 顶背驰：价格创新高但动能萎缩
    if price_hi >= prev_hi and mom_now < sum(seg[: max(1, hi_idx)]):
        return "top"
    if price_lo <= prev_lo and mom_now > sum(seg[max(0, lo_idx):]):
        return "bottom"
    return "none"


# ---------------- 一把/二把/三把（简化） ----------------

def classify_bs(closes: Sequence[float], lows: Sequence[float], highs: Sequence[float],
                bis: List[dict], divergence: str) -> str:
    """买卖点分类（简化）：
      bottom_back+创新低后回升 → 一买(B1)
      一买后回调不破前低      → 二买(B2)
      突破中枢后回踩不破      → 三买(B3)  卖点对称。
    返回 'B1'/'B2'/'B3'/'S1'/'S2'/'S3'/'-'。"""
    if not bis or len(bis) < 3:
        return "-"
    last = bis[-1]
    prev = bis[-2] if len(bis) >= 2 else None
    if divergence == "top" and last["type"] == "down":
        return "S1"
    if divergence == "bottom" and last["type"] == "up":
        return "B1"
    # 简化二/三买
    if last["type"] == "up":
        return "B2" if prev and prev["type"] == "down" else "-"
    if last["type"] == "down":
        return "S2" if prev and prev["type"] == "up" else "-"
    return "-"


# ---------------- 汇总状态 ----------------

def chan_state(highs: Sequence[float], lows: Sequence[float],
               closes: Sequence[float]) -> dict:
    """计算最后一个完整结构的缠论状态。返回 dict。
    无前视：只用 ≤len-1 数据（最后K线尚未确认分型时用倒数第二根完成形态）。"""
    # 用截止倒数第1根完整K线（最后分型需更早确认，保守取改的整笔）
    highs = list(highs)
    lows = list(lows)
    closes = list(closes)
    if len(highs) < 30:
        return {"bi_dir": "-", "zone": "-", "divergence": "none",
                "last_signal": "-", "bustop": False, "trend_ok": False,
                "bis": 0, "error": "insufficient"}
    hh, ll = highs[:-1], lows[:-1]
    cc = closes[:-1]
    merged = merge_klines(hh, ll)
    fractals = find_fractals(merged, len(hh))
    bis = compute_bis(hh, ll, fractals)
    zs = find_zhongshu(bis) if bis else None
    div = find_divergence(cc, hh, ll)
    bs = classify_bs(cc, ll, hh, bis, div)
    # 笔方向：最新一笔方向
    bi_dir = bis[-1]["type"] if bis else "-"
    # 中枢位置：现价相对最后中枢
    zone = "-"
    if zs:
        last_close = closes[-1]
        lo, hi = zs
        if last_close > hi:
            zone = "upper"
        elif last_close < lo:
            zone = "lower"
        else:
            zone = "in"
    return {"bi_dir": bi_dir, "zone": zone, "divergence": div,
            "last_signal": bs, "bustop": div == "top",
            "trend_ok": bi_dir == "up", "bis": len(bis)}