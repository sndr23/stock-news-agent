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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from src.strategy.data_freshness import is_recent_data_date

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_DIR = PROJECT_ROOT / "data" / "strategy_cache"
BJT = timezone(timedelta(hours=8))

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


def _cache_frame_has_valid_columns(frame: object,
                                   required: Tuple[str, ...] = ("close",)) -> bool:
    """缓存必须包含指定列、可解析日期索引和全历史有效数值。"""
    if (not isinstance(frame, pd.DataFrame) or frame.empty
            or not isinstance(frame.index, pd.DatetimeIndex)
            or any(column not in frame.columns for column in required)):
        return False
    try:
        dates = pd.to_datetime(frame.index, errors="coerce")
        values = [pd.to_numeric(frame[column], errors="coerce")
                  for column in required]
    except (AttributeError, TypeError, ValueError):
        return False
    if dates.isna().any():
        return False
    positive_columns = {"open", "close", "high", "low"}
    nonnegative_columns = {"volume", "amount", "turnover"}
    for column, series in zip(required, values):
        finite = series.notna() & ~series.isin([float("inf"), float("-inf")])
        if not finite.all():
            return False
        if column in positive_columns and not (series > 0).all():
            return False
        if column in nonnegative_columns and not (series >= 0).all():
            return False
    return True


def _cache_frame_has_valid_close(frame: object) -> bool:
    """缓存必须包含可解析的日期索引和至少一个有效收盘价。"""
    return _cache_frame_has_valid_columns(frame, ("close",))


_STOCK_CACHE_COLUMNS = ("open", "close", "high", "low", "volume", "amount", "turnover")


def _last_bar_is_fresh(frame: pd.DataFrame, max_lag_days: int = 3) -> bool:
    """按最后交易日判断行情缓存是否仍可作为当前数据使用。"""
    if not _cache_frame_has_valid_close(frame):
        return False
    try:
        last = pd.Timestamp(frame.index.max()).normalize()
    except (TypeError, ValueError):
        return False
    return is_recent_data_date(last, max_lag_days=max_lag_days, calendar="cn")


def _stock_history_is_sufficient(frame: pd.DataFrame,
                                  min_complete_bars: int = 60) -> bool:
    """判断个股源是否有足够完整日线支撑趋势/动量双确认。"""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return False
    try:
        last = pd.Timestamp(frame.index.max()).normalize()
    except (TypeError, ValueError):
        return False
    # 14:45 读取时今日 bar 可能仍是 partial，调用方会剔除；源端需多提供一根。
    today = pd.Timestamp(datetime.now(BJT).date())
    required = min_complete_bars + (1 if last == today else 0)
    return len(frame) >= required


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


def _fetch_stock_daily(code: str, start: str, end: str,
                       adjust: str = "hfq") -> Optional[pd.DataFrame]:
    """单股东财日线，列标准化为英文；默认后复权，可显式选择复权口径。"""
    import akshare as ak
    try:
        df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                start_date=start, end_date=end, adjust=adjust)
    except Exception as e:
        logger.warning("%s 日线获取失败: %s", code, e)
        return None
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    df = df.rename(columns={"日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
                            "最低": "low", "成交量": "volume", "成交额": "amount", "换手率": "turnover"})
    required = ["date", "open", "close", "high", "low", "volume", "amount", "turnover"]
    if any(column not in df.columns for column in required):
        return None
    df = df[required].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    numeric = ["open", "close", "high", "low", "volume", "amount", "turnover"]
    for column in numeric:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    if (df.empty or df["date"].isna().any()
            or df[numeric].isna().any().any()
            or df[numeric].isin([float("inf"), float("-inf")]).any().any()
            or any((df[column] <= 0).any()
                   for column in ("open", "close", "high", "low"))
            or any((df[column] < 0).any()
                   for column in ("volume", "amount", "turnover"))):
        return None
    return df.set_index("date")[["open", "close", "high", "low", "volume", "amount", "turnover"]]


def _load_one_stock(code: str, start: str) -> pd.DataFrame:
    """单股增量加载：缓存末端之后只拉新增；缓存过旧（>KLINE_STALE_DAYS 无更新且今日非周末）全量重建。"""
    end = datetime.now(BJT).strftime("%Y%m%d")
    cached = _cache_get(f"kline_{code}")
    if _cache_frame_has_valid_columns(cached, _STOCK_CACHE_COLUMNS):
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
        stale_days = (pd.Timestamp(datetime.now(BJT).date()) - last.normalize()).days
        if stale_days > KLINE_STALE_DAYS + 4:  # +4 容忍长假
            fresh = _fetch_stock_daily(code, start, end)
            if fresh is not None and not fresh.empty:
                cached = fresh
                time.sleep(_FETCH_SLEEP)
        if not _last_bar_is_fresh(cached):
            logger.warning("%s 个股缓存末根 %s 已过期，免费源刷新失败，返回空",
                           code, cached.index.max())
            return pd.DataFrame()
        _cache_set(f"kline_{code}", cached)
        return cached[cached.index >= pd.Timestamp(start)]
    full = _fetch_stock_daily(code, start, end)
    if full is None or full.empty or not _last_bar_is_fresh(full):
        if full is not None and not full.empty:
            logger.warning("%s 首次全量日线末根 %s 已过期，返回空",
                           code, full.index.max())
        return pd.DataFrame()
    time.sleep(_FETCH_SLEEP)
    _cache_set(f"kline_{code}", full)
    return full


def _fetch_index_frame(symbol: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """指数日线三级降级：index_zh_a_hist(东财) → stock_zh_index_daily_em(东财push2his)
    → stock_zh_index_daily(新浪)。部分网络环境代理仅放行部分主机，多源保证可用性。"""
    import akshare as ak
    pre = "sh" if symbol.startswith(("000", "950")) else "sz"
    sources = (
        ("接口1(东财clist)",
         lambda: ak.index_zh_a_hist(symbol=symbol, period="daily",
                                    start_date=start, end_date=end),
         lambda frame: frame.rename(columns={"日期": "date", "收盘": "close"})),
        ("接口2(东财push2his)",
         lambda: ak.stock_zh_index_daily_em(symbol=f"{pre}{symbol}"),
         lambda frame: frame),
        ("接口3(新浪)",
         lambda: ak.stock_zh_index_daily(symbol=f"{pre}{symbol}"),
         lambda frame: frame),
    )
    for i, (name, fetch, normalize) in enumerate(sources):
        try:
            raw = fetch()
            if raw is None or raw.empty:
                raise ValueError("empty response")
            frame = normalize(raw).copy()
            frame["date"] = pd.to_datetime(frame["date"])
            frame = frame.set_index("date")[["close"]].sort_index()
        except Exception as e:
            next_label = sources[i + 1][0] if i + 1 < len(sources) else "无"
            logger.info("指数%s失败(%s)，降级%s", name, type(e).__name__, next_label)
            continue
        if not _last_bar_is_fresh(frame):
            logger.warning("指数%s末根 %s 已过期，降级下一免费源: %s",
                           name, frame.index.max().date(), symbol)
            continue
        return frame[frame.index >= pd.Timestamp(start)]
    logger.warning("指数 %s 三个免费接口均失败或过期", symbol)
    return None


def _load_index_daily(symbol: str = "000300", start: str = "20190101") -> pd.DataFrame:
    """基准指数日线（不复权），带 TTL=1 天缓存。"""
    end = datetime.now(BJT).strftime("%Y%m%d")
    cached = _cache_get(f"index_{symbol}", ttl_days=1)
    if _cache_frame_has_valid_close(cached):
        last = cached.index.max()
        inc_start = (last + timedelta(days=1)).strftime("%Y%m%d")
        if inc_start <= end:
            inc = _fetch_index_frame(symbol, inc_start, end)
            if inc is not None and not inc.empty:
                cached = pd.concat([cached, inc])
                cached = cached[~cached.index.duplicated(keep="last")].sort_index()
                _cache_set(f"index_{symbol}", cached)
        if not _last_bar_is_fresh(cached):
            logger.warning("指数 %s 缓存末根 %s 已过期，免费源无新数据时返回空",
                           symbol, cached.index.max())
            return pd.DataFrame()
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
    amount 语义随源记录在 DataFrame.attrs 中；不同源之间禁止增量拼接。"""
    import akshare as ak
    pre = "sh" if symbol.startswith(("000", "950")) else "sz"
    secid = f"{'1' if pre == 'sh' else '0'}.{symbol}"

    def _usable(frame: Optional[pd.DataFrame]) -> bool:
        """整段源数据合法且末根新鲜时，才阻止继续回退。"""
        if (not isinstance(frame, pd.DataFrame) or frame.empty
                or "close" not in frame.columns or "amount" not in frame.columns):
            return False
        try:
            dates = (frame["date"] if "date" in frame.columns
                     else pd.Series(frame.index, index=frame.index))
            dates = pd.Series(pd.to_datetime(dates, errors="coerce"),
                              index=frame.index)
            closes = pd.to_numeric(frame["close"], errors="coerce")
            amounts = pd.to_numeric(frame["amount"], errors="coerce")
            if (dates.isna().any()
                    or closes.isna().any()
                    or amounts.isna().any()
                    or closes.isin([float("inf"), float("-inf")]).any()
                    or amounts.isin([float("inf"), float("-inf")]).any()
                    or (closes <= 0).any() or (amounts < 0).any()):
                return False
            valid_dates = dates.dropna()
            if valid_dates.empty:
                return False
            latest = valid_dates.max()
            latest_close = closes[dates == latest]
            if latest_close.empty or (latest_close <= 0).all():
                return False
            return is_recent_data_date(
                latest, max_lag_days=3, calendar="cn")
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    df = None
    source = None
    try:
        df = ak.index_zh_a_hist(symbol=symbol, period="daily",
                                start_date=start, end_date=end)
        df = df.rename(columns={"日期": "date", "收盘": "close", "成交额": "amount"})
        df = df[["date", "close", "amount"]]
        source = "eastmoney_hist"
    except Exception as e:
        logger.info("指数full接口1失败(%s)，降级接口2(直连)", type(e).__name__)
    if not _usable(df):
        if isinstance(df, pd.DataFrame) and not df.empty:
            logger.warning("指数full接口1末根已过期，降级接口2(直连): %s", symbol)
        try:
            df = _fetch_kline_direct(secid, start, end)
            source = "eastmoney_push2his"
        except Exception as e:
            logger.info("指数full接口2(直连)失败(%s)，降级接口3(腾讯)", type(e).__name__)
    if not _usable(df):
        try:
            df = _fetch_tencent_daily(f"{pre}{symbol}", start, end)
            source = "tencent_volume"
        except Exception as e:
            logger.info("指数full接口3(腾讯)失败(%s)，降级接口4", type(e).__name__)
    if not _usable(df):
        try:
            df = ak.stock_zh_index_daily_em(symbol=f"{pre}{symbol}")
            df = df[["date", "close", "amount"]]
            source = "eastmoney_push2his_akshare"
        except Exception as e:
            logger.info("指数full接口4失败(%s)，降级接口5(新浪,无amount)", type(e).__name__)
    if not _usable(df):
        try:
            df = ak.stock_zh_index_daily(symbol=f"{pre}{symbol}")
            df = df[["date", "close"]].copy()
            df["amount"] = 0.0
            source = "sina_volume"
        except Exception as e:
            logger.warning("指数 %s full 四个接口均失败: %s", symbol, e)
            return None
    if not _usable(df):
        logger.warning("指数 %s 五个免费接口均失败或过期", symbol)
        return None
    if "date" not in df.columns:
        df = df.copy()
        df.insert(0, "date", pd.to_datetime(df.index, errors="coerce"))
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    if (df["date"].isna().any() or df["close"].isna().any()
            or df["amount"].isna().any()
            or df["close"].isin([float("inf"), float("-inf")]).any()
            or df["amount"].isin([float("inf"), float("-inf")]).any()
            or (df["close"] <= 0).any() or (df["amount"] < 0).any()):
        logger.warning("指数 %s 选中免费源仍含非法历史行，拒绝该源", symbol)
        return None
    df = df.set_index("date").sort_index()
    df.attrs["strategy_data_source"] = source or "unknown"
    return df[df.index >= pd.Timestamp(start)]


def load_stock_sina(symbol: str = "300308", start: str = "20140101",
                    ttl_days: int = 1) -> pd.DataFrame:
    """个股日线（新浪优先，腾讯/东财免费回退）。

    与 load_index_sina 互补：本函数服务"创业板+旭创双确认"的个股侧数据。
    返回 index=date, columns=[open, close, high, low, volume, amount, turnover]。
    缓存 TTL 默认 1 天；各免费源均校验末根新鲜度。全部失败返回空
    DataFrame（调用方降级该维度）。
    """
    import akshare as ak
    key = f"stock_sina_{symbol}"
    cached = _cache_get(key, ttl_days=ttl_days)
    if (_cache_frame_has_valid_columns(cached, _STOCK_CACHE_COLUMNS)
            and _stock_history_is_sufficient(cached)
            and _last_bar_is_fresh(cached)):
        return cached
    pre = "sh" if symbol.startswith(("6", "9", "5")) else "sz"

    def _normalize(frame: Optional[pd.DataFrame]) -> pd.DataFrame:
        """把新浪/腾讯/东财个股日线统一为调用方需要的宽表。"""
        if frame is None or frame.empty:
            return pd.DataFrame()
        out = frame.copy()
        if "date" not in out.columns:
            out = out.reset_index()
        out = out.rename(columns={
            "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
            "最低": "low", "成交量": "volume", "成交额": "amount", "换手率": "turnover",
        })
        if "date" not in out.columns or "close" not in out.columns:
            return pd.DataFrame()
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
        out["close"] = pd.to_numeric(out["close"], errors="coerce")
        out = out.dropna(subset=["date", "close"])
        for col in ("open", "high", "low", "volume", "amount", "turnover"):
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")
            else:
                out[col] = float("nan")
        out = out.drop_duplicates("date").set_index("date").sort_index()
        return out[["open", "close", "high", "low", "volume", "amount", "turnover"]]

    def _accept(source: str, raw: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
        df = _normalize(raw)
        if df.empty:
            return None
        df = df[df.index >= pd.Timestamp(start)]
        if df.empty:
            return None
        if not _last_bar_is_fresh(df):
            logger.warning("%s个股 %s 返回末根 %s 已过期，继续降级",
                           source, symbol, df.index.max().date())
            return None
        if not _stock_history_is_sufficient(df):
            last = df.index.max().date()
            required = 61 if last == datetime.now(BJT).date() else 60
            logger.warning("%s个股 %s 仅返回 %d 根（末根%s，至少需%d根），继续降级",
                           source, symbol, len(df), last, required)
            return None
        if source != "新浪":
            logger.info("个股 %s 使用%s免费回退", symbol, source)
        _cache_set(key, df)
        return df

    try:
        df = _accept("新浪", ak.stock_zh_a_daily(symbol=f"{pre}{symbol}", adjust="qfq"))
        if df is not None:
            return df
    except Exception as e:
        logger.warning("新浪个股 %s 获取失败(%s)，降级腾讯", symbol, type(e).__name__)

    try:
        df = _accept("腾讯", _fetch_tencent_daily(
            f"{pre}{symbol}", start, datetime.now(BJT).strftime("%Y%m%d")))
        if df is not None:
            return df
    except Exception as e:
        logger.warning("腾讯个股 %s 获取失败(%s)，降级东财", symbol, type(e).__name__)

    try:
        df = _accept("东财", _fetch_stock_daily(
            symbol, start, datetime.now(BJT).strftime("%Y%m%d"), adjust="qfq"))
        if df is not None:
            return df
    except Exception as e:
        logger.warning("东财个股 %s 获取失败(%s)", symbol, type(e).__name__)

    logger.warning("个股 %s 的新浪/腾讯/东财免费接口均失败或过期，返回空", symbol)
    return pd.DataFrame()


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
    # 文件修改时间可能因 GitHub Actions cache 恢复而过期，但其中的末根 bar
    # 仍可能是最近完整交易日；先读缓存，再由末根交易日判断是否可用。
    cached = _cache_get(key)
    if _cache_frame_has_valid_columns(cached, ("close", "amount")):
        # 新鲜度按末根 bar 日期而非墙钟时长判定（修复 2026-08-25 隔日滞后 bug）：
        # 原 TTL=1天 固定窗口 + GitHub Actions 每日 cache 恢复，导致周二/周四命中
        # 前一日缓存、数据滞后一个交易日，核心层用缺日线打分。
        # 2026-09-01 收紧 max_lag_days 3→1：8-29~8-31 业务 run 连续失败期间
        # GitHub Actions cache 冻结在 8-28 版本（末根 8-27），9-1 恢复后 lag=3
        # 仍被判"新鲜"而跳过重拉，8-28/8-31 两个交易日日线缺失并连续污染信号
        # （8-31 与 9-1 核心分完全相同即铁证）。工作日口径 lag≤1 = 末根必须是
        # 今天或上一工作日；长假多拉一次全量无害，宁可重拉不可用旧数据。
        if _last_bar_is_fresh(cached, max_lag_days=1):
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
    if not {"date", "close", "amount"}.issubset(df.columns):
        logger.warning("新浪全量指数 %s 字段缺失，回退短链", symbol)
        return load_index_daily_full(symbol, "20190101")
    for c in ["close", "amount", "high", "low", "open"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    if (df["date"].isna().any()
            or df["close"].isna().any()
            or df["amount"].isna().any()
            or df["close"].isin([float("inf"), float("-inf")]).any()
            or df["amount"].isin([float("inf"), float("-inf")]).any()
            or (df["close"] <= 0).any()
            or (df["amount"] < 0).any()):
        logger.warning("新浪全量指数 %s 含非法日期、收盘或成交额，回退短链", symbol)
        return load_index_daily_full(symbol, "20190101")
    valid_dates = df["date"]
    if valid_dates.empty:
        logger.warning("新浪全量指数 %s 日期无效，回退短链", symbol)
        return load_index_daily_full(symbol, "20190101")
    latest = valid_dates.max()
    latest_rows = df.loc[df["date"] == latest, "close"]
    if latest_rows.notna().sum() == 0:
        logger.warning("新浪全量指数 %s 最新收盘无效，回退短链", symbol)
        return load_index_daily_full(symbol, "20190101")
    df = df.dropna(subset=["close"]).drop_duplicates("date").set_index("date").sort_index()
    keep = ["close", "amount"]
    for c in ("high", "low"):
        if c in df.columns:
            keep.append(c)
    df = df[keep]
    if df.empty:
        return load_index_daily_full(symbol, "20190101")
    if not _last_bar_is_fresh(df):
        logger.warning("新浪全量指数 %s 返回末根已过期，回退短链", symbol)
        return load_index_daily_full(symbol, "20190101")
    _cache_set(key, df)
    return df


def load_index_daily_full(symbol: str = "399006", start: str = "20220101") -> pd.DataFrame:
    """指数日线（close+amount），按同源增量缓存，源切换时整段重建。"""
    end = datetime.now(BJT).strftime("%Y%m%d")
    key = f"index_full_{symbol}"
    # 文件修改时间可能因 GitHub Actions cache 恢复而过期，但其中的末根 bar
    # 仍可能是最近完整交易日；先读缓存，再由末根交易日判断是否可用。
    cached = _cache_get(key)
    if _cache_frame_has_valid_columns(cached, ("close", "amount")):
        last = cached.index.max()
        inc_start = (last + timedelta(days=1)).strftime("%Y%m%d")
        cached_source = cached.attrs.get("strategy_data_source")
        if inc_start <= end and cached_source:
            inc = _fetch_index_full_frame(symbol, inc_start, end)
            inc_source = (inc.attrs.get("strategy_data_source")
                          if isinstance(inc, pd.DataFrame) else None)
            if inc is not None and not inc.empty and inc_source == cached_source:
                cached = pd.concat([cached, inc])
                cached = cached[~cached.index.duplicated(keep="last")].sort_index()
                cached.attrs["strategy_data_source"] = cached_source
                _cache_set(key, cached)
            elif inc is not None and not inc.empty:
                # 成交额（元）与成交量（手）不可共存于同一量价窗口，源切换时全量重建。
                rebuilt = _fetch_index_full_frame(symbol, start, end)
                if rebuilt is not None and not rebuilt.empty:
                    cached = rebuilt
                    _cache_set(key, cached)
        elif inc_start <= end:
            # 历史缓存没有来源元数据，禁止拿未知量纲与新源增量拼接。
            rebuilt = _fetch_index_full_frame(symbol, start, end)
            if rebuilt is not None and not rebuilt.empty:
                cached = rebuilt
                _cache_set(key, cached)
        # 文件 TTL 只能说明缓存最近被写过，不能说明其中包含最近交易日。
        # 免费源全部失败时，过期指数继续参与择时会产生伪造的当日信号。
        last = cached.index.max()
        if not is_recent_data_date(last, max_lag_days=3, calendar="cn"):
            logger.warning("指数 %s 缓存末根 %s 已过期，免费源无新数据时返回空", symbol, last)
            return pd.DataFrame()
        return cached[cached.index >= pd.Timestamp(start)]
    df = _fetch_index_full_frame(symbol, start, end)
    if df is None or df.empty or not _last_bar_is_fresh(df):
        if df is not None and not df.empty:
            logger.warning("指数 %s 免费源返回末根已过期，返回空", symbol)
        return pd.DataFrame()
    _cache_set(key, df)
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
