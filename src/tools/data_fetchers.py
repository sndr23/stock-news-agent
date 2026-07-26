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
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# 北京时区（云端 GitHub Actions 运行在 UTC，必须显式指定 BJT 避免日期过滤错位）
BJT = timezone(timedelta(hours=8))

# ============================================================
# 线程安全内存缓存 (带 TTL)
# ============================================================
_cache = {}
_cache_lock = threading.Lock()
_CACHE_TTL_SECONDS = 3600  # 缓存 1 小时过期
_CACHE_MAX_SIZE = 50  # 最大缓存条目数，防止长期运行 OOM


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
        # 清理过期项，防止长期运行内存泄漏
        now = time.time()
        expired_keys = [k for k, v in _cache.items() if now - v.get("_ts", 0) >= _CACHE_TTL_SECONDS]
        for k in expired_keys:
            _cache.pop(k, None)
        # 超出容量时清理最早的条目
        if len(_cache) >= _CACHE_MAX_SIZE:
            oldest = sorted(_cache.items(), key=lambda x: x[1].get("_ts", 0))[0][0]
            _cache.pop(oldest, None)
        _cache[key] = {"date": today, "data": data, "_ts": now}


# ============================================================
# 当日日期过滤 + 去重工具
# ============================================================

def _is_today(published_at: str) -> bool:
    """精确判断时间字符串是否是今天"""
    if not published_at or not str(published_at).strip():
        return False
    text = str(published_at).strip()
    today = datetime.now(BJT).strftime("%Y-%m-%d")
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
    now = datetime.now(BJT)
    start = (now - timedelta(days=look_back_days)).replace(hour=0, minute=0, second=0, microsecond=0)

    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d", "%Y%m%d", "%Y/%m/%d %H:%M:%S",
                "%Y/%m/%d", "%Y%m%d %H:%M:%S"]:
        try:
            pub_time = datetime.strptime(text, fmt)
            # strptime 返回 naive datetime，需加 BJT 时区才能与 aware now 比较
            pub_time = pub_time.replace(tzinfo=BJT)
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


def dedup_news_3layer(news_list: list, simhash_threshold: int = _SIMHASH_THRESHOLD) -> list:
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
    """东财全球财经快讯 (stock_info_global_em)

    akshare 固定返回最近200条，用 _in_news_window(look_back_days=1) 保留今天+昨天的数据，
    避免零点后或非交易日运行时当日数据过少。
    """
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(AKSHARE_TIMEOUT)
    try:
        import akshare as ak
        df = ak.stock_info_global_em()
        news = []
        for _, row in df.iterrows():
            pub_time = str(row.get("发布时间", ""))
            if not _in_news_window(pub_time, look_back_days=1):
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
    """财联社电报 (直接调用财联社官方API，不依赖akshare)

    新接口: https://www.cls.cn/api/cache?app=CailianpressWeb&name=telegraph&os=web&sv=8.7.9
    旧接口 nodeapi/telegraphList 已废弃(404)，akshare 的 stock_info_global_cls 响应极慢/失败
    新接口返回最近20条电报，无分页参数

    含重试机制: 财联社 API 稳定性较差，日志显示频繁超时，
    单次请求失败时重试最多2次（共3次尝试），每次间隔递增。
    """
    import time as _time

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.cls.cn/telegraph",
    }
    url = "https://www.cls.cn/api/cache"
    params = {
        "app": "CailianpressWeb",
        "name": "telegraph",
        "os": "web",
        "sv": "8.7.9",
    }

    max_retries = 2
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            # 防御 API 返回 {"data": null} 的情况
            data_obj = data.get("data") or {}
            roll_data = data_obj.get("roll_data") or []
            if not roll_data:
                logger.warning("财联社电报: API返回空 roll_data")
                return []

            news = []
            today_str = datetime.now(BJT).strftime("%Y-%m-%d")
            for item in roll_data:
                ctime = item.get("ctime", "")
                if not ctime:
                    continue
                try:
                    # 兼容秒级(10位)和毫秒级(13位)时间戳
                    ctime_int = int(ctime)
                    if ctime_int > 1e12:  # 毫秒级
                        ctime_int = ctime_int // 1000
                    dt = datetime.fromtimestamp(ctime_int, BJT)
                    pub_date = dt.strftime("%Y-%m-%d")
                    pub_time = dt.strftime("%H:%M:%S")
                except (ValueError, TypeError):
                    continue

                # 保留今天+昨天的电报（财联社API只返回20条，零点后当日可能只有1-2条）
                yesterday_str = (datetime.now(BJT) - timedelta(days=1)).strftime("%Y-%m-%d")
                if pub_date not in (today_str, yesterday_str):
                    continue

                # 标题: 优先 title 字段，没有则取 content 前40字
                title = item.get("title", "").strip()
                content = item.get("content", "").strip()
                if not title:
                    title = content[:40] + ("..." if len(content) > 40 else "")
                if not title:
                    continue

                news.append({
                    "title": title,
                    "source": "财联社电报",
                    "content": content,
                    "published_at": f"{pub_date} {pub_time}",
                    "category": "news",
                    "sentiment": "neutral",
                })

            logger.info(f"财联社电报: 原始{len(roll_data)}条, 当日{len(news)}条"
                        + (f" (第{attempt+1}次尝试成功)" if attempt > 0 else ""))
            return news
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                wait = 2 * (attempt + 1)
                logger.warning(f"财联社电报第{attempt+1}次请求失败: {e}, {wait}s后重试...")
                _time.sleep(wait)
            else:
                logger.warning(f"财联社电报获取失败(已重试{max_retries}次): {e}")
    return []


def _fetch_sina_news():
    """新浪财经全球快讯 (stock_info_global_sina)

    akshare 固定返回最近20条，用 _in_news_window(look_back_days=1) 保留今天+昨天的数据。
    """
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(AKSHARE_TIMEOUT)
    try:
        import akshare as ak
        df = ak.stock_info_global_sina()
        news = []
        for _, row in df.iterrows():
            pub_time = str(row.get("时间", ""))
            if not _in_news_window(pub_time, look_back_days=1):
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
    """同花顺全球快讯 (stock_info_global_ths)

    akshare 固定返回最近20条，用 _in_news_window(look_back_days=1) 保留今天+昨天的数据。
    """
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(AKSHARE_TIMEOUT)
    try:
        import akshare as ak
        df = ak.stock_info_global_ths()
        news = []
        for _, row in df.iterrows():
            pub_time = str(row.get("发布时间", ""))
            if not _in_news_window(pub_time, look_back_days=1):
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
    """交易所公告 (stock_notice_report)

    查询最近3天的公告并合并去重，确保非交易日运行时也能获取到最近交易日的公告。
    交易日盘后某一天可能有 1000+ 条公告，盘前/非交易日可能只有几十条。
    """
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(AKSHARE_TIMEOUT)
    try:
        import akshare as ak
        now = datetime.now(BJT)
        # 查询最近3天的公告（覆盖周末：周一运行时能拿到周五+周六的公告）
        dates_to_query = [(now - timedelta(days=i)).strftime("%Y%m%d") for i in range(3)]

        all_dfs = []
        for date_try in dates_to_query:
            try:
                df = ak.stock_notice_report(symbol="全部", date=date_try)
                if df is not None and len(df) > 0:
                    all_dfs.append(df)
            except Exception:
                continue
        if not all_dfs:
            return []

        # 合并多天公告并去重（按 代码+公告标题+公告日期 去重）
        import pandas as pd
        merged_df = pd.concat(all_dfs, ignore_index=True)
        merged_df = merged_df.drop_duplicates(subset=["代码", "公告标题", "公告日期"], keep="first")

        announcements = []
        for _, row in merged_df.iterrows():
            pub_time = str(row.get("公告日期", ""))
            # 保留最近3天窗口内的公告
            if not _in_news_window(pub_time, look_back_days=3):
                continue
            announcements.append({
                "code": str(row.get("代码", "")),
                "name": str(row.get("名称", "")),
                "title": str(row.get("公告标题", "")),
                "type": str(row.get("公告类型", "")),
                "content": str(row.get("公告标题", "")),
                "published_at": pub_time
            })
        logger.info(f"交易所公告: 查询{len(dates_to_query)}天, 合并去重后{len(announcements)}条")
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
        end_date = datetime.now(BJT).strftime("%Y%m%d")
        start_date = (datetime.now(BJT) - timedelta(days=7)).strftime("%Y%m%d")
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
            # 保留最近3个交易日数据（周末/节假日不丢周五数据）
            if not _in_news_window(pub_time, look_back_days=3):
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
        today = datetime.now(BJT)
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
                # 保留最近3个交易日披露的预告
                if not _in_news_window(pub_time, look_back_days=3):
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
    max_workers = max(min(len(tasks), 4), 2)  # 至少 2 个 worker，单任务也并行
    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = {executor.submit(fn): label for fn, label in tasks.items()}

    try:
        for future in as_completed(futures, timeout=total_timeout):
            label = futures[future]
            try:
                result = future.result(timeout=per_source_timeout)
                # 防御返回 None
                results[label] = result if isinstance(result, list) else []
                logger.info(f"{label}: 获取成功, {len(results[label])}条")
            except Exception as e:
                failed_sources.append(label)
                results[label] = []
                logger.warning(f"{label}: 获取失败 - {e}")
    except Exception as e:
        logger.warning(f"并行获取部分超时: {e}")
        # 未完成的源标记为失败并取消
        for future, label in futures.items():
            if not future.done():
                future.cancel()
                failed_sources.append(label)
                results[label] = []
    finally:
        # cancel_futures=True 取消未启动的任务（Python 3.9+）
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
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
    today = datetime.now(BJT).strftime("%Y-%m-%d")
    cache_key = "hs300_constituents"
    cached = _get_cache(cache_key, today)
    if cached is not None:
        return cached

    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(AKSHARE_TIMEOUT)
    try:
        import akshare as ak
        df = ak.index_stock_cons_csindex(symbol="000300")
        # 防御空 DataFrame 或列名变更
        if df is None or df.empty or "成分券代码" not in df.columns:
            logger.warning(f"沪深300成分股: 返回数据异常, df={df is None and 'None' or (df.empty and 'empty' or 'missing column')}")
            return {"codes": set(), "names": set()}
        codes = set(str(c).zfill(6) for c in df["成分券代码"].dropna().tolist() if str(c).strip())
        names_col = "成分券名称" if "成分券名称" in df.columns else None
        names = set(str(n).strip() for n in df[names_col].dropna().tolist() if str(n).strip()) if names_col else set()
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
    today = datetime.now(BJT).strftime("%Y-%m-%d")
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
    today_fmt = datetime.now(BJT).strftime("%Y-%m-%d")
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
    today = datetime.now(BJT).strftime("%Y-%m-%d")
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
