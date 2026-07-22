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
import requests
from typing import Optional

logger = logging.getLogger(__name__)

PUSHPLUS_API = "https://www.pushplus.plus/send"
WXPUSHER_API = "https://wxpusher.zjiecode.com/api/send/message"


def _extract_title_core(title: str) -> str:
    """提取标题核心内容：去掉【】()等括号内容及标点空格，用于相似度比较"""
    import re
    # 防御 None 或非字符串
    title = str(title) if title else ""
    # 去掉【...】、(...)、（...）等括号包裹的前缀/后缀
    t = re.sub(r'[【】\[\]()（）{}<>《》]', '', title)
    # 去掉所有标点和空白
    t = re.sub(r'[^\w\u4e00-\u9fff]', '', t)
    return t


def _title_similarity(t1: str, t2: str) -> float:
    """基于字符集合的 Jaccard 相似度（0-1）"""
    s1, s2 = set(t1), set(t2)
    if not s1 or not s2:
        return 0.0
    return len(s1 & s2) / len(s1 | s2)


def dedup_ranked_by_title(ranked_news: list, threshold: float = 0.6) -> list:
    """排序后标题相似度去重：保留排名靠前的，去掉后续相似标题

    Args:
        ranked_news: 已排序的资讯列表
        threshold: 标题核心内容 Jaccard 相似度阈值，超过则判定重复

    Returns:
        去重后的列表（保持原排序）
    """
    if not ranked_news:
        return ranked_news
    kept = []
    kept_cores = []
    removed = 0
    for news in ranked_news:
        title = news.get("title", "")
        core = _extract_title_core(title)
        is_dup = False
        for kc in kept_cores:
            # 判定重复：Jaccard 相似度高，或短标题核心是长标题核心的子串
            if _title_similarity(core, kc) >= threshold:
                is_dup = True
                break
            # 子串包含：短标题(>=6字)完全包含在长标题中
            shorter, longer = (core, kc) if len(core) <= len(kc) else (kc, core)
            if len(shorter) >= 6 and shorter in longer:
                is_dup = True
                break
        if is_dup:
            removed += 1
        else:
            kept.append(news)
            kept_cores.append(core)
    if removed > 0:
        logger.info(f"推送前标题去重: {len(ranked_news)} -> {len(kept)}条, 去除{removed}条重复")
    return kept


def format_ranked_news_md(ranked_news: list, top_n: int = 20, title: str = "A股资讯日报") -> str:
    """将排名后的资讯格式化为 Markdown 文本

    Args:
        ranked_news: rank_news 返回的列表
        top_n: 取前 N 条
        title: 推送标题

    Returns:
        Markdown 格式的资讯文本（≤5000字，适配 PushPlus 免费版）
    """
    if not ranked_news:
        return f"## {title}\n\n今日暂无重要资讯。"

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

    for i, n in enumerate(ranked_news[:top_n], 1):
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

        icon = dir_icon.get(direction, "—")
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
    try:
        resp = requests.post(PUSHPLUS_API, json=payload, timeout=15)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") == 200:
            logger.info(f"PushPlus 推送成功: {title}")
        else:
            logger.warning(f"PushPlus 推送失败: {result}")
        return result
    except Exception as e:
        logger.error(f"PushPlus 推送异常: {e}")
        return {"code": 500, "msg": str(e)}


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
        # 去掉 Markdown 标记后截取前 20 字作为摘要
        import re
        plain = re.sub(r"[#*>\n\s]", "", content)
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
    try:
        resp = requests.post(WXPUSHER_API, json=payload, timeout=15)
        # 不使用 raise_for_status，直接解析响应体（即使 400/500 也能看到 WxPusher 的错误信息）
        try:
            result = resp.json()
        except Exception:
            result = {"code": resp.status_code, "msg": resp.text[:500]}
        if result.get("code") == 1000:
            logger.info(f"WxPusher 推送成功: {title}")
        else:
            logger.warning(f"WxPusher 推送失败: HTTP {resp.status_code}, response={result}")
        return result
    except Exception as e:
        logger.error(f"WxPusher 推送异常: {e}")
        return {"code": 500, "msg": str(e)}


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
    full_content = f"## {title}\n\n{content}"
    if len(full_content.encode('utf-8')) > 4000:
        full_content = full_content[:3800] + "\n\n...（内容过长已截断）"
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": full_content},
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        try:
            result = resp.json()
        except Exception:
            result = {"errcode": resp.status_code, "errmsg": resp.text[:500]}
        if result.get("errcode") == 0:
            logger.info(f"企业微信推送成功: {title}")
        else:
            logger.warning(f"企业微信推送失败: HTTP {resp.status_code}, response={result}")
        return result
    except Exception as e:
        logger.error(f"企业微信推送异常: {e}")
        return {"errcode": 500, "errmsg": str(e)}


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
    # 推送前标题相似度去重（保留排名靠前的）
    ranked_news = dedup_ranked_by_title(ranked_news)
    content = format_ranked_news_md(ranked_news, top_n=top_n, title=title)

    if wxpusher_token and wxpusher_uid:
        return push_via_wxpusher(wxpusher_token, wxpusher_uid, title, content, summary=summary)
    elif pushplus_token:
        return push_via_pushplus(pushplus_token, title, content)
    elif wecom_webhook:
        return push_via_wecom(wecom_webhook, title, content)
    else:
        logger.error("未配置任何推送后端（wxpusher_token+uid / pushplus_token / wecom_webhook）")
        return {"code": 400, "msg": "未配置推送后端"}
