# -*- coding: utf-8 -*-
"""
业绩预告事件日历（event_data.py）
====================================================
事件驱动因子的可回测数据源：创业板业绩预告（东财 stock_yjyg_em，逐季度）。

设计：把"预约日期的确定性事件"做成历史日历，按公告日对齐交易日，聚合成
指数级景气因子，供影子 IC 验门。因子若 |IC|≥0.05 且样本≥10 才允许进核心层
（对齐项目因子验门纪律），不达标则置 0 关闭（先例：估值因子负贡献关闭）。

数据结构：
  fetch_events()  拉取全市场业绩预告日历，过滤创业板(300/301)，缓存
                  返回 {code: (公告日期, 预告方向标签)}
  方向标签：+1 正向(预增/扭亏/略增/续盈), -1 负向(预减/首亏/续亏/略减), 0 其他
  build_sentiment(events, trading_dates, window) → [ft...] 逐交易日景气分位因子
   每交易日取近 window 天内创业板"正向家数-负向家数"累积信号 → 滚动252日分位→[-1,1]
"""
from __future__ import annotations

import logging
import pickle
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_PATH = PROJECT_ROOT / "data" / "strategy_cache" / "event_yjyg.pkl"

# 预告类型 → 方向：正向(预增/略增/扭亏/续盈), 负向(预减/略减/首亏/续亏)
POSITIVE = ("预增", "略增", "扭亏", "续盈")
NEGATIVE = ("预减", "略减", "首亏", "续亏")


def _quarter_ends(start_year: int = 2015, end_year: Optional[int] = None) -> list:
    """报告期末（各季真实月末：3/31 6/30 9/30 12/31）。"""
    end_year = end_year or datetime.now().year
    out = []
    for y in range(start_year, end_year + 1):
        for m in (("0331", "0630", "0930", "1231")):
            out.append(f"{y}{m}")
    return out


def _direction(yjlx: object) -> int:
    s = str(yjlx or "")
    if any(p in s for p in POSITIVE):
        return 1
    if any(n in s for n in NEGATIVE):
        return -1
    return 0


def fetch_events(force_refresh: bool = False) -> List[Tuple[str, str, int]]:
    """拉取全市场业绩预告，过滤创业板，返回 [(公告日期str, 股票代码, 方向±1/0)]。
    逐年逐季拉取，季度失败独立跳过；按(公告日,股票代码)去重，结果带磁盘缓存。"""
    if not force_refresh and CACHE_PATH.exists():
        try:
            with open(CACHE_PATH, "rb") as f:
                return pickle.load(f)
        except Exception as e:
            logger.warning("事件缓存读取失败(%s)，重建", e)
    import akshare as ak
    rows: List[Tuple[str, str, int]] = []
    for q in _quarter_ends():
        try:
            df = ak.stock_yjyg_em(date=q)
        except Exception as e:
            logger.warning("业绩预告 %s 拉取失败(%s)，跳过", q, type(e).__name__)
            continue
        if df is None or df.empty:
            continue
        if not {"股票代码", "公告日期", "预告类型"}.issubset(df.columns):
            continue
        df = df[df["股票代码"].astype(str).str.startswith(("300", "301"))]
        for _, r in df.iterrows():
            d = str(r["公告日期"])[:10]
            code = str(r["股票代码"])
            rows.append((d, code, _direction(r["预告类型"])))
        logger.info("业绩预告 %s 创业板事件 %d 条（累计 %d）", q, len(df), len(rows))
        time.sleep(0.4)
    # 去重：同一公司同公告日仅保最后一条 → (day, code) 唯一；再排序
    dedup: Dict[Tuple[str, str], int] = {}
    for d, code, s in rows:
        dedup[(d, code)] = s
    rows = sorted(((d, code, s) for (d, code), s in dedup.items()), key=lambda x: x[0])
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_PATH, "wb") as f:
            pickle.dump(rows, f)
        logger.info("业绩预告事件缓存已写：共 %d 条", len(rows))
    except OSError as e:
        logger.warning("事件缓存写入失败(%s)，仅存内存", e)
    return rows


def build_sentiment(events: List[Tuple[str, str, int]], trading_dates: list,
                    window: int = 120) -> List[float]:
    """把事件日历对齐到交易日，生成逐日景气因子。

    对每个交易日 t：统计 (t-window, t] 内创业板 正向家数 - 负向家数 的累积信号
    S_t；再对 S_t 序列做滚动 252 日分位（越大越看多），映射到 [-1,1]。
    事件不足/无事件区间返回中性 0.5 分位→0。
    """
    # 事件按日期聚合成按键(公告日)的净信号（同股同日已去重，逐家累加）
    from collections import defaultdict
    event_map: Dict[str, int] = defaultdict(int)
    for _d, _c, s in events:
        event_map[_d] += s
    # 交易日 → 事件净信号序列
    n = len(trading_dates)
    daily = [0.0] * n
    date_index = {str(d): i for i, d in enumerate(trading_dates)}
    for d, net in event_map.items():
        i = date_index.get(d)
        if i is not None:
            daily[i] += net
    # 滚动 window 累积（含当日）
    cum = [0.0] * n
    run = 0.0
    for i in range(n):
        run += daily[i]
        if i >= window:
            run -= daily[i - window]
        cum[i] = run
    # 滚动 252 日分位 → [-1,1]
    out = [0.0] * n
    for i in range(n):
        lo = max(0, i - 252)
        w = sorted(cum[lo:i + 1])
        if len(w) < 20:
            out[i] = 0.0
            continue
        pct = sum(1 for v in w if v <= cum[i]) / len(w)
        out[i] = round(max(-1.0, min(1.0, 2.0 * pct - 1.0)), 3)
    return out


def py2_sign_simple(yjlx: object) -> Optional[int]:
    """单条方向速查（供校验）。"""
    return _direction(yjlx)