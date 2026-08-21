# -*- coding: utf-8 -*-
"""
海外领先指数数据（overseas.py）
====================================================
为创业板择时提供"海外科技/风险偏好"这一方向独立的领先信息：
  - SOX    费城半导体指数（半导体周期全球领先）
  - NDX    纳斯达克100（海外成长科技宽基，与创业板同质）
  - INX    标普500（全球风险偏好）
均覆盖 2014~今（回测所需全窗）。

数据源：
  SOX = akshare macro_global_sox_index（新浪，1994~今）
  NDX/INX = akshare index_us_stock_sina （新浪美股，.NDX/.INX，2014~今）

缺源/被代理挡 → 返回空 dict，调用方降级该维度为 0（永不阻断信号）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("overseas")

OVS_CACHE_RELPATH = "strategy_cache/ovs_cache.json"


def _load_from_cache(cache_dir: Optional[Path]) -> Optional[dict]:
    cache = (cache_dir or Path("data")) / OVS_CACHE_RELPATH
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("sox") or data.get("ndx"):
                return data
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
        m = (ov or {}).get(key) or {}
        keys = sorted(m.keys())
        # 找最近两个 ≤ as_of 的已收盘日期
        recent = [k for k in keys if k <= as_of.strftime("%Y-%m-%d")]
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
    cached = _load_from_cache(cache_dir)
    if cached:
        return cached
    out = {"sox": {}, "ndx": {}, "inx": {}}
    try:
        import akshare as ak
    except ImportError:
        logger.warning("akshare 不可用，外盘因子缺失（降级为0）")
        return out

    try:
        sdf = ak.macro_global_sox_index()
        for _, r in sdf.iterrows():
            try:
                v = float(r["最新值"])
            except (TypeError, ValueError):
                continue
            if v > 0:
                out["sox"][str(r["日期"])] = round(v, 3)
        logger.info("SOX 费半 %d 条", len(out["sox"]))
    except Exception as e:
        logger.warning("SOX 拉取失败: %s @ %s", type(e).__name__, e)

    for key, sym in (("ndx", ".NDX"), ("inx", ".INX")):
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
                    if v > 0:
                        out[key][str(r[dcol])[:10]] = round(v, 3)
            logger.info("%s %s %d 条", key.upper(), sym, len(out[key]))
        except Exception as e:
            logger.warning("%s 拉取失败: %s @ %s", key.upper(), type(e).__name__, e)

    try:
        cache = (cache_dir or Path("data")) / OVS_CACHE_RELPATH
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning("外盘缓存写失败: %s", type(e).__name__)
    return out