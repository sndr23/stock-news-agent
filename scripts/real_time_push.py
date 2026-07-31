# filepath: scripts/real_time_push.py
"""
实时重大资讯推送（事件驱动，只推重要消息）
====================================================
目标: 像财联社公众号一样，有重大消息立刻推送到手机（微信），日常流水不打扰。

数据源（多源聚合，不限于财联社）:
    - get_stock_news: 东财快讯 + 财联社电报 + 新浪财经 + 同花顺快讯
                      + 富途全球快讯 + 华尔街见闻（6 源并行抓取 + 跨源去重）
    - get_market_signals: 龙虎榜机构动向 + 业绩预告（交易所官方信号）
    每 30 分钟（云端 GitHub Actions）/ 120 秒（本地 --loop）抓取一轮，
    增量检测（事件级指纹去重）→ 规则预筛 → LLM 严格判定 →
    仅推送重大消息到微信，非重大消息静默丢弃。

运行方式:
    云端: GitHub Actions schedule 每 30/60 分钟触发一次
          （见 .github/workflows/realtime-push.yml）
    本地: python scripts/real_time_push.py --loop     # 常驻轮询（守护进程模式）
          python scripts/real_time_push.py --dry-run  # 只诊断不推送不保存状态
          python scripts/real_time_push.py            # 单次执行（等价于一次云端触发）

跨轮次去重（防止重复推送）:
    云端: GitHub Gist（环境变量 GIST_TOKEN + GIST_ID，必须配置，否则 CI 下报错退出）
    本地: logs/real_time_state.json（已被 .gitignore 排除，不入库）
    指纹为事件级: 同事件多源报道（标题措辞不同）共享同一指纹，48h 内只推一次。

环境变量:
    OPENROUTER_API_KEY / OPENROUTER_MODEL_NAME / OPENROUTER_BASE_URL  LLM 判定（复用现有配置）
    PUSHPLUS_TOKEN     PushPlus token（主渠道，推荐，无需企业微信；pushplus.plus 扫码获取）
    WECOM_WEBHOOK      企业微信群机器人 webhook（可选替代渠道）
    GIST_TOKEN / GIST_ID 云端状态持久化（本地可省）
    RT_PUSH_MODE         strict|standard|loose，重要度门槛，默认 strict
    RT_POLL_SECONDS      本地 --loop 轮询间隔秒数，默认 120
    RT_ALWAYS_ON         1=全天7x24推送，0=仅A股交易日，默认 1
    RT_MAX_CANDIDATES    单轮进入 LLM 判定的候选条数上限，默认 40
"""
import os
import sys
import re
import json
import time
import hashlib
import logging
import argparse
from pathlib import Path
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

BJT = timezone(timedelta(hours=8))

logger = logging.getLogger("real_time_push")

# ============================================================
# 复用项目现有能力（不重复造轮子）
# ============================================================
from src.tools.data_fetchers import get_stock_news, get_market_signals  # 多源聚合抓取
from src.tools.calculators import (
    calculate_prefilter_importance,   # 预筛评分
    _EVENT_KEYWORD_GROUPS,            # 事件关键词组
    _extract_core_numbers,            # 核心金额提取
)  # 多源事件签名
from src.tools.push import push_via_wecom, push_via_pushplus  # 推送（含重试）
from src.agent.nodes import _call_llm_api, _repair_json      # LLM 调用与 JSON 单对象修复

# ============================================================
# 常量配置
# ============================================================
# 高信号关键词：命中则跳过预筛分数限制，直接进入 LLM 判定
HIGH_SIGNAL_KEYWORDS = [
    "降准", "降息", "加息", "国常会", "证监会", "央行", "国务院",
    "政治局", "中央经济工作会议", "关税", "制裁", "美联储", "欧央行",
    "印花税", "平准基金", "国家队", "汇金", "注册制", "退市新规",
    "重大资产重组", "立案调查", "涨跌停", "IPO",
]

# 规则预筛门槛：重要度评分 >= 该值 或 命中高信号词 → 进入 LLM 判定
# 多源聚合后候选量远大于单源，门槛从 0.50 提升到 0.55 控住 LLM 成本
PREFILTER_SCORE_MIN = 0.55

# 单轮进入 LLM 判定的候选条数上限（防止突发大行情时候选激增拖慢/超时）
MAX_CANDIDATES_PER_ROUND = 40

# LLM 判定失败时的降级策略：预筛分高 + 命中高信号词 → 直接推（宁可多推不可漏推重大消息）
FALLBACK_PUSH_SCORE_MIN = 0.70

# 批量 LLM 判定每批条数（控制单次请求 token 与延迟）
LLM_BATCH_SIZE = 8

# 状态窗口：指纹保留时长（小时），滚动清理
STATE_WINDOW_HOURS = 48

# Gist 内状态文件名
GIST_STATE_FILENAME = "real_time_state.json"

# ============================================================
# 阈值模式
# ============================================================
def _passes_threshold(mode: str, score, direction: str, scope: str) -> bool:
    """重要度门槛判定（三级模式，默认 strict=财联社风格：只推大消息）

    Args:
        mode: strict / standard / loose
        score: LLM 影响分 0-10
        direction: 6档方向（bullish/bearish=强档；mildly_bullish/
                   mildly_bearish=弱档；neutral/mixed=中性）
        scope: market/sector/stock

    Returns:
        是否值得推送

    关键语义:
    - 个股级(scope=stock)消息不是"重大资讯"（业绩预告、个股公告等），
      严格模式一律不推；standard/loose 可按分数放行。
    """
    try:
        score = float(score or 0)
    except (ValueError, TypeError):
        score = 0.0
    direction = str(direction or "neutral").lower()
    scope = str(scope or "stock").lower()

    # 全市场级影响：任何模式都推
    if scope == "market":
        return True

    if mode == "strict":
        # 只推板块级以上：板块级 强档方向 或 影响分≥7；个股级不推
        return scope == "sector" and (score >= 7 or direction in ("bullish", "bearish"))
    if mode == "standard":
        # 板块/个股：影响分≥6 或 强档方向
        return score >= 6 or (direction in ("bullish", "bearish") and scope in ("sector", "stock"))
    # loose
    return score >= 5


# ============================================================
# 新闻指纹（跨轮次去重，多源事件级）
# ============================================================
def _normalize_title(title: str) -> str:
    """标题归一化：去标点/空白/emoji，仅保留中英文与数字（用于标题兜底指纹）"""
    t = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]+", "", title or "")
    return t[:40]


def _event_signature_light(news: dict) -> tuple:
    """轻量事件签名 (stocks, events, numbers)

    不复用 calculators._event_signature 的 stocks 兜底提取：
    它假设"标题冒号前是股票名"（公告格式），对新闻标题如
    "央行宣布：降准0.5个百分点" 会把"央行宣布"误判为股票，
    导致同一新闻因标点差异（：vs ,）指纹分叉。
    本函数 stocks 仅取显式字段（龙虎榜/业绩预告等自带 name/affected_stocks），
    events/numbers 复用项目的事件关键词组与金额提取。
    """
    stocks = set(news.get("affected_stocks", []) or [])
    name = news.get("name", "")
    if name:
        stocks.add(name)
    text = f"{news.get('title', '')} {news.get('content', '')}"
    events = set()
    for group_name, keywords in _EVENT_KEYWORD_GROUPS:
        if any(kw in text for kw in keywords):
            events.add(group_name)
    numbers = _extract_core_numbers(text)
    return stocks, events, numbers


def _news_fingerprint(news: dict) -> str:
    """多源事件级指纹

    多源聚合下同一事件会被多家媒体报道（标题措辞不同、时间不同），
    不能再按 (时间, 标题) 做指纹。改为两级：
    1. 事件签名（个股+事件组+核心金额）非空 或 命中高信号词
       → 事件级指纹，同事件多源报道共享同一指纹，48h 内只处理/推送一次
    2. 事件签名全空（普通流水新闻）→ 归一化标题指纹
    """
    date = str(news.get("published_at", "") or "")[:10]
    if not date:
        date = datetime.now(BJT).strftime("%Y-%m-%d")
    text = f"{news.get('title', '')} {news.get('content', '')}"
    stocks, events, numbers = _event_signature_light(news)
    hit_signal = [kw for kw in HIGH_SIGNAL_KEYWORDS if kw in text]
    if hit_signal:
        # 命中高信号词（宏观/监管级）：以信号词+事件组+个股为指纹，不掺入数字——
        # 数字表达不稳定（"1700亿" vs "一千七百亿"），且同信号词下数字分叉
        # 会把同一事件的多源报道拆成不同指纹
        key = f"{date}|sig:{sorted(hit_signal)}|ev:{sorted(events)}|st:{sorted(stocks)}"
    elif stocks or events or numbers:
        # 公告/公司事件：个股+事件组+核心金额辅助区分（"XX:回购5亿" vs "XX:回购8亿"）
        key = f"{date}|st:{sorted(stocks)}|ev:{sorted(events)}|num:{sorted(numbers)}"
    else:
        # 普通流水新闻：归一化标题指纹
        key = f"{date}|t:{_normalize_title(news.get('title', ''))}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


# ============================================================
# 状态持久化（Gist 云端 / 本地文件）
# ============================================================
def _empty_state() -> dict:
    return {
        "version": 1,
        "seen": {},  # {fingerprint: {"t": "YYYY-MM-DD HH:MM:SS", "pushed": bool, "title": str}}
    }


def _state_path() -> Path:
    return PROJECT_ROOT / "logs" / "real_time_state.json"


def _is_ci() -> bool:
    """GitHub Actions 环境检测（CI=true 由 GitHub 自动注入）"""
    return os.getenv("CI", "").strip().lower() == "true"


def _gist_load(token: str, gist_id: str) -> dict:
    """从 Gist 读取状态文件内容

    注意: GitHub Gist API 对 gist 元数据有 CDN 缓存，写后立即读会命中
    旧快照（实测写入后 1-3 秒甚至更久读不到新内容），本地 --loop 高频
    模式下可能读到写入前版本导致重复推送。
    对策: URL 追加时间戳查询参数强制绕过缓存（实测有效），
    并保留"状态文件未出现则重试"的双保险。
    """
    import requests
    url = f"https://api.github.com/gists/{gist_id}?ts={int(time.time() * 1000)}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "stock-news-agent-realtime",
    }
    fobj = None
    for attempt in range(3):
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        files = data.get("files") or {}
        fobj = files.get(GIST_STATE_FILENAME)
        if fobj is not None:
            break
        if attempt < 2:
            logger.info(f"Gist 状态文件暂未读到（第{attempt + 1}次，可能命中旧快照），1s 后重试")
            time.sleep(1)
    content = fobj.get("content") if fobj else "{}"
    try:
        state = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("Gist 状态文件 JSON 解析失败，重置为空状态")
        state = _empty_state()
    return state


def _gist_save(token: str, gist_id: str, state: dict) -> None:
    """将状态写回 Gist（整文件原子替换，配合 workflow concurrency 防并发）"""
    import requests
    # 与读取一致追加时间戳参数，避免命中 CDN 缓存返回旧元数据
    url = f"https://api.github.com/gists/{gist_id}?ts={int(time.time() * 1000)}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "stock-news-agent-realtime",
    }
    payload = {
        "files": {
            GIST_STATE_FILENAME: {
                "content": json.dumps(state, ensure_ascii=False, indent=2),
            }
        }
    }
    resp = requests.patch(url, json=payload, headers=headers, timeout=20)
    resp.raise_for_status()


def load_state() -> dict:
    """加载状态：云端优先 Gist，本地用文件"""
    gist_token = os.getenv("GIST_TOKEN", "").strip()
    gist_id = os.getenv("GIST_ID", "").strip()

    if gist_token and gist_id:
        try:
            state = _gist_load(gist_token, gist_id)
            logger.info(f"状态已从 Gist 加载: {len(state.get('seen', {}))} 个指纹")
            return state
        except Exception as e:
            logger.warning(f"Gist 读取失败: {e}")
            if _is_ci():
                # CI 下无法读取状态 → 无法去重 → 宁可报错也不冒险重复推送
                raise RuntimeError(f"CI 环境 Gist 读取失败，禁止无状态运行: {e}")

    # 本地文件
    state_path = _state_path()
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or "seen" not in state:
                state = _empty_state()
            logger.info(f"状态已从本地加载: {len(state.get('seen', {}))} 个指纹")
            return state
        except Exception as e:
            logger.warning(f"本地状态解析失败，重置: {e}")
    return _empty_state()


def save_state(state: dict) -> None:
    """保存状态：云端写 Gist，本地写文件"""
    # 滚动清理过期指纹（48h 窗口）
    cutoff = (datetime.now(BJT) - timedelta(hours=STATE_WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    seen = state.get("seen", {})
    expired = [fp for fp, rec in seen.items() if rec.get("t", "") < cutoff]
    for fp in expired:
        seen.pop(fp, None)
    if expired:
        logger.info(f"清理过期指纹 {len(expired)} 条，剩余 {len(seen)} 条")
    state["seen"] = seen

    gist_token = os.getenv("GIST_TOKEN", "").strip()
    gist_id = os.getenv("GIST_ID", "").strip()
    if gist_token and gist_id:
        _gist_save(gist_token, gist_id, state)
        logger.info(f"状态已保存到 Gist（{len(seen)} 个指纹）")
        return

    if _is_ci():
        # CI 下没有 Gist 配置 → 状态无处可存 → 下轮会重复推送，必须报错
        raise RuntimeError("CI 环境缺少 GIST_TOKEN/GIST_ID，状态无法持久化，禁止无状态运行")

    state_path = _state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"状态已保存到本地文件: {state_path.name}（{len(seen)} 个指纹）")


# ============================================================
# 规则预筛
# ============================================================
def _prefilter(news: dict) -> tuple:
    """规则预筛：返回 (预筛评分, 是否命中高信号词)"""
    score = calculate_prefilter_importance(news)
    text = f"{news.get('title', '')} {news.get('content', '')}"
    hit = any(kw in text for kw in HIGH_SIGNAL_KEYWORDS)
    return float(score), hit


# ============================================================
# LLM 快速重要性判定
# ============================================================
_LLM_SYSTEM_PROMPT = """你是A股资讯重要性审核员。判断每条资讯是否属于"必须立即推送的重大消息"。
只推重大消息，日常流水、常规公司经营新闻一律不推。以下任一条件成立才算重大：
1. 影响整个市场/大盘（宏观政策、央行、证监会、国常会、政治局会议、重大地缘政治事件）
2. 市场影响评分 >= 7（10分制：强烈影响某板块或多家公司）
3. 方向为强利好或强利空（如降准、加息、重大重组、立案调查，而非小幅波动）

对每条输入严格输出一个 JSON 数组元素，字段：
{"title": "原标题", "push": true/false, "score": 0到10的整数, "direction": "bullish|mildly_bullish|neutral|mixed|mildly_bearish|bearish",
 "scope": "market|sector|stock", "sectors": ["板块名"], "reason": "一句话理由"}
direction 必须区分强度：只有影响显著且方向明确才用 bullish/bearish（强档）；
小幅波动用 mildly_bullish/mildly_bearish；方向不明用 neutral/mixed。
不要输出任何 JSON 以外的文字。"""


def _build_llm_user_prompt(items: list) -> str:
    lines = []
    for n in items:
        lines.append(json.dumps({
            "title": str(n.get("title", ""))[:80],
            "content": str(n.get("content", "") or "")[:200],
            "published_at": str(n.get("published_at", "")),
        }, ensure_ascii=False))
    return "请逐条审核以下资讯（不要遗漏任何一条）:\n[\n" + ",\n".join(lines) + "\n]"


def _parse_llm_array(content: str) -> list:
    """解析 LLM 返回的 JSON 数组（容错：代码块包裹/损坏对象/截断）

    注意: 不复用 nodes._safe_parse_json —— 它的容错逻辑只为 filtered_news/
    ranking 等 dict 结构设计，裸数组场景会全部 fallback 失败。
    本函数: 提取代码块 → 逐字符扫描平衡括号 → 逐对象解析（含 _repair_json 兜底）。
    """
    if not content or not content.strip():
        return []
    cleaned = content
    # 提取 ```json ... ``` 代码块
    cb_match = re.search(r'```(?:json)?\s*\n?(.*?)```', cleaned, re.DOTALL)
    if cb_match:
        cleaned = cb_match.group(1)
    # 定位数组起点（防御 LLM 在数组前输出解释文字）
    start = cleaned.find("[")
    if start < 0:
        return []

    items = []
    i = start
    obj_depth = 0
    obj_start = -1
    in_str = False
    escape = False
    while i < len(cleaned):
        ch = cleaned[i]
        if escape:
            escape = False
            i += 1
            continue
        if ch == "\\":
            escape = True
            i += 1
            continue
        if ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch == "{":
                if obj_depth == 0:
                    obj_start = i
                obj_depth += 1
            elif ch == "}":
                obj_depth -= 1
                if obj_depth == 0 and obj_start >= 0:
                    obj_text = cleaned[obj_start:i + 1]
                    try:
                        items.append(json.loads(obj_text))
                    except json.JSONDecodeError:
                        try:
                            items.append(json.loads(_repair_json(obj_text)))
                        except json.JSONDecodeError:
                            logger.warning(f"LLM 返回单对象解析失败，跳过: {obj_text[:60]}")
                    obj_start = -1
        i += 1
    return items


def _llm_judge(items: list) -> list:
    """批量 LLM 判定，返回与 items 一一对应的判定 dict 列表

    返回的每项: {"title", "push", "score", "direction", "scope", "sectors", "reason"}
    LLM 输出非法时该项判定为不推（保守）。
    """
    if not items:
        return []
    results = []
    for start in range(0, len(items), LLM_BATCH_SIZE):
        batch = items[start:start + LLM_BATCH_SIZE]
        try:
            raw = _call_llm_api(_LLM_SYSTEM_PROMPT, _build_llm_user_prompt(batch), timeout=90, max_retries=1)
            entries = _parse_llm_array(raw)
            # 按标题对齐到输入（LLM 可能增减条目/乱序）
            by_title = {}
            for e in entries:
                if isinstance(e, dict):
                    t = str(e.get("title", "") or "").strip()
                    if t:
                        by_title[t] = e
            for n in batch:
                t = str(n.get("title", "") or "").strip()
                e = by_title.get(t) or {}
                results.append({
                    "title": t,
                    "push": bool(e.get("push", False)),
                    "score": e.get("score", 0),
                    "direction": str(e.get("direction", "neutral") or "neutral").lower(),
                    "scope": str(e.get("scope", "stock") or "stock").lower(),
                    "sectors": e.get("sectors") or [],
                    "reason": str(e.get("reason", "") or "").strip(),
                })
            logger.info(f"LLM 判定批次 {start//LLM_BATCH_SIZE + 1}: {len(batch)} 条完成")
        except Exception as e:
            logger.warning(f"LLM 判定批次失败（{len(batch)} 条），该批次按不推处理: {e}")
            for n in batch:
                results.append({
                    "title": str(n.get("title", "")),
                    "push": False,
                    "score": 0,
                    "direction": "neutral",
                    "scope": "stock",
                    "sectors": [],
                    "reason": f"LLM判定失败: {str(e)[:50]}",
                })
    return results


def _fallback_decision(news: dict, pref_score: float, hit_signal: bool) -> dict:
    """LLM 整体失败时的降级判定：预筛分高 + 命中高信号词 → 直接推

    原则: 重大消息宁可多推也不漏推；日常流水宁可漏推也不滥推。
    """
    title = str(news.get("title", ""))
    if hit_signal and pref_score >= FALLBACK_PUSH_SCORE_MIN:
        text = f"{title} {news.get('content', '')}"
        direction = "bullish" if any(k in text for k in ["利好", "上涨", "支持", "放宽", "增持", "降准", "降息", "减税"]) else "bearish"
        return {
            "title": title,
            "push": True,
            "score": round(pref_score * 10, 1),
            "direction": direction,
            "scope": "market",
            "sectors": [],
            "reason": "高信号词+高预筛分（LLM降级判定）",
        }
    return {
        "title": title,
        "push": False,
        "score": round(pref_score * 10, 1),
        "direction": "neutral",
        "scope": "stock",
        "sectors": [],
        "reason": "LLM降级：未达直接推送标准",
    }


# ============================================================
# 推送格式 + 统一推送入口
# ============================================================
_DIR_EMOJI = {
    "bullish": "🔴", "mildly_bullish": "🟠",
    "bearish": "🟢", "mildly_bearish": "🟡",
    "neutral": "⚪", "mixed": "🔷",
}
_DIR_LABEL = {
    "bullish": "强利好", "mildly_bullish": "弱利好",
    "bearish": "强利空", "mildly_bearish": "弱利空",
    "neutral": "中性", "mixed": "多空交织",
}
_SCOPE_LABEL = {"market": "全市场", "sector": "板块", "stock": "个股"}


def format_push_alert(news: dict, judge: dict) -> str:
    """格式化单条快讯为 markdown 文本（红涨绿跌）

    注: 企业微信 markdown 不支持 <font color> 内联 HTML，
    统一用 emoji + 文本标签表达方向（A股惯例红涨绿跌），
    PushPlus / 企业微信均能正确渲染。
    """
    title = str(news.get("title", "") or "")[:80]
    direction = judge.get("direction", "neutral")
    emoji = _DIR_EMOJI.get(direction, "⚪")
    label = _DIR_LABEL.get(direction, "中性")
    score = judge.get("score", 0)
    scope = judge.get("scope", "stock")
    scope_label = _SCOPE_LABEL.get(scope, "个股")
    sectors = judge.get("sectors") or []
    sector_str = "、".join(str(s) for s in sectors[:4]) if sectors else "—"
    reason = str(judge.get("reason", "") or "").strip()[:100]
    source = str(news.get("source", "") or "多源资讯")
    pub = str(news.get("published_at", "") or "")

    lines = [f"{emoji}【{label}】{title}", ""]
    meta = [f"**范围**: {scope_label}", f"**影响分**: {score}", f"**板块**: {sector_str}"]
    lines.append(" | ".join(meta))
    lines.append(f"**来源**: {source} {pub}")
    if reason:
        lines.append("")
        lines.append(f"> {reason}")
    return "\n".join(lines)


def _send_alert_item(push_config: dict, title: str, content: str) -> dict:
    """统一推送入口：按配置选择后端发送单条快讯

    后端优先级: PushPlus > 企业微信群机器人
    成功判定: PushPlus code==200; 企业微信 errcode==0
    """
    if push_config.get("pushplus_token"):
        return push_via_pushplus(push_config["pushplus_token"], title, content)
    if push_config.get("wecom_webhook"):
        return push_via_wecom(push_config["wecom_webhook"], title, content)
    return {"code": 400, "msg": "未配置任何推送后端"}


# ============================================================
# 主流程：单次执行
# ============================================================
def run_once(dry_run: bool = False) -> dict:
    """执行一轮实时推送

    Args:
        dry_run: True 时不推送、不保存状态（用于本地诊断）

    Returns:
        本轮统计 dict
    """
    pushplus_token = os.getenv("PUSHPLUS_TOKEN", "").strip()
    wecom_webhook = os.getenv("WECOM_WEBHOOK", "").strip()
    if not pushplus_token and not wecom_webhook and not dry_run:
        raise RuntimeError("未配置推送后端: 需要 PUSHPLUS_TOKEN（推荐，pushplus.plus 扫码获取）或 WECOM_WEBHOOK，请先配置")
    push_config = {
        "pushplus_token": pushplus_token or None,
        "wecom_webhook": wecom_webhook or None,
    }

    mode = os.getenv("RT_PUSH_MODE", "strict").strip().lower()
    if mode not in ("strict", "standard", "loose"):
        logger.warning(f"RT_PUSH_MODE 无效: {mode}，使用 strict")
        mode = "strict"

    state = load_state()
    seen = state.setdefault("seen", {})

    # 1. 多源聚合抓取：6 大新闻源 + 龙虎榜/业绩预告信号
    # 注意: get_stock_news/get_market_signals 是 LangChain @tool 包装的
    # StructuredTool 实例，需用 .func 取原始函数调用
    try:
        news_list = get_stock_news.func()
        signals = get_market_signals.func()
        news_list = list(news_list) + list(signals)
    except Exception as e:
        logger.error(f"多源抓取失败: {e}", exc_info=True)
        news_list = []
    logger.info(f"多源聚合: 拉取 {len(news_list)} 条")
    if not news_list:
        logger.info("无资讯返回，跳过本轮")
        return {"fetched": 0, "new": 0, "prefiltered": 0, "pushed": 0, "skipped": 0}

    # 2. 增量检测：事件级指纹去重，只处理未见过指纹的条目
    new_items = []
    for n in news_list:
        fp = _news_fingerprint(n)
        if fp in seen:
            continue
        n["_fp"] = fp
        new_items.append(n)
    logger.info(f"增量检测: 新增 {len(new_items)} 条（已见 {len(news_list) - len(new_items)} 条）")
    if not new_items:
        logger.info("无新增资讯，本轮结束")
        save_state(state)  # 触发窗口清理
        return {"fetched": len(news_list), "new": 0, "prefiltered": 0, "pushed": 0, "skipped": 0}

    # 3. 规则预筛（重要度评分 或 高信号词命中）
    candidates = []
    for n in new_items:
        pref_score, hit = _prefilter(n)
        n["_pref_score"] = pref_score
        n["_hit_signal"] = hit
        if pref_score >= PREFILTER_SCORE_MIN or hit:
            candidates.append(n)
    # 候选上限保护：突发大行情候选激增时，按预筛分排序取前 N 条，其余按未推记录
    max_candidates = int(os.getenv("RT_MAX_CANDIDATES", str(MAX_CANDIDATES_PER_ROUND)))
    if len(candidates) > max_candidates:
        candidates.sort(key=lambda x: x["_pref_score"], reverse=True)
        overflow = candidates[max_candidates:]
        candidates = candidates[:max_candidates]
        logger.info(f"候选超过上限({max_candidates})，丢弃低分候选 {len(overflow)} 条")
    logger.info(f"规则预筛: 通过 {len(candidates)}/{len(new_items)} 条")
    if not candidates:
        # 全部不达标：记录指纹，不推送
        now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
        for n in new_items:
            seen[n["_fp"]] = {"t": now, "pushed": False, "title": str(n.get("title", ""))[:60]}
        if not dry_run:
            save_state(state)
        return {"fetched": len(news_list), "new": len(new_items), "prefiltered": 0, "pushed": 0, "skipped": len(new_items)}

    # 4. LLM 严格判定
    judges = _llm_judge(candidates)
    if not judges:
        # LLM 完全失败 → 降级规则判定
        judges = [_fallback_decision(n, n["_pref_score"], n["_hit_signal"]) for n in candidates]

    # 5. 阈值过滤 + 推送
    pushed = 0
    skipped = 0
    now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
    for n, j in zip(candidates, judges):
        if not isinstance(j, dict):
            j = _fallback_decision(n, n.get("_pref_score", 0), n.get("_hit_signal", False))
        # LLM 明确判定不重大（push=false）→ 一票否决，不推送
        # 阈值只对 LLM 认为值得推的条目进一步收紧
        if j.get("push") and _passes_threshold(mode, j.get("score", 0), j.get("direction", "neutral"), j.get("scope", "stock")):
            content = format_push_alert(n, j)
            if dry_run:
                logger.info(f"[dry-run] 将推送: {n.get('title', '')[:50]}")
                print("\n===== 将推送内容预览 =====\n" + content + "\n==========================")
                seen[n["_fp"]] = {"t": now, "pushed": True, "title": str(n.get("title", ""))[:60]}
                pushed += 1
            else:
                result = _send_alert_item(push_config, "重要资讯", content)
                if result.get("code") == 200 or result.get("errcode") == 0:
                    logger.info(f"推送成功: {n.get('title', '')[:50]}")
                    seen[n["_fp"]] = {"t": now, "pushed": True, "title": str(n.get("title", ""))[:60]}
                    pushed += 1
                else:
                    # 推送失败：不记录指纹，下轮重试（避免重大消息丢失）
                    logger.error(f"推送失败（下轮重试）: {n.get('title', '')[:50]} | {result}")
                    skipped += 1
        else:
            seen[n["_fp"]] = {"t": now, "pushed": False, "title": str(n.get("title", ""))[:60]}
            skipped += 1

    # 其余未进入候选的条目也记录指纹
    cand_fps = {n["_fp"] for n in candidates}
    for n in new_items:
        if n["_fp"] not in cand_fps and n["_fp"] not in seen:
            seen[n["_fp"]] = {"t": now, "pushed": False, "title": str(n.get("title", ""))[:60]}

    if not dry_run:
        save_state(state)

    stats = {
        "fetched": len(news_list),
        "new": len(new_items),
        "prefiltered": len(candidates),
        "pushed": pushed,
        "skipped": skipped,
    }
    logger.info(f"本轮完成: 拉取{stats['fetched']} 新增{stats['new']} 预筛{stats['prefiltered']} 推送{stats['pushed']} 未推{stats['skipped']}")
    return stats


def _is_trading_day() -> bool:
    """A股交易日判断（RT_ALWAYS_ON=0 时启用）"""
    try:
        import chinese_calendar  # type: ignore
        from datetime import date
        return chinese_calendar.is_workday(date.today())
    except ImportError:
        return datetime.now(BJT).weekday() < 5


def main():
    parser = argparse.ArgumentParser(description="实时重要资讯推送")
    parser.add_argument("--loop", action="store_true", help="常驻循环模式（本地守护进程）")
    parser.add_argument("--dry-run", action="store_true", help="只诊断不推送不保存状态")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    always_on = os.getenv("RT_ALWAYS_ON", "1").strip() == "1"
    if not always_on and not _is_trading_day():
        logger.info("今日非A股交易日（RT_ALWAYS_ON=0），跳过")
        return

    if args.loop:
        poll_seconds = int(os.getenv("RT_POLL_SECONDS", "120"))
        logger.info(f"实时推送守护进程启动，轮询间隔 {poll_seconds}s（Ctrl+C 退出）")
        while True:
            try:
                run_once(dry_run=args.dry_run)
            except Exception as e:
                logger.error(f"本轮执行异常: {e}", exc_info=True)
            time.sleep(poll_seconds)
    else:
        run_once(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
