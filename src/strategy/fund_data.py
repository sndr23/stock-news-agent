# -*- coding: utf-8 -*-
"""
基金轮动数据层（fund_data.py）
====================================================
从《基金预测》项目移植的已验证数据接口（2026-08 回测方向准确率 94-99%）：
- 季报前十大重仓股（天天基金，含港股 secid）
- 腾讯实时行情批量接口（A股/港股极稳定）
- 场外基金单位净值历史（akshare 东财接口）

叠加本地缓存：持仓快照按周缓存（季报数据季度才变），净值历史当日缓存。
盘中估值 = Σ(季报权重 × 实时涨跌幅) / Σ(权重)，用于 14:30 前的净值预估。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

logger = logging.getLogger("strategy.fund_data")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_DIR = PROJECT_ROOT / "logs"
HOLDINGS_CACHE_PATH = CACHE_DIR / "fund_holdings_cache.json"
HOLDINGS_CACHE_DAYS = 7  # 季报持仓季度才变，缓存一周足够

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://fundf10.eastmoney.com/",
}

# 绕过系统残留代理（代理软件关闭后残留设置触发 WinError 10061）
_SESSION = requests.Session()
_SESSION.trust_env = False


def get_holdings(fund_code: str, top: int = 10) -> list:
    """抓取基金最新季度前十大重仓股（天天基金 jjcc 接口）。

    返回: [{"secid": "1.600519", "code": "600519", "name": "贵州茅台", "weight": 9.87}, ...]
    secid 含市场前缀，可同时覆盖 A股/港股。
    """
    from akshare.utils import demjson
    from bs4 import BeautifulSoup

    url = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
    params = {"type": "jjcc", "code": fund_code, "topline": str(top),
              "year": "", "month": "", "rt": "0.9"}
    r = _SESSION.get(url, params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    data = demjson.decode(r.text[r.text.find("{"):-1])
    table = BeautifulSoup(data["content"], "lxml").find("table")
    if table is None:
        return []
    rows = []
    for tr in table.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        code = tds[1].get_text(strip=True)
        name = tds[2].get_text(strip=True)
        weight = 0.0
        for td in tds[3:]:
            text = td.get_text(strip=True)
            if text.endswith("%"):
                weight = float(text[:-1].strip())
                break
        a = tds[1].find("a")
        secid = a["href"].split("/")[-1] if (a and a.get("href")) else None
        if secid:
            rows.append({"secid": secid, "code": code, "name": name, "weight": weight})
    return rows


def secid_to_tencent(secid: str) -> str:
    """东财 secid(1.600519/0.000858/116.00700) -> 腾讯代码(sh/sz/hk)。"""
    market, code = secid.split(".")
    return {"1": "sh", "0": "sz", "116": "hk"}.get(market, "") + code


def get_quotes(secids: list, retry: int = 3) -> dict:
    """腾讯 qt.gtimg.cn 批量实时涨跌幅。返回 {证券代码: 涨跌幅%}。"""
    symbols = [s for s in map(secid_to_tencent, secids) if s]
    url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
    for attempt in range(retry):
        try:
            r = _SESSION.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            r.encoding = "gbk"
            quotes = {}
            for line in r.text.strip().split(";"):
                if "=" not in line:
                    continue
                f = line.split("=", 1)[1].strip('"').split("~")
                if len(f) < 5:
                    continue
                code = f[2]
                try:
                    cur, prev = float(f[3]), float(f[4])
                    quotes[code] = round((cur - prev) / prev * 100, 2) if prev else 0.0
                except (ValueError, IndexError):
                    continue
            return quotes
        except Exception as exc:
            if attempt == retry - 1:
                logger.warning("行情获取失败(%s)，按已有数据估算", exc)
                return {}
            time.sleep(1)


def load_holdings_cached(fund_codes: list, top: int = 10) -> dict:
    """带本地缓存的持仓抓取：{fund_code: [ {secid,code,name,weight}, ... ]}。
    缓存一周（季报持仓季度才变）；单只抓取失败时回退缓存旧值。"""
    cache = {}
    if HOLDINGS_CACHE_PATH.exists():
        try:
            cache = json.loads(HOLDINGS_CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
    fresh = {"_ts": time.time(), "funds": {}}
    out = {}
    for code in fund_codes:
        entry = (cache.get("funds") or {}).get(code)
        cached_ok = (entry and entry.get("rows")
                     and time.time() - float(cache.get("_ts") or 0) < HOLDINGS_CACHE_DAYS * 86400)
        try:
            rows = get_holdings(code, top)
            if rows:
                fresh["funds"][code] = {"rows": rows}
                out[code] = rows
                continue
        except Exception as e:
            logger.warning("基金 %s 持仓抓取失败: %s", code, type(e).__name__)
        if cached_ok:
            fresh["funds"][code] = entry
            out[code] = entry["rows"]
        else:
            out[code] = []
            if entry:
                fresh["funds"][code] = entry  # 保留旧值供下次降级
    CACHE_DIR.mkdir(exist_ok=True)
    HOLDINGS_CACHE_PATH.write_text(json.dumps(fresh, ensure_ascii=False), encoding="utf-8")
    return out


def intraday_estimate(holdings: list, quotes: dict) -> dict:
    """盘中净值估算（归一化加权）：est = Σ(w_i*r_i)/Σ(w_i)。
    返回 {"est_pct": 估算涨跌%, "covered_w": 参与权重合计%}。"""
    total_w = 0.0
    weighted = 0.0
    for h in holdings:
        pct = quotes.get(h["code"])
        if pct is None or h["weight"] <= 0:
            continue
        total_w += h["weight"]
        weighted += h["weight"] * pct
    est = weighted / total_w if total_w else 0.0
    return {"est_pct": round(est, 2), "covered_w": round(total_w, 1)}


def fund_nav_returns(code: str, days: int = 120) -> list:
    """基金单位净值日增长率序列（升序），返回 [(date_str, pct), ...]。
    akshare 东财接口；失败返回 []（调用方降级为仅盘中信号）。"""
    try:
        import pandas as pd
        import akshare as ak
        df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
        if df is None or df.empty:
            return []
        df["净值日期"] = pd.to_datetime(df["净值日期"])
        df = df.sort_values("净值日期").tail(days)
        ret = df.set_index("净值日期")["日增长率"].astype(float)
        return [(d.strftime("%Y-%m-%d"), round(float(v), 3))
                for d, v in ret.items() if pd.notna(v)]
    except Exception as e:
        logger.warning("基金 %s 净值历史拉取失败: %s", code, type(e).__name__)
        return []
