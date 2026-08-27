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
import os
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


_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                   "ALL_PROXY", "all_proxy")


def _fetch_kline_direct(secid: str, beg: str, end: str) -> Optional[pd.DataFrame]:
    """东财 push2his K线直连（trust_env=False 绕过 Windows 注册表残留代理）。
    返回 DataFrame(date, close, amount)；失败返回 None。"""
    import requests
    s = requests.Session()
    s.trust_env = False
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {"secid": secid, "fields1": "f1,f2,f3",
              "fields2": "f51,f52,f53,f54,f55,f56,f57",
              "klt": "101", "fqt": "1", "beg": beg, "end": end}
    r = s.get(url, params=params, timeout=20,
              headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    data = (r.json().get("data") or {})
    rows = []
    for line in data.get("klines") or []:
        p = str(line).split(",")
        if len(p) >= 7:
            try:
                rows.append((p[0], float(p[2]), float(p[6])))  # date, close, amount
            except ValueError:
                continue
    if not rows:
        return None
    return pd.DataFrame(rows, columns=["date", "close", "amount"])


def _fetch_tencent_daily(code: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """腾讯日K（trust_env=False 直连，本机网络环境下最稳）。分批 ≤640 根。
    返回 date/close/amount——注意 amount 列此处实际是成交量(手)，
    量价维度只做分位比较，量纲无关。"""
    import requests
    s = requests.Session()
    s.trust_env = False
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    rows = []
    cur = pd.Timestamp(start).strftime("%Y-%m-%d")
    end_s = pd.Timestamp(end).strftime("%Y-%m-%d")
    for _ in range(8):  # 640×8 根上限，防死循环
        param = f"{code},day,{cur},{end_s},640,qfq"
        r = s.get(url, params={"param": param}, timeout=20,
                  headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        d = ((r.json().get("data") or {}).get(code) or {})
        batch = d.get("qfqday") or d.get("day") or []
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 640:
            break
        cur = (pd.Timestamp(batch[-1][0]) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    if not rows:
        return None
    df = pd.DataFrame([(x[0], float(x[2]), float(x[5])) for x in rows],
                      columns=["date", "close", "amount"])
    df = df.drop_duplicates(subset="date").sort_values("date")
    return df


def _fetch_index_full_frame(symbol: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """指数日线（close+amount），供量能维度使用。五级降级：
    东财hist(akshare) → push2his直连 → 腾讯日K直连 → push2his(akshare) → 新浪(无amount)。
    amount 语义：东财=成交额(元)，腾讯=成交量(手)——量价维度仅做分位比较，量纲无关。"""
    import akshare as ak
    pre = "sh" if symbol.startswith(("000", "950")) else "sz"
    secid = f"{'1' if pre == 'sh' else '0'}.{symbol}"
    df = None
    try:
        df = ak.index_zh_a_hist(symbol=symbol, period="daily",
                                start_date=start, end_date=end)
        df = df.rename(columns={"日期": "date", "收盘": "close", "成交额": "amount"})
        df = df[["date", "close", "amount"]]
    except Exception as e:
        logger.info("指数full接口1失败(%s)，降级接口2(直连)", type(e).__name__)
    if df is None or df.empty:
        try:
            df = _fetch_kline_direct(secid, start, end)
        except Exception as e:
            logger.info("指数full接口2(直连)失败(%s)，降级接口3(腾讯)", type(e).__name__)
    if df is None or df.empty:
        try:
            df = _fetch_tencent_daily(f"{pre}{symbol}", start, end)
        except Exception as e:
            logger.info("指数full接口3(腾讯)失败(%s)，降级接口4", type(e).__name__)
    if df is None or df.empty:
        try:
            df = ak.stock_zh_index_daily_em(symbol=f"{pre}{symbol}")
            df = df[["date", "close", "amount"]]
        except Exception as e:
            logger.info("指数full接口4失败(%s)，降级接口5(新浪,无amount)", type(e).__name__)
    if df is None or df.empty:
        try:
            df = ak.stock_zh_index_daily(symbol=f"{pre}{symbol}")
            df = df[["date", "close"]].copy()
            df["amount"] = 0.0
        except Exception as e:
            logger.warning("指数 %s full 四个接口均失败: %s", symbol, e)
            return None
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df[df.index >= pd.Timestamp(start)]


def load_stock_sina(symbol: str = "300308", start: str = "20140101",
                    ttl_days: int = 1) -> pd.DataFrame:
    """个股日线（新浪 stock_zh_a_daily 直连，绕东财被挡的代理环境）。

    与 load_index_sina 互补：本函数服务"创业板+旭创双确认"的个股侧数据。
    返回 index=date, columns=[open, close, high, low, volume, amount, turnover]。
    缓存 TTL 默认 1 天。失败返回空 DataFrame（调用方降级该维度）。
    """
    import akshare as ak
    key = f"stock_sina_{symbol}"
    cached = _cache_get(key, ttl_days=ttl_days)
    if cached is not None and not cached.empty:
        return cached
    pre = "sh" if symbol.startswith(("6", "9", "5")) else "sz"
    try:
        df = ak.stock_zh_a_daily(symbol=f"{pre}{symbol}", adjust="qfq")
    except Exception as e:
        logger.warning("新浪个股 %s 获取失败(%s)，返回空", symbol, type(e).__name__)
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.reset_index()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df = df[df.index >= pd.Timestamp(start)]
    _cache_set(key, df)
    return df


def load_index_sina(symbol: str = "399006", datalen: int = 3000,
                    ttl_days: int = 1) -> pd.DataFrame:
    """最终选指数日线全量源（新浪 CN_MarketData.getKLineData 直连，绕代理）。

    与 load_index_daily_full（东财链，被环境代理挡时只剩缓存短历史）互补：
    本函数一次拉全量历史（实测 3000 根/约 12 年），带 amount（量价因子需要），
    trust_env=False 绕 Windows 残留代理。作为 v4 核心层回测与实时上下文的
    历史数据基，优先于东财短链。返回 index=date, columns=[close, amount]。

    缓存 TTL 默认 1 天：日频使用足够，增量由主流程在实时链路用
    load_index_daily_full（东财增量）补齐后者负责；本函数保证全量深度。
    """
    import requests
    pre = "sh" if symbol.startswith(("000", "950")) else "sz"
    tsec = f"{pre}{symbol}"
    key = f"index_sina_full_{symbol}"
    cached = _cache_get(key, ttl_days=ttl_days)
    if cached is not None and not cached.empty:
        # 新鲜度按末根 bar 日期而非墙钟时长判定（修复 2026-08-25 隔日滞后 bug）：
        # 原 TTL=1天 固定窗口 + GitHub Actions 每日 cache 恢复，导致周二/周四命中
        # 前一日缓存、数据滞后一个交易日，核心层用缺日线打分。
        # 新浪接口盘中含当日 bar，缓存末根须不早于"昨天"才新鲜；早于昨天即重拉。
        _last_bar = pd.Timestamp(cached.index.max()).date()
        _today = datetime.now().date()
        if _last_bar >= _today - timedelta(days=1):
            return cached
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={tsec}&scale=240&ma=no&datalen={datalen}")
    session = requests.Session()
    session.trust_env = False
    headers = {"User-Agent": "Mozilla/5.0 Chrome/120.0.0.0",
               "Referer": "https://finance.sina.com.cn/"}
    try:
        r = session.get(url, headers=headers, timeout=25)
        d = r.json()
    except Exception as e:
        logger.warning("新浪全量指数 %s 获取失败(%s)，回退短链", symbol, type(e).__name__)
        return load_index_daily_full(symbol, "20190101")
    if not isinstance(d, list) or not d:
        logger.warning("新浪全量指数 %s 返回异常，回退短链", symbol)
        return load_index_daily_full(symbol, "20190101")
    df = pd.DataFrame(d)
    df = df.rename(columns={"day": "date", "close": "close", "volume": "amount"})
    for c in ["close", "amount", "high", "low", "open"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["close"]).drop_duplicates("date").set_index("date").sort_index()
    keep = ["close", "amount"]
    for c in ("high", "low"):
        if c in df.columns:
            keep.append(c)
    df = df[keep]
    if df.empty:
        return load_index_daily_full(symbol, "20190101")
    _cache_set(key, df)
    return df


def load_index_daily_full(symbol: str = "399006", start: str = "20220101") -> pd.DataFrame:
    """指数日线（close+amount），TTL=1 天增量缓存。量能维度数据源。"""
    end = datetime.now().strftime("%Y%m%d")
    key = f"index_full_{symbol}"
    cached = _cache_get(key, ttl_days=1)
    if cached is not None and not cached.empty:
        last = cached.index.max()
        inc_start = (last + timedelta(days=1)).strftime("%Y%m%d")
        if inc_start <= end:
            inc = _fetch_index_full_frame(symbol, inc_start, end)
            if inc is not None and not inc.empty:
                cached = pd.concat([cached, inc])
                cached = cached[~cached.index.duplicated(keep="last")].sort_index()
                _cache_set(key, cached)
        return cached[cached.index >= pd.Timestamp(start)]
    df = _fetch_index_full_frame(symbol, start, end)
    if df is None or df.empty:
        return pd.DataFrame()
    _cache_set(key, df)
    return df


# ---------------- Tushare Pro 付费优先通道（SNA-01，2026-08-27） ----------------

_TSU_PRO = None          # 进程级单例（含负缓存：验活失败后不再重试）
_TSU_TRIED = False


def _tushare_client():
    """Tushare 客户端（进程级单例）。token 缺失/包未装/初始化或验活失败 → None。

    调用方无条件降级免费源，永不抛错（免费源永不删除纪律）。验活用一次
    轻量 trade_cal 查询，防过期 token 潜伏到取数时才炸。云端 token 由
    Actions secrets 注入 TUSHARE_TOKEN（workflow 接线待 token 提供后做）。
    """
    global _TSU_PRO, _TSU_TRIED
    if _TSU_TRIED:
        return _TSU_PRO
    _TSU_TRIED = True
    token = os.getenv("TUSHARE_TOKEN", "").strip()
    if not token:
        return None
    try:
        import tushare as ts
        pro = ts.pro_api(token)
        pro.query("trade_cal", exchange="SSE", limit=1)
    except Exception as e:
        logger.warning("Tushare 不可用（降级免费源）: %s", type(e).__name__)
        _TSU_PRO = None
        return None
    _TSU_PRO = pro
    return pro


def _tsu_code(symbol: str) -> str:
    """内部 6 位代码 → tushare ts_code（000/950 开头→SH 上证系，其余→SZ 深市系）。"""
    return f"{symbol}.SH" if symbol.startswith(("000", "950")) else f"{symbol}.SZ"


def _fetch_index_tushare(symbol: str, years: int = 12) -> Optional[pd.DataFrame]:
    """Tushare 指数日线（SNA-01 付费优先通道）。

    返回与 load_index_sina 同构：index=date, columns=[close, amount, high, low]。
    amount 口径取 tushare vol（手）——与项目既有约定一致（新浪链 volume→amount、
    腾讯链 amount=手，量价维度只做窗口内分位/比值，量纲无关）。三源各自
    整段缓存（key 隔离），不跨源增量混拼。SNA-01⑤ 一致性抽查将实证两源
    close 对齐率与 vol 比率。
    """
    pro = _tushare_client()
    if pro is None:
        return None
    start = (datetime.now() - timedelta(days=365 * years)).strftime("%Y%m%d")
    try:
        raw = pro.index_daily(ts_code=_tsu_code(symbol), start_date=start)
    except Exception as e:
        logger.warning("Tushare 指数 %s 失败(%s)，降级免费源", symbol, type(e).__name__)
        return None
    if raw is None or raw.empty:
        return None
    # tushare index_daily 原生含 amount(千元) 列；按口径约定量纲列取 vol(手)，
    # 先丢弃原生 amount，否则 rename 后列名重复 → df["amount"] 成 DataFrame → to_numeric 炸
    df = raw.drop(columns=["amount"], errors="ignore").rename(
        columns={"trade_date": "date", "vol": "amount"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["close"]).sort_values("date").set_index("date")
    keep = ["close", "amount"] + [c for c in ("high", "low") if c in df.columns]
    for c in keep:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[~df.index.duplicated(keep="last")]
    return df[keep] if not df.empty else None


def _fetch_stock_tushare(code: str, start: str) -> Optional[pd.DataFrame]:
    """Tushare 个股日线（前复权，SNA-01）。pro_bar adj='qfq' 与新浪
    stock_zh_a_daily(adjust="qfq") 口径对齐（旭创侧趋势/动量因子对复权敏感）。

    返回 index=date, columns=[open, close, high, low, volume, amount]（turnover
    新浪专有，Tushare 无对应列——旭创链路只用 close/趋势/动量，不受影响）。
    volume 单位手、amount 单位千元（与新浪 元 不同，但个股侧无量价因子）。
    """
    pro = _tushare_client()
    if pro is None:
        return None
    end = datetime.now().strftime("%Y%m%d")
    try:
        import tushare as ts
        raw = ts.pro_bar(ts_code=_tsu_code(code), adj="qfq",
                         start_date=start, end_date=end, api=pro)
    except Exception as e:
        logger.warning("Tushare 个股 %s 失败(%s)，降级新浪", code, type(e).__name__)
        return None
    if raw is None or raw.empty:
        return None
    df = raw.rename(columns={"trade_date": "date", "vol": "volume"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["close"]).sort_values("date").set_index("date")
    keep = [c for c in ("open", "close", "high", "low", "volume", "amount")
            if c in df.columns]
    for c in keep:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[~df.index.duplicated(keep="last")]
    return df[keep] if not df.empty else None


def _fresh_by_last_bar(cached: pd.DataFrame, max_lag_days: int = 1) -> bool:
    """缓存新鲜度按末根 bar 日期判定（与 load_index_sina 2026-08-25 修复同口径）：
    末根不早于"昨天"即新鲜。Tushare 盘中只返回 ≤昨日完整收盘，天然满足。"""
    if cached is None or cached.empty:
        return False
    last_bar = pd.Timestamp(cached.index.max()).date()
    return last_bar >= datetime.now().date() - timedelta(days=max_lag_days)


def load_index_primary(symbol: str = "399006", datalen: int = 3000) -> pd.DataFrame:
    """指数日线全量优先链（SNA-01）：Tushare（token 配置时）→ 新浪全量 → 东财短链。

    三源整段返回、独立缓存 key（index_tsu_/index_sina_full_/index_full_），
    绝不跨源增量混拼——防单位口径跳变污染量价分位窗口。任一源失败静默
    降级，永不抛错（缺一维缩一维，永不无信号）。
    """
    if _tushare_client() is not None:
        key = f"index_tsu_{symbol}"
        cached = _cache_get(key)   # 永久缓存，新鲜度由末根 bar 判定
        if cached is not None and not cached.empty and _fresh_by_last_bar(cached):
            return cached
        df = _fetch_index_tushare(symbol, years=max(3, datalen // 220))
        if df is not None and not df.empty:
            _cache_set(key, df)
            return df
        logger.warning("Tushare 指数通道失效，%s 降级新浪链", symbol)
    return load_index_sina(symbol, datalen)


def load_stock_primary(symbol: str = "300308", start: str = "20140101") -> pd.DataFrame:
    """个股日线优先链（SNA-01）：Tushare qfq（token 配置时）→ 新浪 → 空。"""
    if _tushare_client() is not None:
        key = f"stock_tsu_{symbol}"
        cached = _cache_get(key)
        if cached is not None and not cached.empty and _fresh_by_last_bar(cached):
            return cached
        df = _fetch_stock_tushare(symbol, start)
        if df is not None and not df.empty:
            _cache_set(key, df)
            return df
        logger.warning("Tushare 个股通道失效，%s 降级新浪", symbol)
    return load_stock_sina(symbol, start)


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
