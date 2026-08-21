# -*- coding: utf-8 -*-
"""
创业板估值数据（index_pe.py）
====================================================
创业板指(399006)本体无免费日频 PE 源，采用**创业板50(399673.SZ) TTM 滚动市盈率**
作为创业板板块估值的方向代理——创业板50 是创业板头部代表，与其走势/估值同向高相关，
用其 PE 分位捕捉"创业板整体贵贱"完全够用（口径在报告中明示）。

数据源：乐咕乐股（akshare stock_index_pe_lg("创业板50")），覆盖 2009-10 ~ 至今。
PE 分位按"滚动窗"相对估值：在历史大顶(2015/2021)PE 抬升 → 高分位 → 看空；
大底(2018/2024)PE 压缩 → 低分位 → 看多。

纯函数（分位映射/对齐）可单测；网络拉取带本地缓存，缺源降级为全 0（防错）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional, Sequence, Dict

logger = logging.getLogger("index_pe")

PE_CACHE_RELPATH = "strategy_cache/cy50_pe_cache.json"


def pe_to_cheap_pctile(pe_series: Sequence[float], span: int = 500) -> list:
    """滚动市盈率分期 → 便宜度分位 [0,1]。
    用当前 PE 在**过去 span 窗口**的升序分位：PE 高 → 贵（分位高）；
    返回便宜度 cheap = 1 - 分位（cheap 高=便宜→看多，低=贵→看空）。
    与 factor_value_erp 的 s=2*(p-0.5) 对齐：p=cheap 分位。"""
    out = [0.5] * len(pe_series)
    for i in range(1, len(pe_series)):
        lo = max(0, i - span)
        w = sorted(float(v) for v in pe_series[lo:i] if v == v)  # 去 NaN
        if not w:
            continue
        cur = pe_series[i]
        if not cur == cur:  # NaN
            continue
        if cur >= w[-1]:
            out[i] = 1.0
        elif cur <= w[0]:
            out[i] = 0.0
        else:
            out[i] = sum(1 for v in w if v < cur) / len(w)
    return [round(1.0 - p, 4) for p in out]


def align_pe_by_dates(pe_map: Dict[str, float], dates: Sequence[str]) -> list:
    """把 {date: ttm_pe} 对齐到目标 dates（399006 交易日），缺的日期 forward-fill。
    返回与 dates 等长的 PE 序列（首个值缺失填 None→因子置 0）。"""
    out = []
    last = None
    for d in dates:
        v = pe_map.get(d)
        if v is not None:
            last = v
        out.append(last)
    return out


def load_cy50_pe(cache_dir: Optional[Path] = None) -> Dict[str, float]:
    """拉取创业板50 TTM 滚动市盈率历史，带本地缓存。返回 {date_str: pe}。"""
    cache = (cache_dir or Path("data")) / PE_CACHE_RELPATH
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("rows"):
                logger.info("估值缓存命中 %d 条", len(data["rows"]))
                return data["rows"]
        except (OSError, ValueError) as e:
            logger.warning("估值缓存损坏，重拉: %s", type(e).__name__)
    try:
        import akshare as ak
    except ImportError:
        logger.warning("akshare 不可用，估值源缺失（降级 value=0）")
        return {}
    try:
        sdf = ak.stock_index_pe_lg(symbol="创业板50")
    except Exception as e:
        logger.warning("创业板50 PE 拉取失败（估值降级为0）: %s @ %s",
                       type(e).__name__, e)
        return {}
    rows = {}
    try:
        for _, r in sdf.iterrows():
            date = str(r["日期"])
            pe = r.get("滚动市盈率")
            try:
                pe = float(pe)
            except (TypeError, ValueError):
                continue
            if pe > 0:
                rows[date] = round(pe, 3)
    except Exception as e:
        logger.warning("PE 解析失败（估值降级为0）: %s", type(e).__name__)
        return {}
    if not rows:
        logger.warning("PE 序列为空（估值降级为0）")
        return {}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"rows": rows}, ensure_ascii=False),
                         encoding="utf-8")
    except OSError as e:
        logger.warning("估值缓存写入失败: %s", type(e).__name__)
    logger.info("创业板50 TTM PE 拉取 %d 条（%s ~ %s）", len(rows),
                min(rows), max(rows))
    return rows