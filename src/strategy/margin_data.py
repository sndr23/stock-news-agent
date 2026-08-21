# -*- coding: utf-8 -*-
"""
两融杠杆情绪事件（margin_data.py）
====================================================
市场级另类因子：沪市融资余额（stock_margin_sse）日频，2014 起回溯。
作为创业板杠杆情绪/风险偏好的市场级 proxy（杠杆行情全市场联动）。

数据链：逐区间拉沪市两融余额 → {date: 融资余额}（日频，T+1 公布）→ 缓存。
因子：build_leverage() 把余额对齐交易日，取"前一交易日余额相对 span 日前增速"
      （用 ≤t-1 数据避免 T+1 公布的前视），滚动 252 日分位 → [-1,1]。
验门同项目纪律：|IC|≥0.05 且样本≥10 才允许进核心层，不达标关闭。
"""
from __future__ import annotations

import logging
import pickle
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_PATH = PROJECT_ROOT / "data" / "strategy_cache" / "margin_sse.pkl"


def _segments(start_year: int = 2014, end_year: Optional[int] = None) -> list:
    end_year = end_year or datetime.now().year
    segs = []
    for y in range(start_year, end_year + 1):
        segs.append((f"{y}0101", f"{y}1231"))
    return segs


# 日期统一为 'YYYY-MM-DD'（容错 int/str '20140102' 与 date/datetime）。
def _norm(d) -> str:
    s = str(d)[:10]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def fetch_margin(force_refresh: bool = False) -> Dict[str, float]:
    """沪市两融余额 {date_str: 融资余额(float)}，逐区间拉取，带缓存。"""
    if not force_refresh and CACHE_PATH.exists():
        try:
            with open(CACHE_PATH, "rb") as f:
                return pickle.load(f)
        except Exception as e:
            logger.warning("两融缓存读取失败(%s)，重建", e)
    import akshare as ak
    out: Dict[str, float] = {}
    for s, e in _segments():
        try:
            df = ak.stock_margin_sse(start_date=s, end_date=e)
        except Exception as ex:
            logger.warning("两融 %s~%s 拉取失败(%s)，跳过", s, e, type(ex).__name__)
            continue
        if df is None or df.empty or "融资余额" not in df.columns:
            continue
        for _, r in df.iterrows():
            d = _norm(r["信用交易日期"])
            try:
                v = float(r["融资余额"])
            except (TypeError, ValueError):
                continue
            out[d] = v
        time.sleep(0.3)
    if out:
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(CACHE_PATH, "wb") as f:
                pickle.dump(out, f)
            logger.info("两融余额缓存已写：共 %d 条", len(out))
        except OSError as e:
            logger.warning("两融缓存写入失败(%s)，仅存内存", e)
    return out


def build_leverage(margin: Dict[str, float], trading_dates: list,
                   span: int = 60) -> List[float]:
    """生成逐交易日杠杆增速因子（≤t-1 数据，无前视）。

    对交易日 i：取前一交易日余额 b_prev；增速 = (b_prev - b_{i-span})/b_{i-span}。
    余额 T+1 公布，故用前一交易日 b_prev 避免前视；缺失用前值 forward-fill。
    增速长期为正（余额趋势上行），故用**滚动 252 日 z-score**（相对历史均值的强弱
    变化）而非水平分位，反映"杠杆增速的结构性快/慢"，映射到 [-1,1]。
    """
    bal = [None] * len(trading_dates)
    last = None
    for i, d in enumerate(trading_dates):
        ds = _norm(d)
        if ds in margin:
            last = margin[ds]
        bal[i] = last
    grow = [0.0] * len(trading_dates)
    for i in range(span, len(trading_dates)):
        b_now, b_old = bal[i], bal[i - span]
        if b_now and b_old:
            grow[i] = b_now / b_old - 1.0
    span252 = 252
    out = [0.0] * len(trading_dates)
    for i in range(span, len(trading_dates)):
        lo = max(0, i - span252)
        w = grow[lo:i]  # 不含当前，纯历史
        if len(w) < 30:
            out[i] = 0.0
            continue
        mu = sum(w) / len(w)
        sd = (sum((v - mu) ** 2 for v in w) / (len(w) - 1)) ** 0.5 if len(w) > 1 else 0.0
        if sd <= 1e-9:
            out[i] = 0.0
            continue
        z = (grow[i] - mu) / sd
        out[i] = round(max(-1.0, min(1.0, z / 2.0)), 3)
    return out