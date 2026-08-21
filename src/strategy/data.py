# -*- coding: utf-8 -*-
"""
策略数据层（data.py）
====================================================
定位：为多因子策略提供统一、带本地增量缓存的数据访问。所有下游模块只
     依赖本模块返回的宽表（index=date, columns=code），不直接触碰网络。

数据源（免费公开接口，与项目其余部分一致的东财/中证口径）：
- HS300 成分      akshare index_stock_cons_csindex("000300")   中证官网，月度调整
- 个股日线(后复权) akshare stock_zh_a_hist(period="daily", adjust="hfq")
- 沪深300指数日线 akshare index_zh_a_hist(symbol="000300")
- 行业分类        akshare stock_board_industry_name_em + cons_em（东财行业板块，86个）

缓存策略（data/strategy_cache/，.gitignore 已排除）：
- 成分表/行业映射：TTL 7 天（成分月调、行业极少变动）
- 个股日线：每股一个 pickle，增量补齐（只拉缓存末端之后的新日期），全量重建仅当缓存过旧
- 无look-ahead：任何 t 日截面只使用 ≤t 的数据；成分用当期快照（幸存者偏差为v1已知局限，文档注明）

对外主要接口：
- load_universe()            -> (codes, names)
- load_industry_map(codes)   -> {code: industry}
- load_panels(start, codes)  -> PanelData(close/high/low/volume/amount/turnover/index_close)
- PanelData 为 dataclass，字段均为 DataFrame(T×N)
"""
from __future__ import annotations

import logging
import pickle
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_DIR = PROJECT_ROOT / "data" / "strategy_cache"

META_TTL_DAYS = 7          # 成分/行业映射缓存有效期
KLINE_STALE_DAYS = 7       # 个股日线末根超过该天数则全量重建（防除权导致的复权价漂移）
_FETCH_SLEEP = 0.25        # 对东财的礼貌限速（秒/请求）


@dataclass
class PanelData:
    """统一宽表面板：index=交易日，columns=股票代码（统一 6 位字符串）"""
    close: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    volume: pd.DataFrame      # 手
    amount: pd.DataFrame      # 元
    turnover: pd.DataFrame    # %
    index_close: pd.DataFrame  # 基准指数（000300）收盘，单列
    codes: List[str] = field(default_factory=list)

    def returns(self) -> pd.DataFrame:
        """日收益率（截面因子与回测共用同一口径）"""
        return self.close.pct_change()

    def tradable_mask(self, window: int = 1) -> pd.DataFrame:
        """可交易掩码：当日有成交额且非停牌（无行情=NaN）"""
        return self.amount.rolling(window, min_periods=1).max().notna() & (self.amount > 0)


def _cache_get(key: str, ttl_days: Optional[int] = None) -> Optional[object]:
    path = CACHE_DIR / f"{key}.pkl"
    if not path.exists():
        return None
    if ttl_days is not None and time.time() - path.stat().st_mtime > ttl_days * 86400:
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception as e:  # 缓存损坏视同未命中
        logger.warning("缓存 %s 读取失败(%s)，将重建", key, e)
        return None


def _cache_set(key: str, obj: object) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_DIR / f"{key}.pkl.tmp"
    with open(tmp, "wb") as f:
        pickle.dump(obj, f)
    tmp.replace(CACHE_DIR / f"{key}.pkl")   # 原子替换，避免中断产生半截缓存


def load_universe(index_code: str = "000300", force_refresh: bool = False) -> Tuple[List[str], Dict[str, str]]:
    """HS300 成分券（代码列表 + 代码→名称）。缓存 7 天。"""
    if force_refresh:
        codes = names = None
    else:
        cached = _cache_get(f"universe_{index_code}", META_TTL_DAYS)
        codes, names = cached if cached else (None, None)
    if codes is None:
        import akshare as ak
        df = ak.index_stock_cons_csindex(symbol=index_code)
        col_code = "成分券代码" if "成分券代码" in df.columns else df.columns[0]
        col_name = "成分券名称" if "成分券名称" in df.columns else df.columns[1]
        codes = [str(c).zfill(6) for c in df[col_code].tolist()]
        names = {str(c).zfill(6): str(n) for c, n in zip(df[col_code], df[col_name])}
        _cache_set(f"universe_{index_code}", (codes, names))
        logger.info("成分表已刷新：%s 共 %d 只", index_code, len(codes))
    return codes, names


def load_industry_map(codes: List[str], force_refresh: bool = False) -> Dict[str, str]:
    """东财行业板块映射 {code: 行业名}（仅覆盖传入 codes）。缓存 7 天。"""
    if not force_refresh:
        cached = _cache_get("industry_map", META_TTL_DAYS)
        if cached is not None:
            missing = [c for c in codes if c not in cached]
            if not missing:
                return {c: cached[c] for c in codes if c in cached}
    import akshare as ak
    try:
        boards = ak.stock_board_industry_name_em()
    except Exception as e:
        # 行业源不可达时优雅降级：优先用旧缓存，否则全'未知'（中性化退化为仅市值），
        # 优于整条策略链 fail-fast —— 日频任务宁可降级出报告
        logger.warning("行业板块列表获取失败(%s)，退化为缓存/未知行业", type(e).__name__)
        cached = _cache_get("industry_map")
        if cached:
            return {c: cached.get(c, "未知") for c in codes}
        return {c: "未知" for c in codes}
    full: Dict[str, str] = dict(_cache_get("industry_map") or {})
    n_new = 0
    for _, row in boards.iterrows():
        board = str(row["板块名称"])
        try:
            cons = ak.stock_board_industry_cons_em(symbol=board)
        except Exception as e:
            logger.warning("行业 %s 成分获取失败: %s", board, e)
            continue
        for c in cons["代码"].tolist():
            code = str(c).zfill(6)
            if code not in full:
                full[code] = board
                n_new += 1
        time.sleep(_FETCH_SLEEP)
    _cache_set("industry_map", full)
    logger.info("行业映射已刷新：新增 %d，总量 %d", n_new, len(full))
    return {c: full.get(c, "未知") for c in codes}


def _fetch_stock_daily(code: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """单股东财日线（后复权），列标准化为英文。失败返回 None。"""
    import akshare as ak
    try:
        df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                start_date=start, end_date=end, adjust="hfq")
    except Exception as e:
        logger.warning("%s 日线获取失败: %s", code, e)
        return None
    if df is None or df.empty:
        return None
    df = df.rename(columns={"日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
                            "最低": "low", "成交量": "volume", "成交额": "amount", "换手率": "turnover"})
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")[["open", "close", "high", "low", "volume", "amount", "turnover"]]


def _load_one_stock(code: str, start: str) -> pd.DataFrame:
    """单股增量加载：缓存末端之后只拉新增；缓存过旧（>KLINE_STALE_DAYS 无更新且今日非周末）全量重建。"""
    end = datetime.now().strftime("%Y%m%d")
    cached = _cache_get(f"kline_{code}")
    if cached is not None and not cached.empty:
        last = cached.index.max()
        # 增量：从缓存末日+1 拉到今天
        inc_start = (last + timedelta(days=1)).strftime("%Y%m%d")
        if inc_start <= end:
            inc = _fetch_stock_daily(code, inc_start, end)
            if inc is not None:
                cached = pd.concat([cached, inc])
                cached = cached[~cached.index.duplicated(keep="last")].sort_index()
                time.sleep(_FETCH_SLEEP)
        # 复权漂移防护：末根太旧则全量重建一次
        stale_days = (pd.Timestamp.now().normalize() - last.normalize()).days
        if stale_days > KLINE_STALE_DAYS + 4:  # +4 容忍长假
            fresh = _fetch_stock_daily(code, start, end)
            if fresh is not None and not fresh.empty:
                cached = fresh
                time.sleep(_FETCH_SLEEP)
        _cache_set(f"kline_{code}", cached)
        return cached[cached.index >= pd.Timestamp(start)]
    full = _fetch_stock_daily(code, start, end)
    if full is None or full.empty:
        return pd.DataFrame()
    time.sleep(_FETCH_SLEEP)
    _cache_set(f"kline_{code}", full)
    return full


def _fetch_index_frame(symbol: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """指数日线三级降级：index_zh_a_hist(东财) → stock_zh_index_daily_em(东财push2his)
    → stock_zh_index_daily(新浪)。部分网络环境代理仅放行部分主机，多源保证可用性。"""
    import akshare as ak
    pre = "sh" if symbol.startswith(("000", "950")) else "sz"
    df = None
    try:
        df = ak.index_zh_a_hist(symbol=symbol, period="daily", start_date=start, end_date=end)
        df = df.rename(columns={"日期": "date", "收盘": "close"})
    except Exception as e:
        logger.info("指数接口1(东财clist)失败(%s)，降级接口2", type(e).__name__)
    if df is None or df.empty:
        try:
            df = ak.stock_zh_index_daily_em(symbol=f"{pre}{symbol}")
        except Exception as e:
            logger.info("指数接口2(东财push2his)失败(%s)，降级接口3(新浪)", type(e).__name__)
    if df is None or df.empty:
        try:
            df = ak.stock_zh_index_daily(symbol=f"{pre}{symbol}")
        except Exception as e:
            logger.warning("指数 %s 三个接口均失败: %s", symbol, e)
            return None
    if df is None or df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")[["close"]].sort_index()
    return df[df.index >= pd.Timestamp(start)]


def _load_index_daily(symbol: str = "000300", start: str = "20190101") -> pd.DataFrame:
    """基准指数日线（不复权），带 TTL=1 天缓存。"""
    end = datetime.now().strftime("%Y%m%d")
    cached = _cache_get(f"index_{symbol}", ttl_days=1)
    if cached is not None and not cached.empty:
        last = cached.index.max()
        inc_start = (last + timedelta(days=1)).strftime("%Y%m%d")
        if inc_start <= end:
            inc = _fetch_index_frame(symbol, inc_start, end)
            if inc is not None and not inc.empty:
                cached = pd.concat([cached, inc])
                cached = cached[~cached.index.duplicated(keep="last")].sort_index()
                _cache_set(f"index_{symbol}", cached)
        return cached[cached.index >= pd.Timestamp(start)]
    df = _fetch_index_frame(symbol, start, end)
    if df is None or df.empty:
        return pd.DataFrame()
    _cache_set(f"index_{symbol}", df)
    return df


def load_panels(codes: List[str], start: str = "20190101",
                progress_every: int = 50) -> PanelData:
    """加载全部个股宽表 + 基准指数。容忍个别股票失败（从截面剔除）。"""
    buckets = {k: {} for k in ("close", "high", "low", "volume", "amount", "turnover")}
    ok_codes: List[str] = []
    for i, code in enumerate(codes):
        df = _load_one_stock(code, start)
        if df.empty:
            logger.warning("%s 无数据，剔除", code)
            continue
        ok_codes.append(code)
        for k in buckets:
            buckets[k][code] = df[k]
        if progress_every and (i + 1) % progress_every == 0:
            logger.info("日线加载进度 %d/%d", i + 1, len(codes))
    panels = {k: pd.DataFrame(v).sort_index() for k, v in buckets.items()}
    idx_df = _load_index_daily("000300", start)
    if idx_df.empty:
        # 指数是 idio_vol 因子与回测基准的硬依赖：缺失时宁可 fail-fast，
        # 也不要让下游 KeyError/静默剔除指数维度
        raise RuntimeError("基准指数 000300 日线获取失败（三个接口均不可达），"
                           "策略层无法构建因子，请检查网络后重试")
    panel = PanelData(close=panels["close"], high=panels["high"], low=panels["low"],
                      volume=panels["volume"], amount=panels["amount"],
                      turnover=panels["turnover"], index_close=idx_df, codes=ok_codes)
    logger.info("面板就绪：%d 只 × %d 交易日（%s ~ %s）", len(ok_codes),
                len(panel.close.index),
                panel.close.index.min().date() if len(panel.close.index) else "-",
                panel.close.index.max().date() if len(panel.close.index) else "-")
    return panel
