# filepath: src/tools/data_fetchers.py
"""
A股资讯数据获取工具
多源并行聚合: 东财快讯 + 财联社电报 + 新浪/同花顺 + 富途全球 + 华尔街见闻 + 交易所公告
国际资讯覆盖: 富途全球(美股/港股/国际宏观) + 华尔街见闻(地缘政治/外围股市/大宗商品)
抓取全部可用数据, 按当日日期过滤 + 去重
"""
import json
import socket
import logging
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
# 当日日期过滤 + 去重工具
# ============================================================

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

    超短标题（<5字）3-gram 数量不足，SimHash 区分度急剧下降（单字符差异
    可能导致海明距离接近 bits/2），返回 0 让三层去重依赖前两层（URL+精确标题）。
    """
    if not text:
        return 0
    # 超短标题 3-gram 不足，SimHash 不可靠，跳过近似去重
    if len(text) < 5:
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
# 全局设置一次 socket 超时，避免多线程在 ThreadPoolExecutor 中竞争 setdefaulttimeout
# 导致的线程安全问题（某些请求无限挂起或过早超时）。
# akshare 内部不传 timeout 参数，依赖 socket.setdefaulttimeout 作为兜底；
# requests/LLM 调用均显式传 timeout，不受此全局值影响。
socket.setdefaulttimeout(AKSHARE_TIMEOUT)


def _fetch_em_news():
    """东财全球财经快讯 (stock_info_global_em)

    akshare 固定返回最近200条，用 _in_news_window(look_back_days=1) 保留今天+昨天的数据，
    避免零点后或非交易日运行时当日数据过少。
    """
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


def _fetch_cls_news():
    """财联社电报 (直接调用财联社官方API，不依赖akshare)

    新接口: https://www.cls.cn/api/cache?app=CailianpressWeb&name=telegraph&os=web&sv=8.7.9
    旧接口 nodeapi/telegraphList 已废弃(404)，akshare 的 stock_info_global_cls 响应极慢/失败
    新接口返回最近20条电报，无分页参数

    超时优化: 财联社 API 稳定性差且偶发连接挂起，原先单值超时 15s + 重试2次
    最坏耗时 51s，超过 _parallel_fetch 的 per_source_timeout=30，拖垮整体等满
    total_timeout=120s。改为 (connect,read)=(5,8) 元组 + 重试1次，最坏 28s < 30s，
    快速失败让并行框架隔离该源，避免拖慢东财/新浪/同花顺已成功的批次。
    """
    import time as _time

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.cls.cn/telegraph",
    }
    url = "https://www.cls.cn/api/cache"
    # CLS API 版本号: 可从环境变量覆盖，避免 CLS 更新后硬编码版本静默失效
    import os as _os
    _CLS_SV = _os.getenv("CLS_API_SV", "8.7.9")
    params = {
        "app": "CailianpressWeb",
        "name": "telegraph",
        "os": "web",
        "sv": _CLS_SV,
    }

    # (connect, read) 元组：连接阶段 5s 防挂起，读取阶段 8s 防慢响应
    _CLS_TIMEOUT = (5, 8)
    max_retries = 1
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=_CLS_TIMEOUT)
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


def _fetch_ths_news():
    """同花顺全球快讯 (stock_info_global_ths)

    akshare 固定返回最近20条，用 _in_news_window(look_back_days=1) 保留今天+昨天的数据。
    """
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


def _fetch_futu_news():
    """富途全球快讯 (stock_info_global_futu)

    富途牛牛全球财经快讯，覆盖美股/港股/A股及国际宏观，
    固定返回最近50条，国际资讯覆盖度优于东财/新浪/同花顺。
    用 _in_news_window(look_back_days=1) 保留今天+昨天的数据。
    """
    try:
        import akshare as ak
        df = ak.stock_info_global_futu()
        news = []
        for _, row in df.iterrows():
            pub_time = str(row.get("发布时间", ""))
            if not _in_news_window(pub_time, look_back_days=1):
                continue
            content = str(row.get("内容", ""))
            title = str(row.get("标题", ""))
            # 富途标题可能为空，用内容前40字兜底
            if not title:
                title = content[:40] + ("..." if len(content) > 40 else "")
            if not title:
                continue
            news.append({
                "title": title,
                "source": "富途全球快讯",
                "content": content,
                "published_at": pub_time,
                "category": "news",
                "sentiment": "neutral"
            })
        logger.info(f"富途全球快讯: 原始{len(df)}行, 当日{len(news)}条")
        return news
    except Exception as e:
        logger.warning(f"富途全球快讯获取失败: {e}")
        return []


def _fetch_wallstreetcn_news():
    """华尔街见闻实时快讯 (直接API)

    华尔街见闻 global-channel 涵盖全球宏观/地缘政治/外围股市/大宗商品，
    国际资讯覆盖度最优。API返回最近30条，无分页。
    内容字段可能含 HTML 标签，需清洗。
    """
    import time as _time
    import re as _re

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    url = "https://api-one-wscn.awtmt.com/apiv1/content/lives"
    params = {"channel": "global-channel", "limit": 30}

    _WSCN_TIMEOUT = (3, 5)  # connect 3s + read 5s，最坏 3+5+2+3+5=18s < per_source_timeout=30s
    max_retries = 1
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=_WSCN_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("data", {}).get("items", [])
            if not items:
                logger.warning("华尔街见闻: API返回空 items")
                return []

            news = []
            today_str = datetime.now(BJT).strftime("%Y-%m-%d")
            yesterday_str = (datetime.now(BJT) - timedelta(days=1)).strftime("%Y-%m-%d")
            for item in items:
                # display_time 是秒级时间戳
                display_time = item.get("display_time", 0)
                if not display_time:
                    continue
                try:
                    dt = datetime.fromtimestamp(int(display_time), BJT)
                    pub_date = dt.strftime("%Y-%m-%d")
                    pub_time = dt.strftime("%H:%M:%S")
                except (ValueError, TypeError, OSError):
                    continue

                if pub_date not in (today_str, yesterday_str):
                    continue

                # 标题优先 title 字段，没有则从 content 提取纯文本
                title = item.get("title", "").strip()
                content_raw = item.get("content", "")
                # content 可能是 list[{content: "..."}] 或 string
                if isinstance(content_raw, list):
                    content_text = " ".join(
                        str(c.get("content", "")) for c in content_raw if isinstance(c, dict)
                    )
                else:
                    content_text = str(content_raw)

                # 清洗 HTML 标签
                content_text = _re.sub(r'<[^>]+>', '', content_text).strip()
                if not title:
                    title = content_text[:40] + ("..." if len(content_text) > 40 else "")
                if not title:
                    continue

                news.append({
                    "title": title,
                    "source": "华尔街见闻",
                    "content": content_text,
                    "published_at": f"{pub_date} {pub_time}",
                    "category": "news",
                    "sentiment": "neutral",
                })

            logger.info(f"华尔街见闻: 原始{len(items)}条, 当日{len(news)}条"
                        + (f" (第{attempt+1}次尝试成功)" if attempt > 0 else ""))
            return news
        except Exception as e:
            if attempt < max_retries:
                wait = 2 * (attempt + 1)
                logger.warning(f"华尔街见闻第{attempt+1}次请求失败: {e}, {wait}s后重试...")
                _time.sleep(wait)
            else:
                logger.warning(f"华尔街见闻获取失败(已重试{max_retries}次): {e}")
    return []


def _fetch_announcements():
    """交易所公告 (stock_notice_report)

    查询最近3天的公告并合并去重，确保非交易日运行时也能获取到最近交易日的公告。
    交易日盘后某一天可能有 1000+ 条公告，盘前/非交易日可能只有几十条。
    """
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

        # 合并多天公告并去重（列名随 akshare 版本变动，用映射兼容）
        import pandas as pd
        # 列名别名映射：标准名 -> 可能的变体
        _col_map = {
            "code": ["代码", "股票代码", "code"],
            "name": ["名称", "股票简称", "name"],
            "title": ["公告标题", "标题", "title"],
            "type": ["公告类型", "类型", "type"],
            "date": ["公告日期", "日期", "date"],
        }
        def _get_col(row, std_name):
            for alias in _col_map.get(std_name, [std_name]):
                if alias in row:
                    return row[alias]
            return ""
        def _find_col(df_cols, std_name):
            for alias in _col_map.get(std_name, [std_name]):
                if alias in df_cols:
                    return alias
            return None

        merged_df = pd.concat(all_dfs, ignore_index=True)
        # 动态确定去重列名
        _dedup_cols = [c for c in [_find_col(merged_df.columns, "code"),
                                    _find_col(merged_df.columns, "title"),
                                    _find_col(merged_df.columns, "date")] if c]
        if _dedup_cols:
            merged_df = merged_df.drop_duplicates(subset=_dedup_cols, keep="first")

        announcements = []
        for _, row in merged_df.iterrows():
            pub_time = str(_get_col(row, "date"))
            # 保留最近3天窗口内的公告
            if not _in_news_window(pub_time, look_back_days=3):
                continue
            announcements.append({
                "code": str(_get_col(row, "code")),
                "name": str(_get_col(row, "name")),
                "title": str(_get_col(row, "title")),
                "type": str(_get_col(row, "type")),
                "content": str(_get_col(row, "title")),
                "published_at": pub_time
            })
        logger.info(f"交易所公告: 查询{len(dates_to_query)}天, 合并去重后{len(announcements)}条")
        return announcements
    except Exception as e:
        logger.warning(f"交易所公告获取失败: {e}")
        return []


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


def _fetch_yjyg_signal():
    """业绩预告 (stock_yjyg_em)

    日期回查逻辑：按"距今天最近且已过"的报告期排序尝试，
    避免查到未来日期（1月运行时 today.replace(month=12) 产生今年12/31）
    和过期数据（非财报季查到上季度旧数据全部被 _in_news_window 过滤为空）。
    """
    try:
        import akshare as ak
        today = datetime.now(BJT)
        # 报告期固定为季度末：3/31, 6/30, 9/30, 12/31
        # 按距今天数升序排列，优先查最近的已过报告期
        quarter_ends = [(3, 31), (6, 30), (9, 30), (12, 31)]
        candidates = []
        for m, d in quarter_ends:
            report_date = today.replace(month=m, day=d)
            # 未来日期回退到去年（1月运行时12/31应为去年）
            if report_date > today:
                report_date = report_date.replace(year=today.year - 1)
            days_ago = (today - report_date).days
            candidates.append((days_ago, m, d, report_date))
        candidates.sort(key=lambda x: x[0])  # 按距今天数升序

        df = None
        for days_ago, m, d, report_date in candidates:
            try:
                date_str = report_date.strftime("%Y%m%d")
                df = ak.stock_yjyg_em(date=date_str)
                if df is not None and len(df) > 0:
                    break
            except Exception:
                continue
        if df is None or len(df) == 0:
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

_hs300_singleton: dict | None = None


def get_hs300_constituents() -> dict:
    """获取沪深300成分股（代码集合 + 名称集合）。

    进程内单例：沪深300成分股准静态（季度调整），同进程内只请求一次 akshare，
    后续调用直接复用。失败时返回空集合，调用方应据此跳过降权（保守不降权）。
    """
    global _hs300_singleton
    if _hs300_singleton is not None:
        return _hs300_singleton

    try:
        import akshare as ak
        df = ak.index_stock_cons_csindex(symbol="000300")
        # 防御空 DataFrame 或列名变更
        if df is None or df.empty or "成分券代码" not in df.columns:
            logger.warning(f"沪深300成分股: 返回数据异常, df={df is None and 'None' or (df.empty and 'empty' or 'missing column')}")
            _hs300_singleton = {"codes": set(), "names": set()}
            return _hs300_singleton
        codes = set(str(c).zfill(6) for c in df["成分券代码"].dropna().tolist() if str(c).strip())
        names_col = "成分券名称" if "成分券名称" in df.columns else None
        names = set(str(n).strip() for n in df[names_col].dropna().tolist() if str(n).strip()) if names_col else set()
        _hs300_singleton = {"codes": codes, "names": names}
        logger.info(f"沪深300成分股: 获取成功, {len(codes)}只")
        return _hs300_singleton
    except Exception as e:
        logger.warning(f"沪深300成分股获取失败: {e}")
        _hs300_singleton = {"codes": set(), "names": set()}
        return _hs300_singleton


# ============================================================
# LangChain Tools
# ============================================================

@tool
def get_stock_news(data_mode: str = "live") -> list:
    """获取当日全部A股财经新闻（多源聚合: 东财快讯 + 财联社电报 + 富途全球 + 华尔街见闻）。
    自动按当日过滤 + 去重。
    国际资讯覆盖：富途全球快讯(美股/港股/国际宏观) + 华尔街见闻(地缘政治/外围股市/大宗商品)。
    """
    today = datetime.now(BJT).strftime("%Y-%m-%d")

    results = _parallel_fetch({
        _fetch_em_news: "东财快讯",
        _fetch_cls_news: "财联社电报",
        _fetch_sina_news: "新浪财经",
        _fetch_ths_news: "同花顺快讯",
        _fetch_futu_news: "富途全球快讯",
        _fetch_wallstreetcn_news: "华尔街见闻",
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

    return all_news


@tool
def get_announcements(data_mode: str = "live") -> list:
    """获取当日全部交易所公告。"""
    results = _parallel_fetch({_fetch_announcements: "交易所公告"})

    announcements = results.get("交易所公告", [])
    if announcements:
        return announcements
    else:
        logger.warning("akshare获取公告失败, 返回空列表")
        return []


@tool
def get_market_signals(data_mode: str = "live") -> list:
    """获取市场信号情报: 龙虎榜机构动向 + 业绩预告 (交易所官方披露)。"""
    results = _parallel_fetch({
        _fetch_lhb_signal: "龙虎榜信号",
        _fetch_yjyg_signal: "业绩预告信号",
    })

    all_signals = []
    for label, data in results.items():
        all_signals.extend(data)

    return all_signals


data_fetcher_tools = [get_stock_news, get_announcements, get_market_signals]
