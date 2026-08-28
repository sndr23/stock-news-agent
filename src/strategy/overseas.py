# -*- coding: utf-8 -*-
"""
海外领先指数数据（overseas.py）
====================================================
为创业板择时提供"海外科技/风险偏好"这一方向独立的领先信息：
  - SOX    费城半导体指数（半导体周期全球领先）
  - NDX    纳斯达克100（海外成长科技宽基，与创业板同质）
  - INX    标普500（全球风险偏好）
均覆盖 2014~今（回测所需全窗）。

数据源（全部免费，无鉴权）：
  主链 = akshare 新浪封装
  回退 = Yahoo Chart → Stooq CSV；三个序列独立回退并校验末根新鲜度

缺源、限流、浏览器验证或过期 → 返回空 dict，调用方降级该维度为 0（永不阻断信号）。
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Dict, Optional

import pandas as pd
import requests

from src.strategy.data_freshness import is_recent_data_date

logger = logging.getLogger("overseas")

OVS_CACHE_RELPATH = "strategy_cache/ovs_cache.json"

YAHOO_SYMBOLS = {"sox": "^SOX", "ndx": "^NDX", "inx": "^GSPC"}
STOOQ_SYMBOLS = {"sox": "^sox", "ndx": "^ndx", "inx": "^spx"}
_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                 "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}


def _normalize_date_key(value) -> Optional[str]:
    """将外盘接口和缓存日期统一为 YYYY-MM-DD，避免字符串排序/比较失真。"""
    try:
        date = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(date):
        return None
    return date.strftime("%Y-%m-%d")


def _normalize_series(values: dict) -> dict:
    """清理单个外盘序列的日期键和值。"""
    if not isinstance(values, dict):
        return {}
    normalized = {}
    for date, value in values.items():
        date_key = _normalize_date_key(date)
        try:
            close = float(value)
        except (TypeError, ValueError):
            continue
        if date_key and math.isfinite(close) and close > 0:
            normalized[date_key] = round(close, 3)
    return normalized


def _fetch_yahoo_series(symbol: str) -> dict:
    """读取 Yahoo Chart 的免费日线接口，返回标准化收盘序列。"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(
            url, params={"range": "max", "interval": "1d",
                         "events": "history", "includePrePost": "false"},
            headers=_HTTP_HEADERS, timeout=20,
        )
        response.raise_for_status()
        result = ((response.json().get("chart") or {}).get("result") or [])
        if not result:
            return {}
        item = result[0] or {}
        timestamps = item.get("timestamp") or []
        quotes = (item.get("indicators") or {}).get("quote") or []
        closes = (quotes[0] or {}).get("close") if quotes else []
        values = {}
        for timestamp, close in zip(timestamps, closes or []):
            date = _normalize_date_key(pd.to_datetime(timestamp, unit="s", utc=True))
            try:
                close = float(close)
            except (TypeError, ValueError):
                continue
            if date and math.isfinite(close) and close > 0:
                values[date] = round(close, 3)
        return values
    except Exception as e:
        logger.info("Yahoo 外盘 %s 获取失败: %s", symbol, type(e).__name__)
        return {}


def _fetch_stooq_series(symbol: str) -> dict:
    """读取 Stooq 免费 CSV 日线接口，返回标准化收盘序列。"""
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(
            "https://stooq.com/q/d/l/", params={"s": symbol, "i": "d"},
            headers=_HTTP_HEADERS, timeout=20,
        )
        response.raise_for_status()
        frame = pd.read_csv(StringIO(response.text))
        dcol = next((c for c in frame.columns if str(c).lower() == "date"), None)
        ccol = next((c for c in frame.columns if str(c).lower() == "close"), None)
        if dcol is None or ccol is None:
            return {}
        values = {}
        for _, row in frame.iterrows():
            date = _normalize_date_key(row[dcol])
            try:
                close = float(row[ccol])
            except (TypeError, ValueError):
                continue
            if date and math.isfinite(close) and close > 0:
                values[date] = round(close, 3)
        return values
    except Exception as e:
        logger.info("Stooq 外盘 %s 获取失败: %s", symbol, type(e).__name__)
        return {}


def _series_is_fresh(values: dict, max_lag_days: int = 3) -> bool:
    """按单个外盘序列末日期判断缓存是否仍可用于当前风控。"""
    if not isinstance(values, dict) or not values:
        return False
    try:
        last = max(pd.Timestamp(str(d)).date() for d in _normalize_series(values))
    except (TypeError, ValueError):
        return False
    return is_recent_data_date(last, max_lag_days=max_lag_days, calendar="weekday")


def _load_from_cache(cache_dir: Optional[Path]) -> Optional[dict]:
    cache = (cache_dir or Path("data")) / OVS_CACHE_RELPATH
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("外盘缓存根节点不是对象")
            normalized = {key: _normalize_series(data.get(key) or {})
                          for key in ("sox", "ndx", "inx")}
            if any(normalized.values()):
                return normalized
        except (OSError, ValueError) as e:
            logger.warning("外盘缓存损坏，重拉: %s", type(e).__name__)
    return None


def overnight_drop(ov: dict, as_of: datetime) -> float:
    """外盘 t-1 隔夜最大单日跌幅（无前视，只用 ≤as_of 之前已收盘的外盘）。

    对 SOX/纳指/INX 取各自最近一次已收盘收盘价，算单日涨跌幅，返回**最差**者（负值）。
    任一指数缺数据则跳过（按剩余可用算）；全缺返回 0.0（不阻断）。
    设计用途：硬风控盘中急跌的"同源确认"——不支持独立触发，避免近期负相关 regime
    结束后的反向押注。
    """
    worst = 0.0
    used = 0
    for key in ("sox", "ndx", "inx"):
        # 调用方可能直接传入旧缓存；入口再次归一化，避免时间戳键按字符串
        # 排序后漏掉最近的有效日期。
        m = _normalize_series((ov or {}).get(key) or {})
        keys = sorted(m.keys())
        # 找最近两个 ≤ as_of 的已收盘日期
        as_of_key = _normalize_date_key(as_of)
        if not as_of_key:
            continue
        recent = [k for k in keys if k <= as_of_key]
        if len(recent) >= 2:
            d1, d0 = recent[-1], recent[-2]
            v1, v0 = m.get(d1), m.get(d0)
            if v1 and v0:
                drop = v1 / v0 - 1.0
                worst = min(worst, drop)
                used += 1
    return round(worst, 4) if used else 0.0


def load_overseas(cache_dir: Optional[Path] = None) -> dict:
    """拉取三个海外指数 {date: close}，带缓存。返回
    {"sox": {date: float}, "ndx": {...}, "inx": {...}}。单源失败独立降级。"""
    cached = _load_from_cache(cache_dir) or {}
    out = {"sox": {}, "ndx": {}, "inx": {}}
    for key in out:
        values = cached.get(key) or {}
        if _series_is_fresh(values):
            out[key] = values
        elif values:
            logger.info("外盘%s缓存末根已过期，重新拉取免费源", key.upper())
    if all(out.values()):
        return out
    try:
        import akshare as ak
    except ImportError:
        ak = None
        logger.warning("akshare 不可用，尝试独立免费外盘源")

    if not out["sox"] and ak is not None:
        try:
            sdf = ak.macro_global_sox_index()
            for _, r in sdf.iterrows():
                try:
                    v = float(r["最新值"])
                except (TypeError, ValueError):
                    continue
                if math.isfinite(v) and v > 0:
                    date = _normalize_date_key(r["日期"])
                    if date:
                        out["sox"][date] = round(v, 3)
            if not _series_is_fresh(out["sox"]):
                out["sox"] = {}
                logger.warning("SOX 免费源返回末日已过期，降级为空")
            logger.info("SOX 费半 %d 条", len(out["sox"]))
        except Exception as e:
            logger.warning("SOX 拉取失败: %s @ %s", type(e).__name__, e)

    for key, sym in (("ndx", ".NDX"), ("inx", ".INX")):
        if out[key] or ak is None:
            continue
        try:
            tdf = ak.index_us_stock_sina(symbol=sym)
            dcol = next((c for c in tdf.columns if c.lower() == "date"), None)
            ccol = next((c for c in tdf.columns if c.lower() == "close"), None)
            if dcol and ccol:
                for _, r in tdf.iterrows():
                    try:
                        v = float(r[ccol])
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(v) and v > 0:
                        date = _normalize_date_key(r[dcol])
                        if date:
                            out[key][date] = round(v, 3)
            if not _series_is_fresh(out[key]):
                out[key] = {}
                logger.warning("%s 免费源返回末日已过期，降级为空", key.upper())
            logger.info("%s %s %d 条", key.upper(), sym, len(out[key]))
        except Exception as e:
            logger.warning("%s 拉取失败: %s @ %s", key.upper(), type(e).__name__, e)

    # akshare 的新浪封装失败后，使用不同站点的免费源；每个指数独立降级。
    for key in out:
        if out[key]:
            continue
        yahoo = _fetch_yahoo_series(YAHOO_SYMBOLS[key])
        if _series_is_fresh(yahoo):
            out[key] = yahoo
            logger.info("%s Yahoo 免费源 %d 条", key.upper(), len(yahoo))
            continue
        stooq = _fetch_stooq_series(STOOQ_SYMBOLS[key])
        if _series_is_fresh(stooq):
            out[key] = stooq
            logger.info("%s Stooq 免费源 %d 条", key.upper(), len(stooq))
        else:
            logger.warning("%s 外盘免费源均不可用，降级为空", key.upper())

    try:
        cache = (cache_dir or Path("data")) / OVS_CACHE_RELPATH
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning("外盘缓存写失败: %s", type(e).__name__)
    return out
