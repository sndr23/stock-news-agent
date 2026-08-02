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
    推送级事件签名去重: 指纹分裂的同事件报道（不同金额表述/信号词子集）
    在推送前按(LLM主体+事件组+金额+标题相似)合并，同轮只推最优一条，
    且 48h 内已推过的同事件不再推（恩智浦收购Ambarella三源三推实证修复）。
    Gist 写回为读-改-写合并：本地--loop与云端并发时避免互相覆盖 pushed 标记。

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
from src.tools.data_fetchers import get_stock_news, get_market_signals, dedup_news_3layer  # 多源聚合抓取 + 三层近似去重
from src.tools.calculators import (
    calculate_prefilter_importance,   # 预筛评分
    _EVENT_KEYWORD_GROUPS,            # 事件关键词组
    _extract_core_numbers,            # 核心金额提取
)  # 多源事件签名
from src.tools.push import push_via_wecom, push_via_pushplus  # 推送（含重试）
from src.agent.nodes import _call_llm_api, _repair_json      # LLM 调用与 JSON 单对象修复
from src.tools.keyword_tables import (                      # 共享关键词表（单一事实来源）
    HIGH_SIGNAL_KEYWORDS,
    OVERSEAS_TECH_KEYWORDS,
    OVERSEAS_SOURCE_MARKERS,
)

# ============================================================
# 常量配置
# ============================================================
# 高信号关键词（命中则跳过预筛分数限制，直接进入 LLM 判定）：
# 从 src.tools.keyword_tables 导入，与批处理管线 nodes.py 共用同一份，避免漂移。

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
# 外围科技关键词(OVERSEAS_TECH_KEYWORDS)与外围源标记(OVERSEAS_SOURCE_MARKERS)
# 均从 src.tools.keyword_tables 导入（2026-08-01 用户调优版词表已迁入共享模块）。


def _is_overseas_tech(news: dict, sectors: list) -> bool:
    """外围资讯且涉及科技板块 → 增强推送

    美股半导体/科技股/纳指等外围消息，即使 LLM 判定为弱档或个股级，
    只要直接影响 A 股科技板块，也视为值得推送。
    """
    source = str(news.get("source", "") or "")
    if not any(m in source for m in OVERSEAS_SOURCE_MARKERS):
        return False
    text = f"{news.get('title', '')} {news.get('content', '')} {' '.join(str(s) for s in (sectors or []))}"
    return any(kw in text for kw in OVERSEAS_TECH_KEYWORDS)


def _is_domestic_tech(news: dict, sectors: list) -> bool:
    """国内资讯且涉及科技板块 → 增强推送（防 LLM 漏推科技板块资讯）

    用户口径：影响科技板块的资讯不能漏推，但只影响中小市值个股自身的
    业绩/回购/中标等消息即使再轰动也不推。本函数只管"板块级科技影响"——
    调用方需结合 scope==sector（板块级）或 is_leader_stock（科技龙头个股）使用。
    """
    source = str(news.get("source", "") or "")
    if any(m in source for m in OVERSEAS_SOURCE_MARKERS):
        return False
    text = f"{news.get('title', '')} {news.get('content', '')} {' '.join(str(s) for s in (sectors or []))}"
    return any(kw in text for kw in OVERSEAS_TECH_KEYWORDS)


def _passes_threshold(mode: str, score, direction: str, scope: str,
                      leader_stock: bool = False) -> bool:
    """重要度门槛判定（三级模式，默认 strict）

    Args:
        mode: strict / standard / loose
        score: LLM 影响分 0-10
        direction: 6档方向（bullish/bearish=强档；mildly_bullish/
                   mildly_bearish=弱档；neutral/mixed=中性）
        scope: market/sector/stock
        leader_stock: 资讯主体是否为行业龙头个股（LLM 判定 或 命中自选龙头名单）

    Returns:
        是否值得推送

    关键语义（strict 模式）:
    - 全市场级影响: 必推
    - 板块级: 强档方向 或 影响分≥6 → 推（2026-08-01 由≥7放宽：用户反馈大量有用板块资讯被卡）
    - 个股级: 仅行业龙头（龙头股重大消息）且 强档 或 分≥6 → 推；非龙头不推
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

    strong = direction in ("bullish", "bearish")

    if mode == "strict":
        if scope == "sector":
            return score >= 6 or strong
        if scope == "stock":
            # 龙头个股的重要消息可推；非龙头个股不推
            return leader_stock and (score >= 6 or strong)
        return False
    if mode == "standard":
        # 板块/个股：影响分≥6 或 强档方向
        return score >= 6 or (strong and scope in ("sector", "stock"))
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
# 推送级事件去重（跨源同事件只推一次）
# ============================================================
def _to_float(v, default: float = 0.0) -> float:
    """宽容转 float（LLM 返回的 score 可能是字符串/None）"""
    try:
        return float(v or 0)
    except (ValueError, TypeError):
        return default


def _push_event_sig(news: dict, judge: dict) -> dict:
    """生成推送级事件签名：规则抽取(个股/事件组/金额) + LLM主体(entities) + 归一化标题

    指纹 _news_fingerprint 解决"完全同一条"的跨轮去重；本签名解决"同一事件的
    不同报道"（标题措辞/金额表述/信号词子集不同导致指纹分裂，
    恩智浦收购Ambarella三源三推实证）。
    """
    stocks, events, numbers = _event_signature_light(news)
    entities = {str(e).strip() for e in (judge.get("entities") or []) if str(e).strip()}
    return {
        "stocks": sorted(stocks),
        "entities": sorted(entities),
        "events": sorted(events),
        "numbers": sorted(numbers),
        "title_norm": _normalize_title(news.get("title", "")),
    }


def _merge_event_sig(sig_a: dict, sig_b: dict) -> dict:
    """并集合并两个事件签名（分组内传递合并用），标题保留较长者"""
    return {
        "stocks": sorted(set(sig_a.get("stocks") or []) | set(sig_b.get("stocks") or [])),
        "entities": sorted(set(sig_a.get("entities") or []) | set(sig_b.get("entities") or [])),
        "events": sorted(set(sig_a.get("events") or []) | set(sig_b.get("events") or [])),
        "numbers": sorted(set(sig_a.get("numbers") or []) | set(sig_b.get("numbers") or [])),
        "title_norm": sig_a.get("title_norm", "") if len(sig_a.get("title_norm", "")) >= len(sig_b.get("title_norm", "")) else sig_b.get("title_norm", ""),
    }


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（归一化标题≤40字，DP O(n·m) 开销可忽略）"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _is_same_event(sig_a: dict, sig_b: dict) -> bool:
    """判断两个推送级事件签名是否指向同一事件（满足其一即同事件）

    1. 主体(个股/LLM实体)交集非空 且 事件组交集非空 → 同主体同事件
       （"寒武纪股权激励大消息" vs "寒武纪:2026年限制性股票激励计划(草案)"）
    2. 核心金额交集非空 且 事件组交集非空 → 同事件不同措辞同金额
       （"30.53亿补充协议" vs "30.53亿元协议"）
    3. 主体交集为空 但事件组交集非空 且 归一化标题最长公共子串≥5 →
       多源同事件报道（"恩智浦洽谈收购Ambarella" vs "安霸股价因传恩智浦洽谈收购而飙升"）
    4. 双方均无事件组（普通流水）且标题字符集 Jaccard≥0.6 → 同一条目的改写
    """
    ent_a = set(sig_a.get("stocks") or []) | set(sig_a.get("entities") or [])
    ent_b = set(sig_b.get("stocks") or []) | set(sig_b.get("entities") or [])
    ev_a = set(sig_a.get("events") or [])
    ev_b = set(sig_b.get("events") or [])
    num_a = set(sig_a.get("numbers") or [])
    num_b = set(sig_b.get("numbers") or [])
    shared_ev = ev_a & ev_b

    if shared_ev and (ent_a & ent_b):
        return True
    if shared_ev and (num_a & num_b):
        return True
    if shared_ev and not (ent_a & ent_b):
        if _lcs_len(sig_a.get("title_norm", ""), sig_b.get("title_norm", "")) >= 5:
            return True
    if not ev_a and not ev_b:
        ta = set(sig_a.get("title_norm", ""))
        tb = set(sig_b.get("title_norm", ""))
        if ta and tb and len(ta & tb) / len(ta | tb) >= 0.6:
            return True
    return False


# ============================================================
# 状态持久化（Gist 云端 / 本地文件）
# ============================================================
def _empty_state() -> dict:
    return {
        "version": 2,
        "seen": {},  # {fingerprint: {"t": "YYYY-MM-DD HH:MM:SS", "pushed": bool, "title": str}}
        # 已推送事件签名（推送级同事件去重，48h 窗口）：
        # [{"t": str, "stocks": [], "entities": [], "events": [], "numbers": [], "title_norm": str}]
        "pushed_events": [],
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
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            files = data.get("files") or {}
            fobj = files.get(GIST_STATE_FILENAME)
            if fobj is not None:
                break
        except Exception as e:
            logger.warning(f"Gist 读取第{attempt + 1}次失败: {e}")
            fobj = None
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
    # 瞬时网络错误重试（失败会导致已推送标记未落盘 → 下轮重复推送）
    last_error = None
    for attempt in range(3):
        try:
            resp = requests.patch(url, json=payload, headers=headers, timeout=20)
            resp.raise_for_status()
            return
        except Exception as e:
            last_error = e
            if attempt < 2:
                logger.warning(f"Gist 写入第{attempt + 1}次失败: {e}, 1s 后重试")
                time.sleep(1)
    raise RuntimeError(f"Gist 状态写入失败（已重试2次）: {last_error}")


def load_state() -> dict:
    """加载状态：云端优先 Gist，本地用文件"""
    gist_token = os.getenv("GIST_TOKEN", "").strip()
    gist_id = os.getenv("GIST_ID", "").strip()

    if gist_token and gist_id:
        try:
            state = _gist_load(gist_token, gist_id)
            state.setdefault("pushed_events", [])
            logger.info(f"状态已从 Gist 加载: {len(state.get('seen', {}))} 个指纹, "
                        f"{len(state.get('pushed_events', []))} 个已推事件")
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
            state.setdefault("pushed_events", [])
            logger.info(f"状态已从本地加载: {len(state.get('seen', {}))} 个指纹")
            return state
        except Exception as e:
            logger.warning(f"本地状态解析失败，重置: {e}")
    return _empty_state()


def _event_sig_key(e: dict) -> str:
    """事件签名内容键（状态合并去重用）"""
    return ("|".join(sorted(e.get("entities") or [])) + "#"
            + "|".join(sorted(e.get("events") or [])) + "#"
            + "|".join(sorted(e.get("numbers") or [])) + "#"
            + (e.get("title_norm") or ""))


def _merge_state(local: dict, remote: dict) -> dict:
    """合并两份状态（Gist 读-改-写防并发覆盖）：取并集，pushed=True 优先

    本地 --loop 与云端 GitHub Actions 共享同一 Gist 时，两方各自读-改-写，
    直接覆盖写入会丢掉另一方新增的 pushed 标记 → 同一条被重复推送（2026-08-01 实证：
    云端已推送记录被本地轮询覆盖后，Gist 中 pushed=true 记录消失）。
    合并后冲突窗口从"整轮执行"缩小到"单次写入"。
    """
    merged_seen = dict(remote.get("seen", {}) or {})
    for fp, rec in (local.get("seen", {}) or {}).items():
        old = merged_seen.get(fp)
        if old is None or (rec.get("pushed") and not old.get("pushed")):
            merged_seen[fp] = rec
    local["seen"] = merged_seen

    merged_events = {_event_sig_key(e): e for e in (remote.get("pushed_events") or [])}
    for e in (local.get("pushed_events") or []):
        merged_events.setdefault(_event_sig_key(e), e)
    local["pushed_events"] = list(merged_events.values())
    return local


def save_state(state: dict) -> None:
    """保存状态：云端写 Gist（读-改-写合并防并发覆盖），本地写文件"""
    gist_token = os.getenv("GIST_TOKEN", "").strip()
    gist_id = os.getenv("GIST_ID", "").strip()

    if gist_token and gist_id:
        # 写入前先合并最新远端状态，避免并发实例互相覆盖
        try:
            latest = _gist_load(gist_token, gist_id)
            state = _merge_state(state, latest)
        except Exception as e:
            logger.warning(f"Gist 保存前合并失败（将直接覆盖写入）: {e}")
    elif _is_ci():
        # CI 下没有 Gist 配置 → 状态无处可存 → 下轮会重复推送，必须报错
        raise RuntimeError("CI 环境缺少 GIST_TOKEN/GIST_ID，状态无法持久化，禁止无状态运行")

    # 滚动清理过期指纹（48h 窗口）
    cutoff = (datetime.now(BJT) - timedelta(hours=STATE_WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    seen = state.get("seen", {})
    expired = [fp for fp, rec in seen.items() if rec.get("t", "") < cutoff]
    for fp in expired:
        seen.pop(fp, None)
    if expired:
        logger.info(f"清理过期指纹 {len(expired)} 条，剩余 {len(seen)} 条")
    state["seen"] = seen

    # 滚动清理过期已推事件签名（48h 窗口）+ 上限 300 条防爆胀
    pe = [e for e in (state.get("pushed_events") or []) if e.get("t", "") >= cutoff]
    if len(pe) > 300:
        pe = sorted(pe, key=lambda e: e.get("t", ""))[-300:]
    state["pushed_events"] = pe

    if gist_token and gist_id:
        _gist_save(gist_token, gist_id, state)
        logger.info(f"状态已保存到 Gist（{len(seen)} 个指纹, {len(pe)} 个已推事件）")
        return

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
    title = str(news.get("title", "") or "")
    content = str(news.get("content", "") or "")
    name = str(news.get("name", "") or "")
    text = f"{title} {content}"
    # 剥离公司名：避免 "*ST XX" 等公司名命中 "ST" 高信号词直通 LLM 判定
    # （与 nodes._has_high_signal 的处理保持一致）
    if name:
        text = text.replace(name, "")
    hit = any(kw in text for kw in HIGH_SIGNAL_KEYWORDS)
    return float(score), hit


# ============================================================
# LLM 快速重要性判定
# ============================================================
_LLM_SYSTEM_PROMPT = """你是A股资讯重要性审核员。判断每条资讯是否属于"必须立即推送的重大消息"。
推送优先级由高到低，以下任一条件成立则应判为推送：
1. 影响整个市场/大盘（宏观政策、央行、证监会、国常会、政治局会议、重大地缘政治事件）
2. 影响科技板块/科技产业链的资讯（AI、算力、半导体、芯片、存储、光模块/CPO、PCB、MLCC、
   机器人、消费电子等）：板块景气变化、龙头动向、技术突破、产业政策——即使标题未点名个股
3. 科技龙头个股的重大消息（寒武纪、中际旭创、宁德时代、英伟达产业链相关等第一梯队
   公司的重大经营事件、大额订单、业绩剧变、监管动向）
4. 外围（美股/港股/国际宏观/地缘）消息，若其直接影响A股大盘或科技板块
明确不推（无论业绩多好、涨跌多剧烈）：
- 纯个人观点/猜测类言论：政客或机构单方面"怀疑""认为""预计"等表态，没有真实事件或官方立场
  变化、没有实际市场反应佐证，则不视为重大事件——即便话题涉及地缘、石油或美股
- 只影响中小市值个股自身股价的消息：业绩预告/业绩变动、小额回购、增持/减持、中标/签约、
  日常经营、子公司事项、分红送转等——除非该股是行业龙头或直接改变板块逻辑
- 与上述四条优先级均无关的其他资讯

对每条输入严格输出一个 JSON 数组元素，字段：
{"idx": 输入的idx原样回显, "title": "原标题", "push": true/false, "score": 0到10的整数, "direction": "bullish|mildly_bullish|neutral|mixed|mildly_bearish|bearish",
 "scope": "market|sector|stock", "sectors": ["板块名"], "entities": ["事件主体公司/机构规范简称，1-3个，无则空数组"], "is_leader_stock": true/false,
 "reason": "一句话理由"}
direction 必须区分强度：只有影响显著且方向明确才用 bullish/bearish（强档）；
小幅波动用 mildly_bullish/mildly_bearish；方向不明用 neutral/mixed。
is_leader_stock: 仅当该资讯主体是行业龙头个股（市值/地位第一梯队）时为 true，否则 false。
entities: 事件的当事公司/机构/人物规范简称（如"恩智浦""安霸""美联储"），用于跨源同事件去重，必须与标题所述主体一致。
注意: 输出必须是合法 JSON，字符串内的双引号必须转义为 \"（或改用「」）；idx 必须原样回显。
不要输出任何 JSON 以外的文字。"""


def _build_llm_user_prompt(items: list) -> str:
    lines = []
    for n in items:
        lines.append(json.dumps({
            "idx": n.get("_judge_idx"),
            "title": str(n.get("title", ""))[:80],
            "content": str(n.get("content", "") or "")[:200],
            "published_at": str(n.get("published_at", "")),
        }, ensure_ascii=False))
    return "请逐条审核以下资讯（不要遗漏任何一条，idx 原样回显）:\n[\n" + ",\n".join(lines) + "\n]"


def _rescue_judge_object(obj_text: str) -> dict | None:
    """从损坏的 JSON 对象文本中抢救判定字段（应对中文引号未转义等）

    例: {"title": "美联储巴尔金：利率是否已足够高是个"悬而未决"的问题", "push": false, ...}
    json.loads 与 _repair_json 均失败时，用正则直接抽取关键字段，
    避免整条判定被丢弃（漏推根因之一，2026-08-01 日志实证）。
    """
    m_push = re.search(r'"push"\s*:\s*(true|false)', obj_text, re.I)
    m_score = re.search(r'"score"\s*:\s*(\d+(?:\.\d+)?)', obj_text)
    if not m_push or not m_score:
        return None

    def _str_field(name: str, default: str = "") -> str:
        m = re.search(rf'"{name}"\s*:\s*"([^"]*)"', obj_text)
        return m.group(1) if m else default

    out = {
        "push": m_push.group(1).lower() == "true",
        "score": float(m_score.group(1)),
        "direction": _str_field("direction", "neutral"),
        "scope": _str_field("scope", "stock"),
        "reason": _str_field("reason", ""),
    }
    m_idx = re.search(r'"idx"\s*:\s*(\d+)', obj_text)
    if m_idx:
        out["idx"] = int(m_idx.group(1))
    # title 允许内含未转义引号：非贪婪匹配到第一个", "为止
    m_title = re.search(r'"title"\s*:\s*"(.+?)"\s*,', obj_text)
    if m_title:
        out["title"] = m_title.group(1)
    return out


def _parse_llm_array(content: str) -> list:
    """解析 LLM 返回的 JSON 数组（容错：代码块包裹/损坏对象/截断）

    注意: 不复用 nodes._safe_parse_json —— 它的容错逻辑只为 filtered_news/
    ranking 等 dict 结构设计，裸数组场景会全部 fallback 失败。
    本函数: 提取代码块 → 逐字符扫描平衡括号 → 逐对象解析
    （先 _repair_json 兜底，再 _rescue_judge_object 正则抢救）。
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
                            rescued = _rescue_judge_object(obj_text)
                            if rescued is not None:
                                items.append(rescued)
                            else:
                                logger.warning(f"LLM 返回单对象解析失败，跳过: {obj_text[:60]}")
                    obj_start = -1
        i += 1
    return items


def _llm_judge(items: list) -> list:
    """批量 LLM 判定，返回与 items 一一对应的判定 dict 列表

    对齐策略: 给每条注入 _judge_idx 并要求 LLM 回显 idx，按 idx 精确合并
    （标题匹配仅作兜底）。标题精确匹配不可靠——LLM 会改写/截断标题，
    批处理管线实证 60 条仅 26 条标题精确命中，未对齐条目会被误判不推，
    是漏推的主要根因。

    批次异常: 逐条降级为规则判定 _fallback_decision（高信号词+高预筛分仍可推），
    不再整批保守不推——重大消息宁可多推也不漏推。
    """
    if not items:
        return []
    results = [None] * len(items)
    for start in range(0, len(items), LLM_BATCH_SIZE):
        batch = items[start:start + LLM_BATCH_SIZE]
        for offset, n in enumerate(batch):
            n["_judge_idx"] = start + offset
        try:
            raw = _call_llm_api(_LLM_SYSTEM_PROMPT, _build_llm_user_prompt(batch), timeout=90, max_retries=1)
            entries = _parse_llm_array(raw)
            # 按 idx 精确对齐（标题兜底）：LLM 可能增减条目/乱序/改写标题
            by_idx = {}
            by_title = {}
            for e in entries:
                if not isinstance(e, dict):
                    continue
                try:
                    i = int(e.get("idx", -1))
                    if 0 <= i < len(items):
                        by_idx[i] = e
                except (ValueError, TypeError):
                    pass
                t = str(e.get("title", "") or "").strip()
                if t:
                    by_title[t] = e
            for offset, n in enumerate(batch):
                i = start + offset
                t = str(n.get("title", "") or "").strip()
                e = by_idx.get(i) or by_title.get(t)
                if not e:
                    # LLM 成功返回但未回显该条目（截断/遗漏/标题改写导致 idx 与标题均未对齐）。
                    # 不能静默判 push=False——那会把本可推送的重大消息漏掉（漏推主要根因），
                    # 与批次异常同样逐条走规则降级：高信号词+高预筛分仍可推。
                    logger.warning(f"LLM 未回显条目 idx={i}，降级规则判定: {t[:40]}")
                    results[i] = _fallback_decision(n, n.get("_pref_score", 0), n.get("_hit_signal", False))
                    continue
                results[i] = {
                    "title": t,
                    "push": bool(e.get("push", False)),
                    "score": e.get("score", 0),
                    "direction": str(e.get("direction", "neutral") or "neutral").lower(),
                    "scope": str(e.get("scope", "stock") or "stock").lower(),
                    "sectors": e.get("sectors") or [],
                    "entities": [str(x).strip() for x in (e.get("entities") or []) if str(x).strip()],
                    "is_leader_stock": bool(e.get("is_leader_stock", False)),
                    "reason": str(e.get("reason", "") or "").strip(),
                }
            logger.info(f"LLM 判定批次 {start//LLM_BATCH_SIZE + 1}: {len(batch)} 条完成（回显{len(entries)}条）")
        except Exception as e:
            logger.warning(f"LLM 判定批次失败（{len(batch)} 条），逐条降级为规则判定: {e}")
            for offset, n in enumerate(batch):
                results[start + offset] = _fallback_decision(
                    n, n.get("_pref_score", 0), n.get("_hit_signal", False))
    # 防御：任何未填充位置走规则降级
    for i, r in enumerate(results):
        if r is None:
            n = items[i]
            results[i] = _fallback_decision(n, n.get("_pref_score", 0), n.get("_hit_signal", False))
    return results


def _load_leader_watchlist() -> set:
    """加载自选龙头名单（watchlist.json 的 stocks），与 LLM 判定互为补充"""
    try:
        wl = json.loads((PROJECT_ROOT / "watchlist.json").read_text(encoding="utf-8"))
        stocks = wl.get("stocks", []) or []
        names = set()
        for s in stocks:
            if isinstance(s, str):
                names.add(s.strip())
            elif isinstance(s, dict):
                names.add(str(s.get("name", "") or "").strip())
        return {n for n in names if n}
    except Exception:
        return set()


def _hit_watchlist(news: dict, watchlist: set) -> bool:
    """资讯主体是否命中自选龙头名单（标题/名称/代码匹配）"""
    if not watchlist:
        return False
    title = str(news.get("title", "") or "")
    name = str(news.get("name", "") or "")
    content = str(news.get("content", "") or "")
    for n in watchlist:
        if n and (n in title or n in name or n in content):
            return True
    return False


def _fallback_decision(news: dict, pref_score: float, hit_signal: bool) -> dict:
    """LLM 整体失败时的降级判定：预筛分高 + 命中高信号词 → 直接推

    原则: 重大消息宁可多推也不漏推；日常流水宁可漏推也不滥推。
    """
    title = str(news.get("title", ""))
    text = f"{title} {news.get('content', '')}"
    if hit_signal and pref_score >= FALLBACK_PUSH_SCORE_MIN:
        # 方向三态判断（不再二值化：无多空信号的中性流水不应被标为强利空）
        if any(k in text for k in ["利好", "上涨", "支持", "放宽", "增持", "降准", "降息", "减税", "预增", "回购", "中标", "涨停", "签约", "订单", "合同"]):
            direction = "bullish"
        elif any(k in text for k in ["利空", "下跌", "暴跌", "制裁", "退市", "立案", "爆雷", "违约", "处罚", "跌停", "减持", "预减", "亏损"]):
            direction = "bearish"
        else:
            direction = "neutral"
        # scope 按文本语义判定，不硬编码 market（普通公司事件不应因降级获得 market 必推特权）
        if any(k in text for k in ["央行", "国务院", "证监会", "国常会", "政治局", "美联储", "财政部",
                                   "降准", "降息", "加息", "印花税", "关税", "制裁", "战争", "熔断",
                                   "大盘", "A股", "纳指", "美股"]):
            scope = "market"
        elif any(k in text for k in ["半导体", "芯片", "AI", "算力", "光伏", "新能源", "机器人",
                                     "光模块", "CPO", "PCB", "消费电子", "医药", "白酒", "板块"]):
            scope = "sector"
        else:
            scope = "stock"
        return {
            "title": title,
            "push": True,
            "score": round(pref_score * 10, 1),
            "direction": direction,
            "scope": scope,
            "sectors": [],
            "is_leader_stock": False,
            "reason": "高信号词+高预筛分（LLM降级判定）",
        }
    return {
        "title": title,
        "push": False,
        "score": round(pref_score * 10, 1),
        "direction": "neutral",
        "scope": "stock",
        "sectors": [],
        "is_leader_stock": False,
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

    正文处理: 资讯原文 content 截断 300 字符附在推送里（避免只推标题+理由
    让用户看不到资讯内容），企业微信 4096 字节限制内有充足余量。
    """
    title = str(news.get("title", "") or "")[:200]
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
    content_text = str(news.get("content", "") or "").strip()

    lines = [f"{emoji}【{label}】{title}", ""]
    meta = [f"**范围**: {scope_label}", f"**影响分**: {score}", f"**板块**: {sector_str}"]
    lines.append(" | ".join(meta))
    lines.append(f"**来源**: {source} {pub}")
    if content_text:
        body = content_text[:300]
        if len(content_text) > 300:
            body += "..."
        lines.append("")
        lines.append(f"> 📄 {body}")
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
    except Exception as e:
        logger.error(f"多源抓取失败: {e}", exc_info=True)
        news_list = []
        signals = []

    # 跨源近似去重（URL/精确标题之外补一层 SimHash）：同一事件不同措辞的多源
    # 报道先在入口收敛，避免各自进指纹/候选、重复消耗 LLM 判定 token。
    # 注: 只作用于多源新闻(dedup_news_3layer 对标题 SimHash)，signals 是交易所
    # 结构化数据（龙虎榜/业绩预告）且已按代码内去重，模板化标题不参与近似去重，
    # 避免"业绩预告: XX(代码) 预增 幅度+X%" 同模板不同股票被 SimHash 误判重复。
    before_dedup = len(news_list)
    news_list = dedup_news_3layer(list(news_list))
    if len(news_list) < before_dedup:
        logger.info(f"三层近似去重: {before_dedup} -> {len(news_list)} 条")
    news_list = news_list + list(signals)
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

    # 自选龙头名单（watchlist.json），与 LLM 的 is_leader_stock 判定互为补充
    leader_watchlist = _load_leader_watchlist()

    # 5. 同事件合并（跨源同事件只推最优一条）→ 跨轮已推事件拦截 → 阈值过滤 → 推送
    pushed = 0
    skipped = 0
    now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")

    # 5a. 生成推送级事件签名并按同事件分组：指纹解决"同一条"，签名解决"同一事件的不同报道"
    groups = []
    for n, j in zip(candidates, judges):
        if not isinstance(j, dict):
            j = _fallback_decision(n, n.get("_pref_score", 0), n.get("_hit_signal", False))
        sig = _push_event_sig(n, j)
        n["_sig"] = sig
        for g in groups:
            if _is_same_event(sig, g["sig"]):
                g["items"].append((n, j))
                g["sig"] = _merge_event_sig(g["sig"], sig)  # 传递合并，链式同事件也能汇聚
                break
        else:
            groups.append({"sig": sig, "items": [(n, j)]})
    if len(groups) < len(candidates):
        logger.info(f"同事件合并: {len(candidates)}条候选 -> {len(groups)}个事件 "
                    f"(合并{len(candidates) - len(groups)}条多源重复)")

    # 5b. 每组选最优代表（LLM判定可推者优先，其次影响分/预筛分高者），落选者记录指纹不再处理
    reps = []
    for g in groups:
        best = max(g["items"], key=lambda x: (
            bool(x[1].get("push")),
            _to_float(x[1].get("score", 0)),
            x[0].get("_pref_score", 0),
        ))
        reps.append(best)
        for n, _j in g["items"]:
            if n is not best[0]:
                seen[n["_fp"]] = {"t": now, "pushed": False,
                                  "title": str(n.get("title", ""))[:52] + "[同事件合并]"}
                skipped += 1

    # 5c. 逐代表：阈值过滤 → 跨轮同事件拦截 → 推送
    pushed_events = state.setdefault("pushed_events", [])
    for n, j in reps:
        # 判定顺序：
        # 1) LLM 明确判定不重大（push=false）→ 默认一票否决
        # 2) 例外放行：科技板块级资讯/科技龙头个股（_is_domestic_tech 规则兜底，
        #    "科技不能不漏"）或外围科技（_is_overseas_tech）——用户口径：影响科技
        #    板块的资讯必须推，只影响中小市值个股自身的业绩/回购等仍按 LLM 否决
        # 3) 阈值只对 LLM 认为值得推的条目进一步收紧
        # 龙头判定: LLM 标注 或 命中自选龙头名单
        is_leader = bool(j.get("is_leader_stock")) or _hit_watchlist(n, leader_watchlist)
        sectors = j.get("sectors") or []
        scope = str(j.get("scope", "stock") or "stock").lower()
        tech_override = (
            # 板块级科技资讯：LLM 即使判不推也放行（score≥5 或 强方向，避免滥推）
            scope == "sector" and _is_domestic_tech(n, sectors)
            and (_to_float(j.get("score", 0)) >= 5
                 or str(j.get("direction", "neutral")) in ("bullish", "bearish"))
        ) or (
            # 科技龙头个股的重大消息：LLM 判不推时仍放行
            scope == "stock" and is_leader and _is_domestic_tech(n, sectors)
            and (_to_float(j.get("score", 0)) >= 5
                 or str(j.get("direction", "neutral")) in ("bullish", "bearish"))
        )
        if j.get("push") and _passes_threshold(
                mode, j.get("score", 0), j.get("direction", "neutral"),
                j.get("scope", "stock"), leader_stock=is_leader):
            pass_round = True
        elif j.get("push") and _is_overseas_tech(n, sectors):
            # 外围消息且涉及科技板块：即使未达常规阈值也推（用户要求外围科技必推）
            pass_round = True
        elif tech_override:
            # 科技板块级/科技龙头资讯：LLM 判不推也放行（防漏推科技）
            pass_round = True
        else:
            pass_round = False

        if not pass_round:
            seen[n["_fp"]] = {"t": now, "pushed": False, "title": str(n.get("title", ""))[:60]}
            skipped += 1
            continue

        # 跨轮同事件拦截：48h 内已推过同事件则不再推（防多源报道时间差导致重复推送）
        if any(_is_same_event(n["_sig"], pe) for pe in pushed_events):
            logger.info(f"同事件48h内已推送，跳过: {n.get('title', '')[:50]}")
            seen[n["_fp"]] = {"t": now, "pushed": False,
                              "title": str(n.get("title", ""))[:52] + "[同事件已推]"}
            skipped += 1
            continue

        content = format_push_alert(n, j)
        if dry_run:
            logger.info(f"[dry-run] 将推送: {n.get('title', '')[:50]}")
            # Windows 控制台默认 GBK 无法打印 emoji，先切 UTF-8 容错
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
            print("\n===== 将推送内容预览 =====\n" + content + "\n==========================")
            seen[n["_fp"]] = {"t": now, "pushed": True, "title": str(n.get("title", ""))[:60]}
            pushed_events.append({**n["_sig"], "t": now})
            pushed += 1
        else:
            # 推送标题直接用新闻原文标题（避免显示"重要资讯"占位符）
            push_title = str(n.get("title", "") or "")[:80] or "重大资讯"
            result = _send_alert_item(push_config, push_title, content)
            if result.get("code") == 200 or result.get("errcode") == 0:
                logger.info(f"推送成功: {n.get('title', '')[:50]}")
                seen[n["_fp"]] = {"t": now, "pushed": True, "title": str(n.get("title", ""))[:60]}
                pushed_events.append({**n["_sig"], "t": now})
                pushed += 1
            else:
                # 推送失败：不记录指纹，下轮重试（避免重大消息丢失）
                logger.error(f"推送失败（下轮重试）: {n.get('title', '')[:50]} | {result}")
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
        if os.getenv("GIST_TOKEN") and os.getenv("GIST_ID"):
            logger.warning("本地轮询与云端 GitHub Actions 共享同一 Gist 状态。"
                           "若云端定时任务也在运行，建议只保留一个运行端"
                           "（状态已做读-改-写合并，但双端同跑仍浪费 LLM 额度且偶发重复）")
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
