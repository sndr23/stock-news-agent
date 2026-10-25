# filepath: scripts/real_time_push.py
"""
实时重大资讯推送（事件驱动，只推重要消息）
====================================================
目标: 像财联社公众号一样，有重大消息立刻推送到手机（微信），日常流水不打扰。

数据源（多源聚合，不限于财联社）:
    - get_stock_news: 东财快讯 + 财联社电报 + 新浪财经 + 同花顺快讯
                      + 富途全球快讯 + 华尔街见闻 + 金十数据
                      + Google News（8 源并行抓取 + 跨源去重）
    - get_market_signals: 龙虎榜机构动向 + 业绩预告（交易所官方信号）
    每 30 分钟（云端 GitHub Actions）/ 120 秒（本地 --loop）抓取一轮，
    增量检测（事件级指纹去重）→ 规则预筛 → LLM 严格判定 →
    仅推送重大消息到微信，非重大消息静默丢弃。

判定策略（2026-08-03 用户口径: 全部走 LLM，删除规则降级路径）:
    - 预筛通过的全部候选必须由 LLM 判定推/不推，Python 规则不得直接通关。
    - LLM 未回显 / 批次异常 / 无法解析的条目一律标记 judged=False 挂起
      （不推、不落指纹），留待下一轮重新送 LLM 判定。
    - 已删除 _fallback_decision 规则直推路径（高信号词+高预筛分不再绕过 LLM）。

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
from datetime import datetime, timedelta

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


logger = logging.getLogger("real_time_push")







# ============================================================
# 复用项目现有能力（不重复造轮子）
# ============================================================
from src.tools.data_fetchers import (get_stock_news, get_market_signals,
                                     get_announcements, dedup_news_3layer)  # 多源聚合抓取 + 三层近似去重
from src.tools.data_fetchers import load_source_health, save_source_health  # 源健康状态（P1 源健康告警）
from src.tools.calculators import (
    calculate_prefilter_importance,   # 预筛评分
    _extract_core_numbers,            # 核心金额提取
    predict_direction_by_rules,       # 规则方向兜底（词表全，含扭亏/退市/涨超/走弱等）
    )  # 多源事件签名
from src.strategy.state_io import atomic_write_json, get_gist_config, patch_gist_file
from src.strategy.data_freshness import _is_workday
# LLM 调用与 JSON 修复（2026-08-06 起从共享模块导入，不再依赖废弃的批处理管线 nodes.py）
from src.llm_client import _call_llm_api

# ============================================================
# 拆分模块 re-export（本文件保持统一门面：测试与外部脚本经 rtp.X 访问/
# monkeypatch 的名字必须存在于本模块命名空间，且本模块内函数体的全局名
# 查找落在此处——移动函数体内的同名解析由各自模块负责，语义不变）
# ============================================================
from src.rt_push.common import (  # noqa: F401
    BJT,
    _as_list,
    _env_int,
    _to_float,
    _as_bool,
    _normalize_entity,
    _ENTITY_ALIAS,
    _lcs_len,
    _lcs_subseq_len,
    _state_timestamp,
    _parse_bjt,
)
from src.rt_push.same_event import (  # noqa: F401
    _normalize_title,
    _event_signature_light,
    _EXTRA_EVENT_GROUPS,
    _core_fact_anchors,
    _CORE_FACT_GROUPS,
    _title_numeric_anchors,
    _TITLE_NUMERIC_ANCHOR_RE,
    _sectors_overlap,
    _push_event_sig,
    _merge_event_sig,
    _title_direction_conflict,
    _TITLE_BEARISH_HINTS,
    _TITLE_BULLISH_HINTS,
    _session_group,
    _SESSION_GROUPS,
    _market_domain_overlap,
    _MARKET_DOMAIN_MARKERS,
    _shared_index_token,
    _INDEX_TOKENS,
    _session_conflict,
    _entity_overlap,
    _strip_entities,
    _is_index_move_text,
    _NOISE_INDEX_MOVE,
    _NOISE_INDEX_NAMES,
    _is_intraday_move_text,
    _NOISE_INTRADAY_MARKERS,
    _NOISE_COLUMN_MARKERS,
    _DAILY_COLUMN_MARKERS,
    _NOISE_OVERSEAS_STOCK_MOVE,
    _NOISE_OVERSEAS_STOCK_NAMES,
    _BROADCAST_EVENT_VERBS,
    _is_broadcast_body,
    _MAJOR_CORPORATE_EVENT_MARKERS,
    _hit_watchlist,
    _MACRO_DATA_PATTERNS,
    _MACRO_RELEASE_CONTEXT,
    _is_macro_data_release,
    _DEAL_ACTION_WORDS,
    _DEAL_AMOUNT_RE,
    _is_major_deal_event,
    _hard_event_novelty_facts,
    _HARD_EVENT_NOVELTY_GROUPS,
    _CAPACITY_COMPOUND_MARKERS,
    _hard_event_chain_key,
    _hard_event_topic_exemption_available,
    _market_theme_keys,
    _MARKET_THEME_PATTERNS,
    _sig_theme_keys,
    _market_theme_saturated,
    MARKET_TOPIC_PUSH_LIMIT,
    TOPIC_PUSH_WINDOW_H,
    TOPIC_PUSH_LIMIT,
    _is_macro_policy,
    _MACRO_POLICY_KEYWORDS,
    _headline_quality,
    _HEADLINE_EMOTIONAL_MARKERS,
    _HEADLINE_INCREMENTAL_MARKERS,
    _is_truncated_title,
    _TRUNCATED_ENDINGS,
    _title_has_entity,
    _candidate_sort_key,
    _topic_saturated,
    _central_bank_shared,
    _CENTRAL_BANK_ENTITIES,
    _policy_direction_conflict,
    _POLICY_DOVE_WORDS,
    _POLICY_HAWK_WORDS,
    _SameEventCtx,
    _same_event_ctx,
    _MARKET_STATE_EVENTS,
    _same_event_core_facts,
    _same_event_shared_group,
    _EVENT_PHRASE_ANCHORS,
    _same_event_cross_family,
    _is_same_event,
    _same_event_no_group,
    _same_event_market_rules,
    _MACRO_EVENT_WORDS,
    _MACRO_POLICY_EVENT_WORDS,
    _MARKET_MOVE_WORDS,
    _same_event_entity_anchor_rules,
    _same_event_title_similarity,
)
from src.rt_push.llm_judge import (  # noqa: F401
    _LLM_SYSTEM_PROMPT,
    _build_llm_user_prompt,
    _rescue_judge_object,
    _parse_llm_array,
    _hang_judge,
    _format_llm_judge_result,
)
from src.rt_push.state_store import (  # noqa: F401
    _empty_state,
    _is_ci,
    _pending_retry_limit,
    MAX_PENDING_RETRY,
    MAX_PENDING_RETRY_HIGH_SIGNAL,
    _est_seen_bytes,
    _est_pending_bytes,
    _prune_seen,
    SEEN_MAX,
    SEEN_MAX_BYTES,
    PENDING_MAX_BYTES,
    BACKTEST_EVENTS_MAX,
    STATE_WINDOW_HOURS_UNPUSHED,
    _compress_seen_for_gist,
    GIST_STATE_SEEN_KEEP,
    _pending_same_as_pushed,
    _pend_payload,
    _event_sig_key,
    _day_key,
    _mark_candidate_pushed,
    _merge_state,
)
from src.rt_push.push_format import (  # noqa: F401
    _normalize_direction,
    _opposite_events_note,
    _related_recent_note,
    format_push_alert,
    _send_alert_item,
    _DIR_EMOJI,
    _DIR_LABEL,
    _SCOPE_LABEL,
    _BULL_GROUP,
    _BEAR_GROUP,
)
from src.tools.keyword_tables import (                      # 共享关键词表（单一事实来源）
    HIGH_SIGNAL_KEYWORDS,      # noqa: F401  测试经 rtp.HIGH_SIGNAL_KEYWORDS 断言共享词表
    OVERSEAS_TECH_KEYWORDS,    # noqa: F401  测试经 rtp.OVERSEAS_TECH_KEYWORDS 断言共享词表
    OVERSEAS_SOURCE_MARKERS,
    TECH_SECTOR_WORDS_WIDE,     # 2026-08-19: 国内降级判定宽词表（原本地 _TECH_SECTOR_WORDS 收编）
    has_signal_keyword,        # 2026-08-06: 词边界感知的信号词匹配（ST/IPO 防误命中）
    find_signal_fp_keywords,   # 2026-08-07: 指纹专用信号词（排除宽泛市场词，防跨事件指纹合并）
    has_overseas_tech_keyword, # 2026-08-13: 词边界感知的外围科技词匹配（AI 不误命中 DUBAI）
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

_NOISE_LEADER_WHITELIST = (
    "中际旭创", "寒武纪", "宁德时代", "新易盛", "天孚通信", "源杰科技",
    "生益科技", "南亚新材", "鼎泰高科", "中国巨石", "风华高科", "英伟达",
    "台积电", "三星电子", "SK海力士", "美光", "阿斯麦", "ASML",
    "长江存储", "长鑫存储",
)

# 批量 LLM 判定每批条数（控制单次请求 token 与延迟）
LLM_BATCH_SIZE = 4

# 状态窗口：指纹保留时长（小时），滚动清理
STATE_WINDOW_HOURS = 48














# 心跳告警（2026-08-06 新增）：有新增资讯但连续 N 轮 0 推送时输出告警日志，
# 帮助区分"确实没大事" vs "系统静默故障"
HEARTBEAT_ZERO_PUSH_WARN_ROUNDS = 6
# 进程内计数器（本地 --loop 常驻进程跨轮累计；云端单轮运行无影响）
_zero_push_streak = [0]
# watchlist 空名单告警去重（2026-08-13 P1-2：--loop 模式只告警一次，避免每轮刷屏）
_watchlist_warned = [False]

# 源健康告警（2026-09-07 P1）：单源连续 N 轮拉取为空 → 推送一条告警
# （同源 24h 限频 1 次）；全部源同时为空 = 市场静默（如非交易时段），不告警。
# 数据由 data_fetchers.record_source_health 落本地状态（logs/source_health.json）。
SOURCE_HEALTH_ALERT_STREAK = 6
SOURCE_HEALTH_ALERT_INTERVAL_HOURS = 24

# Gist 内状态文件名
GIST_STATE_FILENAME = "real_time_state.json"

# ============================================================
# Gist 状态体积守卫（2026-09-07 P1）
# ============================================================
# Gist 单文件 1MB 是硬限，超限 = 状态写失败 = 推送中断（云端状态已实测 917KB）。
# 上传前检查序列化体积：>950KB 先压缩 seen（保留最近 1800 条）再传；
# 压缩后仍超限则报错（含压缩前后体积），fail-stop 交由人工处理。
GIST_STATE_LIMIT_BYTES = 950_000






def evaluate_source_health_alerts(health: dict, now: datetime | None = None) -> list:
    """从源健康状态生成待推送告警，返回 [(源名, 告警文案)]。

    规则：
    - 单源连续空轮数 >= SOURCE_HEALTH_ALERT_STREAK 才告警（文案含源名与连续空轮数）；
    - 同源 SOURCE_HEALTH_ALERT_INTERVAL_HOURS 小时内已告警过则跳过（24h 限频）；
    - 本轮全部源拉取均为空 = 市场静默（非交易时段正常现象），不告警。
    """
    now = now or datetime.now(BJT)
    entries = {k: v for k, v in (health or {}).items() if isinstance(v, dict)}
    if not entries:
        return []
    counts = {k: int(v.get("count", 0) or 0) for k, v in entries.items()}
    if all(c == 0 for c in counts.values()):
        return []  # 市场静默：全源同时空，非单源故障
    alerts = []
    for label, e in entries.items():
        streak = int(e.get("streak", 0) or 0)
        if streak < SOURCE_HEALTH_ALERT_STREAK:
            continue
        last = _parse_bjt(e.get("last_alert", ""))
        if last and (now - last) < timedelta(hours=SOURCE_HEALTH_ALERT_INTERVAL_HOURS):
            continue
        alerts.append((label,
                       f"⚠️ 源健康告警：数据源「{label}」已连续 {streak} 轮拉取为空"
                       f"（最近记录 {e.get('t', '')}），可能故障，请人工检查。"))
    return alerts


def push_source_health_alerts(push_config: dict, dry_run: bool = False,
                              health: dict | None = None,
                              now: datetime | None = None) -> int:
    """检查源健康并推送告警（同源 24h 限频），返回实际推送条数。

    health/now 参数供测试注入；默认读 data_fetchers 落地的本地源健康状态。
    告警推送成功才回写 last_alert（推送失败下轮重试）；检查异常只告警日志，
    不影响主推送流程。
    """
    try:
        health = load_source_health() if health is None else health
        alerts = evaluate_source_health_alerts(health, now=now)
        if not alerts:
            return 0
        now = now or datetime.now(BJT)
        sent = 0
        for label, msg in alerts:
            if dry_run:
                logger.warning(f"[dry-run] 将推送源健康告警: {msg}")
                continue
            result = _send_alert_item(push_config, "源健康告警", msg)
            if result.get("code") == 200 or result.get("errcode") == 0:
                if isinstance(health.get(label), dict):
                    health[label]["last_alert"] = now.strftime("%Y-%m-%d %H:%M:%S")
                sent += 1
                logger.warning(f"源健康告警已推送: {msg}")
            else:
                logger.error(f"源健康告警推送失败（下轮重试）: {msg} | {result}")
        if sent:
            save_source_health(health)
        return sent
    except Exception as e:
        logger.warning(f"源健康告警检查异常(不影响主流程): {e}")
        return 0

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
    return has_overseas_tech_keyword(text)


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
    return has_overseas_tech_keyword(text)


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

    注（2026-08-07 注释对齐）：生产入口 run_once 5c 已在调用本函数之前执行
    "强档硬门槛"——direction 非 bullish/bearish 一律不推（2026-08-04 用户口径：
    仅强利好/强利空推送），覆盖 market/sector/stock 全 scope。因此本函数中
    market 必推、sector score>=6、loose 模式等分支在当前生产入口下属于
    保留设计（历史/未来模式扩展用），实际运行时能走到这里的判定必然已是强档。
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
    # 2026-08-06 修复：find_signal_keywords 用词边界匹配英文缩写（ST/IPO），
    # 防 "STorage" 误命中 ST 导致不同事件指纹合并（漏推）
    # 2026-08-07 修复：改用 find_signal_fp_keywords——排除宽泛市场/机构词
    # （韩国/纳指/央行/油价等，实测"韩国总统宣布新产业计划"与"韩国半导体出口大增"
    # 因共享"韩国"被合并成同一指纹 → 后一条漏推）。仅命中宽泛词时退回
    # 下方标题指纹分支；跨源同事件仍由推送级 _is_same_event 兜底合并。
    hit_signal = find_signal_fp_keywords(text)
    title_norm = _normalize_title(news.get('title', ''))
    # 标题主语片段（2026-08-13 修复：news 类无 affected_stocks/name 字段，st 恒为空，
    # 导致"寒武纪回购5亿"与"宁德时代回购5亿"事件组+金额相同 → 指纹碰撞漏推。
    # 掺入标题前 6 字区分公司主语（多源同事件报道的主语通常一致，不影响合并）。
    title_head = title_norm[:6]
    if hit_signal:
        # 命中高信号词（宏观/监管级）：以信号词+事件组+个股为指纹，不掺入数字——
        # 数字表达不稳定（"1700亿" vs "一千七百亿"），且同信号词下数字分叉
        # 会把同一事件的多源报道拆成不同指纹
        if stocks:
            key = f"{date}|sig:{sorted(hit_signal)}|ev:{sorted(events)}|st:{sorted(stocks)}"
        else:
            key = f"{date}|sig:{sorted(hit_signal)}|ev:{sorted(events)}|t:{title_head}"
    elif stocks or events or numbers:
        # 公告/公司事件：个股+事件组+核心金额辅助区分（"XX:回购5亿" vs "XX:回购8亿"）
        if stocks:
            key = f"{date}|st:{sorted(stocks)}|ev:{sorted(events)}|num:{sorted(numbers)}"
        else:
            key = f"{date}|ev:{sorted(events)}|num:{sorted(numbers)}|t:{title_head}"
    else:
        # 普通流水新闻：归一化标题指纹
        key = f"{date}|t:{title_norm}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
































# 研报/观点/主题类措辞（2026-08-13 P0 修复：tech_override 排除守卫用——
# 命中此类措辞的科技消息属"无具体事件的定性判断"，即使 LLM 判 push=false
# 也**不得**被科技兜底强制放行，让 LLM 的否决生效。实测："机构称物理AI市场空间"
# "液冷投资向零部件纵深传导" 均命中，此前被 tech_override 放行误推。）
_TECH_OVERRIDE_VIEW_WORDS = [
    "机构称", "机构认为", "研报", "点评", "观点", "看好", "展望", "解读",
    "市场空间", "有望", "传导", "开启", "浪潮", "风口", "逻辑", "下注方向",
    # 2026-08-13 晚间复审残余修复：分析师警告/预测措辞 + 无事件佐证的泛板块措辞
    # 实测"摩根士丹利警告供应瓶颈""大摩示警算力不足""多家上市公司加码液冷散热业务"
    # 均命中此前漏网，tech_override 仍强制放行（误推复现）。补词后 LLM 的 push=false 生效。
    "警告", "示警", "预计", "目标价", "评级", "加码",
]























def _is_noise_push(news: dict, judge: dict, leader_watchlist: set) -> str:
    """栏目汇总/指数播报/盘面异动类噪声识别（2026-08-11 修复）

    返回噪声原因字符串（非空=应过滤不推），空串=正常条目。
    判定顺序：
    1. 栏目汇总类（新闻精选/要闻速递/九点特供/风口研报/午评/收评等）→ 不推；
       但剥离栏目词后仍含高信号词/宏观数据（栏目内嵌重大事件）→ 放行
    2. 指数盘中行情播报（指数名 + 涨跌幅度措辞）→ 不推
    3. 板块/概念盘面异动（异动拉升/直线涨停等）→ 非龙头不推；
       LLM 龙头标记或命中自选名单（如"中际旭创跌超3%"）保留
    """
    title = str(news.get("title", "") or "")
    if not title:
        return ""
    if any(m in title for m in _NOISE_COLUMN_MARKERS):
        # 2026-08-13 修复（漏推实证）：栏目词仅是栏目前缀/栏目名，真正价值在栏目词之外的内容。
        # 此前"只要标题含栏目词即一票否决"，会把"晚间新闻精选：央行宣布降准"、
        # "涨停分析：中际旭创获50亿大单涨停"等栏目内嵌的重大事件误杀。
        # 现剥离全部命中的栏目词后，若剩余仍含高信号词或宏观数据发布，
        # 判定为"栏目内嵌重大事件"放行（后续仍走强档门槛 + LLM push 判定把关）。
        rest = title
        for m in _NOISE_COLUMN_MARKERS:
            if m in rest:
                rest = rest.replace(m, "")
        if any(m in title for m in _DAILY_COLUMN_MARKERS):
            if not (_is_macro_data_release(rest) or _is_major_deal_event(rest)
                    or _hit_watchlist(news, leader_watchlist)):
                return "日报栏目"
            return ""
        # 2026-08-25 审核实证："午评创业板指半日跌35…算力硬件"剥离"午评"后
        # 因 has_signal_keyword（算力硬件）被误放行，实为指数+盘面行情播报。
        # 剥离后若仍是"指数名+行情措辞"或"盘面异动措辞"→ 栏目/行情不推。
        # 但"栏目内嵌重大事件"仍放行（2026-08-13 语义）：CPI 数据发布、龙头
        # 大额经营事件不得因含"跳水/涨停"盘面词被误杀（test_fix_20260813 回归）。
        if has_signal_keyword(rest) or _is_macro_data_release(rest):
            if not (_is_macro_data_release(rest) or _is_major_deal_event(rest)
                    or _hit_headline_entity(rest)):
                if _is_index_move_text(rest) or _is_intraday_move_text(rest):
                    return "栏目汇总"
            return ""
        return "栏目汇总"
    if any(i in title for i in _NOISE_INDEX_NAMES) and any(m in title for m in _NOISE_INDEX_MOVE):
        return "指数播报"
    # 2026-09-04 验收回放实证："SK海力士涨幅扩大至5% 三星电子涨逾3%"被推送——
    # 外盘科技龙头不在指数名单漏网。公司名+严格数字涨幅措辞组合拦截；
    # 事件性新闻（"SK海力士扩产 HBM""台积电营收上涨10%"）不含严格措辞不误拦。
    if (any(i in title for i in _NOISE_OVERSEAS_STOCK_NAMES)
            and any(m in title for m in _NOISE_OVERSEAS_STOCK_MOVE)):
        return "外盘个股播报"
    if any(m in title for m in _NOISE_INTRADAY_MARKERS):
        # 2026-09-04 晚审计回放实证："涨停芯片股背后藏雷！实控人、总经理双双被留置"
        # 会被"涨停"误判盘面异动——但核心是留置/冻结重大事件，盘面词仅是背景。
        # 含重大公司治理/监管事件词时放行（后续仍走强档门槛 + LLM 把关）。
        if not any(k in title for k in _MAJOR_CORPORATE_EVENT_MARKERS):
            is_leader = (_hit_watchlist(news, leader_watchlist)
                         or any(x in title for x in _NOISE_LEADER_WHITELIST)
                         or (bool(judge.get("is_leader_stock"))
                             and float(judge.get("score") or 0) >= 8
                             and str(judge.get("scope") or "") == "stock"))
            if not is_leader:
                return "盘面异动"
    broadcast_reason = _is_broadcast_body(title, exempt_names=leader_watchlist)
    if broadcast_reason and any(x in title for x in _NOISE_LEADER_WHITELIST):
        broadcast_reason = ""
    if broadcast_reason:
        return broadcast_reason
    return ""





























































def _state_path() -> Path:
    return PROJECT_ROOT / "logs" / "real_time_state.json"




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
    last_error = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            files = data.get("files") or {}
            fobj = files.get(GIST_STATE_FILENAME)
            if fobj is not None:
                break
        except Exception as e:
            last_error = e
            logger.warning(f"Gist 读取第{attempt + 1}次失败: {e}")
            fobj = None
        if attempt < 2:
            logger.info(f"Gist 状态文件暂未读到（第{attempt + 1}次，可能命中旧快照），1s 后重试")
            time.sleep(1)
    # 2026-08-13 P0 修复（状态丢失事故根因）：3 次均未读到文件时必须报错，
    # 禁止"空状态运行后覆盖写回"——此前静默返回 "{}" 导致 19:03 轮把
    # 4759 seen/200 pending/62 pushed 历史去重记录全部冲掉。
    if fobj is None:
        raise RuntimeError(f"Gist 状态文件读取失败（3 次尝试均未读到文件）: {last_error}")
    content = fobj.get("content")
    try:
        state = json.loads(content)
    except (json.JSONDecodeError, TypeError) as e:
        # 2026-09-02 P0 根因修复：Gist API GET 的 content 字段在文件接近 ~900KB 时
        # 会被截断返回（fobj["truncated"]=True；实测 917KB 文件 content 截到约
        # 70.8 万字符即断，raw_url 下载同一文件完整可解析）——不是写入侧损坏。
        # run#1214（09-02 07:00 北京）起 21 连败即此问题：json.loads 截断串必抛。
        # 因此解析失败不直接 raise，先回退 raw_url 下载完整内容；仍失败才真正
        # raise（"禁止静默降级空状态"原则不变）。
        raw_url = fobj.get("raw_url")
        if not raw_url:
            logger.error(f"Gist 状态文件 JSON 解析失败且无 raw_url 可回退"
                         f"（内容 {len(content) if content else 0} 字符）: {e}")
            raise
        logger.warning(f"Gist content 解析失败（可能 API 截断，{len(content) if content else 0} 字符）: {e}；"
                       "回退 raw_url 下载完整内容")
        # raw.githubusercontent 同样有 CDN 缓存，追加时间戳参数强制绕过（同上方 API 缓存对策）
        sep = "&" if "?" in raw_url else "?"
        raw_resp = requests.get(f"{raw_url}{sep}ts={int(time.time() * 1000)}",
                                headers=headers, timeout=30)
        raw_resp.raise_for_status()
        state = json.loads(raw_resp.content.decode("utf-8"))
        logger.info(f"raw_url 回退成功（{len(raw_resp.content)} 字节），content 截断绕过")
    # 结构校验：解析成功但缺 seen 等核心键 → 视为损坏（防"{}"等畸形内容被当空状态）
    if not isinstance(state, dict) or "seen" not in state:
        raise ValueError("Gist 状态文件结构异常（缺少 seen 键），拒绝空状态运行")
    return state


def _gist_save(token: str, gist_id: str, state: dict) -> None:
    """将状态写回 Gist（单文件提交，防并发覆盖）。

    2026-08-31 修复：Gists API 不支持条件请求，ccbe890（8-29 P0-2）加入的
    If-Match 头自上线起即被 GitHub 一律拒绝（400），曾致云端零写入 62+ 小时。
    并发安全依赖 workflow concurrency 串行 + 单文件提交；patch_gist_file
    内部自带重试。禁止给 Gist PATCH 加 If-Match/If-None-Match。
    2026-09-04 验收修复：去掉 indent=2——缩进空格使 743KB 的 JSON 膨胀到
    917KB，超出 Gist GET content 截断线（~900KB，实测 917KB 文件 content
    截到 70.8 万字符），每轮 load 被迫走 raw_url 回退慢路径；继续增长逼近
    1MB 硬限将致 PATCH 写入直接失败。紧凑序列化立省 ~174KB（19%）。
    2026-09-07 P1 体积守卫：上传前检查序列化体积，>950KB 先压缩 seen
    （保留最近 1800 条）再传；压缩后仍超限报错（含压缩前后体积），
    宁可本轮失败也不静默写入失败导致推送中断。
    2026-09-08 P0-2：seen 压缩后仍超限时，先把 backtest_events 裁到最新
    500 条再试一次（回测档案只影响统计样本量，最安全的可裁项），
    仍超限时再压缩 seen 到最近 500 条，仍超限才报错。
    """
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    size_before = len(payload.encode("utf-8"))
    if size_before > GIST_STATE_LIMIT_BYTES:
        seen_n_before = len(state.get("seen") or {})
        state["seen"] = _compress_seen_for_gist(state.get("seen") or {})
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        size_after = len(payload.encode("utf-8"))
        logger.warning(f"Gist 状态序列化超限（{size_before}B > {GIST_STATE_LIMIT_BYTES}B），"
                       f"已压缩 seen: {seen_n_before} 条 → {len(state['seen'])} 条，"
                       f"压缩后 {size_after}B")
        if size_after > GIST_STATE_LIMIT_BYTES:
            # 2026-09-08 P0-2：seen 压缩后仍超限，先把回测档案裁到最新 500 条
            # （回测只影响统计样本量，不影响推送正确性，最安全的可裁项）再试一次。
            bt_n_before = len(state.get("backtest_events") or [])
            if bt_n_before > 500:
                state["backtest_events"] = sorted(
                    state.get("backtest_events") or [],
                    key=lambda e: e.get("t", ""))[-500:]
                payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
                size_after = len(payload.encode("utf-8"))
                logger.warning(f"压缩 seen 后仍超限，已裁剪 backtest_events: "
                               f"{bt_n_before} 条 → {len(state['backtest_events'])} 条，"
                               f"裁剪后 {size_after}B")
                if size_after > GIST_STATE_LIMIT_BYTES:
                    # 档案裁剪仍不足以达标时继续压缩 seen，避免其大字段占满状态体积。
                    seen_n_before_fallback = len(state.get("seen") or {})
                    state["seen"] = _compress_seen_for_gist(
                        state.get("seen") or {}, keep=500)
                    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
                    size_after = len(payload.encode("utf-8"))
                    logger.warning(f"裁剪 backtest_events 后仍超限，已进一步压缩 seen: "
                                   f"{seen_n_before_fallback} 条 → {len(state['seen'])} 条，"
                                   f"压缩后 {size_after}B")
        if size_after > GIST_STATE_LIMIT_BYTES:
            logger.error(f"Gist 状态压缩后仍超限: 压缩前 {size_before}B, "
                         f"压缩后 {size_after}B, 上限 {GIST_STATE_LIMIT_BYTES}B")
            raise RuntimeError(f"Gist 状态体积超限: 压缩前 {size_before}B, "
                               f"压缩后 {size_after}B（上限 {GIST_STATE_LIMIT_BYTES}B），"
                               "需人工清理状态文件")
    patch_gist_file(
        GIST_STATE_FILENAME,
        payload,
        token, gist_id,
    )


def load_state() -> dict:
    """加载状态：云端优先 Gist，本地用文件"""
    gist_token, gist_id = get_gist_config()

    if gist_token and gist_id:
        try:
            state = _gist_load(gist_token, gist_id)
            state.setdefault("pushed_events", [])
            state.setdefault("pending", {})
            state.setdefault("watch_announce", [])
            state.setdefault("backtest_events", [])
            logger.info(f"状态已从 Gist 加载: {len(state.get('seen', {}))} 个指纹, "
                        f"{len(state.get('pushed_events', []))} 个已推事件"
                        f", {len(state.get('pending', {}))} 个挂起重试")
            return state
        except Exception as e:
            logger.warning(f"Gist 读取失败: {e}")
            # Gist 已配置但不可读时，禁止回退旧本地状态，避免重复推送或状态回退。
            raise RuntimeError(f"Gist 读取失败，禁止无状态运行: {e}")

    # 本地文件
    state_path = _state_path()
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or "seen" not in state:
                state = _empty_state()
            state.setdefault("pushed_events", [])
            state.setdefault("pending", {})
            state.setdefault("watch_announce", [])
            state.setdefault("backtest_events", [])
            logger.info(f"状态已从本地加载: {len(state.get('seen', {}))} 个指纹, "
                        f"{len(state.get('pending', {}))} 个挂起重试")
            return state
        except Exception as e:
            logger.warning(f"本地状态解析失败，重置: {e}")
    return _empty_state()






def _reinject_pending_items(pending: dict, news_list: list, seen: dict) -> list:
    """pending 主动重注入（2026-09-04 P1-1）：返回本轮未重新抓取到的挂起条目。

    只重注入带 payload 的记录（旧版无 payload 的 pending 记录跳过，等自然过期）；
    已在本轮 news_list 中重新抓到的条目走正常增量检测路径，不重复注入；
    已落 seen 的条目跳过。按 retry 从大到小排序（老条目优先进入判定）。"""
    if not pending:
        return []
    fetched_fps = {_news_fingerprint(n) for n in news_list}
    reinject = []
    for fp, rec in pending.items():
        if fp in seen or fp in fetched_fps:
            continue
        payload = rec.get("payload")
        if not payload:
            continue
        n = dict(payload)
        n["_fp"] = fp
        n["_pend_retry"] = int(rec.get("retry", 0))
        n["_from_pending"] = True
        reinject.append(n)
    reinject.sort(key=lambda x: x["_pend_retry"], reverse=True)
    return reinject










def save_state(state: dict) -> None:
    """保存状态：云端写 Gist（读-改-写合并防并发覆盖），本地写文件"""
    gist_token, gist_id = get_gist_config()

    _merge_failed = False
    if gist_token and gist_id:
        # 写入前先合并最新远端状态，避免并发实例互相覆盖
        try:
            latest = _gist_load(gist_token, gist_id)
            state = _merge_state(state, latest)
        except Exception as e:
            # 2026-08-13 P0 修复（状态丢失事故根因）：合并失败禁止静默覆盖。
            # 此前"直接覆盖写入"导致 19:03 轮把 Gist 中 4759 条历史 seen 全部冲掉。
            # CI 下报错退出（fail-stop：宁可本轮失败，不可丢去重基准）；
            # 本地模式下拒绝覆盖 Gist，降级写本地文件（防远端被冲，用户可手动恢复）。
            logger.error(f"Gist 保存前合并失败: {e}")
            _merge_failed = True
            if _is_ci():
                raise RuntimeError(f"Gist 合并失败，拒绝覆盖写入（防止状态丢失）: {e}")
            logger.warning("本地模式合并失败：拒绝覆盖 Gist（防远端去重基准被冲），状态降级写入本地文件")
    elif _is_ci():
        # CI 下没有 Gist 配置 → 状态无处可存 → 下轮会重复推送，必须报错
        raise RuntimeError("CI 环境缺少 GIST_TOKEN/GIST_ID，状态无法持久化，禁止无状态运行")

    # 滚动清理过期指纹：已推条目保留 48h 供复盘/同事件拦截，未推条目只保留 24h，
    # 避免大量普通未推记录长期占据 seen。pending/pushed_events 等仍统一使用 48h。
    now = datetime.now(BJT)
    cutoff = (now - timedelta(hours=STATE_WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    unpushed_cutoff = (now - timedelta(hours=STATE_WINDOW_HOURS_UNPUSHED)).strftime(
        "%Y-%m-%d %H:%M:%S")
    seen = state.get("seen", {})
    expired = [
        fp for fp, rec in seen.items()
        if rec.get("t", "") < (cutoff if rec.get("pushed") else unpushed_cutoff)
    ]
    for fp in expired:
        seen.pop(fp, None)
    if expired:
        logger.info(f"清理过期指纹 {len(expired)} 条，剩余 {len(seen)} 条")
    # 2026-08-13 P0：seen 无上限导致状态文件膨胀至 0.72MB（Gist 写入截断损坏诱因）。
    # 48h 清理后仍超上限时按上限裁剪，优先淘汰未推条目（见 _prune_seen）。
    # 2026-09-01 修正触发条件：条数上限不是唯一闸门——纯条数未超但字节已超
    # 700KB 时同样裁剪（中文 UTF-8 下 12000 条可能超 1MB Gist 硬限）。
    if len(seen) > SEEN_MAX or _est_seen_bytes(seen) > SEEN_MAX_BYTES:
        before_n = len(seen)
        seen = _prune_seen(seen, SEEN_MAX)
        logger.warning(f"seen 超过上限（{before_n} 条 → {len(seen)} 条），"
                       "优先淘汰未推条目后按时间保留最新（条数/字节双上限）")
    state["seen"] = seen

    # 滚动清理过期挂起重试（48h 窗口）+ 上限 200 条防爆胀 + 重试上限兜底
    pending = state.get("pending", {})
    dropped_retry = 0
    seen = state.get("seen", {})
    for fp, rec in list(pending.items()):
        retry = int(rec.get("retry", 0))
        limit = _pending_retry_limit(rec)
        if retry >= limit:
            old = seen.get(fp)
            if not (old and old.get("pushed")):
                seen[fp] = {"t": rec.get("t", datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")),
                            "pushed": False,
                            "title": str(rec.get("title", ""))[:60] + "[溢出放弃]"}
            pending.pop(fp, None)
            dropped_retry += 1
    pend_expired = [fp for fp, rec in pending.items() if rec.get("t", "") < cutoff]
    for fp in pend_expired:
        pending.pop(fp, None)
    if len(pending) > 200:
        pending = dict(sorted(pending.items(), key=lambda kv: kv[1].get("t", ""))[-200:])
    # 2026-09-04 P1-1：pending 带 payload 后按字节兜底（Gist 1MB 硬限）
    if _est_pending_bytes(pending) > PENDING_MAX_BYTES:
        pending = dict(sorted(pending.items(), key=lambda kv: kv[1].get("t", ""))[-150:])
    state["pending"] = pending
    if pend_expired or dropped_retry or len(pending) != len(state.get("pending", {})):
        logger.info(f"清理过期挂起重试 {len(pend_expired)} 条 + 重试上限放弃 {dropped_retry} 条，剩余 {len(pending)} 条")

    # 滚动清理过期已推事件签名（48h 窗口）+ 上限 300 条防爆胀
    pe_all = state.get("pushed_events") or []
    pe = [e for e in pe_all if e.get("t", "") >= cutoff]
    if len(pe) > 300:
        pe = sorted(pe, key=lambda e: e.get("t", ""))[-300:]
    state["pushed_events"] = pe

    # 2026-09-08 P0-2：被淘汰的已推事件（出 48h 窗口或超 300 上限）先转存回测
    # 档案再丢弃。此前直接淘汰 → 永久丢失，signal_backtest 统计后 1/3/5/10 日
    # 收益时后 3/5/10 日维度永远无数据。档案条目与 pushed_events 原条目字段
    # 完全一致（回测消费同一结构），按时间保留最新 BACKTEST_EVENTS_MAX 条；
    # 体积再由 _gist_save 体积守卫兜底裁剪。并入档案前按 _event_sig_key 去重，
    # 防 Gist 合并把远端残留的已淘汰条目复活后重复转存。
    _pe_ids = {id(e) for e in pe}
    _evicted = [e for e in pe_all if id(e) not in _pe_ids and isinstance(e, dict)]
    archive = list(state.get("backtest_events") or [])
    if _evicted:
        _archived_keys = {_event_sig_key(e) for e in archive if isinstance(e, dict)}
        for e in _evicted:
            _k = _event_sig_key(e)
            if _k not in _archived_keys:
                _archived_keys.add(_k)
                archive.append(e)
        logger.info(f"已推事件淘汰 {len(_evicted)} 条转存 backtest_events（现 {len(archive)} 条）")
    if len(archive) > BACKTEST_EVENTS_MAX:
        before_n = len(archive)
        # 上限裁剪后才排序落盘（无淘汰轮次不重排已有档案，避免每轮全量重写）
        archive = sorted(archive, key=lambda e: e.get("t", ""))[-BACKTEST_EVENTS_MAX:]
        logger.warning(f"backtest_events 超过上限（{before_n} 条 → {len(archive)} 条），"
                       "按时间保留最新")
    state["backtest_events"] = archive

    # 滚动清理过期当日预筛候选（P7-1，48h 窗口）+ 上限 300 条防爆胀
    ce = [e for e in (state.get("candidate_events") or []) if e.get("t", "") >= cutoff]
    if len(ce) > 300:
        before_n = len(ce)
        # 2026-09-07 P0 审计 2.3：纯按时间 FIFO 会把高流量日未推的强档候选挤出。
        # 改为未推条目优先保留（pushed=True 先淘汰），同组内按时间保留最新。
        ce = sorted(ce, key=lambda e: (0 if e.get("pushed") else 1, e.get("t", "")))[-300:]
        logger.warning(f"candidate_events 超过上限（{before_n} 条 → 300 条），"
                       "已推条目优先淘汰，未推候选优先保留")
    state["candidate_events"] = ce

    # watchlist 公告直通限频日志滚动清理（2026-09-07 P1：24h 窗口 + 上限防爆胀）
    state["watch_announce"] = _watch_announce_prune(
        state.get("watch_announce") or [], datetime.now(BJT))

    if gist_token and gist_id and not _merge_failed:
        _gist_save(gist_token, gist_id, state)
        logger.info(f"状态已保存到 Gist（{len(seen)} 个指纹, {len(pe)} 个已推事件）")
        return

    # 无 Gist 配置 或 合并失败降级（本地模式）→ 写本地文件
    state_path = _state_path()
    atomic_write_json(state_path, state)
    logger.info(f"状态已保存到本地文件: {state_path.name}（{len(seen)} 个指纹）")










# 预筛直通主体词表（2026-08-13 修复：漏推实证——"DeepSeek V4 Pro 正式版上线"、
# "长江存储首次跻身全球NAND前三"、"寒武纪五大国产模型适配"、"腾讯营收首超2000亿"、
# "钙钛矿量产技术登《自然》" 均预筛 0.14 被静默丢弃，根因是 HIGH_SIGNAL 词表缺
# 具体科技龙头/重要主体名。本表只用于预筛直通（命中即送 LLM 判定），刻意不并入
# HIGH_SIGNAL_KEYWORDS——避免这些宽泛主体词进入指纹 sig 路径造成不同事件指纹碰撞。
# 是否推送仍由 LLM 严格判定（日常/行情/中小市值消息由 LLM 否决）。
_PREFILTER_HEADLINE_ENTITIES = [
    # 国产 AI/算力/科技龙头
    "DeepSeek", "寒武纪", "中际旭创", "长江存储", "长鑫存储", "华为", "海思",
    "腾讯", "阿里", "阿里巴巴", "字节", "百度", "小米", "比亚迪", "宁德时代",
    # 全球科技/半导体龙头
    "英伟达", "台积电", "三星", "SK海力士", "海力士", "美光", "铠侠", "ASML",
    "OpenAI", "Anthropic", "微软", "谷歌", "苹果", "特斯拉", "Meta", "亚马逊",
    # 新能源/新材料技术
    "钙钛矿", "固态电池",
]


_PREF_WATCHLIST_NAMES_CACHE: list | None = None


def _pref_watchlist_names() -> list:
    """预筛直通用持仓名单（懒加载缓存；watchlist.json 缺失时为空表退化为原行为）"""
    global _PREF_WATCHLIST_NAMES_CACHE
    if _PREF_WATCHLIST_NAMES_CACHE is None:
        try:
            _PREF_WATCHLIST_NAMES_CACHE = sorted(_load_leader_watchlist())
        except Exception:
            _PREF_WATCHLIST_NAMES_CACHE = []
    return _PREF_WATCHLIST_NAMES_CACHE


def _hit_headline_entity(text: str) -> bool:
    """是否命中预筛直通主体词（科技龙头/重要主体）

    2026-09-04 扩展：watchlist 持仓股名同样直通——持仓公告源（质押/回购/
    订单等）标题常无高信号词（"源杰科技:关于完成工商变更登记的公告"），
    不直通会被预筛静默丢弃，与"资讯聚焦持仓"的产品定位冲突。
    直通仅跳过预筛门槛，最终是否推送仍由 LLM 判定把关。
    """
    if not text:
        return False
    if any(e in text for e in _PREFILTER_HEADLINE_ENTITIES):
        return True
    return any(w in text for w in _pref_watchlist_names())


def _prefilter(news: dict) -> tuple:
    """规则预筛：返回 (预筛评分, 是否命中高信号词)

    2026-08-12 修复：高信号词命中 或 宏观数据发布识别（_is_macro_data_release）
    均直通 LLM 判定——确定性宏观数据事件即使预筛分低也不允许静默丢弃。
    2026-08-13 修复：大额经营事件（_is_major_deal_event）与科技龙头主体词
    （_hit_headline_entity）同样直通，防龙头重大订单/发布/里程碑被预筛静默丢弃。
    """
    score = calculate_prefilter_importance(news)
    title = str(news.get("title", "") or "")
    content = str(news.get("content", "") or "")
    name = str(news.get("name", "") or "")
    text = f"{title} {content}"
    # 剥离公司名：避免 "*ST XX" 等公司名命中 "ST" 高信号词直通 LLM 判定
    # （与 nodes._has_high_signal 的处理保持一致）
    if name:
        text = text.replace(name, "")
    # 2026-08-06 修复：改用共享词边界匹配（has_signal_keyword），
    # "STorage/STMicroelectronics" 不再误命中 "ST" 信号词
    hit = (has_signal_keyword(text) or _is_macro_data_release(text)
           or _is_major_deal_event(text) or _hit_headline_entity(text))
    return float(score), hit














def _llm_judge(items: list, deadline: float = 0, env_context: str = "",
               pushed_events: list = None) -> list:
    """批量 LLM 判定，返回与 items 一一对应的判定 dict 列表

    Args:
        items: 待判定候选
        deadline: 总超时熔断时间戳(time.monotonic())，0=不限。
                  2026-08-06 修复：此前调用 _call_llm_api 未传 deadline，
                  多批(最多5批)×90s×2次重试最坏 900s，突破 GitHub Actions
                  timeout-minutes:10 导致任务被强杀（已推送未保存状态→重复推送）。
                  现在批次循环入口检查熔断，逼近 deadline 立即挂起剩余批次。
        env_context: 量化环境上下文（P2-2，_llm_env_context 生成）。
                  非空时注入用户 prompt 头部，LLM 输出 env_note 共振/背离标注；
                  空串时不注入（快照缺失/过期，向后兼容）。
        pushed_events: 已推事件（P6-3 叙事链，条目命中同主体近48h事件时
                  注入 related_recent 字段，None/空不注入）。

    对齐策略: 给每条注入 _judge_idx 并要求 LLM 回显 idx，按 idx 精确合并
    （标题匹配仅作兜底）。标题精确匹配不可靠——LLM 会改写/截断标题，
    批处理管线实证 60 条仅 26 条标题精确命中，未对齐时会被误判不推，
    是漏推的主要根因。

    规则直通限制（2026-08-03 用户口径）: 预筛通过的全部候选必须由 LLM
    判定推/不推，Python 规则不得直接通关。因此 LLM 未回显 / 批次异常 /
    无法解析的条目一律标记 judged=False 挂起（push=False, score=0），
    由 run_once 跳过、不落指纹，留待下一轮重新送 LLM 判定。
    已删除 _fallback_decision 规则直推路径。
    """
    if not items:
        return []
    results = [None] * len(items)
    for start in range(0, len(items), LLM_BATCH_SIZE):
        batch = items[start:start + LLM_BATCH_SIZE]
        # 总超时熔断：逼近 deadline 剩余批次全部挂起下轮重试（避免突破 Actions 10min 上限）
        if deadline and time.monotonic() >= deadline:
            logger.warning(f"LLM 判定逼近总超时熔断，批次 {start//LLM_BATCH_SIZE + 1} 起共 "
                           f"{len(items) - start} 条挂起下轮重试")
            for offset, n in enumerate(batch):
                results[start + offset] = _hang_judge(n)
            for remaining in range(start + LLM_BATCH_SIZE, len(items)):
                results[remaining] = _hang_judge(items[remaining])
            break
        for offset, n in enumerate(batch):
            n["_judge_idx"] = start + offset
        try:
            raw = _call_llm_api(_LLM_SYSTEM_PROMPT, _build_llm_user_prompt(batch, env_context, pushed_events), timeout=45, max_retries=1, deadline=deadline)
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
            missing = []
            for offset, n in enumerate(batch):
                i = start + offset
                t = str(n.get("title", "") or "").strip()
                e = by_idx.get(i) or by_title.get(t)
                if e:
                    results[i] = _format_llm_judge_result(n, e)
                else:
                    missing.append((i, n, t))

            # 批量回显不完整时，对缺失 idx 单条补试，避免一个格式异常拖挂整批。
            for i, n, t in missing:
                if deadline and time.monotonic() >= deadline:
                    results[i] = _hang_judge(n)
                    continue
                try:
                    retry_raw = _call_llm_api(
                        _LLM_SYSTEM_PROMPT,
                        _build_llm_user_prompt([n], env_context, pushed_events),
                        timeout=20, max_retries=1, deadline=deadline,
                    )
                    retry_entries = _parse_llm_array(retry_raw)
                    retry_entry = None
                    for retry_e in retry_entries:
                        if not isinstance(retry_e, dict):
                            continue
                        try:
                            retry_idx = int(retry_e.get("idx", -1))
                        except (ValueError, TypeError):
                            retry_idx = -1
                        if retry_idx == i or str(retry_e.get("title", "") or "").strip() == t:
                            retry_entry = retry_e
                            break
                    # 单条 prompt 没有 idx 时，唯一回显项可安全按请求项归属。
                    if (retry_entry is None and len(retry_entries) == 1
                            and "idx" not in retry_entries[0]):
                        retry_entry = retry_entries[0]
                    if retry_entry is None:
                        raise ValueError("单条重试仍未回显目标 idx")
                    results[i] = _format_llm_judge_result(n, retry_entry)
                    logger.info(f"LLM 缺失 idx={i} 单条重试成功")
                except Exception as retry_error:
                    logger.warning(f"LLM 缺失 idx={i} 单条重试失败，挂起该条: {retry_error}")
                    results[i] = _hang_judge(n)
            logger.info(f"LLM 判定批次 {start//LLM_BATCH_SIZE + 1}: {len(batch)} 条完成（回显{len(entries)}条）")
        except Exception as e:
            logger.warning(f"LLM 判定批次失败（{len(batch)} 条），整批挂起下轮重试: {e}")
            for offset, n in enumerate(batch):
                results[start + offset] = _hang_judge(n)
        # 2026-08-13 P2 修复：批次完成后检查剩余预算——此前只在批次入口检查，
        # 单批最坏 90s×2+2s=182s，300s 预算实际可超（8-07 日志实证单轮 380s）。
        # 批次完成即复查 deadline，逼近则立即挂起剩余批次。
        if deadline and time.monotonic() >= deadline:
            rest = list(range(start + LLM_BATCH_SIZE, len(items)))
            if rest:
                logger.warning(f"LLM 判定批次完成后超时熔断，剩余 {len(rest)} 条挂起下轮重试")
                for r in rest:
                    results[r] = _hang_judge(items[r])
            break
    # 防御：任何未填充位置挂起（不落指纹，下轮重试）
    for i, r in enumerate(results):
        if r is None:
            n = items[i]
            results[i] = _hang_judge(n)
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


def _load_watchlist_stocks() -> list:
    """SNA-06（2026-09-07）：加载 watchlist stocks 原始 [{code, name}]，供异动反查。"""
    try:
        wl = json.loads((PROJECT_ROOT / "watchlist.json").read_text(encoding="utf-8"))
        stocks = wl.get("stocks", []) or []
        out = []
        for s in stocks:
            if isinstance(s, dict) and str(s.get("code", "") or "").strip():
                out.append({"code": str(s["code"]).strip(),
                            "name": str(s.get("name", "") or "").strip()})
        return out
    except Exception:
        return []




def _tech_override_enabled(news: dict, judge: dict, leader_watchlist: set) -> bool:
    """科技兜底放行判定（2026-08-13 P0 重构：从 run_once 5c 提取，便于测试）

    防 LLM 漏推科技硬事件：scope=sector 且含国内科技词（或 stock 且龙头且科技）
    时，LLM 判 push=false 也兜底放行。但命中研报/观点/主题措辞
    （_TECH_OVERRIDE_VIEW_WORDS，如"机构称/市场空间/传导/开启"）的定性判断不放行——
    实测"机构称物理AI市场空间"等研报/观点类曾因此被强制放行误推，让 LLM 的
    push=false 否决恢复生效。
    """
    sectors = _as_list(judge.get("sectors"))
    scope = str(judge.get("scope", "stock") or "stock").lower()
    is_leader = bool(judge.get("is_leader_stock")) or _hit_watchlist(news, leader_watchlist)
    strong = str(judge.get("direction", "neutral")) in ("bullish", "bearish")
    score_ok = _to_float(judge.get("score", 0)) >= 5
    view_text = f"{str(news.get('title', ''))} {str(news.get('content', '') or '')}"
    if any(w in view_text for w in _TECH_OVERRIDE_VIEW_WORDS):
        return False
    if scope == "sector" and _is_domestic_tech(news, sectors) and (score_ok or strong):
        return True
    if scope == "stock" and is_leader and _is_domestic_tech(news, sectors) and (score_ok or strong):
        return True
    return False


# ============================================================
# 风险收缩期联动（2026-08-14 第二阶段）
# factor_collector 检测到贴水走扩/日元急升/放量破位 → risk_off 时，
# 资讯管线对"无硬事件佐证的科技利好"降级不推，与量化资金风险期降杠杆一致；
# 利空/风险资讯不受影响（风险期用户更关心利空）。
# ============================================================
_FACTOR_STATE_PATH = PROJECT_ROOT / "logs" / "factor_state.json"

# 硬事件佐证词（已发生的完成态动作）：命中任一即视为"具体事件"（放行）。
# 2026-08-14 实证调优：移除过宽的"订单/协议/合作/投资/业绩"（常见于"迎订单验证窗口"
# "战略合作""机构投资"等展望/情绪语境，会误放行）；"扩建/投运/良率/产能"等补入。
_HARD_EVENT_WORDS = (
    "中标", "签约", "签署", "获批", "收购", "并购", "入股", "回购", "增持",
    "量产", "扩产", "扩建", "投产", "建成", "投运", "上线", "发布", "拿下",
    "交付", "落户", "良率", "产能", "出货",
)
# 展望修饰词：硬事件动词前 6 字内出现 → 视为"展望/未发生"而非已发生事件，
# 不构成硬事件佐证（如"有望中标""拟收购""订单验证窗口"）。
_FORWARD_MODIFIERS = ("有望", "预期", "预计", "或", "计划", "拟", "验证", "窗口", "开启", "迎")

# 大额数量/规模佐证：不依赖金额归一，覆盖设备、容量、工艺和库存周期等硬数据。
# 刻意不包含涨/跌/百分号，纯行情播报不得因此绕过风险期降级。
_HARD_SCALE_QUANTITY_RE = re.compile(
    r"(?:"
    r"\d[\d,]*(?:\.\d+)?\s*[万亿]?\s*"
    r"(?:台|块|件|片|卡|核|节点|G|纳米|毫瓦|千瓦时|天|"
    r"(?:[A-Za-z][A-Za-z-]*[\s]*)?"
    r"(?:chips?|units?|shares?|vehicles?|servers?|GPUs?))"
    r"|(?:十万卡|万卡|千卡|万片|万块|万台)"
    r")",
    re.IGNORECASE,
)


def _has_non_market_percentage(text: str) -> bool:
    """是否含非纯涨跌行情的百分比硬数据（如良率/市占率）。"""
    for match in re.finditer(r"\d+(?:\.\d+)?\s*%", text or ""):
        context = text[max(0, match.start() - 8):match.end()]
        if not re.search(r"涨幅|跌幅|涨跌|涨|跌", context):
            return True
    return False


def _is_tech_by_sectors(sectors) -> bool:
    """板块是否科技类（降级判定专用宽词表 TECH_SECTOR_WORDS_WIDE，单一数据源）"""
    secs = " ".join(str(s) for s in (sectors or []))
    return any(kw in secs for kw in TECH_SECTOR_WORDS_WIDE)


def _load_factor_state() -> dict:
    """读取 factor_collector 的完整因子状态：配置 Gist 时云端唯一来源，否则读本地文件

    缺失/解析失败 → {}（联动与市场环境行均为增强功能，失败不影响资讯主流程）。
    云端未跑 factor_collector 时读不到 → 返回空 dict，保证向后兼容。
    """
    gist_token, gist_id = get_gist_config()
    if gist_token and gist_id:
        try:
            import requests
            url = f"https://api.github.com/gists/{gist_id}?ts={int(time.time() * 1000)}"
            headers = {"Authorization": f"token {gist_token}",
                       "Accept": "application/vnd.github+json",
                       "User-Agent": "stock-news-agent-realtime"}
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            files = resp.json().get("files") or {}
            fobj = files.get("factor_state.json")
            if fobj is not None:
                state = json.loads(fobj.get("content") or "{}")
                return state if isinstance(state, dict) else {}
        except Exception as e:
            # 联动是增强功能，读取失败降级为空，不使用本地旧状态
            logger.debug(f"Gist 因子状态读取失败，增强联动降级为空: {e}")
        return {}
    try:
        state = json.loads(_FACTOR_STATE_PATH.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _load_factor_risk_state() -> str:
    """读取 factor_collector 的风险状态（_load_factor_state 的便捷封装）

    缺失/解析失败/非预期值 → neutral（不影响现有推送）。云端未跑 factor_collector
    时读不到 → 联动自动失效，保证向后兼容。
    """
    rs = _load_factor_state().get("risk_state", "neutral")
    return rs if rs in ("risk_off", "neutral") else "neutral"


# 市场环境行快照过期阈值（小时）：factor_collector 盘中 15 分钟/盘后 60 分钟一轮，
# 48h 覆盖周末（周五收盘快照在周末推送时仍有参考价值，超 48h 视为过期省略）。
_FACTOR_ENV_MAX_AGE_HOURS = 48


# P10（2026-08-20）风险档位判定阈值：中性状态下的警戒信号组合
_WARN_MAIN_OUTFLOW_YI = 500   # 主力净流出警戒（亿元）
_WARN_GC007 = 3.0            # 资金面利率警戒（%）
_WARN_PCR = 1.5              # 期权恐慌警戒

# 指数显示简称（环境行紧凑用）
_INDEX_SHORT = {"上证指数": "上证", "创业板指": "创业板"}


def _market_risk_grade(snapshot: dict) -> str:
    """风险档位（三档，始终显示）：🔴风险收缩 / 🟡警戒 / 🟢中性

    P10（2026-08-20）：此前 risk_state 仅 risk_off 时显示"⚠️风险收缩期"、
    neutral 静默——用户看到一堆数字却不知综合是安全还是危险。
    现拆三档：risk_off=收缩；neutral 但命中任一警戒信号（高波/极端普跌/
    主力大额流出/资金面收紧/期权恐慌）=警戒；否则=中性。
    """
    if snapshot.get("risk_state") == "risk_off":
        return "🔴风险收缩"
    if any(isinstance(v, dict) and v.get("regime") == "高波"
           for v in (snapshot.get("vol") or {}).values()):
        return "🟡警戒"
    dp = (snapshot.get("breadth") or {}).get("down_pct")
    if isinstance(dp, (int, float)) and dp >= 80:
        return "🟡警戒"
    mn = (snapshot.get("flows") or {}).get("main_net_yi")
    if isinstance(mn, (int, float)) and mn <= -_WARN_MAIN_OUTFLOW_YI:
        return "🟡警戒"
    gc = ((snapshot.get("liquidity") or {}).get("gc007") or {}).get("price")
    if isinstance(gc, (int, float)) and gc >= _WARN_GC007:
        return "🟡警戒"
    pcr = (snapshot.get("option") or {}).get("pcr")
    if isinstance(pcr, (int, float)) and pcr >= _WARN_PCR:
        return "🟡警戒"
    return "🟢中性"


def _factor_env_line(snapshot: dict, now: datetime = None) -> str:
    """把因子快照压成单行"市场环境"（附在资讯推送卡片，P0-1 融合展示）

    P10（2026-08-20）重构：①风险档位三档始终显示（🔴收缩/🟡警戒/🟢中性）
    ②补创业板指（与上证并列）③情绪档位/GC007/期权PCR 始终显示（极端值加⚠）
    ④市场宽度始终显示（跌X%）——此前仅极端/风险态才显示，用户无法判断常态。

    快照缺失/字段空/超过 48h 过期 → 返回 ""（推送退化为原格式，不带该行）。
    """
    if not isinstance(snapshot, dict) or not snapshot:
        return ""
    ts = str(snapshot.get("ts", "") or "")
    try:
        snap_time = datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=BJT)
    except ValueError:
        return ""
    now = now or datetime.now(BJT)
    if snap_time > now:
        return ""
    if (now - snap_time).total_seconds() > _FACTOR_ENV_MAX_AGE_HOURS * 3600:
        return ""

    parts = []
    basis = snapshot.get("basis") or {}
    basis_parts = []
    for code in ("IC", "IM"):
        b = basis.get(code) or {}
        pct = b.get("basis_pct")
        if isinstance(pct, (int, float)):
            basis_parts.append(f"{code} {pct:+.2f}%")
    if basis_parts:
        parts.append("贴水 " + " ".join(basis_parts))
    fx = snapshot.get("fx") or {}
    jpy = fx.get("美元/日元") or {}
    if isinstance(jpy.get("change_pct"), (int, float)):
        parts.append(f"美元/日元 {jpy['change_pct']:+.2f}%")
    indexes = snapshot.get("indexes") or {}
    idx_parts = []
    for ix_name in ("上证指数", "创业板指"):
        ix = indexes.get(ix_name) or {}
        if isinstance(ix.get("change_pct"), (int, float)):
            arrow = "▲" if ix["change_pct"] >= 0 else "▼"
            idx_parts.append(f"{_INDEX_SHORT[ix_name]}{arrow}{ix['change_pct']:+.2f}%")
    if idx_parts:
        parts.append(" ".join(idx_parts))
    # P1-3（2026-08-19）：两市主力净流入（快照无 flows 键时省略，旧快照兼容）
    flows = snapshot.get("flows") or {}
    mn = flows.get("main_net_yi")
    if isinstance(mn, (int, float)) and mn != 0:
        parts.append(f"主力{'净流入' if mn > 0 else '净流出'}{abs(mn):.0f}亿")
    # P3（2026-08-19）：隔夜外盘（AI硬件链先行指标）
    gq = snapshot.get("global") or {}
    for g_name in ("纳斯达克100", "英伟达"):
        g = gq.get(g_name) or {}
        if isinstance(g.get("change_pct"), (int, float)):
            flag = "⚠️" if abs(g["change_pct"]) >= 2.0 else ""
            parts.append(f"{flag}隔夜{g_name.replace('纳斯达克100', '纳指')}{g['change_pct']:+.2f}%")
    # P12（2026-08-21）：韩指 KOSPI（存储链先行；14:30 BJT 收盘早于 A 股，盘中为
    # 实时信号故不带"隔夜"前缀）；P10 常态不显示口径——|涨跌|≥2% 才上环境行
    g = gq.get("韩国KOSPI") or {}
    if isinstance(g.get("change_pct"), (int, float)) and abs(g["change_pct"]) >= 2.0:
        parts.append(f"⚠️韩KOSPI{g['change_pct']:+.2f}%")
    # 市场宽度（P10 始终显示：跌X%）
    dp = (snapshot.get("breadth") or {}).get("down_pct")
    if isinstance(dp, (int, float)):
        flag = "⚠️" if dp >= 80 else ""
        parts.append(f"{flag}跌{dp:.0f}%")
    # 涨停情绪档位（P10 始终显示，亢奋/冰点加 emoji）
    mood = (snapshot.get("sentiment") or {}).get("mood")
    if mood:
        emoji = "🔥" if mood == "亢奋" else ("❄️" if mood == "冰点" else "")
        parts.append(f"{emoji}情绪{mood}")
    # 资金面利率 GC007（P10 始终显示，≥3% 加⚠）
    gc = ((snapshot.get("liquidity") or {}).get("gc007") or {})
    if isinstance(gc.get("price"), (int, float)):
        tight = gc["price"] >= _WARN_GC007
        parts.append(f"{'⚠️' if tight else ''}GC007 {gc['price']:.2f}%")
    # 期权情绪 PCR（P10 始终显示，≥1.5 加⚠）
    pcr = (snapshot.get("option") or {}).get("pcr")
    if isinstance(pcr, (int, float)):
        pcr_mood = "恐慌" if pcr >= 1.3 else ("看涨" if pcr <= 0.55 else "中性")
        parts.append(f"{'⚠️' if pcr >= _WARN_PCR else ''}PCR {pcr:.2f}({pcr_mood})")
    # 高波状态（仅命中时显示）
    if any(isinstance(v, dict) and v.get("regime") == "高波"
           for v in (snapshot.get("vol") or {}).values()):
        parts.append("⚠️高波")
    if not parts:
        return ""
    return f"**市场环境**({ts}): {_market_risk_grade(snapshot)} | " + " | ".join(parts)


def _llm_env_context(snapshot: dict, now: datetime = None) -> str:
    """因子快照 → LLM 判定环境上下文（P2-2 共振/背离标注）

    与 _factor_env_line 同一过期规则（>48h 失效）；失效/缺失 → ""，
    LLM 判定退化为无环境模式（向后兼容：云端 factor_collector 未跑/快照过期不受影响）。
    例："（08-19 15:00）风险收缩期（量化资金防守/降杠杆）；IC贴水-0.92%、IM贴水-0.65%
    （中性策略对冲成本）；美元/日元-0.43%；上证指数-2.40%；两市主力净流出1940亿"
    """
    if not isinstance(snapshot, dict) or not snapshot:
        return ""
    ts = str(snapshot.get("ts", "") or "")
    try:
        snap_time = datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=BJT)
    except ValueError:
        return ""
    now = now or datetime.now(BJT)
    if snap_time > now:
        return ""
    if (now - snap_time).total_seconds() > _FACTOR_ENV_MAX_AGE_HOURS * 3600:
        return ""
    parts = []
    if snapshot.get("risk_state") == "risk_off":
        parts.append("风险收缩期（量化资金防守/降杠杆）")
    basis = snapshot.get("basis") or {}
    basis_parts = []
    for code in ("IC", "IM"):
        b = basis.get(code) or {}
        pct = b.get("basis_pct")
        if isinstance(pct, (int, float)):
            basis_parts.append(f"{code}贴水{pct:+.2f}%")
    if basis_parts:
        parts.append("、".join(basis_parts) + "（中性策略对冲成本）")
    fx = snapshot.get("fx") or {}
    jpy = fx.get("美元/日元") or {}
    if isinstance(jpy.get("change_pct"), (int, float)):
        parts.append(f"美元/日元{jpy['change_pct']:+.2f}%")
    indexes = snapshot.get("indexes") or {}
    for ix_name in ("上证指数", "创业板指"):
        ix = indexes.get(ix_name) or {}
        if isinstance(ix.get("change_pct"), (int, float)):
            parts.append(f"{ix_name}{ix['change_pct']:+.2f}%")
    flows = snapshot.get("flows") or {}
    mn = flows.get("main_net_yi")
    if isinstance(mn, (int, float)) and mn != 0:
        parts.append(f"两市主力净{'流入' if mn > 0 else '流出'}{abs(mn):.0f}亿")
    # P3（2026-08-19）：外盘/波动率/宽度——LLM 判定共振/背离的增量上下文
    # P12：加韩国KOSPI（存储链先行，三星/SK海力士与 A 股半导体同频）
    gq = snapshot.get("global") or {}
    g_parts = []
    for g_name in ("纳斯达克100", "英伟达", "恒生科技指数", "韩国KOSPI"):
        g = gq.get(g_name) or {}
        if isinstance(g.get("change_pct"), (int, float)):
            g_parts.append(f"{g_name}{g['change_pct']:+.2f}%")
    if g_parts:
        parts.append("隔夜" + "、".join(g_parts) + "（AI硬件链/科技股先行指标）")
    vol_parts = []
    for v_name, v in (snapshot.get("vol") or {}).items():
        if isinstance(v, dict) and v.get("regime") == "高波":
            vol_parts.append(f"{v_name}高波（20日波动率{v.get('vol20', 0):.1f}%）")
    if vol_parts:
        parts.append("；".join(vol_parts) + "（机构降杠杆环境）")
    breadth = snapshot.get("breadth") or {}
    dp = breadth.get("down_pct")
    if isinstance(dp, (int, float)) and dp >= 80:
        ld = breadth.get("limit_down", 0)
        parts.append(f"极端普跌（{dp:.0f}%个股下跌，跌停{ld}家）")
    # P4（2026-08-19）：涨停情绪 + 行业资金流——LLM 判定短线情绪类事件的增量上下文
    sentiment = snapshot.get("sentiment") or {}
    if sentiment.get("mood") and sentiment.get("zt"):
        parts.append(f"涨停情绪{sentiment['mood']}"
                     f"（涨停{sentiment.get('zt', 0)}家，连板高度{sentiment.get('max_lbc', 0)}，"
                     f"炸板率{sentiment.get('zbr', 0):.0f}%）")
    sf = snapshot.get("sector_flows") or {}
    in_parts = [f"{n}{v:+.1f}亿" for n, v in (sf.get("inflow") or [])[:3]]
    out_parts = [f"{n}{v:+.1f}亿" for n, v in (sf.get("outflow") or [])[:3]]
    if in_parts:
        parts.append("主力净流入行业：" + "、".join(in_parts))
    if out_parts:
        parts.append("主力净流出行业：" + "、".join(out_parts))
    # P7（2026-08-19）：资金面利率 + 期权情绪——LLM 判定流动性敏感型事件
    # （货币政策/杠杆资金/利率类）与情绪敏感型事件的增量上下文
    liq = snapshot.get("liquidity") or {}
    gc = liq.get("gc007") or {}
    if isinstance(gc.get("price"), (int, float)):
        tight = gc["price"] >= 3.0
        parts.append(f"GC007资金面利率{gc['price']:.2f}%"
                     + ("（资金面收紧，杠杆资金承压）" if tight else "（资金面平稳）"))
    opt = snapshot.get("option") or {}
    if isinstance(opt.get("pcr"), (int, float)):
        pcr = opt["pcr"]
        mood = "恐慌对冲占优" if pcr >= 1.3 else ("看涨占优" if pcr <= 0.55 else "中性")
        parts.append(f"期权PCR{pcr:.2f}（机构情绪{mood}）")
    if not parts:
        return ""
    return f"（{ts}）" + "；".join(parts)


def _risk_off_downgrade(news: dict, judge: dict) -> bool:
    """风险收缩期降级判定：科技 bullish 且无硬事件佐证 → True（降级不推）

    规则：
    - 仅作用于 bullish 科技资讯（板块/个股级）；利空/中性/非科技不受影响
    - 硬事件佐证（任一即放行）：①具体金额（_extract_core_numbers）②百分比/倍数硬数据
      （如"良率98%""产能翻倍"）③完成态硬事件动词（中标/收购/扩建/投产等），
      且动词前无展望修饰（"有望/拟/验证窗口"等）④大额数量/规模量词
      （台/块/件/片/G/纳米/卡/核/节点等）+数字；纯涨跌行情百分比不算硬事件
    - 纯情绪/板块利好（"景气提升""有望受益""空间广阔"等无事件佐证）→ 降级
    """
    if judge.get("direction") != "bullish":
        return False
    sectors = _as_list(judge.get("sectors"))
    # 科技识别三合一（宽松兜底 _is_tech_by_sectors，补 OVERSEAS_TECH_KEYWORDS 缺词）
    if not (_is_domestic_tech(news, sectors) or _is_overseas_tech(news, sectors)
            or _is_tech_by_sectors(sectors)):
        return False
    text = f"{str(news.get('title', ''))} {str(news.get('content', '') or '')[:200]}"
    # ① 具体金额 → 硬事件，放行
    if _extract_core_numbers(text):
        return False
    # ② 百分比/倍数硬数据（良率98%、市占率升至X%、产能翻倍）→ 放行；
    #    纯涨跌百分比（"涨超5%"）不属于事件佐证，仍降级。
    if (_has_non_market_percentage(text)
            or re.search(r"翻[一二两三四五六七八九十百\d]倍|倍增", text)):
        return False
    # ③ 完成态硬事件动词（排除展望修饰）
    for w in _HARD_EVENT_WORDS:
        if w not in text:
            continue
        idx = text.find(w)
        ctx = text[max(0, idx - 6):idx]
        if any(m in ctx for m in _FORWARD_MODIFIERS):
            continue  # 展望性表述（"有望中标"），不算已发生
        return False
    # ④ 大额数量/规模量词（10000台、400G、160,000块、不足10天）→ 放行。
    if _HARD_SCALE_QUANTITY_RE.search(text):
        return False
    return True















# ============================================================
# 交易所公告接入（2026-09-07 P1）：type 白名单 + watchlist 直通
# ============================================================
# 盘后全市场公告可达上千条，直接接入会击穿候选池与 Gist 状态 1MB 硬限——
# 只有下列事件类公告才允许进入推送候选管线，白名单外一律不进。
# 规则有序，首个命中生效（"解除质押"必须排在"质押"之前）；
# 匹配文本 = 公告 type + 标题（akshare 的 type 字段口径不稳，标题兜底）。
ANNOUNCE_TYPE_RULES = [
    ("解除质押", ("解除质押", "解押")),
    ("质押", ("质押",)),
    ("回购", ("回购",)),
    ("增持", ("增持",)),
    ("减持", ("减持",)),
    ("业绩预告", ("业绩预告",)),
    ("业绩快报", ("业绩快报",)),
    ("重大合同", ("重大合同", "中标")),
    ("分红送转", ("分红", "派息", "送转", "利润分配")),
    ("立案处罚", ("立案", "处罚", "监管函", "问询函", "关注函")),
]
ANNOUNCE_SOURCE = "交易所公告"
# watchlist 公告直通防刷屏：同股同类别 24h 内最多推 1 条
WATCH_ANNOUNCE_WINDOW_HOURS = 24
WATCH_ANNOUNCE_MAX_LOG = 100


def _announce_whitelist_category(ann: dict) -> str:
    """公告白名单匹配：返回命中的类别名，白名单外返回空串"""
    if not isinstance(ann, dict):
        return ""
    text = f"{ann.get('type', '') or ''} {ann.get('title', '') or ''}"
    for cat, keywords in ANNOUNCE_TYPE_RULES:
        if any(kw in text for kw in keywords):
            return cat
    return ""


def _announce_to_news_item(ann: dict, category: str) -> dict:
    """白名单公告 → 推送管线新闻条目（后续走既有指纹/去重/预筛链路）"""
    title = str(ann.get("title", "") or "").strip()
    name = str(ann.get("name", "") or "").strip()
    code = str(ann.get("code", "") or "").strip()
    return {
        "title": title,
        "content": title,
        "source": ANNOUNCE_SOURCE,
        "published_at": str(ann.get("published_at", "") or ""),
        "code": code,
        "name": name,
        "affected_stocks": [name] if name else [],
        "category": "news",
        "sentiment": "neutral",
        "_announce_category": category,
    }


def _watch_announce_prune(log: list, now: datetime) -> list:
    """watchlist 公告直通限频日志：滚动清理窗口前记录并限长（防爆胀）"""
    cutoff = (now - timedelta(hours=WATCH_ANNOUNCE_WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    kept = [e for e in (log or []) if isinstance(e, dict) and str(e.get("t", "")) >= cutoff]
    return kept[-WATCH_ANNOUNCE_MAX_LOG:]


def _ingest_announcements(announces: list, watchlist: set, state: dict,
                          now: datetime | None = None) -> list:
    """公告接入主循环入口（2026-09-07 P1）。

    - 白名单外公告一律不进候选（不进指纹、不落 seen）；
    - watchlist 股票的白名单公告：标记直通（_watch_announce），下游跳过预筛
      竞争以高优先级进候选并推送；同股同类别 24h 限 1 条（限频日志
      state['watch_announce']，推送成功时登记）；
    - 其余白名单公告走既有去重/预筛链路正常竞争。
    """
    if not announces:
        return []
    now = now or datetime.now(BJT)
    log = state.setdefault("watch_announce", [])
    log[:] = _watch_announce_prune(log, now)
    items = []
    for ann in announces:
        if not isinstance(ann, dict):
            continue
        cat = _announce_whitelist_category(ann)
        if not cat:
            continue  # 白名单外一律不进候选
        item = _announce_to_news_item(ann, cat)
        if watchlist and _hit_watchlist(item, watchlist):
            key = f"{item.get('code') or item.get('name')}|{cat}"
            if any(e.get("key") == key for e in log):
                logger.info(f"watchlist 公告直通限频（同股同类别 {WATCH_ANNOUNCE_WINDOW_HOURS}h 内已推），"
                            f"跳过: {item.get('title', '')[:50]}")
                continue
            item["_watch_announce"] = True
            item["_watch_announce_key"] = key
        items.append(item)
    return items


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
    pending = state.setdefault("pending", {})

    # 自选龙头名单（watchlist.json）提前加载：公告接入（P1）在抓取阶段即需
    # watchlist 判定直通；与 LLM 的 is_leader_stock 判定互为补充
    leader_watchlist = _load_leader_watchlist()
    if not leader_watchlist and not _watchlist_warned[0]:
        _watchlist_warned[0] = True
        logger.warning("自选龙头名单 watchlist.json 为空，龙头放行退化为 LLM is_leader_stock 单通道"
                       "（如需盘面异动龙头放行/科技龙头兜底双通道，请填入关注名单）")

    # 1. 多源聚合抓取：6 大新闻源 + 龙虎榜/业绩预告信号
    # 注意: get_stock_news/get_market_signals 是 LangChain @tool 包装的
    # StructuredTool 实例，需用 .func 取原始函数调用
    # 2026-08-07 修复：新闻与信号分开 try，单类失败不清空另一类成功数据
    # （此前共用 try，signals 抛异常会把已成功的 news_list 也置空 → 误判"无资讯"）
    try:
        news_list = get_stock_news.func()
    except Exception as e:
        logger.error(f"多源新闻抓取失败: {e}", exc_info=True)
        news_list = []
    try:
        signals = get_market_signals.func()
    except Exception as e:
        logger.error(f"市场信号抓取失败: {e}", exc_info=True)
        signals = []

    # 交易所公告接入（2026-09-07 P1）：type 白名单过滤后进管线；
    # watchlist 股票的白名单公告标记直通（跳过预筛竞争，同股同类别 24h 限 1 条）。
    try:
        raw_announces = get_announcements.func()
    except Exception as e:
        logger.error(f"公告抓取失败: {e}", exc_info=True)
        raw_announces = []
    announce_items = _ingest_announcements(raw_announces, leader_watchlist, state)
    if announce_items:
        n_direct = sum(1 for a in announce_items if a.get("_watch_announce"))
        logger.info(f"公告接入: 白名单命中 {len(announce_items)}/{len(raw_announces or [])} 条"
                    f"（其中 watchlist 直通 {n_direct} 条）")

    # 源健康告警（2026-09-07 P1）：区分"没新闻"与"源挂了"——单源连续多轮空
    # 推送告警（同源 24h 限频），全源同时空视为市场静默不告警。
    # 检查异常只记日志，不影响主推送流程。
    push_source_health_alerts(push_config, dry_run=dry_run)

    # 跨源近似去重（URL/精确标题之外补一层 SimHash）：同一事件不同措辞的多源
    # 报道先在入口收敛，避免各自进指纹/候选、重复消耗 LLM 判定 token。
    # 注: 只作用于多源新闻(dedup_news_3layer 对标题 SimHash)，signals 是交易所
    # 结构化数据（龙虎榜/业绩预告）且已按代码内去重，模板化标题不参与近似去重，
    # 避免"业绩预告: XX(代码) 预增 幅度+X%" 同模板不同股票被 SimHash 误判重复。
    before_dedup = len(news_list)
    news_list = dedup_news_3layer(list(news_list))
    if len(news_list) < before_dedup:
        logger.info(f"三层近似去重: {before_dedup} -> {len(news_list)} 条")
    # 公告与 signals 一样是交易所结构化数据，不参与标题 SimHash 近似去重
    # （模板化标题会误并），指纹层去重已覆盖。
    news_list = news_list + list(signals) + announce_items
    logger.info(f"多源聚合: 拉取 {len(news_list)} 条")

    if not news_list:
        logger.info("无资讯返回，跳过本轮")
        return {"fetched": 0, "new": 0, "prefiltered": 0, "pushed": 0, "skipped": 0}

    # 2. 增量检测：事件级指纹去重，只处理未见过指纹的条目
    # pending 中的溢出条目允许重新进入（防漏推：候选溢出不再永久 seen）
    new_items = []
    for n in news_list:
        fp = _news_fingerprint(n)
        if fp in seen:
            continue
        n["_fp"] = fp
        n["_pend_retry"] = int((pending.get(fp) or {}).get("retry", 0))
        new_items.append(n)

    # 2026-09-04 P1-1 修复：pending 条目主动重注入——滚出源窗口的挂起条目
    # 此前只能等重新被抓取（fp 相同）才重回判定，财联社 20 条滚动窗内溢出
    # 条目 1-2 轮后即永久躺 pending 到 48h 过期（实证：特斯拉 Cybercab retry=1
    # 挂 18h 从未进判定）。溢出时已存全量 payload，这里把本轮未重新抓到的
    # pending 条目按 retry 从大到小补注入候选流（老条目优先）。
    reinject = _reinject_pending_items(pending, news_list, seen)
    if reinject:
        new_items = reinject + new_items
        logger.info(f"pending 主动重注入 {len(reinject)} 条（滚出源窗口的挂起条目）")

    logger.info(f"增量检测: 新增 {len(new_items)} 条（已见 {len(news_list) - len(new_items)} 条）")
    if not new_items:
        logger.info("无新增资讯，本轮结束")
        if not dry_run:
            save_state(state)  # 触发窗口清理
        return {"fetched": len(news_list), "new": 0, "prefiltered": 0, "pushed": 0, "skipped": 0}

    # 3. 规则预筛（重要度评分 或 高信号词命中）
    # 标题党治理（2026-09-07 P1）：以省略号结尾的截断标题信息不完整，
    # 仍进入预筛和候选池，但在候选溢出排序时沉底；后续其他源给出完整标题
    # 时，完整版自然胜出。
    now_pref = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
    candidates = []
    for n in new_items:
        if _is_truncated_title(str(n.get("title", "") or "")):
            n["_truncated"] = True
            logger.info(f"截断标题进入候选并降权: {str(n.get('title', ''))[:40]}")
        pref_score, hit = _prefilter(n)
        n["_pref_score"] = pref_score
        # watchlist 公告直通（2026-09-07 P1）：跳过预筛竞争，等同高信号
        # （候选恒进入 + 溢出排序恒优先）
        if n.get("_watch_announce"):
            hit = True
        n["_hit_signal"] = hit
        if pref_score >= PREFILTER_SCORE_MIN or hit:
            candidates.append(n)
    # 候选上限保护：突发大行情候选激增时，按预筛分排序取前 N 条
    # 2026-08-06 修复（P1-2）：溢出条目不写 seen（此前被末尾循环永久 seen → 48h 漏推），
    # 改为记入 pending 挂起，下轮重新进入增量检测与 LLM 判定。
    # 2026-08-07 修复（分层配额）：此前仅按 _pref_score 全局截断——预筛分混合了
    # 科技/板块/来源偏好，极端行情下"立案调查/降准"等高信号核心事件可能被
    # 低分科技噪声挤出第 41 位。现改为两级排序：命中高信号词（核心事件）恒优先，
    # 普通候选再按预筛分排序，保证核心事件在溢出时不被挤占。
    # 放弃策略同步收紧：普通条目连续 MAX_PENDING_RETRY 轮、高信号条目连续
    # MAX_PENDING_RETRY_HIGH_SIGNAL 轮溢出后均写 seen，既保留核心事件重试窗口，
    # 又防止高信号条目永久占满 pending。
    max_candidates = _env_int("RT_MAX_CANDIDATES", MAX_CANDIDATES_PER_ROUND)
    overflow = []
    if len(candidates) > max_candidates:
        # 2026-08-12 修复（防偏科分层）：高信号核心事件恒优先 → 宏观/政策/地缘类
        # 次优先 → 普通候选按预筛分。此前"高信号>预筛分"两级排序中，同为高信号的
        # 科技条目（韩国/存储/英伟达等宽泛词也命中）仍可能把宏观数据挤出——
        # 今日 36 推中存储 17 条、宏观 0 条实证。宏观优先确保 CPI/降准等
        # 确定性宏观事件在溢出时不被打压。
        candidates.sort(key=_candidate_sort_key, reverse=True)
        overflow = candidates[max_candidates:]
        candidates = candidates[:max_candidates]
        now_for_pend = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
        for n in overflow:
            retry = int(n.get("_pend_retry", 0)) + 1
            if retry >= _pending_retry_limit(n):
                # 连续多轮溢出：写 seen 防无限重试；高信号使用更长的独立上限。
                seen[n["_fp"]] = {"t": now_for_pend, "pushed": False,
                                  "title": str(n.get("title", ""))[:60] + "[溢出放弃]"}
                logger.info(f"候选溢出重试{retry}轮仍无法进入判定，放弃: {n.get('title', '')[:40]}")
            else:
                # 2026-09-04 P1-1：存全量 payload，滚出源窗口后下轮主动重注入
                pending[n["_fp"]] = {"t": now_for_pend, "retry": retry,
                                     "title": str(n.get("title", ""))[:60],
                                     "payload": _pend_payload(n)}
        logger.info(f"候选超过上限({max_candidates})，溢出 {len(overflow)} 条进入挂起重试")
        overflow_fps = {n["_fp"] for n in overflow}
    else:
        overflow_fps = set()
    logger.info(f"规则预筛: 通过 {len(candidates)}/{len(new_items)} 条")
    if not candidates:
        # 全部不达标：记录指纹，不推送（跳过 pending 溢出条目——它们下轮重试；
        # 已有 seen 记录的条目不覆盖，保留 [截断] 等标注原因）
        now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
        for n in new_items:
            if n["_fp"] in pending or n["_fp"] in seen:
                continue
            seen[n["_fp"]] = {"t": now, "pushed": False, "title": str(n.get("title", ""))[:60]}
        if not dry_run:
            save_state(state)
        return {"fetched": len(news_list), "new": len(new_items), "prefiltered": 0, "pushed": 0, "skipped": len(new_items)}

    # 4. LLM 严格判定（全部候选必须经 LLM 判定，无规则降级路径）
    # 总超时熔断（2026-08-06）：本轮 LLM 判定最多 300s，超过则剩余批次挂起下轮重试。
    # 防止多批×重试叠加突破 GitHub Actions timeout-minutes:10（此前最坏 900s）。
    # P2-2（2026-08-19）：LLM 判定注入量化环境上下文（风险状态/贴水/汇率/主力资金），
    # LLM 输出 env_note 共振/背离标注；factor_state 提前至此加载一次，
    # 下方 5c 的风险收缩期联动与市场环境行复用同一份（一次 Gist 读取三用）。
    factor_state = _load_factor_state()
    env_context = _llm_env_context(factor_state.get("snapshot") or {})
    # P6-3：已推事件提前加载（原 5c 处初始化上移）——LLM 叙事链上下文 + 5c 推送复用
    pushed_events = state.setdefault("pushed_events", [])
    _llm_deadline = time.monotonic() + 300
    judges = _llm_judge(candidates, deadline=_llm_deadline, env_context=env_context,
                        pushed_events=pushed_events)
    if not judges:
        # 防御：_llm_judge 异常返回空 → 全部挂起下轮重试（不推、不落指纹）
        judges = [_hang_judge(n) for n in candidates]

    # 5. 同事件合并（跨源同事件只推最优一条）→ 跨轮已推事件拦截 → 阈值过滤 → 推送
    pushed = 0
    skipped = 0
    now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")

    # 5a. 生成推送级事件签名并按同事件分组：指纹解决"同一条"，签名解决"同一事件的不同报道"
    groups = []
    for n, j in zip(candidates, judges):
        if not isinstance(j, dict):
            j = _hang_judge(n)
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
                # 挂起条目（LLM 未判定，judged=False）不落 seen：若在此写
                # seen[pushed=False] 会违背"未判定不落指纹、下轮重试"核心策略——
                # 该源的这条资讯将永久不再被尝试判定/推送。
                if not _j.get("judged", True):
                    continue
                seen[n["_fp"]] = {"t": now, "pushed": False,
                                  "title": str(n.get("title", ""))[:52] + "[同事件合并]"}
                skipped += 1

    # 5c. 逐代表：LLM未判定挂起 → 强档方向门槛 → 阈值过滤 → 跨轮同事件拦截 → 推送
    # pushed_events 已在 LLM 判定前初始化（P6-3 上移）
    # 风险收缩期联动（2026-08-14 第二阶段）：factor_collector 写入的 risk_state，
    # risk_off 时对无硬事件佐证的科技利好降级不推。
    # 2026-08-22 停用：不再附"市场环境"因子快照行（用户决定——已上线创业板择时
    # 系统，14:30 择时报告含完整因子快照+仓位信号，资讯推送附该行冗余且时间戳过期）。
    # _factor_env_line 函数保留（纯函数+测试覆盖），需要时可恢复。
    risk_state = factor_state.get("risk_state")
    if risk_state not in ("risk_off", "neutral"):
        risk_state = "neutral"
    # 全局日推送上限（2026-09-07 P0 审计 3.2）：0=不启用（默认，向后兼容）。
    # 每轮读取 env（而非模块级缓存），便于测试与热调参。
    daily_push_limit = _env_int("RT_MAX_PUSH_PER_DAY", 0)
    _cand_seen = set()  # P7-1：本轮已记录的候选 (日期,事件签名)，防同轮重复落 candidate_events
    for n, j in reps:
        hard_event_exemption = False
        hard_event_chain = ""
        # watchlist 公告直通（2026-09-07 P1）：白名单公告已跳过预筛竞争，
        # 此处继续跳过强档方向/噪声/阈值/风险降级/题材饱和/日限额等闸门直接
        # 推送（LLM 判定仍必须完成——judged=False 照常挂起；48h 同事件拦截
        # 保留防重复推送）。
        watch_direct = bool(n.get("_watch_announce"))
        if not j.get("judged", True):
            # 2026-08-03 用户口径：全部资讯必须经 LLM 判定。
            # 未判定条目不推、不落指纹 → 下轮重新送 LLM 判定（避免规则误判方向）。
            logger.info(f"LLM 未判定，挂起下轮重试: {n.get('title', '')[:40]}")
            skipped += 1
            continue
        cand = None
        # P7-1（2026-08-22）：预筛候选持久化——资讯维度可消费"当日全部重大候选"而非仅强档推送。
        # 每个经 LLM 判定的候选（含方向+事件签名）写入 candidate_events，今日作用域、按事件去重。
        # pushed 与否由 pushed_events 权威记录；此处统一量，供择时 news_modifier 并集读取。
        _dir0 = str(j.get("direction") or "")
        _ck = (now[:10], _event_sig_key(n.get("_sig") or {}))
        if _ck not in _cand_seen:
            _cand_seen.add(_ck)
            cand = {**n.get("_sig", {}), "dir": _dir0, "t": now, "pushed": False,
                    "score": j.get("score"),
                    "reason": str(j.get("reason") or "")[:60],
                    "source": str(n.get("source") or "")[:30]}
            state.setdefault("candidate_events", []).append(cand)
        else:
            cand = next(
                (e for e in (state.get("candidate_events") or [])
                 if (_day_key(e), _event_sig_key(e)) == _ck),
                None,
            )
        # 2026-08-04 用户口径：仅强利好/强利空（bullish/bearish）推送；
        # 弱档/中性/混合（mildly_bullish/mildly_bearish/neutral/mixed）一律不推，
        # 覆盖 market/sector/stock、外围科技必推、科技防漏推等全部路径。
        # 例外：watchlist 公告直通不适用（解除质押等公告方向常被判中性/弱档）。
        if not watch_direct and j.get("direction") not in ("bullish", "bearish"):
            logger.info(f"非强档方向({j.get('direction')})，不推: {n.get('title', '')[:40]}")
            if cand is not None:
                cand["gate"] = "direction"
            seen[n["_fp"]] = {"t": now, "pushed": False, "title": str(n.get("title", ""))[:60]}
            skipped += 1
            continue
        # 2026-08-11 修复（审核实证 13/61 滥推）：栏目汇总/指数播报/盘面异动类
        # 即使 LLM 判强档也硬过滤（"晚间新闻精选""隔夜要闻""KOSPI涨超2%""概念异动拉升"），
        # 属"非重大消息"，按用户口径（仅重大事件推送）不应推。记 seen 标注原因。
        noise_reason = None if watch_direct else _is_noise_push(n, j, leader_watchlist)
        if noise_reason:
            logger.info(f"噪声过滤({noise_reason})，不推: {n.get('title', '')[:40]}")
            if cand is not None:
                cand["gate"] = f"noise:{noise_reason}"
            seen[n["_fp"]] = {"t": now, "pushed": False,
                              "title": str(n.get("title", ""))[:52] + f"[{noise_reason}不推]"}
            skipped += 1
            continue
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
        # 2026-08-13 P0 修复：tech_override 排除研报/观点/主题类。
        # 强档门槛后 `score>=5 or strong` 恒真，tech_override 原本退化为
        # "scope=sector+科技词即放行"——"机构称物理AI"等研报/观点类在 LLM 判
        # push=false 后被强制放行（抵消 prompt 收紧，今日误推实证）。
        # 命中 _TECH_OVERRIDE_VIEW_WORDS（定性判断措辞）的科技消息不放行，
        # 让 LLM 的 push=false 生效；仅未命中观点词（可能被 LLM 漏判的硬事件）兜底放行。
        tech_override = _tech_override_enabled(n, j, leader_watchlist)
        if watch_direct:
            # watchlist 公告直通：跳过阈值竞争直接推送
            pass_round = True
        elif j.get("push") and _passes_threshold(
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
            if cand is not None:
                cand["gate"] = "threshold"
            seen[n["_fp"]] = {"t": now, "pushed": False, "title": str(n.get("title", ""))[:60]}
            skipped += 1
            continue

        # 风险收缩期降级（2026-08-14 第二阶段联动）：risk_off 时，科技利好若无硬事件
        # 佐证（金额/订单/公告/获批等）则降级不推——与量化资金风险期降杠杆一致；
        # 利空/风险资讯不受影响（风险期更应提示）。seen 标注原因便于复核。
        if risk_state == "risk_off" and not watch_direct and _risk_off_downgrade(n, j):
            logger.info(f"风险收缩期降级(科技利好无硬事件佐证): {n.get('title', '')[:40]}")
            if cand is not None:
                cand["gate"] = "risk_off"
            seen[n["_fp"]] = {"t": now, "pushed": False,
                              "title": str(n.get("title", ""))[:52] + "[风险收缩期降级]"}
            skipped += 1
            continue

        # 跨轮同事件拦截：48h 内已推过同事件则不再推（防多源报道时间差导致重复推送）
        if any(_is_same_event(n["_sig"], pe) for pe in pushed_events):
            logger.info(f"同事件48h内已推送，跳过: {n.get('title', '')[:50]}")
            if cand is not None:
                cand["gate"] = "same_event"
            seen[n["_fp"]] = {"t": now, "pushed": False,
                              "title": str(n.get("title", ""))[:52] + "[同事件已推]"}
            skipped += 1
            continue

        # 同题材饱和拦截（2026-08-12 防偏科）：同一板块/实体 24h 内已推达上限
        # （默认 5 条）后不再推——存储行情日 17 推实证；market 级（宏观数据/大盘）
        # 豁免，CPI 等宏观数据永不受限。
        if not watch_direct and _topic_saturated(n["_sig"], pushed_events):
            hard_event_chain = _hard_event_chain_key(n["_sig"], n)
            if _hard_event_topic_exemption_available(
                    n, j, n["_sig"], pushed_events):
                hard_event_exemption = True
                logger.info(f"同题材已饱和，但硬事件新颖度豁免一次: {n.get('title', '')[:50]}")
            else:
                logger.info(f"同题材已饱和(≥{TOPIC_PUSH_LIMIT}条/24h)，不推: {n.get('title', '')[:50]}")
                if cand is not None:
                    cand["gate"] = "topic_saturated"
                seen[n["_fp"]] = {"t": now, "pushed": False,
                                  "title": str(n.get("title", ""))[:52] + "[同题材已饱和]"}
                skipped += 1
                continue

        # 全局日推送上限（2026-09-07 P0 审计 3.2）：计数源=pushed_events 当日条数。
        # 触顶后非高信号条目一律不推（保护 pushplus 200条/天限额与用户收件箱）；
        # 高信号（has_signal_keyword 命中，宏观/监管级核心事件）豁免防漏推。
        if daily_push_limit > 0 and not watch_direct and not has_signal_keyword(
                f"{n.get('title', '')} {n.get('content', '')}"):
            today_pushed = sum(1 for pe in pushed_events
                               if str(pe.get("t", ""))[:10] == now[:10])
            if today_pushed >= daily_push_limit:
                logger.info(f"已达日推送上限({daily_push_limit})，不推: {n.get('title', '')[:50]}")
                if cand is not None:
                    cand["gate"] = "daily_limit"
                seen[n["_fp"]] = {"t": now, "pushed": False,
                                  "title": str(n.get("title", ""))[:52] + "[日限额不推]"}
                skipped += 1
                continue

        # P6-2：跨事件矛盾附注（近48h同主体反向已推事件）——叙事链"矛盾"环节
        opposite_note = _opposite_events_note(n["_sig"], str(j.get("direction") or ""),
                                              pushed_events)
        content = format_push_alert(n, j, opposite_note=opposite_note)
        if dry_run:
            logger.info(f"[dry-run] 将推送: {n.get('title', '')[:50]}")
            # Windows 控制台默认 GBK 无法打印 emoji，先切 UTF-8 容错
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
            print("\n===== 将推送内容预览 =====\n" + content + "\n==========================")
            seen[n["_fp"]] = {"t": now, "pushed": True, "title": str(n.get("title", ""))[:60]}
            _mark_candidate_pushed(state, _ck)
            if cand is not None:
                cand["gate"] = "pushed"
            if watch_direct:
                # 同股同类别 24h 限频登记（推送成功才计，失败下轮可重试）
                state.setdefault("watch_announce", []).append(
                    {"key": n.get("_watch_announce_key", ""), "t": now})
            # dir 字段（P0-3 2026-08-19）：盘后复盘按方向统计利多/利空占比。
            # 2026-09-01：补存原文 title（_sig 只有去标点 title_norm，可读性差），
            # 供盘后复盘列表统一以 pushed_events 为唯一数据源时直接展示。
            # 2026-09-04 P1-3：补存 source，供每源推送贡献率审计。
            event_record = {**n["_sig"], "dir": j.get("direction"), "t": now,
                            "title": str(n.get("title", ""))[:60],
                            "source": str(n.get("source", "") or "")[:30]}
            if hard_event_exemption:
                event_record.update({"topic_exemption": True,
                                     "topic_exemption_chain": hard_event_chain})
            pushed_events.append(event_record)
            pushed += 1
        else:
            # 推送标题直接用新闻原文标题（避免显示"重要资讯"占位符）
            push_title = str(n.get("title", "") or "")[:80] or "重大资讯"
            result = _send_alert_item(push_config, push_title, content)
            if result.get("code") == 200 or result.get("errcode") == 0:
                logger.info(f"推送成功: {n.get('title', '')[:50]}")
                seen[n["_fp"]] = {"t": now, "pushed": True, "title": str(n.get("title", ""))[:60]}
                _mark_candidate_pushed(state, _ck)
                if cand is not None:
                    cand["gate"] = "pushed"
                if watch_direct:
                    # 同股同类别 24h 限频登记（推送成功才计，失败下轮可重试）
                    state.setdefault("watch_announce", []).append(
                        {"key": n.get("_watch_announce_key", ""), "t": now})
                event_record = {**n["_sig"], "dir": j.get("direction"), "t": now,
                                "title": str(n.get("title", ""))[:60],
                                "source": str(n.get("source", "") or "")[:30]}
                if hard_event_exemption:
                    event_record.update({"topic_exemption": True,
                                         "topic_exemption_chain": hard_event_chain})
                pushed_events.append(event_record)
                pushed += 1
            else:
                # 推送失败：挂起 pending 下轮重试（2026-09-07 P0 审计修复——
                # 此前什么都不写，条目滚出源窗口后静默漏推且无重试上限）。
                # retry 达 MAX_PENDING_RETRY 仍失败 → 写 seen 放弃（防持续假失败
                # 无限重试）；否则带 payload 挂起，复用 _reinject_pending_items
                # 通道下轮主动重注入。
                retry = int(n.get("_pend_retry", 0)) + 1
                if retry >= MAX_PENDING_RETRY:
                    seen[n["_fp"]] = {"t": now, "pushed": False,
                                      "title": str(n.get("title", ""))[:52] + "[推送失败放弃]"}
                    logger.error(f"连续{retry}轮推送失败，放弃: {n.get('title', '')[:50]}")
                else:
                    pending[n["_fp"]] = {"t": now, "retry": retry,
                                         "title": str(n.get("title", ""))[:60],
                                         "payload": _pend_payload(n)}
                    logger.error(f"推送失败（第{retry}次挂起，下轮重试）: "
                                 f"{n.get('title', '')[:50]} | {result}")
                skipped += 1

    # 其余未进入候选的条目也记录指纹（跳过溢出挂起的 pending 条目——它们下轮重试）
    cand_fps = {n["_fp"] for n in candidates}
    for n in new_items:
        if n["_fp"] not in cand_fps and n["_fp"] not in seen and n["_fp"] not in pending:
            seen[n["_fp"]] = {"t": now, "pushed": False, "title": str(n.get("title", ""))[:60]}

    # pending 条目本轮已重新进入判定并落 seen（推/不推均已定论）→ 从 pending 移除，
    # 避免陈旧挂起记录残留导致计数混乱（2026-08-06 P1-2）
    for n in candidates:
        if n["_fp"] in seen:
            pending.pop(n["_fp"], None)
    for fp in list(pending.keys()):
        if fp in seen:
            pending.pop(fp, None)
        else:
            # 2026-08-25 审核实证：同事件已推送但 pending 记录因措辞/来源不同
            #（fp 不同）无法被上文清理，持续无限重试（华为 retry=4/Meta retry=6）。
            # 兜底：其标题与某条已推事件标题高度相似 → 视为已推同事件，移除。
            rec = pending[fp]
            pt = str(rec.get("title") or "")
            if pt and any(_pending_same_as_pushed(pt, pe.get("title_norm") or "")
                          for pe in pushed_events):
                pending.pop(fp, None)

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

    # 心跳告警（2026-08-06 新增）：有新增资讯但连续多轮 0 推送时，提示可能系统异常
    # （正常行情下新资讯经 LLM 判定后通常有少量推送；长期 0 推送可能是 LLM 端点故障/
    # 阈值过严/数据源异常，人工需确认而非静默接受）
    if stats["new"] > 0 and stats["pushed"] == 0:
        _zero_push_streak[0] += 1
        if _zero_push_streak[0] >= HEARTBEAT_ZERO_PUSH_WARN_ROUNDS:
            logger.warning(
                f"[heartbeat] 已连续 {_zero_push_streak[0]} 轮有新增资讯但 0 推送！"
                f"请检查 LLM 端点/推送后端/阈值配置（fetch={stats['fetched']}, "
                f"new={stats['new']}, prefilter={stats['prefiltered']}）")
    else:
        _zero_push_streak[0] = 0  # 有推送或本轮无新增 → 重置计数
    return stats


# ============================================================
# P0-2/P0-3 盘前简报 + 盘后复盘（融合展示，2026-08-19）
# ============================================================
# 隔夜窗口：昨日收盘 15:00（BJT）之后发布的资讯都算"隔夜/盘前要闻"
_OVERNIGHT_SINCE_HOUR = 15
# 盘前简报要闻条数上限（信息密度优先，超出的按预筛分截断）
BRIEF_TOP_N = 5

_PUB_TIME_FORMATS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                     "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                     "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"]


def _parse_pub_time(text: str):
    """解析 published_at 为 naive BJT datetime；失败返回 None（调用方保留该条不误删）"""
    text = str(text or "").strip()
    for fmt in _PUB_TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# 板块标签归一规则：有序，越具体越靠前，一个标签只归入一个规范类（防重复计数）。
# 2026-09-01 审核实证：LLM 抽的 sectors 是自由文本，'半导体'/'半导体设备'/
# '半导体/HBM'/'半导体/晶圆代工' 各计 1 票 → Top3 退化为并列 2 票的噪声。
_SECTOR_CANON_RULES = (
    ("存储芯片", ("存储芯片", "半导体/存储芯片", "hbm", "dram", "nand", "闪存", "存储器")),
    ("光模块", ("光模块", "cpo", "光通信")),
    ("AI算力", ("ai算力", "ai/算力", "ai芯片", "ai硬件", "算力", "云计算",
                "服务器", "数据中心", "idc")),
    ("半导体", ("半导体", "晶圆", "国产替代", "芯片", "封测", "光刻", "设备")),
    ("能源", ("能源", "原油", "石油", "油气", "石化", "煤炭", "天然气")),
    ("宏观", ("宏观", "利率", "汇率", "美联储", "央行", "cpi", "pmi")),
    ("电子元件", ("mlcc", "电子元件", "被动元件", "电容")),
    ("港股", ("中概股", "港股")),
    ("化工", ("化工", "化学")),
    ("制造业", ("制造业", "工业")),
)


def _canonical_sector(raw) -> str:
    """把 LLM 自由板块标签归一到规范名；空/无效标签返回 ''。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    low = s.lower()
    for canon, keys in _SECTOR_CANON_RULES:
        if any(k in low for k in keys):
            return canon
    return s


def _direction_asof(factor_state: dict) -> str:
    """量化方向结论的归属日期（YYYY-MM-DD）；无 direction_history 时返回 ''。

    2026-09-01：direction_history 以日期为键，最新键即最近一次打分的交易日。
    last_direction 只是值，本身不带时间，必须回查 history 才能判断时效。
    """
    hist = factor_state.get("direction_history") or {}
    if not isinstance(hist, dict) or not hist:
        return ""
    keys = [str(k) for k in hist.keys() if str(k).strip()]
    return max(keys) if keys else ""


def _stale_trading_days(asof: str, today: str, cap: int = 15) -> int:
    """asof 之后（不含当日）到 today 之间新增的交易日数。

    返回 -1 表示日期无法解析/asof 缺失。交易日口径复用 _is_trading_day
    （中国工作日，chinese_calendar 缺失时退化为周一至周五）。
    """
    try:
        d0 = datetime.strptime(str(asof)[:10], "%Y-%m-%d").date()
        d1 = datetime.strptime(str(today)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return -1
    if d1 <= d0:
        return 0
    n = 0
    cur = d0 + timedelta(days=1)
    while cur <= d1 and n < cap:
        try:
            if _is_trading_day(cur):
                n += 1
        except Exception:
            pass
        cur += timedelta(days=1)
    return n


def _snapshot_block(factor_state: dict, title: str) -> list:
    """把 factor_collector 的紧凑快照格式化为 markdown 块（盘前/盘后共用）"""
    snap = factor_state.get("snapshot") or {}
    lines = [f"### {title}", ""]
    if not snap:
        lines.append("- 因子快照缺失（factor_collector 未运行或首次部署），仅资讯维度")
        return lines
    ts = str(snap.get("ts", "") or "")
    risk = str(snap.get("risk_state", "") or "")
    risk_txt = "⚠️ 风险收缩期" if risk == "risk_off" else "中性"
    lines.append(f"- 风险状态: {risk_txt}（更新 {ts}）")
    for name in ("上证指数", "创业板指"):
        idx = (snap.get("indexes") or {}).get(name) or {}
        if idx:
            arrow = "▲" if idx.get("change_pct", 0) >= 0 else "▼"
            lines.append(f"- {name}: {arrow} {idx.get('change_pct', 0):+.2f}%"
                         f"（{idx.get('price', 0):.2f}，{idx.get('trend', '')}）")
    basis = snap.get("basis") or {}
    if basis:
        parts = []
        for code in ("IF", "IC", "IM", "IH"):
            b = basis.get(code)
            if isinstance(b, dict) and isinstance(b.get("basis_pct"), (int, float)):
                tag = "贴水" if b["basis_pct"] < 0 else "升水"
                parts.append(f"{code} {tag}{abs(b['basis_pct']):.2f}%"
                             f"(年化{b.get('annual_pct', 0):+.1f}%)")
        if parts:
            lines.append("- 股指期货: " + "｜".join(parts))
    fx = snap.get("fx") or {}
    fx_parts = []
    for label in ("美元/日元", "美元/在岸人民币"):
        f = fx.get(label) or {}
        if isinstance(f.get("change_pct"), (int, float)):
            fx_parts.append(f"{label} {f['price']:.4f}({f['change_pct']:+.2f}%)")
    if fx_parts:
        lines.append("- 汇率: " + "｜".join(fx_parts))
    # P3（2026-08-19）：隔夜外盘 / 宽度 / 波动率 / 风格
    gq = snap.get("global") or {}
    g_parts = []
    for g_name in ("纳斯达克100", "标普500", "英伟达", "恒生科技指数"):
        g = gq.get(g_name) or {}
        if isinstance(g.get("change_pct"), (int, float)):
            arrow = "▲" if g["change_pct"] >= 0 else "▼"
            warn = "⚠️" if abs(g["change_pct"]) >= 2.0 else ""
            g_parts.append(f"{g_name}{warn}{arrow}{g['change_pct']:+.2f}%")
    if g_parts:
        lines.append("- 隔夜外盘: " + "｜".join(g_parts))
    breadth = snap.get("breadth") or {}
    if breadth.get("down_pct") is not None:
        lines.append(f"- 市场宽度: 涨{breadth.get('adv', 0)}/跌{breadth.get('dec', 0)}"
                     f"（{breadth.get('down_pct', 0):.0f}%下跌）"
                     f"｜涨停{breadth.get('limit_up', 0)}/跌停{breadth.get('limit_down', 0)}"
                     f"｜跌超5% {breadth.get('big_down', 0)}")
    vol = snap.get("vol") or {}
    vol_parts = []
    for v_name, v in vol.items():
        if isinstance(v, dict):
            vol_parts.append(f"{v_name} {v.get('vol20', 0):.1f}%"
                             f"({v.get('pctile', 0):.0f}分位{'高波' if v.get('regime') == '高波' else v.get('regime', '')})")
    if vol_parts:
        lines.append("- 波动率: " + "｜".join(vol_parts))
    style = snap.get("style") or {}
    if style.get("trend"):
        lines.append(f"- 风格轮动: {style.get('trend')}"
                     f"（50/1000比价20日{style.get('chg20', 0):+.1f}%）")
    # P4（2026-08-19）：涨停情绪 + 行业资金流
    sentiment = snap.get("sentiment") or {}
    if sentiment.get("zt"):
        mood = sentiment.get("mood", "")
        flag = "🔥" if mood == "亢奋" else ("❄️" if mood == "冰点" else "")
        lines.append(f"- 涨停情绪: {flag}{mood}｜涨停{sentiment.get('zt', 0)}"
                     f"（连板高度{sentiment.get('max_lbc', 0)}）"
                     f"｜炸板率{sentiment.get('zbr', 0):.0f}%")
    # 2026-09-01：资金维度全部显式标注来源成败。审核实证：全市场净流出 309 亿
    # 未进复盘，而行业资金流源已连失败 41 轮仍在展示残缺数据 → 信息严重偏斜。
    # 原则：有数据就展示，无数据必须写"数据缺失"，绝不整行静默消失或补零。
    failed_srcs = [str(x) for x in ((snap.get("sources") or {}).get("failed") or [])]
    mflow = snap.get("flows") or {}
    mn = mflow.get("main_net_yi")
    mgy = mflow.get("margin_yi")
    mf_parts = []
    if isinstance(mn, (int, float)):
        m_arrow = "▲" if mn >= 0 else "▼"
        mf_parts.append(f"{m_arrow} 主力净{'流入' if mn >= 0 else '流出'} {abs(mn):.0f} 亿")
    elif "资金流" in failed_srcs:
        mf_parts.append("主力净流入 数据缺失")
    if isinstance(mgy, (int, float)):
        mf_parts.append(f"两融 {mgy:.0f} 亿（{mflow.get('margin_chg_yi', 0):+.0f}）")
    if mf_parts:
        lines.append("- 资金流向: " + "｜".join(mf_parts))
    sf = snap.get("sector_flows") or {}
    in_s = "、".join(f"{n} {v:+.1f}亿" for n, v in (sf.get("inflow") or [])[:3])
    out_s = "、".join(f"{n} {v:+.1f}亿" for n, v in (sf.get("outflow") or [])[:3])
    if in_s or out_s:
        # 流出空不再显示裸"—"（8-31 审核实证"流出 —"无法区分真无流出 vs
        # 数据缺半边）。fetch_sector_flows 有行数守卫，能产出 inflow 非空 +
        # outflow 空的组合即"全部返回行业主力净流入≥0"这一合法市场事实。
        out_show = out_s or ("无净流出行业" if in_s else "—")
        lines.append(f"- 行业资金: 流入 {in_s or '—'} ｜ 流出 {out_show}")
    elif "行业资金流" in failed_srcs:
        lines.append("- 行业资金: 数据缺失（源失败已整弃，≠无资金异动）")
    # P7（2026-08-19）：资金面利率 + 期权情绪（影子因子，简报常规展示）
    liq = snap.get("liquidity") or {}
    gc = liq.get("gc007") or {}
    if isinstance(gc.get("price"), (int, float)):
        tight = gc["price"] >= 3.0
        flag = "⚠️" if tight else ""
        gc1 = liq.get("gc001") or {}
        gc1_s = (f"｜GC001 {gc1['price']:.2f}%"
                 if isinstance(gc1.get("price"), (int, float)) else "")
        lines.append(f"- 资金面利率: {flag}GC007 {gc['price']:.2f}%{gc1_s}"
                     + ("（资金面收紧）" if tight else "（平稳）"))
    opt = snap.get("option") or {}
    if isinstance(opt.get("pcr"), (int, float)):
        pcr = opt["pcr"]
        flag = "⚠️" if pcr >= 1.5 else ""
        mood = ("恐慌对冲占优" if pcr >= 1.3 else
                "看涨情绪占优" if pcr <= 0.55 else "情绪中性")
        lines.append(f"- 期权情绪: {flag}PCR {pcr:.2f}（{mood}）"
                     f"｜认购 {opt.get('call_vol', 0)} / 认沽 {opt.get('put_vol', 0)} 张")
    # P5-2：盘中弱翻转提示（未达强信号门槛不单独推，简报兜底可见）
    weak = factor_state.get("weak_direction") or {}
    if weak.get("dir") and weak.get("dir") != factor_state.get("last_direction"):
        lines.append(f"- 盘中弱信号: {weak.get('dir')}（{weak.get('score', 0):+.2f}，"
                     f"未达强信号门槛，仅供参考）")
    # P5-3：数据健康度（源成功率）
    src = snap.get("sources") or {}
    if src.get("total"):
        ratio = src.get("ok", 0) / src["total"]
        flag = "⚠️" if ratio < 0.7 else ""
        # 2026-09-01：附异常源名单（8-31 审核实证：12/14 但不知哪 2 个源失败）
        failed = src.get("failed") or []
        tag = f"（异常：{'、'.join(str(x) for x in failed)}）" if failed else ""
        lines.append(f"- 数据健康度: {flag}{src.get('ok', 0)}/{src['total']} 源正常{tag}")
    last_dir = str(factor_state.get("last_direction", "") or "")
    if last_dir:
        # P4-6：IC 加权已启用时标注（含样本数），人工决策时知悉合成口径
        ic = factor_state.get("factor_ic") or {}
        ic_tag = ""
        if ic.get("weights"):
            n = ic.get("n")
            ic_tag = f"（IC加权，n={n}）" if isinstance(n, int) else "（IC加权）"
        # 2026-09-01 方向停更保护（审核实证：9-01 盘后展示的"偏多"实为 8-28
        # 打分，与当日"创业板指 空头排列"反向，却无任何时效标注 → 误导决策）。
        # 方向结论必须自带日期；停更 ≥1 个交易日即降级，禁止冒充当日结论。
        today_str = datetime.now(BJT).strftime("%Y-%m-%d")
        asof = _direction_asof(factor_state)
        stale = _stale_trading_days(asof, today_str) if asof else -1
        if stale < 0:
            lines.append(f"- 量化综合方向: ⚠️ {last_dir}（打分日期未知："
                         "factor_collector 未记录 direction_history，勿作当日结论）")
        elif stale >= 1:
            lines.append(f"- 量化综合方向: ⚠️ 待更新（末次 {asof}「{last_dir}」，"
                         f"已停更 {stale} 个交易日，非当日结论）")
        else:
            lines.append(f"- 量化综合方向: {last_dir}{ic_tag}")
    return lines


def _push_config_from_env() -> dict:
    """从环境变量构建推送配置（与 run_once 同口径）"""
    return {
        "pushplus_token": os.getenv("PUSHPLUS_TOKEN", "").strip() or None,
        "wecom_webhook": os.getenv("WECOM_WEBHOOK", "").strip() or None,
    }


def run_morning_brief(dry_run: bool = False) -> dict:
    """盘前简报（08:45 触发）：隔夜要闻 Top5 + 因子环境 + 自选关注

    只读设计：不调用 LLM、不写状态（不落 seen/pushed_events）——
    隔夜资讯仍由常规 30 分钟轮询正常判定推送，简报只是提前聚合预览。
    """
    now = datetime.now(BJT)
    factor_state = _load_factor_state()

    lines = [f"## 📋 盘前简报 {now.strftime('%Y-%m-%d %H:%M')}", ""]

    # 1. 隔夜要闻 Top5（预筛分排序，带规则方向提示；无 LLM 判定，人工把关）
    lines.append("### 隔夜要闻（按重要度 Top5）")
    lines.append("")
    try:
        news_list = list(get_stock_news.func())
    except Exception as e:
        logger.error(f"盘前简报新闻抓取失败: {e}")
        news_list = []
    try:
        signals = list(get_market_signals.func())
    except Exception:
        signals = []
    news_list = dedup_news_3layer(news_list) + signals

    since = (now - timedelta(days=1)).replace(hour=_OVERNIGHT_SINCE_HOUR, minute=0,
                                              second=0, microsecond=0)
    overnight = []
    for n in news_list:
        pt = _parse_pub_time(n.get("published_at"))
        if pt is not None and pt < since.replace(tzinfo=None):
            continue
        score, hit = _prefilter(n)
        overnight.append((score, bool(hit), n))
    overnight.sort(key=lambda x: (x[1], x[0]), reverse=True)
    if overnight:
        # 信号类（龙虎榜/业绩预告，交易所结构化数据）预筛分恒定 0.75，
        # 会挤占隔夜宏观/事件要闻席位——简报是"事件综述"，信号最多占 2 席
        signal_slots = 2
        shown = 0
        for score, hit, n in overnight:
            title = str(n.get("title", "") or "")
            is_signal = title.startswith("龙虎榜:") or title.startswith("业绩预告:")
            if is_signal:
                if signal_slots <= 0:
                    continue
                signal_slots -= 1
            if shown >= BRIEF_TOP_N:
                break
            shown += 1
            source = str(n.get("source", "") or "")
            direction = predict_direction_by_rules(title[:60], str(n.get("content", "") or ""))
            emoji = _DIR_EMOJI.get(direction, "⚪")
            star = "⚡" if hit else ""
            lines.append(f"{shown}. {emoji} {title[:60]}（{source}，重要度{score:.2f}）{star}")
    else:
        lines.append("- 隔夜无重要资讯")
    lines.append("")

    # 2. 因子环境（来自 factor_collector 最近一轮快照）
    lines.extend(_snapshot_block(factor_state, "因子环境"))
    lines.append("")

    # 3. 自选关注（P1-2：附最近一轮快照行情——08:45 盘前为昨日收盘口径）
    watchlist = _load_leader_watchlist()
    if watchlist:
        lines.append("### 自选关注（最近快照）")
        lines.append("")
        stocks_snap = (factor_state.get("snapshot") or {}).get("stocks") or {}
        for name in sorted(watchlist):
            s = stocks_snap.get(name) or {}
            if isinstance(s.get("change_pct"), (int, float)) and s.get("price"):
                arrow = "▲" if s["change_pct"] >= 0 else "▼"
                lines.append(f"- {name} {s['price']} {arrow}{s['change_pct']:+.2f}%")
            else:
                lines.append(f"- {name}")
        lines.append("")

    content = "\n".join(lines).rstrip() + "\n"
    stats = {"overnight_candidates": len(overnight), "pushed": 0}

    if dry_run:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        print("\n===== 盘前简报预览 =====\n" + content + "\n========================")
        return stats
    push_config = _push_config_from_env()
    if not push_config["pushplus_token"] and not push_config["wecom_webhook"]:
        raise RuntimeError("盘前简报推送失败: 未配置 PUSHPLUS_TOKEN 或 WECOM_WEBHOOK")
    result = _send_alert_item(push_config, f"盘前简报 {now.strftime('%m-%d %H:%M')}", content)
    if result.get("code") == 200 or result.get("errcode") == 0:
        stats["pushed"] = 1
        logger.info("盘前简报推送成功")
    else:
        logger.error(f"盘前简报推送失败: {result}")
    return stats


def run_evening_review(dry_run: bool = False, on_date: str = None) -> dict:
    """盘后复盘（15:10 触发）：当日已推事件回顾 + 推送统计 + 因子环境

    只读设计：读 state 不写 state（当日数据由常规轮询维护，复盘仅聚合展示）。
    on_date（2026-09-02）：补发指定日期复盘（如凌晨补发前一交易日），
    默认当天。日期仅过滤事件列表，因子环境始终取 factor_state 最新快照。
    """
    now = datetime.now(BJT)
    today = on_date or now.strftime("%Y-%m-%d")
    state = load_state()
    factor_state = _load_factor_state()

    lines = [f"## 📊 盘后复盘 {today}", ""]

    # 1. 当日已推事件（唯一数据源：pushed_events）
    # 2026-09-01 口径统一：此前事件列表取 seen、方向分布取 pushed_events，
    # 两者清理策略不同（SEEN_MAX 条 vs 300 条，均 48h 窗口）必然漂移——
    # 9-01 实锤：列表 28 条 / 方向分布 29 条，差的那条正是被 seen 容量挤出、
    # 只存在于 pushed_events 的 00:32「君正股份 DRAM 涨价」。
    # 现同一份 today_events 同时供列表与方向分布使用，杜绝双口径。
    today_events = [pe for pe in (state.get("pushed_events") or [])
                    if str(pe.get("t", "")).startswith(today)]
    todays = sorted(today_events, key=lambda r: str(r.get("t", "")))
    lines.append(f"### 今日已推事件（{len(todays)} 条）")
    lines.append("")
    if todays:
        for rec in todays:
            t = str(rec.get("t", ""))[11:16]
            # 2026-09-01 起的新条目带原文 title；历史条目回落到 title_norm
            title = str(rec.get("title") or rec.get("title_norm") or "")[:60]
            lines.append(f"- {t} {title}")
    else:
        lines.append("- 今日无推送（平静日或系统未运行）")
    lines.append("")

    # 2. 推送方向统计（与事件列表同源：pushed_events 当日条目，P0-3 起带 dir）
    bull = sum(1 for pe in today_events if pe.get("dir") == "bullish")
    bear = sum(1 for pe in today_events if pe.get("dir") == "bearish")
    if today_events:
        lines.append("### 方向分布")
        lines.append("")
        lines.append(f"- 利好 {bull} 条｜利空 {bear} 条｜未标注 {len(today_events) - bull - bear} 条")
        # 板块热度 Top3（2026-09-01：先归一再计数，同分用板块名做二级排序键。
        # 此前 sorted(key=count, reverse=True) 在并列 2 票时按 dict 插入顺序取
        # 前三，Top3 实际是任意的，掩盖了"半导体/AI算力各 7 票"的真实主线）
        sector_count = {}
        for pe in today_events:
            for s in _as_list(pe.get("sectors")):
                canon = _canonical_sector(s)
                if canon:
                    sector_count[canon] = sector_count.get(canon, 0) + 1
        top_sectors = sorted(sector_count.items(), key=lambda x: (-x[1], x[0]))[:3]
        if top_sectors:
            lines.append("- 板块热度: " + "、".join(f"{s}({c})" for s, c in top_sectors))
        lines.append("")

    # 3. 因子环境
    lines.extend(_snapshot_block(factor_state, "收盘因子环境"))
    lines.append("")

    content = "\n".join(lines).rstrip() + "\n"
    stats = {"pushed_events": len(todays), "pushed": 0}

    if dry_run:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        print("\n===== 盘后复盘预览 =====\n" + content + "\n========================")
        return stats
    push_config = _push_config_from_env()
    if not push_config["pushplus_token"] and not push_config["wecom_webhook"]:
        raise RuntimeError("盘后复盘推送失败: 未配置 PUSHPLUS_TOKEN 或 WECOM_WEBHOOK")
    result = _send_alert_item(push_config, f"盘后复盘 {today}", content)
    if result.get("code") == 200 or result.get("errcode") == 0:
        stats["pushed"] = 1
        logger.info("盘后复盘推送成功")
    else:
        logger.error(f"盘后复盘推送失败: {result}")
    return stats


def _is_trading_day(day=None) -> bool:
    """A股交易日判断（RT_ALWAYS_ON=0 时启用）。"""
    return _is_workday(day or datetime.now(BJT).date(), "cn")


def main():
    parser = argparse.ArgumentParser(description="实时重要资讯推送")
    parser.add_argument("--loop", action="store_true", help="常驻循环模式（本地守护进程）")
    parser.add_argument("--dry-run", action="store_true", help="只诊断不推送不保存状态")
    parser.add_argument("--brief", choices=["morning", "evening"],
                        help="盘前简报(morning, 08:45)/盘后复盘(evening, 15:10)，融合因子快照（P0-2/P0-3）")
    parser.add_argument("--date", default=None,
                        help="补发指定日期的复盘（YYYY-MM-DD，与 --brief evening 搭配；默认当天）")
    args = parser.parse_args()

    # 2026-08-13 P2 修复：删除重复 basicConfig——src.config 已配置 root logger
    # （StreamHandler + logs/agent.log FileHandler），此处重复调用为 no-op 且配置不一致。
    # 仅当 root 无 handler 时兜底配置（如第三方以最小方式导入本模块）。

    always_on = os.getenv("RT_ALWAYS_ON", "1").strip() == "1"
    if not always_on and not _is_trading_day():
        logger.info("今日非A股交易日（RT_ALWAYS_ON=0），跳过")
        return

    if args.brief:
        # 盘前简报/盘后复盘：独立入口，只读不写状态（不影响常规轮询去重）
        if args.brief == "morning":
            run_morning_brief(dry_run=args.dry_run)
        else:
            run_evening_review(dry_run=args.dry_run, on_date=args.date)
        return

    if args.loop:
        poll_seconds = _env_int("RT_POLL_SECONDS", 120)
        logger.info(f"实时推送守护进程启动，轮询间隔 {poll_seconds}s（Ctrl+C 退出）")
        gist_token, gist_id = get_gist_config()
        if gist_token and gist_id:
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
