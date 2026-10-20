# filepath: src/rt_push/push_format.py
"""推送渲染：方向归一、相关事件注记、告警发送（自 scripts/real_time_push.py 拆出，行为不变）"""

import logging
import time

from src.rt_push.common import _as_list, _normalize_entity, _state_timestamp, _to_float
from src.tools.calculators import predict_direction_by_rules
from src.tools.push import push_via_pushplus, push_via_wecom


logger = logging.getLogger("real_time_push")



def _normalize_direction(value, news: dict = None) -> str:
    """归一化 LLM 返回的方向值到 6 档标准值

    问题: LLM 输出不稳定，direction 可能返回非标准值（"bullish "带空格、
    中文"看涨"、"强利好"、乱码等），直接进 _passes_threshold 会丢失
    bullish/bearish 强档位 → 板块/个股资讯被误降级为"不推"（用户反馈：
    利好利空被降级、判断不准）。
    本函数: 标准值直通；常见别名归一；无法识别时用规则方向兜底
    predict_direction_by_rules（词表更全）而非一律 neutral。

    2026-08-03 修复（用户反馈"利空显示利好"）:
    - 混合方向检测优先：含"涨"又含"跌/回落"（先涨后跌/涨后回落/冲高回落/
      利好兑现后回落等）→ 判 mixed，不再因"涨/升"别名先于"跌/降"命中而误判 bullish。
    - 别名子串匹配改为按 key 长度降序（长别名优先），避免"弱利好"被"利好"抢先。
    """
    if value is None:
        value = ""
    raw = str(value).strip().lower()
    if raw in ("bullish", "mildly_bullish", "bearish", "mildly_bearish", "neutral", "mixed"):
        return raw
    # 混合方向检测：同一字符串中既有多头又有多空信号 → mixed
    _BULL_MARKERS = ("涨", "升", "利好", "看多", "看涨", "偏多", "走强", "上行")
    _BEAR_MARKERS = ("跌", "降", "利空", "看空", "看跌", "偏空", "走弱", "回落", "下行")
    if any(k in raw for k in _BULL_MARKERS) and any(k in raw for k in _BEAR_MARKERS):
        return "mixed"
    alias_map = {
        "看涨": "bullish", "看多": "bullish", "利多": "bullish", "利好": "bullish",
        "强利好": "bullish", "偏多": "bullish", "强势": "bullish", "positive": "bullish",
        "涨": "bullish", "升": "bullish",
        "看跌": "bearish", "看空": "bearish", "利淡": "bearish", "利空": "bearish",
        "强利空": "bearish", "偏空": "bearish", "弱势": "bearish", "negative": "bearish",
        "跌": "bearish", "降": "bearish",
        "弱利好": "mildly_bullish", "小幅利好": "mildly_bullish",
        "弱利空": "mildly_bearish", "小幅利空": "mildly_bearish",
        "多空交织": "mixed", "中性": "neutral", "无影响": "neutral",
        "中性偏多": "mildly_bullish", "中性偏空": "mildly_bearish",
    }
    if raw in alias_map:
        return alias_map[raw]
    # 别名内嵌（如 "Mildly Bullish"、"偏多中性"）：按关键词子串匹配，
    # key 按长度降序——长别名（弱利好/中性偏多）优先于单字（利好/涨/跌）
    for key, mapped in sorted(alias_map.items(), key=lambda kv: len(kv[0]), reverse=True):
        if key and key in raw:
            return mapped
    if raw.startswith("mildly"):
        return raw if raw in ("mildly_bullish", "mildly_bearish") else "neutral"
    # 无法识别：用规则方向兜底（比一律 neutral 更准）
    if news is not None:
        rule_dir = predict_direction_by_rules(
            str(news.get("title", "") or ""), str(news.get("content", "") or ""))
        if rule_dir in ("bullish", "bearish"):
            return rule_dir
    return "neutral"


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


# P6（2026-08-19）跨事件叙事链：方向分组口径
_BULL_GROUP = {"bullish", "mildly_bullish"}
_BEAR_GROUP = {"bearish", "mildly_bearish"}


def _opposite_events_note(sig: dict, cur_dir: str, pushed_events: list, hours: int = 48) -> str:
    """跨事件矛盾检测（P6-2）：近 N 小时同主体反向已推事件

    叙事链的"矛盾"环节——今日利空事件 vs 48h 前同主体利多事件，推送时双向可见，
    用户看到完整叙事而非孤立判定（LLM 单条独立判定的补强）。返回附注文本，
    无匹配/方向中性返回 ""。
    """
    d = str(cur_dir or "")
    if d in _BULL_GROUP:
        opp, opp_label = _BEAR_GROUP, "利空"
    elif d in _BEAR_GROUP:
        opp, opp_label = _BULL_GROUP, "利多"
    else:
        return ""
    ents = {_normalize_entity(e) for e in _as_list(sig.get("stocks"))} | \
           {_normalize_entity(e) for e in _as_list(sig.get("entities"))}
    if not ents:
        return ""
    now_ts = time.time()
    hits = []
    for pe in pushed_events:
        if str(pe.get("dir") or "") not in opp:
            continue
        ts = _state_timestamp(pe.get("t"))
        if ts is None:
            continue
        if now_ts - ts > hours * 3600:
            continue
        pents = {_normalize_entity(e) for e in _as_list(pe.get("stocks"))} | \
                {_normalize_entity(e) for e in _as_list(pe.get("entities"))}
        shared = ents & pents
        # 2026-08-25 实证误配：英伟达产品发布（主实体=英伟达）挂上了"易中天三大
        # 巨头集体下跌"（英伟达只是文中偶然提及的第三实体，主实体是光模块三巨头），
        # 暗示不存在的叙事矛盾。收紧：共享实体须为对方事件的主实体（stocks∪首实体），
        # 或双方共享 ≥2 实体（强同主体）。偶然提及的上下文实体不再触发"反向"附注。
        if not shared:
            continue
        pe_primary = {_normalize_entity(e) for e in _as_list(pe.get("stocks"))}
        pe_ents = _as_list(pe.get("entities"))
        if pe_ents:
            pe_primary.add(_normalize_entity(pe_ents[0]))
        if not (shared & pe_primary) and len(shared) < 2:
            continue
        hits.append(f"{str(pe.get('title_norm') or '')[:40]}（{opp_label}）")
        if len(hits) >= 2:
            break
    if not hits:
        return ""
    return f"近{hours}h同主体反向已推事件：" + "、".join(hits)


def _related_recent_note(n: dict, pushed_events: list, hours: int = 48) -> list:
    """该条资讯的近 N 小时同主体已推事件（P6-3 叙事链：LLM 判定的增量上下文）

    判定时 items 尚无 _sig（签名依赖判定结果生成），按已推事件的主体词
    在标题+正文中的子串匹配。返回 ["标题（方向）", ...]（≤3 条），无匹配返回 []。
    """
    if not pushed_events:
        return []
    text = f"{n.get('title', '')} {n.get('content', '')}"
    now_ts = time.time()
    out = []
    for pe in pushed_events:
        # 2026-08-25 收紧（与 _opposite_events_note 同口径）：仅用对方事件的
        # 主实体（stocks∪首实体）匹配本文。此前全实体子串匹配会把"文中偶然
        # 提及的上下文实体"（如易中天事件里的英伟达）注入为"同主体反向叙事"，
        # prompt 又规定反向叙事可上调 score——造成弱关联虚加分。
        kws = [str(e) for e in (_as_list(pe.get("stocks")) + _as_list(pe.get("entities"))[:1])
               if str(e).strip()]
        if not any(k in text for k in kws):
            continue
        ts = _state_timestamp(pe.get("t"))
        if ts is None:
            continue
        if now_ts - ts > hours * 3600:
            continue
        label = _DIR_LABEL.get(str(pe.get("dir") or ""), "中性")
        out.append(f"{str(pe.get('title_norm') or '')[:36]}（{label}）")
        if len(out) >= 3:
            break
    return out


def format_push_alert(news: dict, judge: dict, factor_env: str = "", opposite_note: str = "") -> str:
    """格式化单条快讯为 markdown 文本（红涨绿跌）

    注: 企业微信 markdown 不支持 <font color> 内联 HTML，
    统一用 emoji + 文本标签表达方向（A股惯例红涨绿跌），
    PushPlus / 企业微信均能正确渲染。

    正文处理: 资讯原文 content 截断 300 字符附在推送里（避免只推标题+理由
    让用户看不到资讯内容），企业微信 4096 字节限制内有充足余量。

    factor_env（P0-1 2026-08-19 融合展示）: 由 _factor_env_line 生成的单行
    因子快照（"市场环境: ...IC贴水/日元/上证..."），空串时不附该行，
    向后兼容（factor_collector 未跑/快照过期 → 推送退化为原格式）。
    env_note（P2-2 2026-08-19，2026-08-22 停用展示）: judge 的共振/背离标注
    （LLM 结合量化环境生成，如"背离: 利好但风险收缩期，谨慎对待"）。已不再渲染到
    推送（用户决定——择时报告已覆盖量化环境判断）；LLM prompt/解析链路保留，
    需要时可恢复展示。
    opposite_note（P6-2 2026-08-19）: 跨事件矛盾附注（近48h同主体反向已推
    事件），空串不展示——叙事链的"矛盾"环节，双向可见。
    """
    title = str(news.get("title", "") or "")[:200]
    direction = judge.get("direction", "neutral")
    emoji = _DIR_EMOJI.get(direction, "⚪")
    label = _DIR_LABEL.get(direction, "中性")
    # 2026-08-13 P2 修复：LLM 返回 "score": null 时 e.get 命中 key 返回 None，
    # 直接 f-string 会显示 "影响分: None"；统一走 _to_float 兜底。
    score = _to_float(judge.get("score", 0))
    scope = judge.get("scope", "stock")
    scope_label = _SCOPE_LABEL.get(scope, "个股")
    sectors = _as_list(judge.get("sectors"))
    sector_str = "、".join(str(s) for s in sectors[:4]) if sectors else "—"
    reason = str(judge.get("reason", "") or "").strip()[:100]
    env_note = str(judge.get("env_note", "") or "").strip()[:60]
    source = str(news.get("source", "") or "多源资讯")
    pub = str(news.get("published_at", "") or "")
    content_text = str(news.get("content", "") or "").strip()

    lines = [f"{emoji}【{label}】{title}", ""]
    meta = [f"**范围**: {scope_label}", f"**影响分**: {score}", f"**板块**: {sector_str}"]
    lines.append(" | ".join(meta))
    if factor_env:
        lines.append(factor_env)
    if opposite_note:
        lines.append(f"**⚠️ {opposite_note}")
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
    """统一推送入口：按配置选择后端发送单条快讯，主后端失败时跨后端故障转移

    后端优先级: PushPlus > 企业微信群机器人
    成功判定: PushPlus code==200; 企业微信 errcode==0
    主后端失败且配置了备用后端时，自动转移到备用后端补齐（避免单后端故障丢推）。
    """
    backends = []
    if push_config.get("pushplus_token"):
        backends.append(("pushplus", lambda: push_via_pushplus(
            push_config["pushplus_token"], title, content)))
    if push_config.get("wecom_webhook"):
        backends.append(("wecom", lambda: push_via_wecom(
            push_config["wecom_webhook"], title, content)))
    if not backends:
        return {"code": 400, "msg": "未配置任何推送后端"}

    last = None
    for name, fn in backends:
        try:
            result = fn()
        except Exception as e:
            result = None
            logger.error(f"推送后端 {name} 异常: {e}")
        if result is None:
            result = {"code": -1, "errcode": -1, "msg": f"{name} 返回空/异常结果"}
        is_ok = (name == "pushplus" and result.get("code") == 200) or \
                (name == "wecom" and result.get("errcode") == 0)
        if is_ok:
            if last is not None:
                logger.info(f"推送故障转移成功: 主端失败后经 {name} 补齐")
            return result
        last = result
        logger.warning(f"推送后端 {name} 失败，尝试故障转移: {result}")
    return last
