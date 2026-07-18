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

PUSHPLUS_API = "http://www.pushplus.plus/send"
WXPUSHER_API = "https://wxpusher.zjiecode.com/api/send/message"


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
    # 方向 emoji 映射（PushPlus Markdown 支持 emoji）
    dir_icon = {
        "bullish": "▲",
        "bearish": "▼",
        "neutral": "—",
    }
    band_icon = {
        "bullish": "强多",
        "mildly_bullish": "偏多",
        "neutral": "中性",
        "mixed": "多空交织",
        "mildly_bearish": "偏空",
        "bearish": "强空",
    }

    for i, n in enumerate(ranked_news[:top_n], 1):
        n_title = n.get("title", "")[:60]
        band = n.get("impact_band", "neutral")
        direction = n.get("impact_direction", "neutral")
        score = n.get("market_impact_score", 0)
        total = n.get("total_score", 0)
        conf = n.get("confidence", "medium")
        sectors = n.get("affected_sectors", [])
        sector_str = "、".join(sectors[:2]) if sectors else "—"

        icon = dir_icon.get(direction, "—")
        band_label = band_icon.get(band, band)
        conf_label = {"high": "高", "medium": "中", "low": "低"}.get(conf, conf)

        lines.append(
            f"**{i}. {icon} [{band_label}] {n_title}**\n\n"
            f"> 影响分: {score:.1f} | 综合分: {total:.4f} | 置信: {conf_label} | 板块: {sector_str}\n"
        )

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
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": f"## {title}\n\n{content}"},
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        resp.raise_for_status()
        result = resp.json()
        if result.get("errcode") == 0:
            logger.info(f"企业微信推送成功: {title}")
        else:
            logger.warning(f"企业微信推送失败: {result}")
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
