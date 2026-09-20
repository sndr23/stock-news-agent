# filepath: scripts/intraday_sweep.py
"""SNA-06 盘中异动反查链路（2026-09-07）

用户痛点：盘中急跌急拉通常由"小作文"驱动，正规新闻渠道很难捕捉——
等财联社/东财快讯滚动出来，行情已经走完。需要分钟级的异动监控 +
反查消息面，比正规渠道更快定位"到底是什么消息"。

设计（用户 2026-09-07 拍板）：
1. 复用现有 30 分钟 realtime-push 轮询，每轮顺带拉一次分钟 K 线对比
   （零新增调度成本，异动消息延迟最多 30 分钟）。
2. 触发条件：watchlist 个股在检测窗口（默认 30 分钟）内出现 1 分钟
   涨跌幅超阈值（默认 ±2%，env SWEEP_MOVE_PCT 可覆盖）的异动。
3. 反查：异动时刻前后 10 分钟内，在已抓取的本轮新闻流（news_list）+
   watchlist 公告里找关联消息（标题含股票名/代码）。
4. 推送口径（用户拍板）：只推"找不到关联消息"的异动（疑似小作文）——
   找到消息的不推，因为 30 分钟后正规推送会覆盖；找不到的才值得立刻知道。
5. 正常涨跌不推送（只记录日志）。
6. 与 _is_noise_push 解耦：本链路独立于主推送管线，直接构造推送，
   不进 LLM 判定（异动本身是硬事实，不需要方向判定）。

去重：异动按 (股票代码, 异动方向, 自然小时) 键写 state["sweep_events"]，
同股同方向 1 小时内只推一次（防单边行情连续触发刷屏）。
"""

import os
import json
import logging
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

BJT = timezone(timedelta(hours=8))

# 检测窗口（分钟）：覆盖 30 分钟轮询间隔，跨轮无缝衔接
SWEEP_WINDOW_MIN = int(os.getenv("SWEEP_WINDOW_MIN", "30") or 30)
# 1 分钟涨跌幅触发阈值（%）
SWEEP_MOVE_PCT = float(os.getenv("SWEEP_MOVE_PCT", "2.0") or 2.0)
# 反查窗口（分钟）：异动时刻前后各 N 分钟内的新闻算"关联"
SWEEP_NEWS_LOOKUP_MIN = int(os.getenv("SWEEP_NEWS_LOOKUP_MIN", "10") or 10)
# 同股同方向冷却（小时）：窗口内重复异动不重复推
SWEEP_COOLDOWN_HOURS = float(os.getenv("SWEEP_COOLDOWN_HOURS", "1.0") or 1.0)
# 每轮推送上限（防极端行情刷屏）
SWEEP_MAX_PUSH_PER_ROUND = int(os.getenv("SWEEP_MAX_PUSH_PER_ROUND", "3") or 3)

# 腾讯分钟K线（与 factor_collector.fetch_minute_kline 同源；独立实现避免
# 引入 factor_collector 的重依赖链——real_time_push 已经很重）
_MKLINE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={sym},m1,,{count}"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _tencent_symbol(code: str) -> str:
    """6 位代码 → 腾讯带市场前缀符号（深 sz / 沪 sh，北交所暂不支持）。"""
    code = str(code or "").strip()
    if not code or not code.isdigit() or len(code) != 6:
        return ""
    return ("sh" if code.startswith(("6", "9", "5")) else "sz") + code


def _fetch_minute_closes(session_get, symbol: str, count: int = 32) -> list:
    """拉一只股票的分钟收盘序列 [(time_str, close)]，升序；失败返回 []。

    session_get: requests.get（注入以便测试 mock）。
    """
    url = _MKLINE_URL.format(sym=symbol, count=count)
    try:
        resp = session_get(url, headers={"User-Agent": _UA}, timeout=(3, 8))
        resp.raise_for_status()
        data = json.loads(resp.text or "{}").get("data") or {}
        rows = (data.get(symbol) or {}).get("m1") or []
    except Exception as e:
        logger.warning(f"分钟K线拉取失败 {symbol}: {e}")
        return []
    out = []
    for r in rows:
        if not isinstance(r, list) or len(r) < 3:
            continue
        try:
            out.append((str(r[0]), float(r[2])))
        except (ValueError, TypeError):
            continue
    return out


def _parse_mkline_time(t: str, today: str):
    """'202609071451' + '2026-09-07' → aware datetime（BJT）；失败返回 None。"""
    try:
        return datetime.strptime(f"{today[:10]} {t[8:10]}:{t[10:12]}",
                                 "%Y-%m-%d %H:%M").replace(tzinfo=BJT)
    except (ValueError, IndexError):
        # 跨场景兜底：t 自带完整日期
        try:
            return datetime.strptime(t[:12], "%Y%m%d%H%M").replace(tzinfo=BJT)
        except ValueError:
            return None


def detect_sweeps(session_get, watch_stocks: list, now: datetime = None,
                  window_min: int = None, threshold_pct: float = None) -> list:
    """扫描 watchlist，返回触发异动列表。

    Args:
        session_get: requests.get（注入）
        watch_stocks: [{"code": "300308", "name": "中际旭创"}, ...]
        now: 当前时刻（测试注入）；默认 datetime.now(BJT)
        window_min / threshold_pct: 覆盖默认阈值（测试注入）

    Returns:
        [{"code", "name", "time": datetime, "move_pct": float, "price": float}, ...]
        单只股票窗口内多次触发只保留幅度最大的一次。
    """
    now = now or datetime.now(BJT)
    window_min = int(window_min if window_min is not None else SWEEP_WINDOW_MIN)
    threshold_pct = float(threshold_pct if threshold_pct is not None else SWEEP_MOVE_PCT)
    sweeps = []
    window_start = now - timedelta(minutes=window_min)
    for s in watch_stocks:
        code = str(s.get("code", "") or "").strip()
        name = str(s.get("name", "") or "").strip()
        symbol = _tencent_symbol(code)
        if not symbol:
            continue
        bars = _fetch_minute_closes(session_get, symbol, count=window_min + 8)
        if len(bars) < 2:
            continue
        best = None  # (move_pct, time, price)
        for i in range(1, len(bars)):
            t_prev, c_prev = bars[i - 1]
            t_cur, c_cur = bars[i]
            if c_prev <= 0:
                continue
            dt = _parse_mkline_time(t_cur, now.strftime("%Y-%m-%d"))
            if dt is None or dt < window_start or dt > now:
                continue
            move = (c_cur / c_prev - 1.0) * 100.0
            if abs(move) >= threshold_pct:
                if best is None or abs(move) > abs(best[0]):
                    best = (move, dt, c_cur)
        if best is not None:
            sweeps.append({"code": code, "name": name, "time": best[1],
                           "move_pct": round(best[0], 2), "price": best[2]})
    return sweeps


def find_related_news(sweep: dict, news_list: list, lookup_min: int = None) -> list:
    """在新闻流里找异动关联消息（标题含股票名/代码 + 时间在窗口内）。

    Returns: [{"title", "source", "published_at"}]（无 → []，即疑似小作文）
    """
    lookup_min = int(lookup_min if lookup_min is not None else SWEEP_NEWS_LOOKUP_MIN)
    name = str(sweep.get("name", "") or "")
    code = str(sweep.get("code", "") or "")
    t = sweep.get("time")
    if not name or not isinstance(t, datetime):
        return []
    t = _ensure_aware(t)
    lo = t - timedelta(minutes=lookup_min)
    hi = t + timedelta(minutes=lookup_min)
    hits = []
    for n in news_list or []:
        title = str(n.get("title", "") or "")
        if not title or (name not in title and code not in title):
            continue
        pub = str(n.get("published_at", "") or "")
        # 无时间戳的新闻无法判窗口，保守视为"有关联"（宁可不推小作文，不误报）
        if not pub:
            hits.append({"title": title[:80], "source": str(n.get("source", ""))[:30],
                         "published_at": ""})
            continue
        pt = _parse_news_time(pub)
        if pt is None:
            hits.append({"title": title[:80], "source": str(n.get("source", ""))[:30],
                         "published_at": pub[:30]})
            continue
        if lo <= pt <= hi:
            hits.append({"title": title[:80], "source": str(n.get("source", ""))[:30],
                         "published_at": pub[:30]})
    return hits


def _ensure_aware(dt: datetime) -> datetime:
    """naive datetime（测试/脏数据）补 BJT 时区，aware 原样返回。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=BJT)
    return dt


def _parse_news_time(pub: str):
    """新闻时间戳 'YYYY-MM-DD HH:MM[:SS]' → aware datetime；失败返回 None。"""
    for fmt, width in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16)):
        try:
            return datetime.strptime(str(pub)[:width], fmt).replace(tzinfo=BJT)
        except ValueError:
            continue
    return None


def _sweep_cooldown_key(sweep: dict) -> str:
    return f"{sweep.get('code')}#{'up' if sweep.get('move_pct', 0) > 0 else 'down'}"


def filter_by_cooldown(sweeps: list, sweep_state: list, now: datetime) -> list:
    """按 (股票, 方向, 冷却小时) 去重；返回本轮应处理的异动。"""
    out = []
    for s in sweeps:
        key = _sweep_cooldown_key(s)
        t = s.get("time")
        if not isinstance(t, datetime):
            continue
        recent = [e for e in sweep_state or []
                  if e.get("key") == key and isinstance(e.get("t"), str)]
        blocked = False
        for e in recent:
            try:
                pt = datetime.strptime(e["t"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJT)
            except ValueError:
                continue
            if abs((t - pt).total_seconds()) < SWEEP_COOLDOWN_HOURS * 3600:
                blocked = True
                break
        if not blocked:
            out.append(s)
    return out


def format_sweep_alert(sweep: dict) -> tuple:
    """构造推送 (title, content)。红涨绿跌 A 股惯例。"""
    name = sweep.get("name", "")
    move = sweep.get("move_pct", 0.0)
    price = sweep.get("price", 0.0)
    t = sweep.get("time")
    t_str = t.strftime("%H:%M") if isinstance(t, datetime) else "--:--"
    up = move > 0
    emoji = "🔴" if up else "🟢"
    arrow = "急拉" if up else "急跌"
    title = f"{name}{arrow}提醒｜1分钟{move:+.1f}%（无消息面）"
    content = (
        f"{emoji} **盘中异动（疑似小作文）**\n\n"
        f"- 股票：**{name}**（{sweep.get('code')}）\n"
        f"- 异动：1分钟内**{move:+.2f}%**（{arrow}），现价 {price:.2f}，时间 {t_str}\n"
        f"- 消息面：本轮新闻流与公告中**未找到关联消息**（前后{SWEEP_NEWS_LOOKUP_MIN}分钟窗口）\n\n"
        f"⚠️ 无正规消息源却出现急涨急跌，通常由传闻/小作文驱动，注意甄别与风险。\n"
        f"_（数据：腾讯分钟K线，自动反查）_"
    )
    return title, content


def run_sweep(session_get, watch_stocks: list, news_list: list,
              sweep_state: list, now: datetime = None,
              send_push=None, dry_run: bool = False,
              window_min=None, threshold_pct=None) -> dict:
    """主入口：扫描 → 反查 → 过滤 → 推送。返回统计。

    Args:
        session_get: requests.get
        watch_stocks: watchlist.json stocks
        news_list: 本轮已抓取的全部新闻（含公告）——run_once 抓取后传入
        sweep_state: state["sweep_events"] 列表（调用方持有并持久化）
        send_push: fn(title, content) -> dict；None 时只记录不推
        now / window_min / threshold_pct: 测试注入

    Returns:
        {"detected": N, "pushed": M, "suppressed_with_news": K}
    """
    now = now or datetime.now(BJT)
    # 非交易时段不扫（周末/深夜拉分钟K线无意义）
    if now.weekday() >= 5 or not (datetime.strptime("09:30", "%H:%M").time()
                                   <= now.time() <= datetime.strptime("15:05", "%H:%M").time()):
        return {"detected": 0, "pushed": 0, "suppressed_with_news": 0}
    sweeps = detect_sweeps(session_get, watch_stocks, now=now,
                           window_min=window_min, threshold_pct=threshold_pct)
    if not sweeps:
        return {"detected": 0, "pushed": 0, "suppressed_with_news": 0}
    logger.info(f"[sweep] 检测到 {len(sweeps)} 只 watchlist 个股分钟级异动: "
                + ", ".join(f"{s['name']}{s['move_pct']:+.1f}%" for s in sweeps))
    sweeps = filter_by_cooldown(sweeps, sweep_state, now)
    pushed = 0
    suppressed = 0
    for s in sweeps[:SWEEP_MAX_PUSH_PER_ROUND]:
        related = find_related_news(s, news_list)
        # 冷却登记（无论是否推送，避免同股同方向反复扫描反复进入候选）
        sweep_state.append({"key": _sweep_cooldown_key(s),
                            "t": s["time"].strftime("%Y-%m-%d %H:%M:%S"),
                            "move_pct": s["move_pct"],
                            "related_news": len(related)})
        if related:
            # 用户口径：找到消息的不推（30 分钟后正规推送覆盖）
            suppressed += 1
            logger.info(f"[sweep] {s['name']} 异动已找到 {len(related)} 条关联消息，"
                        f"不推（正规推送将覆盖）: {related[0].get('title', '')[:40]}")
            continue
        title, content = format_sweep_alert(s)
        if dry_run:
            logger.info(f"[sweep][dry-run] 将推送: {title}")
            pushed += 1
            continue
        if send_push is None:
            logger.warning(f"[sweep] 无推送通道，仅记录: {title}")
            continue
        result = send_push(title, content)
        if result.get("code") == 200 or result.get("errcode") == 0:
            logger.info(f"[sweep] 推送成功: {title}")
            pushed += 1
        else:
            logger.error(f"[sweep] 推送失败: {title} | {result}")
    # 状态裁剪：只留 48h，防 Gist 体积膨胀
    cutoff = (now - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
    sweep_state[:] = [e for e in sweep_state if str(e.get("t", "")) >= cutoff][-200:]
    return {"detected": len(sweeps), "pushed": pushed, "suppressed_with_news": suppressed}
