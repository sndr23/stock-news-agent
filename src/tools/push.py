# filepath: src/tools/push.py
"""
微信推送模块（纯云端方案）
支持 PushPlus / WxPusher / 企业微信群机器人 三种后端

PushPlus:
    - 免费版 200条/天，单条 ≤5000字
    - 微信扫码登录 https://www.pushplus.plus/ 获取 token
    - 需要实名认证

WxPusher:
    - 无需实名认证，微信扫码登录 https://wxpusher.zjiecode.com/admin/
    - 标准推送：appToken + UID
    - content < 40000字，summary ≤ 20字（微信卡片显示）
    - 微信 ClawBot 渠道 24小时限 10 条（每天2次推送够用）

企业微信群机器人:
    - 无每日限额，20条/分钟
    - 企业微信建群 → 添加群机器人 → 复制 webhook URL
"""
import logging
import time
import requests
from typing import Optional

logger = logging.getLogger(__name__)

# 事件去重 / 同股公告限额 / 标题去重统一在 calculators 实现，Web UI 与推送共用
from src.tools.calculators import (
    dedup_ranked_by_event,
    _event_signature,
    _EVENT_KEYWORD_GROUPS,
    dedup_and_cap_for_display,
    dedup_ranked_by_title,
    _extract_title_core,
    _title_similarity,
)

PUSHPLUS_API = "https://www.pushplus.plus/send"
WXPUSHER_API = "https://wxpusher.zjiecode.com/api/send/message"

# 推送重试配置
_PUSH_MAX_RETRIES = 2
_PUSH_RETRY_BASE_DELAY = 2  # 指数退避基数: 2s, 4s


def _post_with_retry(url: str, json_payload: dict, timeout: int = 15,
                     max_retries: int = _PUSH_MAX_RETRIES,
                     success_checker=None) -> dict:
    """带指数退避重试的 POST 请求

    Args:
        url: 请求 URL
        json_payload: JSON 请求体
        timeout: 单次请求超时秒数
        max_retries: 最大重试次数（不含首次）
        success_checker: 判断响应是否成功的回调，返回 True 表示成功

    Returns:
        API 返回的 JSON dict
    """
    last_result = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(url, json=json_payload, timeout=timeout)
            try:
                result = resp.json()
            except Exception:
                result = {"code": resp.status_code, "msg": resp.text[:500]}
            last_result = result
            if success_checker and success_checker(result):
                if attempt > 0:
                    logger.info(f"推送第{attempt+1}次尝试成功")
                return result
            # 响应非成功，判断是否值得重试（4xx 客户端错误不重试）
            status = resp.status_code
            if 400 <= status < 500 and status != 429:
                logger.warning(f"推送客户端错误({status})，不重试: {result}")
                return result
        except Exception as e:
            last_result = {"code": 500, "msg": str(e)}
            logger.warning(f"推送第{attempt+1}次请求异常: {e}")

        if attempt < max_retries:
            delay = _PUSH_RETRY_BASE_DELAY * (2 ** attempt)
            logger.info(f"等待{delay}s后重试推送...")
            time.sleep(delay)

    return last_result or {"code": 500, "msg": "推送失败且无响应"}


def format_ranked_news_md(ranked_news: list, top_n: int = 20, title: str = "A股资讯日报", max_chars: int = 0) -> str:
    """将排名后的资讯格式化为 Markdown 文本

    Args:
        ranked_news: rank_news 返回的列表
        top_n: 取前 N 条
        title: 推送标题
        max_chars: 最大字符数限制（0=不限）。
                   PushPlus 免费版限 5000 字，传入 max_chars=4800 可在格式化阶段
                   就动态减少条数，避免最终粗暴截断导致内容不完整。

    Returns:
        Markdown 格式的资讯文本
    """
    if not ranked_news:
        return f"## {title}\n\n今日暂无重要资讯。"

    # 有字数限制时，动态计算可推送的条数（每条约150-200字）
    effective_top_n = top_n
    if max_chars > 0:
        # 预估每条约占180字（标题+标签+板块+影响分+理由+推理链）
        estimated_per_item = 180
        max_by_chars = max(1, max_chars // estimated_per_item)
        effective_top_n = min(top_n, max_by_chars)

    lines = [f"## {title}\n"]
    # A股惯例：红涨绿跌
    # 方向颜色映射（HTML font 标签，WxPusher markdown 渲染器支持内联 HTML）
    _RED = "#e23a3a"    # 利好红
    _GREEN = "#2e7d32"  # 利空绿
    _GRAY = "#888888"   # 中性灰

    dir_icon = {
        "bullish": "▲",
        "bearish": "▼",
        "neutral": "—",
    }
    # band → 图标（统一从 band 推导，避免 direction 与 band 不一致时标题行自相矛盾）
    band_icon = {
        "bullish": "▲", "mildly_bullish": "▲",
        "bearish": "▼", "mildly_bearish": "▼",
        "neutral": "—", "mixed": "◆",
    }
    band_label_map = {
        "bullish": "强利好",
        "mildly_bullish": "弱利好",
        "neutral": "中性",
        "mixed": "多空交织",
        "mildly_bearish": "弱利空",
        "bearish": "强利空",
    }
    # band → 颜色（以 band 为准，它比 direction 更精确）
    band_color = {
        "bullish": _RED, "mildly_bullish": _RED,
        "bearish": _GREEN, "mildly_bearish": _GREEN,
        "neutral": _GRAY, "mixed": _GRAY,
    }

    # 影响范围标签
    scope_icon = {
        "market": "🌍市场",
        "sector": "🏭板块",
        "stock": "📌个股",
    }

    for i, n in enumerate(ranked_news[:effective_top_n], 1):
        n_title = str(n.get("title", "") or "")[:60]
        band = n.get("impact_band", "neutral")
        direction = n.get("impact_direction", "neutral")
        # 防御 score 为字符串或 None
        try:
            score = float(n.get("market_impact_score", 0) or 0)
        except (ValueError, TypeError):
            score = 0.0
        sectors = n.get("affected_sectors", []) or []
        sector_str = "、".join(str(s) for s in sectors[:2]) if sectors else "—"
        stocks = n.get("affected_stocks", []) or []
        stock_str = "、".join(str(s) for s in stocks[:3]) if stocks else ""
        reason = str(n.get("impact_reason", "") or "").strip()[:80]
        scope = str(n.get("influence_scope", "") or "").strip()
        scope_label = scope_icon.get(scope, "")
        chain = str(n.get("analysis_chain", "") or "").strip()[:120]

        # 统一从 band 推导图标/标签/颜色，避免 direction 与 band 不一致时标题行自相矛盾
        icon = band_icon.get(band, "—")
        b_label = band_label_map.get(band, band)
        b_color = band_color.get(band, _GRAY)

        # 第一行：序号 + 彩色方向标签 + 影响范围 + 标题
        # 方向标签用 HTML font 着色：红=利好 绿=利空 灰=中性
        colored_tag = f'<font color="{b_color}">{icon} {b_label}</font>'
        title_parts = [f"**{i}. {colored_tag}"]
        if scope_label:
            title_parts.append(f'<font color="{_GRAY}">{scope_label}</font>')
        title_parts.append(f"{n_title}**")
        lines.append(" ".join(title_parts) + "\n")
        # 第二行：板块 + 个股 + 影响分
        meta_parts = [f"板块: {sector_str}", f"影响分: {score:.1f}"]
        if stock_str:
            meta_parts.append(f"个股: {stock_str}")
        lines.append(f"> {' | '.join(meta_parts)}\n")
        # 第三行：影响逻辑 + 推理链
        if reason:
            lines.append(f"> {reason}\n")
        if chain:
            lines.append(f"> 💭 {chain}\n")

    # 截断到 39000 字（WxPusher 支持 40000 字，留 1000 字给标题和边距）
    # 注意：PushPlus 免费版限 5000 字，调用方需自行选择后端
    content = "\n".join(lines)
    if len(content) > 39000:
        content = content[:39000] + "\n\n... (更多资讯请查看完整报告)"
    return content


def push_via_pushplus(token: str, title: str, content: str, template: str = "markdown") -> dict:
    """通过 PushPlus 推送消息到微信

    Args:
        token: PushPlus token
        title: 消息标题
        content: 消息内容（Markdown）
        template: 模板类型（markdown / txt / html）

    Returns:
        PushPlus API 返回的 JSON
    """
    # PushPlus 免费版限 5000 字，超长截断
    if len(content) > 4800:
        content = content[:4800] + "\n\n... (更多资讯请查看完整报告)"
    payload = {
        "token": token,
        "title": title,
        "content": content,
        "template": template,
    }
    result = _post_with_retry(
        PUSHPLUS_API, payload, timeout=15,
        success_checker=lambda r: r.get("code") == 200)
    if result.get("code") == 200:
        logger.info(f"PushPlus 推送成功: {title}")
    else:
        logger.warning(f"PushPlus 推送失败: {result}")
    return result


def push_via_wxpusher(
    app_token: str,
    uid: str,
    title: str,
    content: str,
    content_type: int = 3,
    summary: str = "",
) -> dict:
    """通过 WxPusher 推送消息到微信

    Args:
        app_token: WxPusher 应用 appToken（AT_xxx）
        uid: 接收消息的用户 UID
        title: 消息标题（仅用于日志）
        content: 消息内容（Markdown/HTML/Text）
        content_type: 内容类型整数（1=文字 2=html 3=markdown）
        summary: 消息摘要（≤20字，显示在微信卡片上，不传则截取前20字）

    Returns:
        WxPusher API 返回的 JSON
    """
    # summary 限制 20 字，不传则自动截取
    if not summary:
        # 去掉 HTML 标签 + Markdown 标记后截取前 20 字作为摘要
        import re
        plain = re.sub(r"<[^>]+>", "", content)      # 去 HTML 标签 <font ...>
        plain = re.sub(r"[#*>\n\s]", "", plain)       # 去 Markdown 标记
        summary = plain[:20]
    if len(summary) > 20:
        summary = summary[:20]

    payload = {
        "appToken": app_token,
        "content": content,
        "summary": summary,
        "contentType": content_type,
        "uids": [uid],
    }
    # 记录请求参数（脱敏）方便诊断 400 等错误
    logger.info(
        f"WxPusher 请求: appToken={app_token[:6]}***, uid={uid[:6]}***, "
        f"content_len={len(content)}, summary_len={len(summary)}"
    )
    result = _post_with_retry(
        WXPUSHER_API, payload, timeout=15,
        success_checker=lambda r: r.get("code") == 1000)
    if result.get("code") == 1000:
        logger.info(f"WxPusher 推送成功: {title}")
    else:
        logger.warning(f"WxPusher 推送失败: {result}")
    return result


def push_via_wecom(webhook_url: str, title: str, content: str) -> dict:
    """通过企业微信群机器人推送消息

    Args:
        webhook_url: 企业微信群机器人 webhook URL
        title: 消息标题（仅用于日志）
        content: Markdown 内容

    Returns:
        企业微信 API 返回的 JSON
    """
    # 企业微信 markdown 消息体限制 4096 字节，超出会返回 errcode=45008
    # 必须按 UTF-8 字节长度截断，不能按字符数截断（中文一字占3字节）
    full_content = f"## {title}\n\n{content}"
    encoded = full_content.encode('utf-8')
    if len(encoded) > 4000:
        # 按字节截断到 3800 字节（留 296 字节给截断提示）
        # decode errors='ignore' 丢弃截断产生的半个 UTF-8 字符
        truncated = encoded[:3800].decode('utf-8', errors='ignore')
        full_content = truncated + "\n\n...（内容过长已截断）"
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": full_content},
    }
    result = _post_with_retry(
        webhook_url, payload, timeout=15,
        success_checker=lambda r: r.get("errcode") == 0)
    if result.get("errcode") == 0:
        logger.info(f"企业微信推送成功: {title}")
    else:
        logger.warning(f"企业微信推送失败: {result}")
    return result


def push_news(
    ranked_news: list,
    pushplus_token: Optional[str] = None,
    wxpusher_token: Optional[str] = None,
    wxpusher_uid: Optional[str] = None,
    wecom_webhook: Optional[str] = None,
    top_n: int = 20,
    title: str = "A股资讯日报",
    summary: str = "",
) -> dict:
    """推送资讯到微信（统一入口，自动选择后端）

    优先级：WxPusher > PushPlus > 企业微信
    至少配置一个后端，否则返回错误

    Args:
        ranked_news: rank_news 返回的列表
        pushplus_token: PushPlus token（可选）
        wxpusher_token: WxPusher appToken（可选，需配合 wxpusher_uid）
        wxpusher_uid: WxPusher 用户 UID（可选，需配合 wxpusher_token）
        wecom_webhook: 企业微信群机器人 webhook URL（可选）
        top_n: 推送前 N 条
        title: 推送标题
        summary: WxPusher 摘要（≤20字，不传则自动截取）

    Returns:
        推送结果 dict
    """
    # 防御: top_n 必须为正整数，负值会导致切片从末尾取
    top_n = max(1, min(int(top_n), 100))

    # 展示层统一去重（标题相似度 + 同事件 + 宏观簇限流 + 同股公告限额）
    # dedup_and_cap_for_display 内部已包含标题去重，无需重复调用
    ranked_news = dedup_and_cap_for_display(ranked_news)

    # 按推送后端限制动态控制字数：
    # PushPlus 免费版限5000字 → 格式化阶段就限制条数，避免最终粗暴截断
    # WxPusher/企业微信 字数限制宽松，不需预限制
    _max_chars = 0
    if pushplus_token and not (wxpusher_token and wxpusher_uid):
        _max_chars = 4800  # PushPlus 免费版限5000字，留200字余量
    elif wecom_webhook and not (wxpusher_token and wxpusher_uid) and not pushplus_token:
        _max_chars = 3800  # 企业微信 markdown 限4096字节

    content = format_ranked_news_md(ranked_news, top_n=top_n, title=title, max_chars=_max_chars)

    # 自动生成有意义的摘要（如未传入）：取推送条数 + 最高分资讯
    if not summary:
        push_count = min(len(ranked_news), top_n)
        top_title = ""
        if ranked_news:
            raw = str(ranked_news[0].get("title", "") or "")
            import re as _re
            top_title = _re.sub(r"<[^>]+>", "", raw)[:15]
        summary = f"今日{push_count}条重要资讯" + (f"｜{top_title}" if top_title else "")
        if len(summary) > 20:
            summary = summary[:20]

    if wxpusher_token and wxpusher_uid:
        return push_via_wxpusher(wxpusher_token, wxpusher_uid, title, content, summary=summary)
    elif pushplus_token:
        return push_via_pushplus(pushplus_token, title, content)
    elif wecom_webhook:
        return push_via_wecom(wecom_webhook, title, content)
    else:
        logger.error("未配置任何推送后端（wxpusher_token+uid / pushplus_token / wecom_webhook）")
        return {"code": 400, "msg": "未配置推送后端"}
