# filepath: src/tools/data_fetchers.py
"""
A股资讯数据获取工具
多源并行聚合: 东财快讯 + 财联社电报 + 交易所公告
抓取全部可用数据, 按当日日期过滤 + 去重
"""
import json
import socket
import logging
import threading
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# ============================================================
# 线程安全内存缓存 (带 TTL)
# ============================================================
_cache = {}
_cache_lock = threading.Lock()
_CACHE_TTL_SECONDS = 3600  # 缓存 1 小时过期


def _get_cache(key: str, today: str):
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached.get("date") == today:
            import time
            ts = cached.get("_ts", 0)
            if time.time() - ts < _CACHE_TTL_SECONDS:
                logger.info(f"命中缓存: {key}")
                return cached.get("data")
            else:
                logger.info(f"缓存过期: {key}")
                _cache.pop(key, None)
    return None


def _set_cache(key: str, data, today: str):
    import time
    with _cache_lock:
        _cache[key] = {"date": today, "data": data, "_ts": time.time()}


# ============================================================
# 当日日期过滤 + 去重工具
# ============================================================

def _is_today(published_at: str) -> bool:
    """精确判断时间字符串是否是今天"""
    if not published_at or not str(published_at).strip():
        return False
    text = str(published_at).strip()
    today = datetime.now().strftime("%Y-%m-%d")
    # 精确匹配 YYYY-MM-DD 格式
    if text == today:
        return True
    # 匹配 YYYY-MM-DD HH:MM:SS 或 YYYY-MM-DD HH:MM (要求日期后有空白分隔)
    if len(text) >= 11 and text[:10] == today and (text[10] in (' ', 'T', '\t')):
        return True
    # 匹配 YYYYMMDD 格式 (正好8位)
    today_no_dash = today.replace("-", "")
    if text == today_no_dash:
        return True
    if len(text) >= 9 and text[:8] == today_no_dash and text[8] in (' ', 'T', '\t'):
        return True
    # 匹配 YYYY/MM/DD 格式
    today_slash = today.replace("-", "/")
    if text == today_slash:
        return True
    if len(text) >= 11 and text[:10] == today_slash and text[10] in (' ', 'T', '\t'):
        return True
    return False


def _dedup_by_title(news_list: list) -> list:
    """按标题去重 (完全匹配)"""
    seen = set()
    result = []
    for n in news_list:
        title = n.get("title", "").strip()
        if title and title not in seen:
            seen.add(title)
            result.append(n)
    return result


# ============================================================
# 去重工具（URL 规范化 + SimHash + 日期窗口）
# 借鉴 TradingAgents get_global_news_yfinance 的 seen_titles + _in_news_window
# ============================================================

NO_DATA_SENTINEL = "NO_DATA_AVAILABLE"
_SIMHASH_THRESHOLD = 3


def _normalize_url(url: str) -> str:
    """URL 规范化：去 query/fragment，统一 host 小写，去 www 前缀与末尾斜杠"""
    if not url or not url.strip():
        return ""
    from urllib.parse import urlsplit
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    # 手动拼接：urlunsplit 在有 netloc 时会自动补末尾斜杠，无法满足去斜杠需求
    return f"{parts.scheme.lower()}://{host}{path}"


def _simhash(text: str, bits: int = 32) -> int:
    """字符级 3-gram SimHash，对中文短标题友好。

    用 hashlib.md5 保证跨进程确定性（内置 hash() 受 PYTHONHASHSEED 随机化，
    会导致测试不可复现）。bits=32：64-bit 下短文本单字符差异海明距离过大
    （实测"半导体板块大涨创历史新高"与加"！"-版距离 8），32-bit 在区分度与
    容差间平衡（相似文本距离 2，不同文本距离 13）。
    """
    if not text:
        return 0
    grams = [text[i:i + 3] for i in range(max(len(text) - 2, 0))]
    if not grams:
        return 0
    v = [0] * bits
    for g in grams:
        h = int.from_bytes(hashlib.md5(g.encode('utf-8')).digest()[:8], 'big') & ((1 << bits) - 1)
        for i in range(bits):
            v[i] += 1 if (h >> i) & 1 else -1
    fingerprint = 0
    for i in range(bits):
        if v[i] > 0:
            fingerprint |= (1 << i)
    return fingerprint


def _hamming(a: int, b: int) -> int:
    """海明距离"""
    return bin(a ^ b).count("1")


def _in_news_window(published_at: str, look_back_days: int = 1) -> bool:
    """只保留 [今天-look_back_days, 今天] 的资讯，排除未来日期"""
    if not published_at or not str(published_at).strip():
        return False
    text = str(published_at).strip()
    now = datetime.now()
    # 窗口起点取 look_back_days 天前的 00:00，避免时间分量导致"昨天 00:00"被误判为早于起点
    start = (now - timedelta(days=look_back_days)).replace(hour=0, minute=0, second=0, microsecond=0)

    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d", "%Y%m%d", "%Y/%m/%d %H:%M:%S",
                "%Y/%m/%d", "%Y%m%d %H:%M:%S"]:
        try:
            pub_time = datetime.strptime(text, fmt)
            if start <= pub_time <= now:
                return True
            return False
        except ValueError:
            continue

    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    if text == today or text.startswith(today + " ") or text.startswith(today + "T"):
        return True
    if look_back_days >= 1 and (text == yesterday or text.startswith(yesterday + " ")):
        return True
    return False


def dedup_news三层(news_list: list, simhash_threshold: int = _SIMHASH_THRESHOLD) -> list:
    """三层去重：URL 精确 → 标题精确 → SimHash 近似"""
    seen_urls = set()
    after_url = []
    for news in news_list:
        url = _normalize_url(news.get("url", ""))
        if url:
            if url in seen_urls:
                continue
            seen_urls.add(url)
        after_url.append(news)

    seen_titles = set()
    after_title = []
    for news in after_url:
        title = news.get("title", "").strip()
        if title:
            if title in seen_titles:
                continue
            seen_titles.add(title)
        after_title.append(news)

    result = []
    fingerprints = []
    for news in after_title:
        title = news.get("title", "").strip()
        if not title:
            result.append(news)
            continue
        fp = _simhash(title)
        is_dup = False
        for existing_fp in fingerprints:
            if _hamming(fp, existing_fp) <= simhash_threshold:
                is_dup = True
                break
        if not is_dup:
            result.append(news)
            fingerprints.append(fp)

    return result


# ============================================================
# 实盘数据抓取
# ============================================================

AKSHARE_TIMEOUT = 20


def _fetch_em_news():
    """东财全球财经快讯 (stock_info_global_em)"""
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(AKSHARE_TIMEOUT)
    try:
        import akshare as ak
        df = ak.stock_info_global_em()
        news = []
        for _, row in df.iterrows():
            pub_time = str(row.get("发布时间", ""))
            if not _is_today(pub_time):
                continue
            news.append({
                "title": str(row.get("标题", "")),
                "source": "东方财富快讯",
                "content": str(row.get("摘要", "")),
                "published_at": pub_time,
                "category": "news",
                "sentiment": "neutral"
            })
        logger.info(f"东财快讯: 原始{len(df)}行, 当日{len(news)}条")
        return news
    except Exception as e:
        logger.warning(f"东财快讯获取失败: {e}")
        return []
    finally:
        socket.setdefaulttimeout(old_timeout)


def _fetch_cls_news():
    """财联社电报 (stock_info_global_cls)"""
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(AKSHARE_TIMEOUT)
    try:
        import akshare as ak
        # 尝试多个可能的 symbol 参数值
        df = None
        for sym in ["全部", "财经", ""]:
            try:
                df = ak.stock_info_global_cls(symbol=sym)
                if df is not None and len(df) > 0:
                    break
            except Exception:
                continue
        if df is None or len(df) == 0:
            return []
        news = []
        for _, row in df.iterrows():
            pub_date = str(row.get("发布日期", ""))
            if not _is_today(pub_date):
                continue
            pub_time = str(row.get("发布时间", ""))
            published_at = f"{pub_date} {pub_time}" if pub_date else ""
            news.append({
                "title": str(row.get("标题", "")),
                "source": "财联社电报",
                "content": str(row.get("内容", "")),
                "published_at": published_at,
                "category": "news",
                "sentiment": "neutral"
            })
        logger.info(f"财联社电报: 原始{len(df)}行, 当日{len(news)}条")
        return news
    except Exception as e:
        logger.warning(f"财联社电报获取失败: {e}")
        return []
    finally:
        socket.setdefaulttimeout(old_timeout)


def _fetch_sina_news():
    """新浪财经全球快讯 (stock_info_global_sina)"""
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(AKSHARE_TIMEOUT)
    try:
        import akshare as ak
        df = ak.stock_info_global_sina()
        news = []
        for _, row in df.iterrows():
            pub_time = str(row.get("时间", ""))
            if not _is_today(pub_time):
                continue
            content = str(row.get("内容", ""))
            title = content[:30] + ("..." if len(content) > 30 else "")
            news.append({
                "title": title,
                "source": "新浪财经",
                "content": content,
                "published_at": pub_time,
                "category": "news",
                "sentiment": "neutral"
            })
        logger.info(f"新浪财经: 原始{len(df)}行, 当日{len(news)}条")
        return news
    except Exception as e:
        logger.warning(f"新浪财经获取失败: {e}")
        return []
    finally:
        socket.setdefaulttimeout(old_timeout)


def _fetch_ths_news():
    """同花顺全球快讯 (stock_info_global_ths)"""
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(AKSHARE_TIMEOUT)
    try:
        import akshare as ak
        df = ak.stock_info_global_ths()
        news = []
        for _, row in df.iterrows():
            pub_time = str(row.get("发布时间", ""))
            if not _is_today(pub_time):
                continue
            news.append({
                "title": str(row.get("标题", "")),
                "source": "同花顺快讯",
                "content": str(row.get("内容", "")),
                "published_at": pub_time,
                "category": "news",
                "sentiment": "neutral"
            })
        logger.info(f"同花顺快讯: 原始{len(df)}行, 当日{len(news)}条")
        return news
    except Exception as e:
        logger.warning(f"同花顺快讯获取失败: {e}")
        return []
    finally:
        socket.setdefaulttimeout(old_timeout)


def _fetch_announcements():
    """交易所公告 (stock_notice_report)"""
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(AKSHARE_TIMEOUT)
    try:
        import akshare as ak
        today = datetime.now().strftime("%Y%m%d")
        # 尝试今天和前一天的公告
        df = None
        for date_try in [today, (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")]:
            try:
                df = ak.stock_notice_report(symbol="全部", date=date_try)
                if df is not None and len(df) > 0:
                    break
            except Exception:
                continue
        if df is None or len(df) == 0:
            return []
        announcements = []
        for _, row in df.iterrows():
            pub_time = str(row.get("公告日期", ""))
            if not _is_today(pub_time):
                continue
            announcements.append({
                "code": str(row.get("代码", "")),
                "name": str(row.get("名称", "")),
                "title": str(row.get("公告标题", "")),
                "type": str(row.get("公告类型", "")),
                "content": str(row.get("公告标题", "")),
                "published_at": pub_time
            })
        logger.info(f"交易所公告: 当日{len(announcements)}条")
        return announcements
    except Exception as e:
        logger.warning(f"交易所公告获取失败: {e}")
        return []
    finally:
        socket.setdefaulttimeout(old_timeout)


# ============================================================
# 信号情报
# ============================================================

YJYG_DIRECTION_MAP = {
    "预增": "bullish", "略增": "bullish", "扭亏": "bullish", "续盈": "bullish",
    "预减": "bearish", "略减": "bearish", "首亏": "bearish", "增亏": "bearish", "续亏": "bearish",
    "减亏": "bullish", "不确定": "neutral",
}


def _fetch_lhb_signal():
    """龙虎榜机构买卖统计 (stock_lhb_jgmmtj_em)"""
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(AKSHARE_TIMEOUT)
    try:
        import akshare as ak
        from datetime import datetime, timedelta
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
        df = ak.stock_lhb_jgmmtj_em(start_date=start_date, end_date=end_date)

        signals = []
        for _, row in df.iterrows():
            net_buy = row.get("机构买入净额", 0)
            if net_buy is None or net_buy < 30000000:
                continue
            code = str(row.get("代码", ""))
            name = str(row.get("名称", ""))
            net_buy_yi = net_buy / 1e8
            direction = "bullish"
            pub_time = str(row.get("上榜日期", ""))
            if not _is_today(pub_time):
                continue

            signals.append({
                "title": f"龙虎榜: {name}({code}) 机构净买入{net_buy_yi:.2f}亿元",
                "source": "交易所龙虎榜",
                "content": f"{name}({code})上榜原因:{row.get('上榜原因', '')}, "
                           f"机构买入净额{net_buy_yi:.2f}亿, 涨跌幅{row.get('涨跌幅', 0):.2f}%",
                "published_at": pub_time,
                "category": "signal",
                "sentiment": direction,
                "impact_direction": direction,
                "affected_sectors": [],
                "affected_stocks": [name] if name else [],
                "impact_reason": f"机构净买入{net_buy_yi:.2f}亿元, 资金面利好",
                "_sort_key": abs(net_buy_yi)
            })

        signals.sort(key=lambda x: x.get("_sort_key", 0), reverse=True)
        for s in signals:
            s.pop("_sort_key", None)
        seen_codes = set()
        deduped_signals = []
        for s in signals:
            stock = s.get("affected_stocks", [""])[0] if s.get("affected_stocks") else ""
            code_match = ""
            for part in s.get("title", "").split("("):
                if ")" in part:
                    code_match = part.split(")")[0]
                    break
            key = code_match or stock or s.get("title", "")
            if key not in seen_codes:
                seen_codes.add(key)
                deduped_signals.append(s)
        signals = deduped_signals[:15]
        logger.info(f"龙虎榜信号: 原始{len(df)}行 -> 筛选{len(signals)}条机构动向")
        return signals
    except Exception as e:
        logger.warning(f"龙虎榜信号获取失败: {e}")
        return []
    finally:
        socket.setdefaulttimeout(old_timeout)


def _fetch_yjyg_signal():
    """业绩预告 (stock_yjyg_em)"""
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(AKSHARE_TIMEOUT)
    try:
        import akshare as ak
        from datetime import datetime
        today = datetime.now()
        for month_day in [(6, 30), (3, 31), (9, 30), (12, 31)]:
            try:
                date_str = today.replace(month=month_day[0], day=month_day[1]).strftime("%Y%m%d")
                df = ak.stock_yjyg_em(date=date_str)
                if len(df) > 0:
                    break
            except Exception:
                continue
        else:
            return []

        signals = []
        for _, row in df.iterrows():
            amplitude = row.get("业绩变动幅度", 0)
            if amplitude is None:
                continue
            if amplitude > 100 or amplitude < -50:
                code = str(row.get("股票代码", ""))
                name = str(row.get("股票简称", ""))
                yj_type = str(row.get("预告类型", "不确定"))
                direction = YJYG_DIRECTION_MAP.get(yj_type, "neutral")
                change_desc = str(row.get("业绩变动", ""))[:150]
                pub_time = str(row.get("公告日期", ""))
                if not _is_today(pub_time):
                    continue

                signals.append({
                    "title": f"业绩预告: {name}({code}) {yj_type} 幅度{amplitude:.1f}%",
                    "source": "交易所业绩预告",
                    "content": change_desc,
                    "published_at": pub_time,
                    "category": "signal",
                    "sentiment": direction,
                    "impact_direction": direction,
                    "affected_sectors": [],
                    "affected_stocks": [name] if name else [],
                    "impact_reason": f"业绩{yj_type}, 变动幅度{amplitude:.1f}%, {'业绩显著改善' if direction == 'bullish' else '业绩显著下滑' if direction == 'bearish' else '业绩变动'}",
                    "_sort_key": abs(float(amplitude))
                })

        signals.sort(key=lambda x: x.get("_sort_key", 0), reverse=True)
        for s in signals:
            s.pop("_sort_key", None)
        seen_codes = set()
        deduped_signals = []
        for s in signals:
            code_match = ""
            for part in s.get("title", "").split("("):
                if ")" in part:
                    code_match = part.split(")")[0]
                    break
            key = code_match or s.get("title", "")
            if key not in seen_codes:
                seen_codes.add(key)
                deduped_signals.append(s)
        signals = deduped_signals[:20]
        logger.info(f"业绩预告信号: 原始{len(df)}行 -> 筛选{len(signals)}条高变动预告")
        return signals
    except Exception as e:
        logger.warning(f"业绩预告信号获取失败: {e}")
        return []
    finally:
        socket.setdefaulttimeout(old_timeout)


# ============================================================
# 并行获取器 (改进版: 每源独立超时, 失败源隔离)
# ============================================================

def _parallel_fetch(tasks: dict, per_source_timeout: int = 30, total_timeout: int = 120):
    """并行获取多个数据源，每个源独立超时，失败源不影响其他源"""
    results = {}
    failed_sources = []
    executor = ThreadPoolExecutor(max_workers=min(len(tasks), 4))
    futures = {executor.submit(fn): label for fn, label in tasks.items()}

    try:
        for future in as_completed(futures, timeout=total_timeout):
            label = futures[future]
            try:
                results[label] = future.result(timeout=per_source_timeout)
                logger.info(f"{label}: 获取成功, {len(results[label])}条")
            except Exception as e:
                failed_sources.append(label)
                results[label] = []
                logger.warning(f"{label}: 获取失败 - {e}")
    except Exception as e:
        logger.warning(f"并行获取部分超时: {e}")
        # 未完成的源标记为失败
        for future, label in futures.items():
            if not future.done():
                failed_sources.append(label)
                results[label] = []
    finally:
        executor.shutdown(wait=False)

    if failed_sources:
        logger.warning(f"以下数据源失败: {failed_sources}")

    return results


# ============================================================
# 沪深300成分股 (用于排序时识别优质个股)
# ============================================================

def get_hs300_constituents() -> dict:
    """获取沪深300成分股（代码集合 + 名称集合），带内存缓存（1天TTL）。

    用于排序时识别"非垃圾股"：沪深300以内视为优质个股，不降权。
    失败时返回空集合，调用方应据此跳过降权（保守不降权）。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    cache_key = "hs300_constituents"
    cached = _get_cache(cache_key, today)
    if cached is not None:
        return cached

    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(AKSHARE_TIMEOUT)
    try:
        import akshare as ak
        df = ak.index_stock_cons_csindex(symbol="000300")
        codes = set(str(c).zfill(6) for c in df["成分券代码"].tolist())
        names = set(str(n).strip() for n in df["成分券名称"].tolist() if str(n).strip())
        result = {"codes": codes, "names": names}
        _set_cache(cache_key, result, today)
        logger.info(f"沪深300成分股: 获取成功, {len(codes)}只")
        return result
    except Exception as e:
        logger.warning(f"沪深300成分股获取失败: {e}")
        return {"codes": set(), "names": set()}
    finally:
        socket.setdefaulttimeout(old_timeout)


# ============================================================
# LangChain Tools
# ============================================================

@tool
def get_stock_news(data_mode: str = "live") -> list:
    """获取当日全部A股财经新闻（多源聚合: 东财快讯 + 财联社电报）。
    自动按当日过滤 + 去重。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    cache_key = f"stock_news_{data_mode}"
    cached = _get_cache(cache_key, today)
    if cached is not None:
        return cached

    results = _parallel_fetch({
        _fetch_em_news: "东财快讯",
        _fetch_cls_news: "财联社电报",
        _fetch_sina_news: "新浪财经",
        _fetch_ths_news: "同花顺快讯",
    })

    all_news = []
    for label, data in results.items():
        all_news.extend(data)

    before_dedup = len(all_news)
    all_news = _dedup_by_title(all_news)
    logger.info(f"跨源去重: {before_dedup} -> {len(all_news)}条")

    if not all_news:
        logger.warning("所有实时新闻源失败, 返回空列表")
        return []

    _set_cache(cache_key, all_news, today)
    return all_news


@tool
def get_announcements(data_mode: str = "live") -> list:
    """获取当日全部交易所公告。"""
    today_fmt = datetime.now().strftime("%Y-%m-%d")
    cache_key = f"announcements_{data_mode}"
    cached = _get_cache(cache_key, today_fmt)
    if cached is not None:
        return cached

    results = _parallel_fetch({_fetch_announcements: "交易所公告"})

    announcements = results.get("交易所公告", [])
    if announcements:
        _set_cache(cache_key, announcements, today_fmt)
        return announcements
    else:
        logger.warning("akshare获取公告失败, 返回空列表")
        return []


@tool
def get_market_signals(data_mode: str = "live") -> list:
    """获取市场信号情报: 龙虎榜机构动向 + 业绩预告 (交易所官方披露)。"""
    today = datetime.now().strftime("%Y-%m-%d")
    cache_key = f"market_signals_{data_mode}"
    cached = _get_cache(cache_key, today)
    if cached is not None:
        return cached

    results = _parallel_fetch({
        _fetch_lhb_signal: "龙虎榜信号",
        _fetch_yjyg_signal: "业绩预告信号",
    })

    all_signals = []
    for label, data in results.items():
        all_signals.extend(data)

    if all_signals:
        _set_cache(cache_key, all_signals, today)

    return all_signals


data_fetcher_tools = [get_stock_news, get_announcements, get_market_signals]
