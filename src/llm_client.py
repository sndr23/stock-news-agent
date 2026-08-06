# filepath: src/llm_client.py
"""
LLM 调用客户端（共享模块，单一事实来源）
====================================================
从 src/agent/nodes.py 抽取（2026-08-06）：批处理管线 nodes.py 已 DEPRECATED，
实时推送 scripts/real_time_push.py 仅复用其 LLM 调用能力。将
_call_llm_api / _repair_json / _safe_parse_json 迁至本模块，
使生产代码与废弃批处理管线解耦（可后续移除 langgraph 依赖）。

nodes.py 保留 re-import 兼容（测试 patch 路径 src.agent.nodes._call_llm_api
仍可用），新代码请直接从 src.llm_client 导入。
"""
import json
import logging
import re
import time

logger = logging.getLogger(__name__)

from src.config import OPENROUTER_API_KEY, OPENROUTER_MODEL_NAME, OPENROUTER_BASE_URL, IS_OPENROUTER_OFFICIAL


def _call_llm_api(system_prompt: str, user_prompt: str, timeout: int = 90, max_retries: int = 2, deadline: float = 0) -> str:
    """直接用 requests 调用 LLM API

    关键: trust_env=False 禁止 requests 读取系统代理设置(Windows注册表/env vars),
    避免代理服务未运行时导致 ProxyError/ConnectionRefused

    Args:
        system_prompt: 系统提示词
        user_prompt: 用户提示词
        timeout: 单次请求超时秒数
        max_retries: 最大重试次数
        deadline: 总超时熔断时间戳(time.monotonic())，0=不限。
                  逼近 deadline 立即放弃重试并抛异常，避免重试叠加突破总超时。

    Returns:
        LLM 返回的文本内容
    """
    import requests

    url = f"{OPENROUTER_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    # OpenRouter 官方要求 HTTP-Referer 和 X-Title 请求头，否则返回 402
    if IS_OPENROUTER_OFFICIAL:
        headers["HTTP-Referer"] = "https://github.com/stock-news-agent"
        headers["X-Title"] = "StockNewsAgent"
    payload = {
        "model": OPENROUTER_MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.1,  # 结构化输出场景降低温度
        "max_tokens": 16384
    }

    last_error = None
    for attempt in range(max_retries + 1):
        # 总超时熔断：逼近 deadline 立即放弃重试并返回上层降级
        if deadline and time.monotonic() >= deadline:
            raise Exception(f"LLM 调用逼近总超时熔断，放弃重试（已尝试 {attempt} 次）")
        session = requests.Session()
        # 官方端点(OpenRouter)需科学上网保留代理；Agnes 等中转端点禁用代理避免 ConnectionRefused
        session.trust_env = IS_OPENROUTER_OFFICIAL
        try:
            resp = session.post(url, json=payload, headers=headers, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            if content:  # 非空内容才返回
                return content
            else:
                last_error = f"第{attempt+1}次返回空内容"
                logger.warning(last_error)
        except Exception as e:
            last_error = str(e)
            logger.warning(f"第{attempt+1}次调用失败: {e}")
        finally:
            session.close()

        if attempt < max_retries:
            # 重试前再次确认未超 deadline（避免退避等待期间已超时仍继续重试）
            if deadline and time.monotonic() >= deadline:
                raise Exception(f"LLM 调用逼近总超时熔断，放弃重试（已尝试 {attempt + 1} 次）")
            # 指数退避: 2s, 4s
            wait_time = 2 ** (attempt + 1)
            logger.info(f"等待 {wait_time}s 后重试（Agnes 端点）...")
            time.sleep(wait_time)

    raise Exception(f"LLM API 调用失败，已重试 {max_retries} 次: {last_error}")


def _repair_json(text: str) -> str:
    """尝试修复LLM返回的JSON格式问题"""
    # 替换中文引号
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    text = text.replace('\u2018', "'").replace('\u2019', "'")
    # 替换中文冒号
    text = text.replace('\uff1a', ':')
    # 替换中文逗号
    text = text.replace('\uff0c', ',')
    # 替换中文句号
    text = text.replace('\u3002', '.')
    # 替换中文顿号
    text = text.replace('\u3001', ',')
    # 替换不可见字符
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    # 转义JSON字符串值内的换行符（Claude等模型常返回未转义的换行）
    # 逐字符扫描：在双引号字符串内，把裸 \n \r \t 转义
    result = []
    in_string = False
    escape = False
    for ch in text:
        if escape:
            result.append(ch)
            escape = False
            continue
        if ch == '\\':
            result.append(ch)
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if in_string:
            if ch == '\n':
                result.append('\\n')
            elif ch == '\r':
                result.append('\\r')
            elif ch == '\t':
                result.append('\\t')
            else:
                result.append(ch)
        else:
            result.append(ch)
    return ''.join(result)


def _safe_parse_json(content: str) -> dict:
    """安全解析LLM返回的JSON, 多重容错"""
    if not content or not content.strip():
        return {"filtered_news": [], "removed_count": 0}

    # 预处理: 清理可能的乱码
    cleaned = content
    cleaned = cleaned.replace('\u0000', '')  # 移除null字符
    # 先提取代码块（避免代码块标记被破坏），处理 ```json / ``` 等变体
    cb_match = re.search(r'```(?:json)?\s*\n?(.*?)```', cleaned, re.DOTALL)
    if cb_match:
        cleaned = cb_match.group(1)
    elif "```" in cleaned:
        parts = cleaned.split("```")
        if len(parts) >= 3:
            cleaned = parts[1]
    # 先修复中文标点（中文引号/冒号/逗号等），再做不可见字符清理
    cleaned = _repair_json(cleaned)
    # 清理不可见字符，但保留中文引号/标点（已转换为ASCII）和中文汉字
    cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', cleaned)

    cleaned = cleaned.strip()

    if not cleaned:
        return {"filtered_news": [], "removed_count": 0}

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 尝试提取 filtered_news 数组
    match = re.search(r'\{[^{}]*"(?:filtered_news|removed_count)"[^{}]*\}', cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    news_match = re.search(r'"filtered_news"\s*:\s*\[', cleaned)
    if news_match:
        start = news_match.end() - 1
        depth = 0
        for i in range(start, len(cleaned)):
            if cleaned[i] == '[':
                depth += 1
            elif cleaned[i] == ']':
                depth -= 1
                if depth == 0:
                    try:
                        news_array = json.loads(cleaned[start:i+1])
                        return {"filtered_news": news_array, "removed_count": 0}
                    except json.JSONDecodeError:
                        break

    # 截断JSON修复：LLM输出被max_tokens截断时，JSON不完整
    # 策略：逐字符扫描filtered_news数组，提取所有完整的{}对象
    recovered_items = []
    fn_match = re.search(r'"filtered_news"\s*:\s*\[', cleaned)
    if fn_match and len(cleaned) <= 200_000:
        array_start = fn_match.end()  # 指向 [ 后第一个字符
        i = array_start
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
            if ch == '\\':
                escape = True
                i += 1
                continue
            if ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch == '{':
                    if obj_depth == 0:
                        obj_start = i
                    obj_depth += 1
                elif ch == '}':
                    obj_depth -= 1
                    if obj_depth == 0 and obj_start >= 0:
                        obj_text = cleaned[obj_start:i+1]
                        try:
                            recovered_items.append(json.loads(obj_text))
                        except json.JSONDecodeError:
                            try:
                                recovered_items.append(json.loads(_repair_json(obj_text)))
                            except json.JSONDecodeError:
                                pass
                        obj_start = -1
            i += 1
    if recovered_items:
        logger.info(f"截断JSON修复: 恢复了 {len(recovered_items)} 条完整记录")
        return {"filtered_news": recovered_items, "removed_count": 0}

    # ranking 结构兜底提取（rerank 输出：{"ranking": [...]}）
    rk_match = re.search(r'"ranking"\s*:\s*\[', cleaned)
    if rk_match and len(cleaned) <= 200_000:
        array_start = rk_match.end()
        recovered_ranking = []
        i = array_start
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
            if ch == '\\':
                escape = True
                i += 1
                continue
            if ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch == '{':
                    if obj_depth == 0:
                        obj_start = i
                    obj_depth += 1
                elif ch == '}':
                    obj_depth -= 1
                    if obj_depth == 0 and obj_start >= 0:
                        obj_text = cleaned[obj_start:i+1]
                        try:
                            recovered_ranking.append(json.loads(obj_text))
                        except json.JSONDecodeError:
                            try:
                                recovered_ranking.append(json.loads(_repair_json(obj_text)))
                            except json.JSONDecodeError:
                                pass
                        obj_start = -1
            i += 1
        if recovered_ranking:
            logger.info(f"截断JSON修复(ranking): 恢复了 {len(recovered_ranking)} 条重排记录")
            return {"ranking": recovered_ranking, "filtered_news": []}

    # adjustments 结构兜底提取（LLM调分输出：{"adjustments": [...]}）
    adj_match = re.search(r'"adjustments"\s*:\s*\[', cleaned)
    if adj_match and len(cleaned) <= 200_000:
        array_start = adj_match.end()
        recovered_adjustments = []
        i = array_start
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
            if ch == '\\':
                escape = True
                i += 1
                continue
            if ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch == '{':
                    if obj_depth == 0:
                        obj_start = i
                    obj_depth += 1
                elif ch == '}':
                    obj_depth -= 1
                    if obj_depth == 0 and obj_start >= 0:
                        obj_text = cleaned[obj_start:i+1]
                        try:
                            recovered_adjustments.append(json.loads(obj_text))
                        except json.JSONDecodeError:
                            try:
                                recovered_adjustments.append(json.loads(_repair_json(obj_text)))
                            except json.JSONDecodeError:
                                pass
                        obj_start = -1
            i += 1
        if recovered_adjustments:
            logger.info(f"截断JSON修复(adjustments): 恢复了 {len(recovered_adjustments)} 条调分记录")
            return {"adjustments": recovered_adjustments, "filtered_news": []}

    # 最终降级: 返回空结果而不是抛出异常
    logger.warning(f"JSON解析最终失败，降级返回空结果: {cleaned[:100]}...")
    return {"filtered_news": [], "removed_count": 0}
